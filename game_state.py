"""
Game state tracking — parses server packets into a live data model.

GameState holds HP, mana, position, creatures, and messages.
Called from the proxy callbacks on every server packet.
"""

import logging
import struct
import time
from collections import deque

from protocol import ServerOpcode

log = logging.getLogger("game_state")

MAX_CREATURE_AGE = 120  # seconds — prune non-DLL creatures older than this
PRUNE_INTERVAL = 1.0    # seconds — minimum time between creature prune passes

# Sanity-check limits for brute-force stats search
MAX_VALID_HP = 50000
MAX_VALID_LEVEL = 5000
MAX_VALID_MANA = 50000
MAX_VALID_CAPACITY = 100000


class GameState:
    """Plain data holder for parsed game state."""

    def __init__(self):
        # Player identity
        self.player_id: int = 0  # creature ID of the player (from 0x0A)

        # Player stats
        self.hp: int = 0
        self.max_hp: int = 0
        self.mana: int = 0
        self.max_mana: int = 0
        self.level: int = 0
        self.experience: int = 0
        self.capacity: int = 0
        self.magic_level: int = 0
        self.soul: int = 0

        # Position (x, y, z)
        self.position: tuple[int, int, int] = (0, 0, 0)

        # Creatures: {id: {"health": 0-100}}
        self.creatures: dict[int, dict] = {}

        # Chat messages ring buffer
        self.messages: deque = deque(maxlen=50)

        # Timestamp of last map data packet (for creature pruning)
        self.last_map_time: float = 0
        self._last_prune_time: float = 0

        # When True, MAP_SLICE position updates are skipped (DLL provides position)
        self.dll_position_active: bool = False


def parse_server_packet(opcode: int, reader, gs: GameState) -> None:
    """Parse the first opcode (called by the old single-opcode callback)."""
    try:
        _parse(opcode, reader, gs)
    except Exception as e:
        log.debug(f"parse_server_packet error opcode 0x{opcode:02X}: {e}")


def scan_packet(data: bytes, gs: GameState) -> None:
    """Scan full decrypted packet for ALL known opcodes.

    OT packets bundle multiple messages. We iterate sequentially:
    for messages we fully consume, we advance and keep going.
    For messages we can't consume (map data etc.), we stop and
    then do a targeted search for important opcodes we missed.
    """
    pos = 0
    found_stats = False
    has_map_data = False
    while pos < len(data):
        opcode = data[pos]
        pos += 1
        if opcode in (ServerOpcode.LOGIN_OR_PENDING, ServerOpcode.MAP_DESCRIPTION,
                      ServerOpcode.MAP_SLICE_NORTH, ServerOpcode.MAP_SLICE_EAST,
                      ServerOpcode.MAP_SLICE_SOUTH, ServerOpcode.MAP_SLICE_WEST):
            has_map_data = True
        try:
            new_pos = _parse_at(opcode, data, pos, gs)
        except Exception:
            break
        if new_pos < 0:
            break  # Unknown or variable-length message — stop
        if opcode == ServerOpcode.PLAYER_STATS:
            found_stats = True
        pos = new_pos

    # Fallback: search remaining data for PLAYER_STATS if not found yet
    if not found_stats and pos < len(data):
        _search_for_stats(data, pos, gs)

    # Creature tracking is handled entirely by DLL bridge — no packet scanning.

    # Prune stale non-DLL creatures (throttled to once per second)
    now = time.time()
    if now - gs._last_prune_time >= PRUNE_INTERVAL:
        gs._last_prune_time = now
        gs.creatures = {
            cid: info for cid, info in gs.creatures.items()
            if info.get("source") == "dll" or now - info.get("t", 0) <= MAX_CREATURE_AGE
        }



def _search_for_stats(data: bytes, start: int, gs: GameState) -> None:
    """Brute-force search for 0xA0 PLAYER_STATS after sequential scan stopped."""
    STATS_SIZE = 36  # u32 format
    for i in range(start, len(data) - STATS_SIZE):
        if data[i] != ServerOpcode.PLAYER_STATS:
            continue
        p = i + 1
        try:
            hp = struct.unpack_from('<I', data, p)[0]
            max_hp = struct.unpack_from('<I', data, p + 4)[0]
            level = struct.unpack_from('<H', data, p + 20)[0]
        except (struct.error, IndexError):
            continue
        # Tight sanity check to avoid false positives in map data
        if max_hp == 0 or max_hp > MAX_VALID_HP or hp > max_hp:
            continue
        if level == 0 or level > MAX_VALID_LEVEL:
            continue
        # Additional mana/capacity checks
        try:
            mana = struct.unpack_from('<I', data, p + 23)[0]
            max_mana = struct.unpack_from('<I', data, p + 27)[0]
            capacity = struct.unpack_from('<I', data, p + 8)[0]
        except (struct.error, IndexError):
            continue
        if max_mana > MAX_VALID_MANA or mana > max_mana:
            continue
        if capacity == 0 or capacity > MAX_VALID_CAPACITY:
            continue
        # Looks valid — parse fully
        _parse_at(ServerOpcode.PLAYER_STATS, data, p, gs)
        log.info(f"STATS found via fallback search at offset {i}")
        return


def _parse_at(opcode: int, data: bytes, pos: int, gs: GameState) -> int:
    """Parse one message at `pos` (after opcode byte).

    Returns new position after consuming the message, or -1 if we can't
    consume (unknown opcode or variable-length map data).
    """

    # PLAYER_STATS — 36 bytes (u32 format confirmed from raw dump)
    # u32 hp, u32 max_hp, u32 capacity, u64 exp, u16 level, u8 lvl%,
    # u32 mana, u32 max_mana, u8 mlvl, u8 mlvl%, u8 soul, u16 stamina
    if opcode == ServerOpcode.PLAYER_STATS:
        needed = 36
        if pos + needed > len(data):
            return -1
        gs.hp = struct.unpack_from('<I', data, pos)[0]
        gs.max_hp = struct.unpack_from('<I', data, pos + 4)[0]
        gs.capacity = struct.unpack_from('<I', data, pos + 8)[0]
        gs.experience = struct.unpack_from('<Q', data, pos + 12)[0]
        gs.level = struct.unpack_from('<H', data, pos + 20)[0]
        # pos+22: u8 level%
        gs.mana = struct.unpack_from('<I', data, pos + 23)[0]
        gs.max_mana = struct.unpack_from('<I', data, pos + 27)[0]
        gs.magic_level = data[pos + 31]
        # pos+32: u8 mlvl%
        gs.soul = data[pos + 33]
        # pos+34: u16 stamina
        log.info(
            f"Stats: HP={gs.hp}/{gs.max_hp} MP={gs.mana}/{gs.max_mana} "
            f"Lv={gs.level} XP={gs.experience} ML={gs.magic_level}"
        )
        return pos + needed

    # CREATURE_HEALTH — 5 bytes: u32 + u8
    # Only update existing creatures — never create new entries (avoids phantoms)
    if opcode == ServerOpcode.CREATURE_HEALTH:
        if pos + 5 > len(data):
            return -1
        cid = struct.unpack_from('<I', data, pos)[0]
        health = data[pos + 4]
        if cid in gs.creatures:
            gs.creatures[cid]["health"] = health
            gs.creatures[cid]["t"] = time.time()
        return pos + 5

    # CREATURE_MOVE — 11 bytes: pos(5) + u8 + pos(5)
    if opcode == ServerOpcode.CREATURE_MOVE:
        if pos + 11 > len(data):
            return -1
        # Skip — we just consume the bytes
        return pos + 11

    # TEXT_MESSAGE — variable: u8 type + string(u16 len + chars)
    if opcode == ServerOpcode.TEXT_MESSAGE:
        if pos + 3 > len(data):
            return -1
        msg_type = data[pos]
        str_len = struct.unpack_from('<H', data, pos + 1)[0]
        end = pos + 3 + str_len
        if end > len(data):
            return -1
        text = data[pos + 3:end].decode('latin-1', errors='replace')
        gs.messages.append({"type": msg_type, "text": text})
        log.info(f"TEXT_MESSAGE(type={msg_type}): {text}")
        return end

    # LOGIN_OR_PENDING — u32 player_id, u16 draw_speed, u8 can_report_bugs
    # Then MAP_DESCRIPTION with position
    if opcode == ServerOpcode.LOGIN_OR_PENDING:
        if pos + 4 > len(data):
            return -1
        gs.player_id = struct.unpack_from('<I', data, pos)[0]
        log.info(f"LOGIN: player_id={gs.player_id}")
        pos += 4
        # Search for MAP_DESCRIPTION within next 10 bytes (skip draw_speed/flags)
        search_end = min(pos + 10, len(data) - 5)
        for i in range(pos, search_end):
            if data[i] == ServerOpcode.MAP_DESCRIPTION:
                x = struct.unpack_from('<H', data, i + 1)[0]
                y = struct.unpack_from('<H', data, i + 3)[0]
                z = data[i + 5]
                if 100 < x < 65000 and 100 < y < 65000 and z < 16:
                    gs.position = (x, y, z)
                    gs.creatures = {cid: info for cid, info in gs.creatures.items() if info.get("source") == "dll"}
                    gs.last_map_time = time.time()
                    log.info(f"LOGIN position: ({x}, {y}, {z})")
                    break
        return -1  # Can't skip the rest (tile data follows)

    # MAP_DESCRIPTION — read position then stop (can't skip tile data)
    if opcode == ServerOpcode.MAP_DESCRIPTION:
        if pos + 5 > len(data):
            return -1
        x = struct.unpack_from('<H', data, pos)[0]
        y = struct.unpack_from('<H', data, pos + 2)[0]
        z = data[pos + 4]
        gs.position = (x, y, z)
        gs.creatures = {cid: info for cid, info in gs.creatures.items() if info.get("source") == "dll"}
        gs.last_map_time = time.time()
        log.info(f"MAP_DESCRIPTION: pos=({x}, {y}, {z}) — creatures cleared")
        return -1  # Can't skip tile data

    # MAP_SLICE — update position, but can't skip tile data
    if opcode in (ServerOpcode.MAP_SLICE_NORTH, ServerOpcode.MAP_SLICE_EAST,
                  ServerOpcode.MAP_SLICE_SOUTH, ServerOpcode.MAP_SLICE_WEST):
        if not gs.dll_position_active:
            x, y, z = gs.position
            if opcode == ServerOpcode.MAP_SLICE_NORTH:
                gs.position = (x, y - 1, z)
            elif opcode == ServerOpcode.MAP_SLICE_EAST:
                gs.position = (x + 1, y, z)
            elif opcode == ServerOpcode.MAP_SLICE_SOUTH:
                gs.position = (x, y + 1, z)
            elif opcode == ServerOpcode.MAP_SLICE_WEST:
                gs.position = (x - 1, y, z)
        gs.last_map_time = time.time()
        return -1  # Can't skip tile data

    # ── Fixed-size opcodes we can safely skip ──────────────────────

    # MAGIC_EFFECT — 6 bytes: pos(5) + u8 effect
    if opcode == ServerOpcode.MAGIC_EFFECT:
        return pos + 6 if pos + 6 <= len(data) else -1

    # SHOOT_EFFECT — 11 bytes: from_pos(5) + to_pos(5) + u8 effect
    if opcode == ServerOpcode.SHOOT_EFFECT:
        return pos + 11 if pos + 11 <= len(data) else -1

    # CREATURE_LIGHT — 6 bytes: u32 creature_id + u8 level + u8 color
    if opcode == ServerOpcode.CREATURE_LIGHT:
        return pos + 6 if pos + 6 <= len(data) else -1

    # CREATURE_SPEED — 6 bytes: u32 creature_id + u16 speed
    if opcode == ServerOpcode.CREATURE_SPEED:
        return pos + 6 if pos + 6 <= len(data) else -1

    # CREATURE_SKULL — 5 bytes: u32 creature_id + u8 skull
    if opcode == ServerOpcode.CREATURE_SKULL:
        return pos + 5 if pos + 5 <= len(data) else -1

    # CREATURE_PARTY — 5 bytes: u32 creature_id + u8 shield
    if opcode == ServerOpcode.CREATURE_PARTY:
        return pos + 5 if pos + 5 <= len(data) else -1

    # PLAYER_ICONS — 2 bytes: u16 icons
    if opcode == ServerOpcode.PLAYER_ICONS:
        return pos + 2 if pos + 2 <= len(data) else -1

    # PLAYER_CANCEL_WALK — 1 byte: u8 direction
    if opcode == ServerOpcode.PLAYER_CANCEL_WALK:
        return pos + 1 if pos + 1 <= len(data) else -1

    # PING — 0 bytes
    if opcode == ServerOpcode.PING:
        return pos

    # PLAYER_CANCEL_ATTACK — 0 bytes
    if opcode == ServerOpcode.PLAYER_CANCEL_ATTACK:
        return pos

    # Unknown opcode — stop
    return -1


def _parse(opcode: int, reader, gs: GameState) -> None:
    """Legacy single-opcode parser (used by first-opcode callback)."""
    if opcode == ServerOpcode.LOGIN_OR_PENDING:
        gs.player_id = reader.read_u32()
        log.info(f"LOGIN: player_id={gs.player_id}")
    elif opcode == ServerOpcode.CREATURE_HEALTH:
        creature_id = reader.read_u32()
        health = reader.read_u8()
        if creature_id in gs.creatures:
            gs.creatures[creature_id]["health"] = health
            gs.creatures[creature_id]["t"] = time.time()
    elif opcode == ServerOpcode.PLAYER_STATS:
        gs.hp = reader.read_u32()
        gs.max_hp = reader.read_u32()
        gs.capacity = reader.read_u32()
        gs.experience = struct.unpack('<Q', reader.read_bytes(8))[0]
        gs.level = reader.read_u16()
        reader.read_u8()  # level %
        gs.mana = reader.read_u32()
        gs.max_mana = reader.read_u32()
        gs.magic_level = reader.read_u8()
        reader.read_u8()  # mlvl %
        gs.soul = reader.read_u8()
        # skip stamina (u16)
        log.info(
            f"Stats: HP={gs.hp}/{gs.max_hp} MP={gs.mana}/{gs.max_mana} "
            f"Lv={gs.level} XP={gs.experience}"
        )
    elif opcode == ServerOpcode.TEXT_MESSAGE:
        msg_type = reader.read_u8()
        text = reader.read_string()
        gs.messages.append({"type": msg_type, "text": text})
        log.info(f"TEXT_MESSAGE(type={msg_type}): {text}")
