"""
Game state tracking — parses server packets into a live data model.

GameState holds HP, mana, position, creatures, and messages.
Called from the proxy callbacks on every server packet.
"""

import logging
import struct
from collections import deque

log = logging.getLogger("game_state")


class GameState:
    """Plain data holder for parsed game state."""

    def __init__(self):
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

        # Creatures: {id: {"health": 0-100, "position": (x,y,z)}}
        self.creatures: dict[int, dict] = {}

        # Chat messages ring buffer
        self.messages: deque = deque(maxlen=50)


def parse_server_packet(opcode: int, reader, gs: GameState) -> None:
    """Parse the first opcode (called by the old single-opcode callback)."""
    try:
        _parse(opcode, reader, gs)
    except Exception:
        pass


def scan_packet(data: bytes, gs: GameState) -> None:
    """Scan full decrypted packet for ALL known opcodes.

    OT packets bundle multiple messages. We iterate sequentially:
    for messages we fully consume, we advance and keep going.
    For messages we can't consume (map data etc.), we stop.
    """
    pos = 0
    while pos < len(data):
        opcode = data[pos]
        pos += 1
        try:
            new_pos = _parse_at(opcode, data, pos, gs)
        except Exception:
            break
        if new_pos < 0:
            break  # Unknown or variable-length message — stop
        pos = new_pos


def _parse_at(opcode: int, data: bytes, pos: int, gs: GameState) -> int:
    """Parse one message at `pos` (after opcode byte).

    Returns new position after consuming the message, or -1 if we can't
    consume (unknown opcode or variable-length map data).
    """

    # PLAYER_STATS 0xA0 — DBV custom format (36 bytes)
    # u32 hp, u32 max_hp, u32 cap_oz, u64 exp, u16 level, u8 lvl%,
    # u32 mana, u32 max_mana, u8 mlvl, u8 mlvl%, u8 soul
    if opcode == 0xA0:
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
        # pos+34: u16 stamina (minutes)
        log.debug(
            f"Stats: HP={gs.hp}/{gs.max_hp} MP={gs.mana}/{gs.max_mana} "
            f"Lv={gs.level} XP={gs.experience} ML={gs.magic_level}"
        )
        return pos + needed

    # CREATURE_HEALTH 0x8C — 5 bytes: u32 + u8
    if opcode == 0x8C:
        if pos + 5 > len(data):
            return -1
        cid = struct.unpack_from('<I', data, pos)[0]
        health = data[pos + 4]
        gs.creatures.setdefault(cid, {})["health"] = health
        return pos + 5

    # CREATURE_MOVE 0x6D — 11 bytes: pos(5) + u8 + pos(5)
    if opcode == 0x6D:
        if pos + 11 > len(data):
            return -1
        # Skip — we just consume the bytes
        return pos + 11

    # TEXT_MESSAGE 0xB4 — variable: u8 type + string(u16 len + chars)
    if opcode == 0xB4:
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

    # MAP_DESCRIPTION 0x64 — read position then stop (can't skip tile data)
    if opcode == 0x64:
        if pos + 5 > len(data):
            return -1
        x = struct.unpack_from('<H', data, pos)[0]
        y = struct.unpack_from('<H', data, pos + 2)[0]
        z = data[pos + 4]
        gs.position = (x, y, z)
        log.info(f"MAP_DESCRIPTION: pos=({x}, {y}, {z})")
        return -1  # Can't skip tile data

    # MAP_SLICE 0x65-0x68 — update position, but can't skip tile data
    if opcode in (0x65, 0x66, 0x67, 0x68):
        x, y, z = gs.position
        if opcode == 0x65:
            gs.position = (x, y - 1, z)
        elif opcode == 0x66:
            gs.position = (x + 1, y, z)
        elif opcode == 0x67:
            gs.position = (x, y + 1, z)
        elif opcode == 0x68:
            gs.position = (x - 1, y, z)
        return -1  # Can't skip tile data

    # Unknown opcode — stop
    return -1


def _parse(opcode: int, reader, gs: GameState) -> None:
    """Legacy single-opcode parser (used by first-opcode callback)."""
    # Only handle simple fixed-size messages here
    if opcode == 0x8C:
        creature_id = reader.read_u32()
        health = reader.read_u8()
        gs.creatures.setdefault(creature_id, {})["health"] = health
    elif opcode == 0xB4:
        msg_type = reader.read_u8()
        text = reader.read_string()
        gs.messages.append({"type": msg_type, "text": text})
        log.info(f"TEXT_MESSAGE(type={msg_type}): {text}")
