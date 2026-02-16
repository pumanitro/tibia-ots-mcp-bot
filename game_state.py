"""
Game state tracking — parses server packets into a live data model.

GameState holds HP, mana, position, creatures, and messages.
Called from the proxy callbacks on every server packet.
"""

import logging
import struct
import time
from collections import deque

log = logging.getLogger("game_state")


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
    For messages we can't consume (map data etc.), we stop and
    then do a targeted search for important opcodes we missed.
    """
    pos = 0
    found_stats = False
    has_map_data = False
    while pos < len(data):
        opcode = data[pos]
        pos += 1
        if opcode in (0x0A, 0x64, 0x65, 0x66, 0x67, 0x68):
            has_map_data = True
        try:
            new_pos = _parse_at(opcode, data, pos, gs)
        except Exception:
            break
        if new_pos < 0:
            break  # Unknown or variable-length message — stop
        if opcode == 0xA0:
            found_stats = True
        pos = new_pos

    # Fallback: search remaining data for PLAYER_STATS if not found yet
    if not found_stats and pos < len(data):
        _search_for_stats(data, pos, gs)

    # Only scan for creature markers in packets that contain map data
    # (prevents false positives from random bytes in non-map packets)
    if has_map_data:
        _scan_for_creatures(data, gs)

    # Prune creatures that are dead or weren't refreshed by recent map data.
    # When the player moves, map packets arrive and the scanner refreshes
    # visible creatures.  Any creature not refreshed within 15s of the last
    # map packet is off-screen.  When standing still, no pruning happens
    # (no new map data = no stale threshold to compare against).
    stale = [cid for cid, info in gs.creatures.items()
             if info.get("health", 0) == 0
             or (gs.last_map_time > 0
                 and info.get("t", 0) < gs.last_map_time - 15)]
    for cid in stale:
        del gs.creatures[cid]


def _scan_for_creatures(data: bytes, gs: GameState) -> None:
    """Scan raw packet data for 0x0061 (unknown creature) markers in map data.

    Only scans for 0x0061 which has strong validation (name + health).
    0x0062 (known creature) is skipped — too few bytes to validate reliably.
    """
    found = 0
    i = 0
    end = len(data) - 2
    while i < end:
        if data[i] != 0x61 or data[i + 1] != 0x00:
            i += 1
            continue

        # 0x0061 unknown creature: u32 remove_id, u32 creature_id,
        # u16 name_len, name, u8 health%
        if i + 2 + 4 + 4 + 2 > len(data):
            break
        p = i + 2
        remove_id = struct.unpack_from('<I', data, p)[0]
        p += 4
        creature_id = struct.unpack_from('<I', data, p)[0]
        p += 4
        # remove_id must be 0 (new) or a valid creature ID
        if remove_id != 0 and remove_id < 0x10000000:
            i += 1
            continue
        if creature_id < 0x10000000:
            i += 1
            continue
        if p + 2 > len(data):
            i += 1
            continue
        name_len = struct.unpack_from('<H', data, p)[0]
        p += 2
        if name_len < 1 or name_len > 30 or p + name_len + 1 > len(data):
            i += 1
            continue
        name_bytes = data[p:p + name_len]
        # Name must start with A-Z and contain only letters/spaces/apostrophes
        if name_bytes[0] < 65 or name_bytes[0] > 90:  # not A-Z
            i += 1
            continue
        if not all(b == 32 or b == 39 or (65 <= b <= 90) or (97 <= b <= 122) for b in name_bytes):
            i += 1
            continue
        name = name_bytes.decode('latin-1')
        p += name_len
        health = data[p]
        if health > 100:
            i += 1
            continue
        gs.creatures[creature_id] = {
            "health": health, "t": time.time(), "name": name,
            "px": gs.position[0], "py": gs.position[1],
        }
        found += 1
        i = p + 1

    if found:
        log.info(f"SCAN: {found} creature(s) in {len(data)}B, total={len(gs.creatures)}")


def _search_for_stats(data: bytes, start: int, gs: GameState) -> None:
    """Brute-force search for 0xA0 PLAYER_STATS after sequential scan stopped."""
    STATS_SIZE = 36  # u32 format
    for i in range(start, len(data) - STATS_SIZE):
        if data[i] != 0xA0:
            continue
        p = i + 1
        try:
            hp = struct.unpack_from('<I', data, p)[0]
            max_hp = struct.unpack_from('<I', data, p + 4)[0]
            level = struct.unpack_from('<H', data, p + 20)[0]
        except (struct.error, IndexError):
            continue
        # Tight sanity check to avoid false positives in map data
        if max_hp == 0 or max_hp > 50000 or hp > max_hp:
            continue
        if level == 0 or level > 5000:
            continue
        # Looks valid — parse fully
        _parse_at(0xA0, data, p, gs)
        log.info(f"STATS found via fallback search at offset {i}")
        return


def _parse_at(opcode: int, data: bytes, pos: int, gs: GameState) -> int:
    """Parse one message at `pos` (after opcode byte).

    Returns new position after consuming the message, or -1 if we can't
    consume (unknown opcode or variable-length map data).
    """

    # PLAYER_STATS 0xA0 — 36 bytes (u32 format confirmed from raw dump)
    # u32 hp, u32 max_hp, u32 capacity, u64 exp, u16 level, u8 lvl%,
    # u32 mana, u32 max_mana, u8 mlvl, u8 mlvl%, u8 soul, u16 stamina
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
        # pos+34: u16 stamina
        log.info(
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
        entry = gs.creatures.setdefault(cid, {})
        entry["health"] = health
        entry["t"] = time.time()
        entry["px"] = gs.position[0]
        entry["py"] = gs.position[1]
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

    # LOGIN_OR_PENDING 0x0A — first u32 is the player's creature ID
    if opcode == 0x0A:
        if pos + 4 > len(data):
            return -1
        gs.player_id = struct.unpack_from('<I', data, pos)[0]
        log.info(f"LOGIN: player_id={gs.player_id}")
        return -1  # Can't skip the rest (map data follows)

    # MAP_DESCRIPTION 0x64 — read position then stop (can't skip tile data)
    if opcode == 0x64:
        if pos + 5 > len(data):
            return -1
        x = struct.unpack_from('<H', data, pos)[0]
        y = struct.unpack_from('<H', data, pos + 2)[0]
        z = data[pos + 4]
        gs.position = (x, y, z)
        gs.creatures.clear()  # Clear stale creatures on map change
        gs.last_map_time = time.time()
        log.info(f"MAP_DESCRIPTION: pos=({x}, {y}, {z}) — creatures cleared")
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
        gs.last_map_time = time.time()
        return -1  # Can't skip tile data

    # ── Fixed-size opcodes we can safely skip ──────────────────────

    # MAGIC_EFFECT 0x83 — 6 bytes: pos(5) + u8 effect
    if opcode == 0x83:
        return pos + 6 if pos + 6 <= len(data) else -1

    # SHOOT_EFFECT 0x85 — 11 bytes: from_pos(5) + to_pos(5) + u8 effect
    if opcode == 0x85:
        return pos + 11 if pos + 11 <= len(data) else -1

    # CREATURE_LIGHT 0x8D — 6 bytes: u32 creature_id + u8 level + u8 color
    if opcode == 0x8D:
        return pos + 6 if pos + 6 <= len(data) else -1

    # CREATURE_SPEED 0x8F — 6 bytes: u32 creature_id + u16 speed
    if opcode == 0x8F:
        return pos + 6 if pos + 6 <= len(data) else -1

    # CREATURE_SKULL 0x90 — 5 bytes: u32 creature_id + u8 skull
    if opcode == 0x90:
        return pos + 5 if pos + 5 <= len(data) else -1

    # CREATURE_PARTY 0x91 — 5 bytes: u32 creature_id + u8 shield
    if opcode == 0x91:
        return pos + 5 if pos + 5 <= len(data) else -1

    # PLAYER_ICONS 0xA2 — 2 bytes: u16 icons
    if opcode == 0xA2:
        return pos + 2 if pos + 2 <= len(data) else -1

    # PLAYER_CANCEL_WALK 0xB5 — 1 byte: u8 direction
    if opcode == 0xB5:
        return pos + 1 if pos + 1 <= len(data) else -1

    # PING 0x1D — 0 bytes
    if opcode == 0x1D:
        return pos

    # Unknown opcode — stop
    return -1


def _parse(opcode: int, reader, gs: GameState) -> None:
    """Legacy single-opcode parser (used by first-opcode callback)."""
    if opcode == 0x0A:
        gs.player_id = reader.read_u32()
        log.info(f"LOGIN: player_id={gs.player_id}")
    elif opcode == 0x8C:
        creature_id = reader.read_u32()
        health = reader.read_u8()
        entry = gs.creatures.setdefault(creature_id, {})
        entry["health"] = health
        entry["t"] = time.time()
        entry["px"] = gs.position[0]
        entry["py"] = gs.position[1]
    elif opcode == 0xA0:
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
    elif opcode == 0xB4:
        msg_type = reader.read_u8()
        text = reader.read_string()
        gs.messages.append({"type": msg_type, "text": text})
        log.info(f"TEXT_MESSAGE(type={msg_type}): {text}")
