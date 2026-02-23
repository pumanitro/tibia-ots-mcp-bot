"""
Dashboard API — HTTP for actions + WebSocket for real-time state push.

HTTP runs in a daemon thread.  WebSocket runs in the main asyncio loop
and pushes game state to all connected dashboards every 100ms.
"""

import asyncio
import json
import logging
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

log = logging.getLogger("dashboard_api")

# References set by start_api()
_main_loop: asyncio.AbstractEventLoop | None = None
_state = None  # will be set to the BotState instance
ACTIONS_DIR = Path(__file__).parent / "actions"

API_PORT = 8089
WS_PORT = 8090
WS_PUSH_INTERVAL = 0.1  # seconds between WebSocket pushes


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
        if self.path == "/api/recordings":
            return self._handle_list_recordings()
        # GET /api/cavebot/actions_map/<name>
        parts = self.path.strip("/").split("/")
        if (len(parts) == 4 and parts[0] == "api" and parts[1] == "cavebot"
                and parts[2] == "actions_map"):
            return self._handle_get_actions_map(parts[3])
        # GET /api/recordings/<name>
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "recordings":
            return self._handle_get_recording(parts[2])
        self._json_response({"error": "not found"}, 404)

    def do_POST(self):
        # POST /api/actions/{name}/toggle
        # POST /api/actions/{name}/restart
        parts = self.path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "actions":
            name = parts[2]
            if not all(c.isalnum() or c == '_' for c in name):
                self._json_response({"error": "invalid action name"}, 400)
                return
            verb = parts[3]
            if verb == "toggle":
                return self._handle_toggle(name)
            if verb == "restart":
                return self._handle_restart(name)

        # Cavebot endpoints
        if self.path == "/api/cavebot/record/start":
            return self._handle_cavebot_record_start()
        if self.path == "/api/cavebot/record/stop":
            return self._handle_cavebot_record_stop()
        if self.path == "/api/cavebot/play":
            return self._handle_cavebot_play()
        if self.path == "/api/cavebot/play/stop":
            return self._handle_cavebot_play_stop()

        self._json_response({"error": "not found"}, 404)

    def do_DELETE(self):
        parts = self.path.strip("/").split("/")
        # DELETE /api/actions/{name}
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "actions":
            name = parts[2]
            if not all(c.isalnum() or c == '_' for c in name):
                self._json_response({"error": "invalid action name"}, 400)
                return
            return self._handle_delete(name)
        # DELETE /api/recordings/{name}
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "recordings":
            name = parts[2]
            return self._handle_delete_recording(name)
        self._json_response({"error": "not found"}, 404)

    # ── GET /api/state ─────────────────────────────────────────────
    def _handle_get_state(self):
        body = _build_state_json().encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

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

    # ── Cavebot endpoints ─────────────────────────────────────────

    def _handle_cavebot_record_start(self):
        body = self._read_body()
        name = body.get("name", "").strip()
        if not name:
            self._json_response({"error": "name is required"}, 400)
            return
        if _main_loop is None or _state is None:
            self._json_response({"error": "bot not started"}, 503)
            return
        import cavebot
        err = cavebot.start_recording(_state, name)
        if err:
            self._json_response({"error": err}, 400)
        else:
            self._json_response({"ok": True, "message": f"Recording '{name}' started"})

    def _handle_cavebot_record_stop(self):
        if _state is None:
            self._json_response({"error": "bot not started"}, 503)
            return
        body = self._read_body()
        discard = body.get("discard", False)
        import cavebot
        rec = cavebot.stop_recording(_state, discard=discard)
        if rec is None:
            msg = "Recording discarded" if discard else "No recording in progress"
            self._json_response({"ok": True, "message": msg})
        else:
            count = len(rec.get("waypoints", []))
            self._json_response({"ok": True, "message": f"Saved '{rec['name']}' ({count} waypoints)"})

    def _handle_cavebot_play(self):
        body = self._read_body()
        name = body.get("name", "").strip()
        loop = body.get("loop", False)
        if not name:
            self._json_response({"error": "name is required"}, 400)
            return
        if _main_loop is None or _state is None:
            self._json_response({"error": "bot not started"}, 503)
            return

        main_mod = sys.modules["__main__"]
        future = asyncio.run_coroutine_threadsafe(
            main_mod._async_play_recording(name, loop), _main_loop
        )
        try:
            result = future.result(timeout=5)
            self._json_response({"ok": True, "message": result})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_cavebot_play_stop(self):
        if _main_loop is None or _state is None:
            self._json_response({"error": "bot not started"}, 503)
            return

        main_mod = sys.modules["__main__"]
        future = asyncio.run_coroutine_threadsafe(
            main_mod._async_stop_playback(), _main_loop
        )
        try:
            result = future.result(timeout=5)
            self._json_response({"ok": True, "message": result})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_list_recordings(self):
        import cavebot
        recs = cavebot.list_recordings()
        self._json_response({"recordings": recs})

    def _handle_get_recording(self, name: str):
        import cavebot
        rec = cavebot.load_recording(name)
        if rec is None:
            self._json_response({"error": f"Recording '{name}' not found"}, 404)
        else:
            self._json_response(rec)

    def _handle_get_actions_map(self, name: str):
        import cavebot
        rec = cavebot.load_recording(name)
        if rec is None:
            self._json_response({"error": f"Recording '{name}' not found"}, 404)
            return
        actions_map = cavebot.build_actions_map(rec)
        text_preview = cavebot.actions_map_to_text(actions_map)
        self._json_response({
            "name": name,
            "actions_map": actions_map,
            "text_preview": text_preview,
            "node_count": len(actions_map),
        })

    def _handle_delete_recording(self, name: str):
        import cavebot
        if cavebot.delete_recording(name):
            self._json_response({"ok": True, "message": f"Deleted '{name}'"})
        else:
            self._json_response({"error": f"Recording '{name}' not found"}, 404)


_started = False


# ── Shared state builder (used by HTTP and WebSocket) ──────────────

def _build_state_json() -> str:
    """Build the full state JSON string. Thread-safe read of global state."""
    st = _state
    if st is None:
        return json.dumps({"connected": False, "actions": []})

    main_mod = sys.modules.get("__main__")
    INTERNAL_ACTIONS = getattr(main_mod, "INTERNAL_ACTIONS", {"dll_bridge"})
    actions_settings = st.settings.get("actions", {})
    action_names = sorted(
        p.stem for p in ACTIONS_DIR.glob("*.py")
        if p.stem != "__init__" and p.stem not in INTERNAL_ACTIONS
    ) if ACTIONS_DIR.exists() else []

    action_tasks = dict(st._action_tasks)
    actions = []
    for name in action_names:
        cfg = actions_settings.get(name, {})
        enabled = cfg.get("enabled", False)
        task = action_tasks.get(name)
        running = task is not None and not task.done()
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
        logs = list(st.action_logs.get(name, []))
        entry = {
            "name": name, "enabled": enabled,
            "running": running, "description": desc,
        }
        if logs:
            entry["logs"] = logs
        actions.append(entry)

    pkt_server = st.game_proxy.packets_from_server if st.game_proxy else 0
    pkt_client = st.game_proxy.packets_from_client if st.game_proxy else 0

    gs = st.game_state
    stats_age = time.time() - gs.stats_updated_at if gs.stats_updated_at else -1
    player = {
        "hp": gs.hp, "max_hp": gs.max_hp,
        "mana": gs.mana, "max_mana": gs.max_mana,
        "level": gs.level, "experience": gs.experience,
        "position": list(gs.position),
        "magic_level": gs.magic_level, "soul": gs.soul,
        "stats_age": round(stats_age, 1),
    }

    creatures_snapshot = dict(gs.creatures)

    player_z = gs.position[2] if gs.position[2] != 0 else 0
    if player_z == 0 and gs.player_id in creatures_snapshot:
        player_z = creatures_snapshot[gs.player_id].get("z", 0)
    if player_z == 0:
        z_counts: dict[int, int] = {}
        for info in creatures_snapshot.values():
            cz = info.get("z", 0)
            if 1 <= cz <= 15:
                z_counts[cz] = z_counts.get(cz, 0) + 1
        if z_counts:
            player_z = max(z_counts, key=z_counts.get)

    creatures = [
        {"id": cid, "health": info.get("health", 0), "name": info.get("name", ""),
         "x": info.get("x", 0), "y": info.get("y", 0), "z": info.get("z", 0)}
        for cid, info in creatures_snapshot.items()
        if player_z == 0 or info.get("z") == player_z
    ]

    # DLL status
    dll_bridge_task = action_tasks.get("dll_bridge")
    dll_bridge_running = dll_bridge_task is not None and not dll_bridge_task.done()
    dll_injected = False
    try:
        bridge = getattr(st.game_state, "dll_bridge", None)
        if bridge is not None:
            dll_injected = bridge.connected
    except Exception:
        pass

    # Cavebot state
    import cavebot as _cb
    rec_waypoints = list(st.recording_waypoints) if st.recording_active else []
    cavebot_logs = list(st.action_logs.get("cavebot", []))
    cavebot_state = {
        "recording": {
            "active": st.recording_active,
            "name": st.recording_name,
            "waypoint_count": len(rec_waypoints),
            "waypoints": rec_waypoints[-20:],
        },
        "playback": {
            "active": st.playback_active,
            "recording_name": st.playback_recording_name,
            "index": st.playback_index,
            "total": st.playback_total,
            "loop": st.playback_loop,
            "logs": cavebot_logs[-100:],
            "minimap": st.playback_minimap,
            "actions_map_count": len(st.playback_actions_map),
        },
        "recordings": _cb.list_recordings(),
    }

    return json.dumps({
        "connected": st.connected,
        "actions": actions,
        "packets_from_server": pkt_server,
        "packets_from_client": pkt_client,
        "player": player,
        "creatures": creatures,
        "dll_injected": dll_injected,
        "dll_bridge_connected": dll_bridge_running and dll_injected,
        "cavebot": cavebot_state,
    })


# ── WebSocket push server ──────────────────────────────────────────

_ws_clients: set = set()


async def _ws_handler(websocket):
    """Handle a single WebSocket client connection."""
    _ws_clients.add(websocket)
    log.info(f"WS client connected ({len(_ws_clients)} total)")
    try:
        # Send initial state immediately
        await websocket.send(_build_state_json())
        # Keep connection alive — just wait for disconnect
        async for _ in websocket:
            pass  # ignore any messages from client
    except Exception:
        pass
    finally:
        _ws_clients.discard(websocket)
        log.info(f"WS client disconnected ({len(_ws_clients)} total)")


async def _ws_push_loop():
    """Push state to all connected WebSocket clients every WS_PUSH_INTERVAL."""
    while True:
        if _ws_clients:
            msg = _build_state_json()
            dead = []
            for ws in list(_ws_clients):
                try:
                    await ws.send(msg)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                _ws_clients.discard(ws)
        await asyncio.sleep(WS_PUSH_INTERVAL)


async def _start_ws_server():
    """Start the WebSocket server on the asyncio event loop."""
    try:
        import websockets
        server = await websockets.serve(_ws_handler, "127.0.0.1", WS_PORT)
        log.info(f"WebSocket server listening on ws://127.0.0.1:{WS_PORT}")
        asyncio.create_task(_ws_push_loop())
        await server.wait_closed()
    except ImportError:
        log.warning("websockets not installed — WS push disabled (pip install websockets)")
    except OSError as e:
        log.error(f"WebSocket server failed to bind port {WS_PORT}: {e}")
    except Exception as e:
        log.error(f"WebSocket server failed: {e}")


def start_api(loop: asyncio.AbstractEventLoop, bot_state) -> None:
    """Start the HTTP API in a daemon thread + WebSocket in asyncio loop."""
    global _main_loop, _state, _started
    _main_loop = loop
    _state = bot_state

    if _started:
        return
    _started = True

    # HTTP API (daemon thread)
    try:
        server = HTTPServer(("127.0.0.1", API_PORT), _Handler)
    except OSError as e:
        log.error(f"Failed to start HTTP server on port {API_PORT}: {e}")
        _started = False
        return
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info(f"Dashboard API listening on http://127.0.0.1:{API_PORT}")

    # WebSocket push server (asyncio)
    asyncio.run_coroutine_threadsafe(_start_ws_server(), loop)
