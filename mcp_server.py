"""
DBVictory Bot — MCP Server

Exposes the bot's actions as MCP tools so Claude Code can control the
game character through natural language.

Transport: stdio  (stdout = JSON-RPC, all logging → stderr)
"""

import asyncio
import importlib
import importlib.util
import json
import logging
import subprocess
import sys
import os
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

# ── Constants ───────────────────────────────────────────────────────
SERVER_HOST = os.environ.get("DBV_SERVER_HOST", "87.98.220.215")
LOGIN_PORT = 7171
GAME_PORT = 7172
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

class BotState:
    """Holds proxy references and connection status."""

    def __init__(self):
        self.login_proxy: OTProxy | None = None
        self.game_proxy: OTProxy | None = None
        self.ready: bool = False
        self._auto_task: asyncio.Task | None = None
        self._proxy_tasks: list[asyncio.Task] = []
        self.settings: dict = load_settings()
        self._action_tasks: dict[str, asyncio.Task] = {}
        self.game_state: GameState = GameState()

    @property
    def connected(self) -> bool:
        return self.ready and self.game_proxy is not None


state = BotState()


# ── Bot context (passed to action scripts as `bot`) ────────────────

class BotContext:
    """API surface available to action scripts via the `bot` parameter."""

    def __init__(self):
        self._log = logging.getLogger("action")

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

    async def _wrapper():
        try:
            log.info(f"[action:{name}] Started")
            await mod.run(bot_ctx)
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
            f'tasklist /FI "IMAGENAME eq {name}" /NH',
            capture_output=True, text=True, shell=True, timeout=5,
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
        lines.append(f"[OK] Proxy: connected (server={svr} client={cli} packets)")
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
    """Fire-and-forget launch of the Electron dashboard. Only launches once."""
    global _dashboard_launched
    if _dashboard_launched:
        return
    import shutil
    dashboard_dir = Path(__file__).parent / "dashboard"
    if not (dashboard_dir / "package.json").exists():
        log.warning("Dashboard not found — skipping launch.")
        return

    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm is None:
        log.warning("npm not found on PATH — cannot launch dashboard.")
        return

    # Kill anything already on the dashboard port
    _kill_port(DASHBOARD_PORT)

    try:
        # Start Next.js dev server
        subprocess.Popen(
            f'"{npm}" run dev',
            cwd=str(dashboard_dir),
            shell=True,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        log.info(f"Next.js dev server launching on port {DASHBOARD_PORT}")
        # Start Electron (waits for Next.js via wait-on)
        subprocess.Popen(
            f'"{npm}" run electron:only',
            cwd=str(dashboard_dir),
            shell=True,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        _dashboard_launched = True
        log.info(f"Electron launching (waiting for port {DASHBOARD_PORT}).")
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

    # 2. Cancel proxy tasks
    for task in state._proxy_tasks:
        if not task.done():
            task.cancel()
    state._proxy_tasks.clear()

    # 3. Close proxy connections
    for proxy in (state.game_proxy, state.login_proxy):
        if proxy:
            try:
                if proxy.client_writer:
                    proxy.client_writer.close()
                if proxy.server_writer:
                    proxy.server_writer.close()
            except Exception:
                pass

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

    # Re-import after reload
    from game_state import GameState, scan_packet as _sp
    globals()['scan_packet'] = _sp
    globals()['parse_server_packet'] = __import__('game_state').parse_server_packet

    # 6. Reset global state
    state.login_proxy = None
    state.game_proxy = None
    state.ready = False
    state._auto_task = None
    state._action_tasks.clear()
    state.game_state = GameState()
    state.settings = load_settings()

    log.info("Bot reset complete.")
    return "Bot reset. Call start_bot to reconnect."


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

    log.info(f"Attached to client (PID: {pm.process_id})")

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
    def on_server_packet(opcode, reader):
        try:
            name = ServerOpcode(opcode).name
        except ValueError:
            name = "?"
        log.debug(f"[S->C] 0x{opcode:02X} ({name})")
        parse_server_packet(opcode, reader, state.game_state)

    def on_client_packet(opcode, reader):
        try:
            name = ClientOpcode(opcode).name
        except ValueError:
            name = "?"
        log.debug(f"[C->S] 0x{opcode:02X} ({name})")
        # Log USE_ITEM details so we can discover item IDs
        if opcode == ClientOpcode.USE_ITEM:
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

    def on_raw_server_data(data):
        scan_packet(data, state.game_state)

    state.game_proxy.on_server_packet = on_server_packet
    state.game_proxy.on_client_packet = on_client_packet
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

    # Wait up to 120 s for the player to log in
    for _ in range(240):
        if state.ready:
            actions_started = _start_all_enabled_actions()
            actions_msg = f" {actions_started} automated action(s) started." if actions_started else ""

            status = _build_status_report()
            return (
                f"Bot started successfully. Patched {patched} IP(s). "
                f"Game session is active.{actions_msg}\n\n"
                f"=== Component Status ===\n{status}"
            )
        await asyncio.sleep(0.5)

    status = _build_status_report()
    return (
        f"Proxies are running (patched {patched} IP(s)) but no login detected yet. "
        f"Log in through the game client.\n\n"
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


# ── Entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
