"""
Lightweight HTTP API for the dashboard (stdlib only, zero new deps).

Runs in a daemon thread.  Read-only endpoints access global state directly;
mutating endpoints dispatch to the main asyncio loop.
"""

import asyncio
import json
import logging
import sys
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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
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

    def do_DELETE(self):
        # DELETE /api/actions/{name}
        parts = self.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "actions":
            return self._handle_delete(parts[2])
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

        gs = st.game_state
        player = {
            "hp": gs.hp,
            "max_hp": gs.max_hp,
            "mana": gs.mana,
            "max_mana": gs.max_mana,
            "level": gs.level,
            "experience": gs.experience,
            "position": list(gs.position),
            "magic_level": gs.magic_level,
            "soul": gs.soul,
        }

        # Use the best available z: gs.position if valid, else infer from
        # the player's own creature entry in DLL data
        player_z = gs.position[2] if gs.position[2] != 0 else 0
        if player_z == 0 and gs.player_id in gs.creatures:
            player_z = gs.creatures[gs.player_id].get("z", 0)
        if player_z == 0:
            # Last resort: use most common z among DLL creatures
            z_counts: dict[int, int] = {}
            for info in gs.creatures.values():
                cz = info.get("z", 0)
                if 1 <= cz <= 15:
                    z_counts[cz] = z_counts.get(cz, 0) + 1
            if z_counts:
                player_z = max(z_counts, key=z_counts.get)

        creatures = [
            {"id": cid, "health": info.get("health", 0), "name": info.get("name", ""),
             "x": info.get("x", 0), "y": info.get("y", 0), "z": info.get("z", 0)}
            for cid, info in gs.creatures.items()
            if player_z == 0 or info.get("z") == player_z
        ]

        self._json_response({
            "connected": st.connected,
            "actions": actions,
            "packets_from_server": pkt_server,
            "packets_from_client": pkt_client,
            "player": player,
            "creatures": creatures,
        })

    # ── POST /api/actions/{name}/toggle ────────────────────────────
    def _handle_toggle(self, name: str):
        body = self._read_body()
        enabled = body.get("enabled", True)
        log.info(f"Toggle request: {name} -> enabled={enabled}")

        if _main_loop is None:
            log.warning("Toggle failed: _main_loop is None")
            self._json_response({"error": "bot not started"}, 503)
            return

        # Access the real __main__ module (mcp_server runs as __main__)
        main_mod = sys.modules["__main__"]
        future = asyncio.run_coroutine_threadsafe(
            main_mod._async_toggle_action(name, enabled), _main_loop
        )
        try:
            result = future.result(timeout=5)
            log.info(f"Toggle result: {result}")
            self._json_response({"ok": True, "message": result})
        except Exception as e:
            log.error(f"Toggle error: {e}")
            self._json_response({"error": str(e)}, 500)

    # ── POST /api/actions/{name}/restart ───────────────────────────
    def _handle_restart(self, name: str):
        if _main_loop is None:
            self._json_response({"error": "bot not started"}, 503)
            return

        main_mod = sys.modules["__main__"]
        future = asyncio.run_coroutine_threadsafe(
            main_mod._async_restart_action(name), _main_loop
        )
        try:
            result = future.result(timeout=5)
            self._json_response({"ok": True, "message": result})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)


    # ── DELETE /api/actions/{name} ───────────────────────────────
    def _handle_delete(self, name: str):
        action_file = ACTIONS_DIR / f"{name}.py"
        if not action_file.exists():
            self._json_response({"error": f"action '{name}' not found"}, 404)
            return

        # Disable first if running
        if _main_loop is not None:
            main_mod = sys.modules["__main__"]
            try:
                future = asyncio.run_coroutine_threadsafe(
                    main_mod._async_toggle_action(name, False), _main_loop
                )
                future.result(timeout=5)
            except Exception:
                pass

        # Delete the file
        try:
            action_file.unlink()
            log.info(f"Deleted action: {name}")
            self._json_response({"ok": True, "message": f"Deleted {name}"})
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
