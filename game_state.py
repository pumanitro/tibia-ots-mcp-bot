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

    # Prune dead creatures always; prune stale only when we have fresh map data.
    now = time.time()
    stale = [cid for cid, info in gs.creatures.items()
             if info.get("source") != "dll"
             and (info.get("health", 0) == 0
                  or (has_map_data and now - info.get("t", 0) > 3))]
    for cid in stale:
        del gs.creatures[cid]

    # DEBUG: log creature state on map packets
    if has_map_data and gs.creatures:
        ages = {info.get("name", "?"): f"{now - info.get('t', 0):.1f}s"
                for info in gs.creatures.values()}
        log.info(f"CREATURES after prune: kept={len(gs.creatures)} pruned={len(stale)} ages={ages}")


def _scan_for_creatures(data: bytes, gs: GameState) -> None:
    """Scan raw packet data for creature markers in map data.

    DEBUG MODE: dumps raw bytes after valid name matches to determine format.
    """
    found = 0
    debug_lines = []
    i = 0
    end = len(data) - 2
    while i < end:
        # ── 0x0061 unknown creature ────────────────────────────────
        if data[i] == 0x61 and data[i + 1] == 0x00:
            if i + 2 + 4 + 4 + 2 > len(data):
                break
            p = i + 2
            remove_id = struct.unpack_from('<I', data, p)[0]
            p += 4
            creature_id = struct.unpack_from('<I', data, p)[0]
            p += 4
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
            if name_len < 1 or name_len > 30 or p + name_len > len(data):
                i += 1
                continue
            name_bytes = data[p:p + name_len]
            if name_bytes[0] < 65 or name_bytes[0] > 90:
                i += 1
                continue
            if not all(b == 32 or b == 39 or (65 <= b <= 90) or (97 <= b <= 122) for b in name_bytes):
                i += 1
                continue
            name = name_bytes.decode('latin-1')
            p += name_len

            # DEBUG: dump 30 bytes after name for format analysis
            trail = min(30, len(data) - p)
            raw = data[p:p + trail]
            hex_dump = ' '.join(f'{b:02x}' for b in raw)
            debug_lines.append(
                f"0x0061 @{i}: id={creature_id:#010x} rm={remove_id:#010x} "
                f"name='{name}' trail=[{hex_dump}]"
            )

            # Minimal validation: just health + direction
            if p + 2 > len(data):
                i += 1
                continue
            health = data[p]; p += 1
            direction = data[p]; p += 1
            if health > 100 or direction > 3:
                i += 1
                continue

            # Don't overwrite DLL-sourced entries (they have accurate position data)
            if creature_id in gs.creatures and gs.creatures[creature_id].get("source") == "dll":
                gs.creatures[creature_id]["health"] = health
                gs.creatures[creature_id]["t"] = time.time()
            else:
                gs.creatures[creature_id] = {
                    "health": health, "t": time.time(), "name": name,
                    "z": gs.position[2],
                }
            found += 1
            # Skip past the outfit+trailing bytes (we'll figure out exact size from dump)
            i = p + 10  # rough skip to avoid re-scanning same region
            continue

        i += 1

    if debug_lines:
        with open("creature_format_dump.txt", "a") as f:
            f.write(f"=== {time.strftime('%H:%M:%S')} pktlen={len(data)} ===\n")
            for line in debug_lines:
                f.write(line + "\n")
            f.write("\n")

    if found:
        log.info(f"SCAN: {found} creature(s) in {len(data)}B, total={len(gs.creatures)}")

    # DEBUG: also save first large map packet for offline analysis
    if len(data) > 2000 and not hasattr(gs, '_dumped_map'):
        gs._dumped_map = True
        with open("map_packet_dump.bin", "wb") as f:
            f.write(data)


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
    # Only update existing creatures — never create new entries (avoids phantoms)
    if opcode == 0x8C:
        if pos + 5 > len(data):
            return -1
        cid = struct.unpack_from('<I', data, pos)[0]
        health = data[pos + 4]
        if cid in gs.creatures:
            gs.creatures[cid]["health"] = health
            gs.creatures[cid]["t"] = time.time()
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

    # LOGIN_OR_PENDING 0x0A — u32 player_id, u16 draw_speed, u8 can_report_bugs
    # Then 0x64 MAP_DESCRIPTION with position
    if opcode == 0x0A:
        if pos + 4 > len(data):
            return -1
        gs.player_id = struct.unpack_from('<I', data, pos)[0]
        log.info(f"LOGIN: player_id={gs.player_id}")
        pos += 4
        # Search for 0x64 within next 10 bytes (skip draw_speed/flags)
        search_end = min(pos + 10, len(data) - 5)
        for i in range(pos, search_end):
            if data[i] == 0x64:
                x = struct.unpack_from('<H', data, i + 1)[0]
                y = struct.unpack_from('<H', data, i + 3)[0]
                z = data[i + 5]
                if 100 < x < 65000 and 100 < y < 65000 and z < 16:
                    gs.position = (x, y, z)
                    gs.creatures.clear()
                    gs.last_map_time = time.time()
                    log.info(f"LOGIN position: ({x}, {y}, {z})")
                    break
        return -1  # Can't skip the rest (tile data follows)

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
        if creature_id in gs.creatures:
            gs.creatures[creature_id]["health"] = health
            gs.creatures[creature_id]["t"] = time.time()
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
