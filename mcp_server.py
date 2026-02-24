"""
DBVictory Bot — MCP Server

Exposes the bot's actions as MCP tools so Claude Code can control the
game character through natural language.

Transport: stdio  (stdout = JSON-RPC, all logging → stderr)
"""

import asyncio
import collections
import importlib
import importlib.util
import json
import logging
import subprocess
import sys
import os
import time as _time
import traceback
from pathlib import Path

# ── Logging to stderr (stdout reserved for MCP JSON-RPC) ────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mcp_server")

# ── Ensure project root is on sys.path ──────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from proxy import OTProxy
from game_state import GameState, parse_server_packet, scan_packet
from protocol import (
    Direction,
    build_walk_packet,
    build_turn_packet,
    build_say_packet,
    build_attack_packet,
    build_follow_packet,
    build_stop_walk_packet,
    build_ping_packet,
    build_use_item_packet,
    build_move_item_packet,
    build_look_packet,
    build_set_fight_modes_packet,
    PacketWriter,
    ClientOpcode,
    ServerOpcode,
    PacketReader,
)
from constants import SERVER_HOST, LOGIN_PORT, GAME_PORT
import cavebot

# ── Constants ───────────────────────────────────────────────────────
SETTINGS_FILE = Path(__file__).parent / "bot_settings.json"
ACTIONS_DIR = Path(__file__).parent / "actions"
INTERNAL_ACTIONS = {"dll_bridge"}


def load_settings() -> dict:
    """Load settings from disk, returning defaults if file doesn't exist."""
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Failed to load settings: {e}")
    return {"actions": {}}


def save_settings(settings: dict) -> None:
    """Persist settings to disk."""
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


# ── Global state ────────────────────────────────────────────────────

MAX_ACTION_LOGS = 200


class BotState:
    """Holds proxy references and connection status."""

    def __init__(self):
        self.login_proxy: OTProxy | None = None
        self.game_proxy: OTProxy | None = None
        self.ready: bool = False
        self._auto_task: asyncio.Task | None = None
        self._login_wait_task: asyncio.Task | None = None
        self._login_event: asyncio.Event | None = None
        self._proxy_tasks: list[asyncio.Task] = []
        self.settings: dict = load_settings()
        self._action_tasks: dict[str, asyncio.Task] = {}
        self.game_state: GameState = GameState()
        self.action_logs: dict[str, collections.deque] = {}

        # Cavebot recording state
        self.recording_active: bool = False
        self.recording_name: str = ""
        self.recording_waypoints: list[dict] = []
        self.recording_start_pos: tuple = (0, 0, 0)
        self.recording_start_time: float = 0
        self._recording_callback = None

        # Cavebot playback state
        self.playback_active: bool = False
        self.playback_recording_name: str = ""
        self.playback_index: int = 0
        self.playback_total: int = 0
        self.playback_loop: bool = False
        self.playback_actions_map: list[dict] = []
        self.playback_minimap: dict | None = None
        self.playback_failed_nodes: set[int] = set()

    @property
    def connected(self) -> bool:
        return self.ready and self.game_proxy is not None


state = BotState()


# ── Bot context (passed to action scripts as `bot`) ────────────────

class BotContext:
    """API surface available to action scripts via the `bot` parameter."""

    def __init__(self, action_name: str = ""):
        self._log = logging.getLogger("action")
        self._action_name = action_name

    # ── connection ──────────────────────────────────────────────────
    @property
    def is_connected(self) -> bool:
        return state.connected

    # ── low-level ───────────────────────────────────────────────────
    async def inject_to_server(self, packet: bytes) -> None:
        if state.game_proxy:
            await state.game_proxy.inject_to_server(packet)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    # ── convenience helpers ─────────────────────────────────────────
    async def use_item_in_container(
        self, item_id: int, container: int = 0, slot: int = 0
    ) -> None:
        """Use an item from an open container.

        Args:
            item_id: The item's type/sprite ID.
            container: Container ID (64=first backpack, 65=second, ...).
                       Pass raw ID as captured from packets.
            slot: Slot position within the container.
        """
        pkt = build_use_item_packet(0xFFFF, container, slot, item_id, slot, 0)
        await self.inject_to_server(pkt)

    async def use_item_on_map(
        self, x: int, y: int, z: int, item_id: int,
        stack_pos: int = 0, index: int = 0,
    ) -> None:
        pkt = build_use_item_packet(x, y, z, item_id, stack_pos, index)
        await self.inject_to_server(pkt)

    async def say(self, text: str) -> None:
        await self.inject_to_server(build_say_packet(text))

    async def walk(self, direction: str, steps: int = 1, delay: float = 0.3) -> None:
        d = _resolve_direction(direction)
        if d is None:
            return
        for _ in range(steps):
            await self.inject_to_server(build_walk_packet(d))
            if steps > 1:
                await self.sleep(delay)

    def log(self, msg: str) -> None:
        self._log.info(msg)
        if self._action_name:
            buf = state.action_logs.get(self._action_name)
            if buf is None:
                buf = collections.deque(maxlen=MAX_ACTION_LOGS)
                state.action_logs[self._action_name] = buf
            ts = _time.strftime("%H:%M:%S")
            buf.append(f"[{ts}] {msg}")

    # ── game state properties ────────────────────────────────────────
    @property
    def hp(self) -> int:
        return state.game_state.hp

    @property
    def max_hp(self) -> int:
        return state.game_state.max_hp

    @property
    def mana(self) -> int:
        return state.game_state.mana

    @property
    def max_mana(self) -> int:
        return state.game_state.max_mana

    @property
    def level(self) -> int:
        return state.game_state.level

    @property
    def experience(self) -> int:
        return state.game_state.experience

    @property
    def capacity(self) -> int:
        return state.game_state.capacity

    @property
    def speed(self) -> int:
        return state.game_state.speed

    @property
    def player_icons(self) -> int:
        return state.game_state.player_icons

    @property
    def position(self) -> tuple[int, int, int]:
        return state.game_state.position

    @property
    def player_id(self) -> int:
        return state.game_state.player_id

    @property
    def creatures(self) -> dict:
        return state.game_state.creatures

    @property
    def messages(self):
        return state.game_state.messages

    # ── advanced (for packet sniffing / hooks) ──────────────────────
    @property
    def game_proxy(self):
        return state.game_proxy

    @property
    def state(self):
        return state


bot_ctx = BotContext()


# ── Action loader / runner ─────────────────────────────────────────

def _load_action_module(name: str):
    """Dynamically load (or reload) actions/<name>.py and return the module."""
    if not all(c.isalnum() or c == '_' for c in name):
        return None
    path = ACTIONS_DIR / f"{name}.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(f"actions.{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _start_action(name: str) -> str | None:
    """Load an action script and launch its run(bot) as a background task.
    Returns an error string on failure, None on success."""
    # Don't double-start
    if name in state._action_tasks and not state._action_tasks[name].done():
        return f"'{name}' is already running."

    mod = _load_action_module(name)
    if mod is None:
        return f"actions/{name}.py not found."
    if not hasattr(mod, "run") or not asyncio.iscoroutinefunction(mod.run):
        return f"actions/{name}.py has no async def run(bot)."

    ctx = BotContext(action_name=name)

    async def _wrapper():
        try:
            log.info(f"[action:{name}] Started")
            await mod.run(ctx)
        except asyncio.CancelledError:
            log.info(f"[action:{name}] Stopped")
        except Exception:
            log.error(f"[action:{name}] Crashed:\n{traceback.format_exc()}")

    state._action_tasks[name] = asyncio.create_task(_wrapper())
    return None


def _stop_action(name: str) -> bool:
    """Cancel a running action task. Returns True if it was running."""
    task = state._action_tasks.pop(name, None)
    if task and not task.done():
        task.cancel()
        return True
    return False


def _start_all_enabled_actions() -> int:
    """Start background tasks for all enabled actions. Returns count started."""
    count = 0
    # Always start internal actions
    for name in INTERNAL_ACTIONS:
        err = _start_action(name)
        if err:
            log.warning(f"[action:{name}] Could not auto-start internal: {err}")
        else:
            count += 1
    # Start user-enabled actions
    for name, cfg in state.settings.get("actions", {}).items():
        if name in INTERNAL_ACTIONS:
            continue  # already started above
        if cfg.get("enabled", False):
            err = _start_action(name)
            if err:
                log.warning(f"[action:{name}] Could not auto-start: {err}")
            else:
                count += 1
    return count


def _discover_actions() -> list[str]:
    """Return sorted list of action names from .py files in actions/ dir."""
    if not ACTIONS_DIR.exists():
        return []
    return sorted(p.stem for p in ACTIONS_DIR.glob("*.py") if p.stem != "__init__")


# ── Shared async functions (used by MCP tools AND dashboard_api) ────

async def _async_toggle_action(name: str, enabled: bool) -> str:
    """Enable or disable an action. Returns a status message."""
    if name in INTERNAL_ACTIONS:
        return f"Action '{name}' is an internal service and cannot be toggled."
    path = ACTIONS_DIR / f"{name}.py"
    if not path.exists():
        return f"actions/{name}.py not found."

    actions = state.settings.setdefault("actions", {})
    actions.setdefault(name, {})["enabled"] = enabled
    save_settings(state.settings)

    if enabled:
        if state.connected:
            err = _start_action(name)
            if err:
                return f"Enabled but failed to start: {err}"
            return f"Action '{name}' enabled and started."
        return f"Action '{name}' enabled. It will auto-start when the bot connects."
    else:
        was_running = _stop_action(name)
        return f"Action '{name}' disabled{' and stopped' if was_running else ''}."


async def _async_restart_action(name: str) -> str:
    """Stop and re-start an action. Returns a status message."""
    path = ACTIONS_DIR / f"{name}.py"
    if not path.exists():
        return f"actions/{name}.py not found."

    _stop_action(name)
    await asyncio.sleep(0.1)
    err = _start_action(name)
    if err:
        return f"Failed to restart: {err}"
    return f"Action '{name}' restarted (code reloaded from disk)."


_dashboard_launched = False
DASHBOARD_PORT = 4747


def _kill_port(port: int) -> None:
    """Kill any process listening on the given TCP port (Windows)."""
    try:
        result = subprocess.run(
            f'netstat -ano | findstr "LISTENING" | findstr ":{port} "',
            capture_output=True, text=True, shell=True,
        )
        pids = set()
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if parts:
                pids.add(parts[-1])
        for pid in pids:
            if pid and pid != "0" and pid.isdigit():
                subprocess.run(["taskkill", "/F", "/PID", pid],
                               capture_output=True)
                log.info(f"Killed PID {pid} on port {port}")
    except Exception as e:
        log.debug(f"_kill_port({port}): {e}")


def _check_process_running(name: str) -> bool:
    """Check if a process with the given name is running (Windows)."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        return name.lower() in result.stdout.lower()
    except Exception:
        return False


def _check_port_listening(port: int) -> bool:
    """Check if something is listening on the given TCP port."""
    try:
        result = subprocess.run(
            f'netstat -ano | findstr "LISTENING" | findstr ":{port} "',
            capture_output=True, text=True, shell=True, timeout=5,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def _check_pipe_exists() -> bool:
    """Check if the dbvbot named pipe exists (DLL loaded and running)."""
    try:
        from dll_bridge import DllBridge
        bridge = DllBridge()
        exists = bridge.pipe_exists()
        return exists
    except Exception:
        return False


def _build_status_report() -> str:
    """Build a multi-line status report of all components."""
    lines = []

    # MCP Server — always OK if we're here
    lines.append("[OK] MCP Server: running")

    # Game client
    client_running = _check_process_running("dbvStart.exe")
    if client_running:
        lines.append("[OK] Game Client: dbvStart.exe detected")
    else:
        lines.append("[!!] Game Client: dbvStart.exe NOT found")

    # Proxy
    if state.ready and state.game_proxy:
        svr = state.game_proxy.packets_from_server
        cli = state.game_proxy.packets_from_client
        has_writers = (state.game_proxy.server_writer is not None
                       and state.game_proxy.client_writer is not None)
        logged_in = state.game_proxy.logged_in
        if logged_in and has_writers:
            lines.append(f"[OK] Proxy: connected (server={svr} client={cli} packets)")
        else:
            lines.append(f"[!!] Proxy: BROKEN (logged_in={logged_in} writers={has_writers} "
                         f"server={svr} client={cli})")
    elif state.game_proxy:
        lines.append("[..] Proxy: started, waiting for login")
    else:
        lines.append("[--] Proxy: not started")

    # DLL pipe
    pipe_ok = _check_pipe_exists()
    if pipe_ok:
        lines.append("[OK] DLL Pipe: dbvbot pipe available")
    else:
        lines.append("[--] DLL Pipe: not detected (dll_bridge action will inject)")

    # Dashboard / Electron
    dashboard_listening = _check_port_listening(DASHBOARD_PORT)
    electron_running = _check_process_running("electron.exe")
    if electron_running and dashboard_listening:
        lines.append(f"[OK] Dashboard: Electron running on port {DASHBOARD_PORT}")
    elif dashboard_listening:
        lines.append(f"[OK] Dashboard: dev server on port {DASHBOARD_PORT} (Electron starting...)")
    elif _dashboard_launched:
        lines.append("[..] Dashboard: launch requested, waiting for startup...")
    else:
        lines.append("[--] Dashboard: not launched")

    # Player info
    gs = state.game_state
    if gs.player_id:
        pos = gs.position
        lines.append(f"[OK] Player: id={gs.player_id:#010x} pos=({pos[0]},{pos[1]},{pos[2]}) "
                      f"HP={gs.hp}/{gs.max_hp} creatures={len(gs.creatures)}")

    return "\n".join(lines)


def _launch_dashboard() -> None:
    """Fire-and-forget launch of the Electron dashboard.

    Re-launches Electron if it has exited; skips Next.js if already on port.
    """
    global _dashboard_launched
    import shutil
    dashboard_dir = Path(__file__).parent / "dashboard"
    if not (dashboard_dir / "package.json").exists():
        log.warning("Dashboard not found — skipping launch.")
        return

    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm is None:
        log.warning("npm not found on PATH — cannot launch dashboard.")
        return

    nextjs_running = _check_port_listening(DASHBOARD_PORT)
    electron_running = _check_process_running("electron.exe")

    if nextjs_running and electron_running:
        log.info("Dashboard already fully running — skipping launch.")
        _dashboard_launched = True
        return

    try:
        # Start Next.js dev server only if not already listening
        if not nextjs_running:
            _kill_port(DASHBOARD_PORT)
            subprocess.Popen(
                f'"{npm}" run dev',
                cwd=str(dashboard_dir),
                shell=True,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            log.info(f"Next.js dev server launching on port {DASHBOARD_PORT}")

        # Start Electron only if not already running
        if not electron_running:
            subprocess.Popen(
                f'"{npm}" run electron:only',
                cwd=str(dashboard_dir),
                shell=True,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            log.info("Electron launching (waiting for Next.js).")

        _dashboard_launched = True
    except Exception as e:
        log.warning(f"Failed to launch dashboard: {e}")


# ── MCP Server ──────────────────────────────────────────────────────

mcp = FastMCP(
    "dbvictory-bot",
    instructions=(
        "DBVictory game bot. Use start_bot first to patch the client and "
        "launch the proxy. Then use the other tools to control the character."
    ),
)

# ── Direction helpers ───────────────────────────────────────────────

DIR_MAP = {
    "n": Direction.NORTH, "north": Direction.NORTH,
    "s": Direction.SOUTH, "south": Direction.SOUTH,
    "e": Direction.EAST,  "east": Direction.EAST,
    "w": Direction.WEST,  "west": Direction.WEST,
    "ne": Direction.NORTHEAST, "northeast": Direction.NORTHEAST,
    "se": Direction.SOUTHEAST, "southeast": Direction.SOUTHEAST,
    "sw": Direction.SOUTHWEST, "southwest": Direction.SOUTHWEST,
    "nw": Direction.NORTHWEST, "northwest": Direction.NORTHWEST,
}

TURN_DIR_MAP = {
    "n": Direction.NORTH, "north": Direction.NORTH,
    "s": Direction.SOUTH, "south": Direction.SOUTH,
    "e": Direction.EAST,  "east": Direction.EAST,
    "w": Direction.WEST,  "west": Direction.WEST,
}


def _resolve_direction(direction: str, allow_diagonal: bool = True) -> Direction | None:
    d = direction.strip().lower()
    if allow_diagonal:
        return DIR_MAP.get(d)
    return TURN_DIR_MAP.get(d)


# ── Bot reset / hot-reload ───────────────────────────────────────────

async def _reset_bot() -> str:
    """Tear down the running bot, hot-reload modules, and reset state."""
    global _dashboard_launched
    import game_state as _gs_mod
    import dashboard_api as _da_mod

    log.info("=== RESETTING BOT ===")

    # 1. Stop all running actions
    for name in list(state._action_tasks):
        _stop_action(name)
    await asyncio.sleep(0.1)

    # 2. Close listening servers FIRST (prevents old server accepting new clients)
    for proxy in (state.game_proxy, state.login_proxy):
        if proxy:
            if hasattr(proxy, 'close_server'):
                proxy.close_server()
            # Also close client/server sockets (unblocks relay reads)
            for writer_attr in ('client_writer', 'server_writer'):
                w = getattr(proxy, writer_attr, None)
                if w:
                    try:
                        w.close()
                    except Exception:
                        pass

    # 2b. Cancel active connection handler tasks (now unblocked by socket close)
    tasks_to_await = []
    for proxy in (state.game_proxy, state.login_proxy):
        if proxy:
            ct = getattr(proxy, '_connection_task', None)
            if ct and not ct.done():
                ct.cancel()
                tasks_to_await.append(ct)

    # 2c. Cancel proxy tasks (serve_forever)
    for task in state._proxy_tasks:
        if not task.done():
            task.cancel()
            tasks_to_await.append(task)
    state._proxy_tasks.clear()

    # 2d. Actually await all cancelled tasks to ensure they're fully stopped
    for t in tasks_to_await:
        try:
            await asyncio.wait_for(t, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass

    # 3. Nil out proxy references (sockets already closed above)
    for proxy in (state.game_proxy, state.login_proxy):
        if proxy:
            proxy.client_writer = None
            proxy.server_writer = None
            proxy.logged_in = False

    await asyncio.sleep(0.5)  # Windows needs time for port release

    # 4. Kill dashboard (Electron + Next.js dev server)
    _kill_port(DASHBOARD_PORT)
    try:
        subprocess.run(
            'taskkill /F /IM electron.exe',
            shell=True, capture_output=True,
        )
    except Exception:
        pass
    _dashboard_launched = False

    # 5. Hot-reload Python modules
    try:
        importlib.reload(_gs_mod)
        log.info("Reloaded game_state.py")
    except Exception as e:
        log.warning(f"Failed to reload game_state: {e}")
    try:
        importlib.reload(_da_mod)
        log.info("Reloaded dashboard_api.py")
    except Exception as e:
        log.warning(f"Failed to reload dashboard_api: {e}")
    import cavebot as _cb_mod
    try:
        importlib.reload(_cb_mod)
        log.info("Reloaded cavebot.py")
    except Exception as e:
        log.warning(f"Failed to reload cavebot: {e}")

    # Re-import after reload (use the already-reloaded module refs)
    globals()['scan_packet'] = _gs_mod.scan_packet
    globals()['parse_server_packet'] = _gs_mod.parse_server_packet
    from game_state import GameState

    # 5b. Cancel the login-wait task
    login_task = getattr(state, '_login_wait_task', None)
    if login_task and not login_task.done():
        login_task.cancel()
        try:
            await asyncio.wait_for(login_task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass

    # 6. Reset global state
    state.login_proxy = None
    state.game_proxy = None
    state.ready = False
    state._login_event = asyncio.Event()
    state._auto_task = None
    state._action_tasks.clear()
    state.game_state = GameState()
    state.settings = load_settings()

    # Reset cavebot state
    state.recording_active = False
    state.recording_name = ""
    state.recording_waypoints = []
    state.recording_start_pos = (0, 0, 0)
    state.recording_start_time = 0
    state._recording_callback = None
    state.playback_active = False
    state.playback_recording_name = ""
    state.playback_index = 0
    state.playback_total = 0
    state.playback_loop = False
    state.playback_actions_map = []
    state.playback_minimap = None
    state.playback_failed_nodes = set()

    log.info("Bot reset complete.")
    return "Bot reset. Call start_bot to reconnect."


# ── Force-close game TCP connections ─────────────────────────────────

def _close_game_connections(pid: int) -> int:
    """Force-close active TCP connections belonging to the game process.

    Uses the Windows SetTcpEntry API to reset TCP connections, causing the
    game client to disconnect and return to the login screen.  Requires
    admin privileges (which we already have since pymem works).

    Returns the number of connections closed.
    """
    import ctypes
    import ctypes.wintypes
    import struct as _struct
    import socket

    # MIB_TCPROW_OWNER_PID for GetExtendedTcpTable
    class MIB_TCPROW_OWNER_PID(ctypes.Structure):
        _fields_ = [
            ("dwState", ctypes.wintypes.DWORD),
            ("dwLocalAddr", ctypes.wintypes.DWORD),
            ("dwLocalPort", ctypes.wintypes.DWORD),
            ("dwRemoteAddr", ctypes.wintypes.DWORD),
            ("dwRemotePort", ctypes.wintypes.DWORD),
            ("dwOwningPid", ctypes.wintypes.DWORD),
        ]

    # MIB_TCPROW for SetTcpEntry (no PID field)
    class MIB_TCPROW(ctypes.Structure):
        _fields_ = [
            ("dwState", ctypes.wintypes.DWORD),
            ("dwLocalAddr", ctypes.wintypes.DWORD),
            ("dwLocalPort", ctypes.wintypes.DWORD),
            ("dwRemoteAddr", ctypes.wintypes.DWORD),
            ("dwRemotePort", ctypes.wintypes.DWORD),
        ]

    TCP_TABLE_OWNER_PID_ALL = 5
    AF_INET = 2
    MIB_TCP_STATE_DELETE_TCB = 12

    iphlpapi = ctypes.windll.iphlpapi

    # First call to get buffer size
    buf_size = ctypes.wintypes.DWORD(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(buf_size), False,
                                  AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)

    buf = ctypes.create_string_buffer(buf_size.value)
    ret = iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(buf_size), False,
                                        AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
    if ret != 0:
        log.warning(f"GetExtendedTcpTable failed: {ret}")
        return 0

    # Parse the table
    num_entries = _struct.unpack_from('<I', buf.raw, 0)[0]
    row_size = ctypes.sizeof(MIB_TCPROW_OWNER_PID)
    closed = 0

    for i in range(num_entries):
        offset = 4 + i * row_size
        row_data = buf.raw[offset:offset + row_size]
        if len(row_data) < row_size:
            break

        row = MIB_TCPROW_OWNER_PID.from_buffer_copy(row_data)

        if row.dwOwningPid != pid:
            continue

        # Convert port from network byte order
        remote_port = socket.ntohs(row.dwRemotePort & 0xFFFF)
        local_port = socket.ntohs(row.dwLocalPort & 0xFFFF)
        remote_ip = socket.inet_ntoa(_struct.pack('<I', row.dwRemoteAddr))

        # Close connections to game/login server ports (not localhost proxy)
        if remote_port in (GAME_PORT, LOGIN_PORT) and remote_ip != "127.0.0.1":
            log.info(f"Closing game connection: {remote_ip}:{remote_port} (state={row.dwState})")
            close_row = MIB_TCPROW()
            close_row.dwState = MIB_TCP_STATE_DELETE_TCB
            close_row.dwLocalAddr = row.dwLocalAddr
            close_row.dwLocalPort = row.dwLocalPort
            close_row.dwRemoteAddr = row.dwRemoteAddr
            close_row.dwRemotePort = row.dwRemotePort
            result = iphlpapi.SetTcpEntry(ctypes.byref(close_row))
            if result == 0:
                closed += 1
                log.info(f"  -> Closed successfully")
            else:
                log.warning(f"  -> SetTcpEntry failed: {result}")

    return closed


# ── Tools ───────────────────────────────────────────────────────────

@mcp.tool()
async def start_bot() -> str:
    """Patch the game client's memory and start the login + game proxies.

    The game client (dbvStart.exe) must already be running and sitting at
    the login screen.  This tool patches the server IP in memory to
    redirect traffic through the local proxy, then waits for the player
    to log in through the game client.
    """
    if state.ready:
        # Auto-reset: tear down everything and continue to fresh start
        log.info("Bot already running — auto-resetting...")
        await _reset_bot()

    # ── Patch client memory ─────────────────────────────────────────
    try:
        import pymem
    except ImportError:
        return "ERROR: pymem is not installed. Run: pip install pymem"

    try:
        pm = pymem.Pymem("dbvStart.exe")
    except Exception as e:
        return f"ERROR: Could not attach to dbvStart.exe — is the game running? ({e})"

    game_pid = pm.process_id
    log.info(f"Attached to client (PID: {game_pid})")

    # ── Force-close existing game connections ──────────────────────
    # If the client is already logged in, close its TCP connections so it
    # drops back to the login screen and reconnects through our proxy.
    closed = _close_game_connections(game_pid)
    if closed > 0:
        log.info(f"Closed {closed} existing game connection(s) — client should return to login screen")
        await asyncio.sleep(2)  # Give the client time to process the disconnect

    from patcher import find_server_address_in_memory, patch_memory

    ip_locs = find_server_address_in_memory(pm)
    localhost = b"127.0.0.1"
    patched = sum(1 for addr, old in ip_locs if patch_memory(pm, addr, old, localhost))
    pm.close_process()

    if patched == 0:
        # Client may already be patched from a previous session — continue anyway
        log.warning("No IPs patched (client may already be patched). Continuing...")

    log.info(f"Patched {patched} server IP(s)")

    # ── Create proxies ──────────────────────────────────────────────
    state.login_proxy = OTProxy(SERVER_HOST, LOGIN_PORT, LOGIN_PORT, is_login_proxy=True)
    state.game_proxy = OTProxy(SERVER_HOST, GAME_PORT, GAME_PORT, is_login_proxy=False)

    # Callbacks
    # NOTE: on_server_packet removed — scan_packet handles all opcodes via
    # on_raw_server_data, avoiding redundant double-parsing of every packet.

    def on_client_packet(opcode, reader):
        try:
            name = ClientOpcode(opcode).name
        except ValueError:
            name = "?"
        log.debug(f"[C->S] 0x{opcode:02X} ({name})")
        # Track current attack target in game_state
        if opcode == ClientOpcode.ATTACK:
            try:
                cid = reader.read_u32()
                state.game_state.attack_target_id = cid
            except Exception:
                pass
        # Log USE_ITEM details so we can discover item IDs
        elif opcode == ClientOpcode.USE_ITEM:
            try:
                pos = reader.read_position()
                item_id = reader.read_u16()
                stack_pos = reader.read_u8()
                log.info(f"[SNIFF] USE_ITEM pos={pos} item_id={item_id} stack={stack_pos}")
            except Exception:
                pass

    def on_login_success(keys):
        state.ready = True
        log.info("=== BOT READY — game session established ===")
        # Signal the login-wait task immediately (no polling delay)
        event = getattr(state, '_login_event', None)
        if event:
            event.set()
        # Direct auto-start: schedule action startup from the callback itself.
        # This is the bulletproof path — no watcher task, no race condition.
        async def _direct_start():
            # Brief delay for proxy to finish setting up server_writer
            for _ in range(10):
                await asyncio.sleep(0.5)
                if (state.game_proxy and state.game_proxy.logged_in
                        and state.game_proxy.server_writer is not None):
                    break
            n = _start_all_enabled_actions()
            log.info(f"on_login_success: {n} action(s) auto-started")
        asyncio.ensure_future(_direct_start())

    def on_raw_server_data(data):
        scan_packet(data, state.game_state)

    state.game_proxy.register_client_packet_callback(on_client_packet)
    state.game_proxy.on_login_success = on_login_success
    state.game_proxy.on_raw_server_data = on_raw_server_data

    # ── Launch proxies as background tasks ──────────────────────────
    state._proxy_tasks = [
        asyncio.create_task(state.login_proxy.start()),
        asyncio.create_task(state.game_proxy.start()),
    ]

    log.info("Proxies started — waiting for player to log in via the game client...")

    # Launch dashboard API and Electron immediately (don't wait for login)
    import dashboard_api
    dashboard_api.start_api(asyncio.get_running_loop(), state)
    _launch_dashboard()

    # Background task: wait for login, then start actions automatically
    # Uses an asyncio.Event for instant wakeup (no polling race condition).
    state._login_event = asyncio.Event()
    # If already ready (e.g. fast login), pre-set the event
    if state.ready:
        state._login_event.set()

    async def _wait_for_login_and_start():
        t0 = time.time()
        log.info(f"[TIMING] waiting for login... t=0.0s")
        try:
            await asyncio.wait_for(state._login_event.wait(), timeout=120.0)
        except asyncio.TimeoutError:
            log.warning(f"[TIMING] No login detected after {time.time()-t0:.1f}s — actions not started.")
            return

        t1 = time.time()
        log.info(f"[TIMING] login event fired at t={t1-t0:.1f}s — verifying proxy...")

        # Verify proxy connection is functional
        for i in range(10):
            await asyncio.sleep(0.5)
            if (state.game_proxy and state.game_proxy.logged_in
                    and state.game_proxy.server_writer is not None):
                t2 = time.time()
                log.info(f"[TIMING] proxy verified at t={t2-t0:.1f}s (waited {t2-t1:.1f}s)")
                break
        else:
            t2 = time.time()
            log.info(f"[TIMING] proxy verify timeout at t={t2-t0:.1f}s — starting actions anyway")

        actions_started = _start_all_enabled_actions()
        t3 = time.time()
        log.info(f"[TIMING] {actions_started} action(s) started at t={t3-t0:.1f}s")

    state._login_wait_task = asyncio.create_task(_wait_for_login_and_start())

    status = _build_status_report()
    return (
        f"Bot launched. Patched {patched} IP(s). Proxies listening.\n"
        f"Log in through the game client — actions will auto-start.\n\n"
        f"=== Component Status ===\n{status}"
    )


@mcp.tool()
async def walk(direction: str, steps: int = 1) -> str:
    """Walk the character in a direction.

    Args:
        direction: One of n, s, e, w, ne, se, sw, nw (or full name like north).
        steps: Number of tiles to walk (default 1).
    """
    if not state.connected:
        return "Bot is not connected. Call start_bot first."

    d = _resolve_direction(direction)
    if d is None:
        return f"Unknown direction: {direction}. Use n/s/e/w/ne/se/sw/nw."

    for i in range(steps):
        await state.game_proxy.inject_to_server(build_walk_packet(d))
        if steps > 1:
            await asyncio.sleep(0.3)

    return f"Walked {d.name} {steps} step(s)."


@mcp.tool()
async def turn(direction: str) -> str:
    """Turn the character to face a cardinal direction.

    Args:
        direction: One of n, s, e, w (or full name).
    """
    if not state.connected:
        return "Bot is not connected. Call start_bot first."

    d = _resolve_direction(direction, allow_diagonal=False)
    if d is None:
        return f"Unknown direction: {direction}. Turn only supports n/s/e/w."

    await state.game_proxy.inject_to_server(build_turn_packet(d))
    return f"Turned {d.name}."


@mcp.tool()
async def say(text: str) -> str:
    """Send a chat message in the game.

    Args:
        text: The message to say.
    """
    if not state.connected:
        return "Bot is not connected. Call start_bot first."

    await state.game_proxy.inject_to_server(build_say_packet(text))
    return f"Said: {text}"


@mcp.tool()
async def attack(creature_id: int) -> str:
    """Attack a creature by its ID.

    Args:
        creature_id: The numeric creature ID to attack.
    """
    if not state.connected:
        return "Bot is not connected. Call start_bot first."

    await state.game_proxy.inject_to_server(build_attack_packet(creature_id))
    return f"Attacking creature {creature_id}."


@mcp.tool()
async def follow(creature_id: int) -> str:
    """Follow a creature by its ID.

    Args:
        creature_id: The numeric creature ID to follow.
    """
    if not state.connected:
        return "Bot is not connected. Call start_bot first."

    await state.game_proxy.inject_to_server(build_follow_packet(creature_id))
    return f"Following creature {creature_id}."


@mcp.tool()
async def stop() -> str:
    """Stop the character from walking."""
    if not state.connected:
        return "Bot is not connected. Call start_bot first."

    await state.game_proxy.inject_to_server(build_stop_walk_packet())
    return "Stopped walking."


@mcp.tool()
async def autowalk(direction: str, steps: int = 100, delay: float = 0.5) -> str:
    """Auto-walk in a direction as a background task.

    Args:
        direction: One of n, s, e, w, ne, se, sw, nw.
        steps: Maximum number of steps (default 100).
        delay: Seconds between each step (default 0.5).
    """
    if not state.connected:
        return "Bot is not connected. Call start_bot first."

    d = _resolve_direction(direction)
    if d is None:
        return f"Unknown direction: {direction}."

    # Cancel existing auto-walk
    if state._auto_task and not state._auto_task.done():
        state._auto_task.cancel()

    async def _loop():
        for _ in range(steps):
            if not state.connected:
                break
            await state.game_proxy.inject_to_server(build_walk_packet(d))
            await asyncio.sleep(delay)

    state._auto_task = asyncio.create_task(_loop())
    return f"Auto-walking {d.name} for up to {steps} steps (delay={delay}s). Use stop_autowalk to cancel."


@mcp.tool()
async def stop_autowalk() -> str:
    """Cancel a running auto-walk task."""
    if state._auto_task and not state._auto_task.done():
        state._auto_task.cancel()
        state._auto_task = None
        if state.connected:
            await state.game_proxy.inject_to_server(build_stop_walk_packet())
        return "Auto-walk cancelled."
    return "No auto-walk is running."


@mcp.tool()
async def use_item(x: int, y: int, z: int, item_id: int, stack_pos: int = 0, index: int = 0) -> str:
    """Use an item at a map position.

    Args:
        x: Map X coordinate.
        y: Map Y coordinate.
        z: Map Z (floor) coordinate.
        item_id: The item's type/sprite ID.
        stack_pos: Stack position on the tile (default 0).
        index: Container index (default 0).
    """
    if not state.connected:
        return "Bot is not connected. Call start_bot first."

    await state.game_proxy.inject_to_server(
        build_use_item_packet(x, y, z, item_id, stack_pos, index)
    )
    return f"Used item {item_id} at ({x}, {y}, {z})."


@mcp.tool()
async def move_item(
    from_x: int, from_y: int, from_z: int,
    item_id: int, from_stack: int,
    to_x: int, to_y: int, to_z: int,
    count: int = 1,
) -> str:
    """Move/drag an item from one position to another.

    Args:
        from_x: Source X coordinate.
        from_y: Source Y coordinate.
        from_z: Source Z coordinate.
        item_id: The item's type/sprite ID.
        from_stack: Stack position at the source.
        to_x: Destination X coordinate.
        to_y: Destination Y coordinate.
        to_z: Destination Z coordinate.
        count: Number of items to move (default 1).
    """
    if not state.connected:
        return "Bot is not connected. Call start_bot first."

    await state.game_proxy.inject_to_server(
        build_move_item_packet((from_x, from_y, from_z), item_id, from_stack, (to_x, to_y, to_z), count)
    )
    return f"Moved {count}x item {item_id} from ({from_x},{from_y},{from_z}) to ({to_x},{to_y},{to_z})."


@mcp.tool()
async def look_at(x: int, y: int, z: int, item_id: int, stack_pos: int = 0) -> str:
    """Look at a tile or item on the map.

    Args:
        x: Map X coordinate.
        y: Map Y coordinate.
        z: Map Z coordinate.
        item_id: The item/object ID on the tile.
        stack_pos: Stack position (default 0).
    """
    if not state.connected:
        return "Bot is not connected. Call start_bot first."

    await state.game_proxy.inject_to_server(build_look_packet(x, y, z, item_id, stack_pos))
    return f"Looking at item {item_id} at ({x}, {y}, {z})."


# ── Automated action tools ──────────────────────────────────────────

@mcp.tool()
async def list_actions() -> str:
    """List all action scripts in the actions/ folder with their enabled/running status and source preview.

    To create a new action, write a .py file to the actions/ folder with an
    async def run(bot) entry point.  The `bot` object provides:
      bot.use_item_in_container(item_id, container, slot)
      bot.say(text)
      bot.walk(direction, steps, delay)
      bot.inject_to_server(packet)
      bot.sleep(seconds)
      bot.is_connected
      bot.log(msg)
    """
    names = [n for n in _discover_actions() if n not in INTERNAL_ACTIONS]
    if not names:
        return "No actions found. Create a .py file in the actions/ folder with async def run(bot)."

    settings_actions = state.settings.get("actions", {})

    # Gather data for each action
    rows = []
    for name in names:
        cfg = settings_actions.get(name, {})
        enabled = cfg.get("enabled", False)
        task = state._action_tasks.get(name)
        running = task is not None and not task.done()

        if enabled and running:
            status = ">>> RUNNING"
        elif enabled:
            status = "ON (idle)"
        else:
            status = "OFF"

        # Read docstring as description
        path = ACTIONS_DIR / f"{name}.py"
        desc = ""
        source = ""
        try:
            source = path.read_text(encoding="utf-8")
            for line in source.splitlines():
                s = line.strip().strip('"').strip("'")
                if s:
                    desc = s[:50]
                    break
        except OSError:
            pass

        rows.append((name, status, desc, source))

    # Build ASCII table
    name_w = max(max(len(r[0]) for r in rows), 6)
    stat_w = max(max(len(r[1]) for r in rows), 6)
    desc_w = max(max(len(r[2]) for r in rows), 11)

    sep = f"+{'-' * (name_w + 2)}+{'-' * (stat_w + 2)}+{'-' * (desc_w + 2)}+"
    hdr = f"| {'Action':<{name_w}} | {'Status':<{stat_w}} | {'Description':<{desc_w}} |"

    lines = [sep, hdr, sep]
    for name, status, desc, source in rows:
        lines.append(f"| {name:<{name_w}} | {status:<{stat_w}} | {desc:<{desc_w}} |")
    lines.append(sep)

    # Append source preview for each action
    for name, _, _, source in rows:
        preview = source.rstrip()
        if len(preview) > 500:
            preview = preview[:500] + "\n... (truncated)"
        lines.append(f"\n--- {name}.py ---")
        lines.append(preview)

    return "\n".join(lines)


@mcp.tool()
async def toggle_action(name: str, enabled: bool = True) -> str:
    """Enable or disable an action, starting or stopping its background task.

    Args:
        name: Action name (filename without .py, e.g. "eat_food").
        enabled: True to enable and start, False to disable and stop.
    """
    return await _async_toggle_action(name, enabled)


@mcp.tool()
async def remove_action(name: str) -> str:
    """Delete an action script and its settings.

    Args:
        name: Action name (filename without .py, e.g. "eat_food").
    """
    if name in INTERNAL_ACTIONS:
        return f"Action '{name}' is an internal service and cannot be removed."
    if not all(c.isalnum() or c == '_' for c in name):
        return "Invalid action name. Only alphanumeric characters and underscores are allowed."

    _stop_action(name)

    path = ACTIONS_DIR / f"{name}.py"
    existed = path.exists()
    if existed:
        path.unlink()

    actions = state.settings.get("actions", {})
    was_in_settings = actions.pop(name, None) is not None
    if was_in_settings:
        save_settings(state.settings)

    if not existed and not was_in_settings:
        return f"Action '{name}' not found."
    return f"Removed action '{name}'."


@mcp.tool()
async def restart_action(name: str) -> str:
    """Stop and re-start a running action (reloads the .py file from disk).

    Args:
        name: Action name (filename without .py).
    """
    return await _async_restart_action(name)


@mcp.tool()
async def set_fight_modes(fight_mode: int = 1, chase_mode: int = 0, safe_mode: int = 1) -> str:
    """Set combat/fight modes.

    Args:
        fight_mode: 1 = offensive, 2 = balanced, 3 = defensive.
        chase_mode: 0 = stand still, 1 = chase opponent.
        safe_mode: 0 = PvP enabled, 1 = safe mode (no PvP).
    """
    if not state.connected:
        return "Bot is not connected. Call start_bot first."

    await state.game_proxy.inject_to_server(
        build_set_fight_modes_packet(fight_mode, chase_mode, safe_mode)
    )
    mode_names = {1: "offensive", 2: "balanced", 3: "defensive"}
    chase_names = {0: "stand", 1: "chase"}
    safe_names = {0: "PvP", 1: "safe"}
    return (
        f"Fight modes set: {mode_names.get(fight_mode, fight_mode)}, "
        f"{chase_names.get(chase_mode, chase_mode)}, "
        f"{safe_names.get(safe_mode, safe_mode)}."
    )


@mcp.tool()
async def send_ping() -> str:
    """Send a ping packet to the game server."""
    if not state.connected:
        return "Bot is not connected. Call start_bot first."

    await state.game_proxy.inject_to_server(build_ping_packet())
    return "Ping sent."


@mcp.tool()
async def get_status() -> str:
    """Get the current connection state and packet statistics."""
    status = _build_status_report()

    # Running actions
    running = [n for n, t in state._action_tasks.items() if t and not t.done()]
    if running:
        status += f"\n\nRunning actions: {', '.join(running)}"

    auto = "running" if (state._auto_task and not state._auto_task.done()) else "idle"
    status += f"\nAuto-walk: {auto}"

    return f"=== Component Status ===\n{status}"


@mcp.tool()
async def logout() -> str:
    """Send a logout packet to the game server."""
    if not state.connected:
        return "Bot is not connected. Call start_bot first."

    pw = PacketWriter()
    pw.write_u8(ClientOpcode.LOGOUT)
    await state.game_proxy.inject_to_server(pw.data)
    return "Logout packet sent."


# ── Cavebot recording / playback tools ─────────────────────────────

@mcp.tool()
async def start_recording(name: str) -> str:
    """Begin recording navigation waypoints (walk, use items, stairs, doors).

    Args:
        name: Name for the recording (alphanumeric, hyphens, underscores).
    """
    if not state.connected:
        return "Bot is not connected. Call start_bot first."
    err = cavebot.start_recording(state, name)
    if err:
        return f"Error: {err}"
    return f"Recording '{name}' started. Walk around, use doors/stairs/ladders. Call stop_recording when done."


@mcp.tool()
async def stop_recording() -> str:
    """Stop recording and save waypoints to disk."""
    if not state.recording_active:
        return "No recording in progress."
    rec = cavebot.stop_recording(state)
    if rec is None:
        return "Recording stopped (no data saved)."
    count = len(rec.get("waypoints", []))
    return f"Recording '{rec['name']}' saved with {count} waypoint(s)."


@mcp.tool()
async def list_recordings() -> str:
    """List all saved cavebot recordings."""
    recs = cavebot.list_recordings()
    if not recs:
        return "No recordings found. Use start_recording to create one."
    lines = []
    for r in recs:
        lines.append(f"  {r['name']}  ({r['count']} waypoints)  created: {r['created_at']}")
    return "Saved recordings:\n" + "\n".join(lines)


@mcp.tool()
async def delete_recording(name: str) -> str:
    """Delete a saved cavebot recording.

    Args:
        name: Name of the recording to delete.
    """
    if cavebot.delete_recording(name):
        return f"Recording '{name}' deleted."
    return f"Recording '{name}' not found."


async def _async_play_recording(name: str, loop: bool = False) -> str:
    """Start playback (shared by MCP tool and dashboard API)."""
    if not state.connected:
        return "Bot is not connected. Call start_bot first."
    if state.playback_active:
        return f"Already playing '{state.playback_recording_name}'. Stop it first."

    rec = cavebot.load_recording(name)
    if rec is None:
        return f"Recording '{name}' not found."

    waypoints = rec.get("waypoints", [])
    if not waypoints:
        return f"Recording '{name}' has no waypoints."

    state.playback_active = True
    state.playback_recording_name = name
    state.playback_index = 0
    state.playback_total = len(waypoints)
    state.playback_loop = loop

    # Start the cavebot action
    err = _start_action("cavebot")
    if err:
        state.playback_active = False
        return f"Failed to start playback: {err}"

    loop_str = " (looping)" if loop else ""
    return f"Playing recording '{name}' ({len(waypoints)} waypoints){loop_str}."


async def _async_stop_playback() -> str:
    """Stop playback (shared by MCP tool and dashboard API)."""
    if not state.playback_active:
        return "No playback in progress."
    _stop_action("cavebot")
    name = state.playback_recording_name
    state.playback_active = False
    state.playback_recording_name = ""
    state.playback_index = 0
    state.playback_total = 0
    state.playback_loop = False
    state.playback_actions_map = []
    state.playback_minimap = None
    state.playback_failed_nodes = set()
    return f"Playback of '{name}' stopped."


@mcp.tool()
async def play_recording(name: str, loop: bool = False) -> str:
    """Start playing back a saved cavebot recording.

    Args:
        name: Name of the recording to play.
        loop: Whether to loop the recording (default False).
    """
    return await _async_play_recording(name, loop)


@mcp.tool()
async def stop_playback() -> str:
    """Stop cavebot playback."""
    return await _async_stop_playback()


# ── Entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
