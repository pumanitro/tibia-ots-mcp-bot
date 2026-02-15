"""
Lightweight HTTP API for the dashboard (stdlib only, zero new deps).

Runs in a daemon thread.  Read-only endpoints access global state directly;
mutating endpoints dispatch to the main asyncio loop.
"""

import asyncio
import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

log = logging.getLogger("dashboard_api")

# References set by start_api()
_main_loop: asyncio.AbstractEventLoop | None = None
_state = None  # will be set to the BotState instance
ACTIONS_DIR = Path(__file__).parent / "actions"

API_PORT = 8089


class _Handler(BaseHTTPRequestHandler):
    """Minimal REST handler — no frameworks needed."""

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    # ── CORS ───────────────────────────────────────────────────────
    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    # ── Helpers ────────────────────────────────────────────────────
    def _json_response(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    # ── Routes ─────────────────────────────────────────────────────
    def do_GET(self):
        if self.path == "/api/state":
            return self._handle_get_state()
        self._json_response({"error": "not found"}, 404)

    def do_POST(self):
        # POST /api/actions/{name}/toggle
        # POST /api/actions/{name}/restart
        parts = self.path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "actions":
            name = parts[2]
            verb = parts[3]
            if verb == "toggle":
                return self._handle_toggle(name)
            if verb == "restart":
                return self._handle_restart(name)
        self._json_response({"error": "not found"}, 404)

    # ── GET /api/state ─────────────────────────────────────────────
    def _handle_get_state(self):
        st = _state
        if st is None:
            self._json_response({"connected": False, "actions": []})
            return

        actions_settings = st.settings.get("actions", {})
        action_names = sorted(
            p.stem for p in ACTIONS_DIR.glob("*.py") if p.stem != "__init__"
        ) if ACTIONS_DIR.exists() else []

        actions = []
        for name in action_names:
            cfg = actions_settings.get(name, {})
            enabled = cfg.get("enabled", False)
            task = st._action_tasks.get(name)
            running = task is not None and not task.done()

            # Read first line of docstring as description
            desc = ""
            try:
                src = (ACTIONS_DIR / f"{name}.py").read_text(encoding="utf-8")
                for line in src.splitlines():
                    s = line.strip().strip('"').strip("'")
                    if s:
                        desc = s
                        break
            except OSError:
                pass

            actions.append({
                "name": name,
                "enabled": enabled,
                "running": running,
                "description": desc,
            })

        pkt_server = st.game_proxy.packets_from_server if st.game_proxy else 0
        pkt_client = st.game_proxy.packets_from_client if st.game_proxy else 0

        self._json_response({
            "connected": st.connected,
            "actions": actions,
            "packets_from_server": pkt_server,
            "packets_from_client": pkt_client,
        })

    # ── POST /api/actions/{name}/toggle ────────────────────────────
    def _handle_toggle(self, name: str):
        body = self._read_body()
        enabled = body.get("enabled", True)

        if _main_loop is None:
            self._json_response({"error": "bot not started"}, 503)
            return

        # Import the shared async function
        from mcp_server import _async_toggle_action
        future = asyncio.run_coroutine_threadsafe(
            _async_toggle_action(name, enabled), _main_loop
        )
        try:
            result = future.result(timeout=5)
            self._json_response({"ok": True, "message": result})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    # ── POST /api/actions/{name}/restart ───────────────────────────
    def _handle_restart(self, name: str):
        if _main_loop is None:
            self._json_response({"error": "bot not started"}, 503)
            return

        from mcp_server import _async_restart_action
        future = asyncio.run_coroutine_threadsafe(
            _async_restart_action(name), _main_loop
        )
        try:
            result = future.result(timeout=5)
            self._json_response({"ok": True, "message": result})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)


_started = False


def start_api(loop: asyncio.AbstractEventLoop, bot_state) -> None:
    """Start the HTTP API in a daemon thread. Safe to call multiple times."""
    global _main_loop, _state, _started
    _main_loop = loop
    _state = bot_state

    if _started:
        return
    _started = True

    server = HTTPServer(("127.0.0.1", API_PORT), _Handler)

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info(f"Dashboard API listening on http://127.0.0.1:{API_PORT}")
