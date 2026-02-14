"""
DBVictory Bot — MCP Server

Exposes the bot's actions as MCP tools so Claude Code can control the
game character through natural language.

Transport: stdio  (stdout = JSON-RPC, all logging → stderr)
"""

import asyncio
import logging
import sys
import os

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
SERVER_HOST = "87.98.220.215"
LOGIN_PORT = 7171
GAME_PORT = 7172

# ── Global state ────────────────────────────────────────────────────

class BotState:
    """Holds proxy references and connection status."""

    def __init__(self):
        self.login_proxy: OTProxy | None = None
        self.game_proxy: OTProxy | None = None
        self.ready: bool = False
        self._auto_task: asyncio.Task | None = None
        self._proxy_tasks: list[asyncio.Task] = []

    @property
    def connected(self) -> bool:
        return self.ready and self.game_proxy is not None


state = BotState()

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
        return "Bot is already running and connected."

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
        return "ERROR: Could not patch any server IPs. Try running as Administrator."

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

    def on_client_packet(opcode, reader):
        try:
            name = ClientOpcode(opcode).name
        except ValueError:
            name = "?"
        log.debug(f"[C->S] 0x{opcode:02X} ({name})")

    def on_login_success(keys):
        state.ready = True
        log.info("=== BOT READY — game session established ===")

    state.game_proxy.on_server_packet = on_server_packet
    state.game_proxy.on_client_packet = on_client_packet
    state.game_proxy.on_login_success = on_login_success

    # ── Launch proxies as background tasks ──────────────────────────
    state._proxy_tasks = [
        asyncio.create_task(state.login_proxy.start()),
        asyncio.create_task(state.game_proxy.start()),
    ]

    log.info("Proxies started — waiting for player to log in via the game client...")

    # Wait up to 120 s for the player to log in
    for _ in range(240):
        if state.ready:
            return (
                f"Bot started successfully. Patched {patched} IP(s). "
                "Game session is active — you can now send commands."
            )
        await asyncio.sleep(0.5)

    return (
        f"Proxies are running (patched {patched} IP(s)) but no login detected yet. "
        "Log in through the game client.  Use get_status to check later."
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
    if state.game_proxy is None:
        return "Bot has not been started yet. Call start_bot first."

    auto = "running" if (state._auto_task and not state._auto_task.done()) else "idle"
    return (
        f"Connected: {state.ready}\n"
        f"Packets from server: {state.game_proxy.packets_from_server}\n"
        f"Packets from client: {state.game_proxy.packets_from_client}\n"
        f"Auto-walk: {auto}"
    )


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
