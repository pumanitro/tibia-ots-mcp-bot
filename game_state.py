"""
Game state tracking — parses server packets into a live data model.

GameState holds HP, mana, position, creatures, and messages.
Called from the proxy callbacks on every server packet.
"""

import logging
import struct
import sys
import time
from collections import deque

from constants import PACKET_FORMATS as PF
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
        self.speed: int = 0

        # Player condition icons bitmask (from 0xA2 PLAYER_ICONS)
        self.player_icons: int = 0

        # World light (from 0x82 WORLD_LIGHT)
        self.world_light_level: int = 0
        self.world_light_color: int = 0

        # Current attack target (creature ID from client ATTACK opcode, 0 = none)
        self.attack_target_id: int = 0

        # Lure mode flag — when True, auto_targeting suppresses attacks
        self.lure_active: bool = False

        # Timestamp of last "You can't throw there" server message
        self.last_cant_throw: float = 0

        # Unreachable creatures blacklist: {creature_id: expiry_timestamp}
        # Populated by cavebot when no-damage timeout fires.
        # auto_targeting and _count_nearby_monsters skip these.
        self.unreachable_creatures: dict[int, float] = {}

        # Position (x, y, z)
        self.position: tuple[int, int, int] = (0, 0, 0)

        # Creatures: {id: {"health": 0-100}}
        self.creatures: dict[int, dict] = {}

        # Chat messages ring buffer
        self.messages: deque = deque(maxlen=50)

        # Timestamp of last map data packet (for creature pruning)
        self.last_map_time: float = 0
        self._last_prune_time: float = 0

        # Timestamp of last PLAYER_STATS update (for debugging HP freshness)
        self.stats_updated_at: float = 0

        # When True, MAP_SLICE position updates are skipped (DLL provides position)
        self.dll_position_active: bool = False

        # Packet-derived position — always updated by MAP_SLICE, never by DLL.
        # Used by cavebot recording for accurate per-step position tracking.
        self.packet_position: tuple[int, int, int] = (0, 0, 0)

        # Tile updates ring buffer — (timestamp, x, y, z) for use_item verification
        self.tile_updates: deque = deque(maxlen=50)

        # Server events ring buffer — (timestamp, event_type, data_dict)
        self.server_events: deque = deque(maxlen=100)
        # Timestamp of last CANCEL_WALK from server
        self.cancel_walk_time: float = 0
        # Last client walk delta for cancel_walk revert
        self._last_walk_delta: tuple[int, int] = (0, 0)

        # Protection Zone detection — updated by server text messages
        self.in_protection_zone: bool = False

        # Session kill counter (set by cavebot/mcp_server, read by dashboard)
        self.session_kills: int = 0

        # Kill telemetry — kill events with metadata for route analysis
        # Capped to prevent unbounded memory growth during long sessions
        self.kill_log: deque = deque(maxlen=5000)
        self._prev_experience: int = 0
        # Dedup set for CREATURE_HEALTH=0 kill counting (avoid double-counting)
        self._recent_kills: set[int] = set()
        self._recent_kills_cleanup: float = 0



_stats_debug_file = None
_stats_debug_count = 0


def _dump_stats_debug(gs: GameState, raw_hex: str | None) -> None:
    """Write PLAYER_STATS values to stats_debug.txt for HP/Mana diagnosis."""
    global _stats_debug_file, _stats_debug_count
    _stats_debug_count += 1
    if _stats_debug_count > 500:
        return  # cap debug output to prevent unbounded file growth
    import os
    try:
        if _stats_debug_file is None:
            path = os.path.join(os.path.dirname(__file__), "stats_debug.txt")
            _stats_debug_file = open(path, "a", encoding="utf-8")
        ts = time.strftime("%H:%M:%S")
        hex_part = f" raw={raw_hex}" if raw_hex else ""
        _stats_debug_file.write(
            f"[{ts}] HP={gs.hp}/{gs.max_hp} MP={gs.mana}/{gs.max_mana} "
            f"Cap={gs.capacity} XP={gs.experience} Lv={gs.level} "
            f"ML={gs.magic_level} Soul={gs.soul}{hex_part}\n"
        )
        if _stats_debug_count % 5 == 0:
            _stats_debug_file.flush()
    except Exception:
        pass


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
    found_icons = False
    handled_map_slice = False
    while pos < len(data):
        opcode = data[pos]
        pos += 1
        try:
            new_pos = _parse_at(opcode, data, pos, gs)
        except Exception:
            break
        if new_pos < 0:
            # _parse_at returned -1 but may have updated position (MAP_SLICE/MAP_DESCRIPTION)
            if opcode in (ServerOpcode.MAP_SLICE_NORTH, ServerOpcode.MAP_SLICE_EAST,
                          ServerOpcode.MAP_SLICE_SOUTH, ServerOpcode.MAP_SLICE_WEST,
                          ServerOpcode.MAP_DESCRIPTION, ServerOpcode.LOGIN_OR_PENDING):
                handled_map_slice = True
                _map_slice_dbg(f"SEQ handled 0x{opcode:02X} at pos={pos-1} → pos={gs.position}")
            else:
                _map_slice_dbg(f"SEQ STOPPED at 0x{opcode:02X} pos={pos-1} pktlen={len(data)} "
                               f"next5={data[pos-1:pos+4].hex()}")
            break
        if opcode == ServerOpcode.PLAYER_STATS:
            found_stats = True
        if opcode == ServerOpcode.PLAYER_ICONS:
            found_icons = True
        pos = new_pos

    # Fallback disabled — brute-force byte search hits false positives in text
    # data (0x65='e', 0x67='g'). Position is now tracked via client walk packets.
    if not handled_map_slice:
        # Only log, don't try to fix from server data
        pass

    # Search full packet for tile update opcodes (0x6A/0x6B/0x6C)
    # Done on full data (not just remainder) because tile updates can appear
    # anywhere, including after map data that stopped sequential parsing.
    _search_for_tile_updates(data, 0, gs)

    # Fallback: search remaining data for PLAYER_STATS if not found yet
    if not found_stats and pos < len(data):
        _search_for_stats(data, pos, gs)

    # Fallback: search remaining data for PLAYER_ICONS if not found yet
    if not found_icons and pos < len(data):
        _search_for_icons(data, pos, gs)

    # Creature tracking is handled entirely by DLL bridge — no packet scanning.

    # Prune stale non-DLL creatures (throttled to once per second)
    now = time.time()
    if now - gs._last_prune_time >= PRUNE_INTERVAL:
        gs._last_prune_time = now
        gs.creatures = {
            cid: info for cid, info in gs.creatures.items()
            if info.get("source") == "dll" or now - info.get("t", 0) <= MAX_CREATURE_AGE
        }


_map_slice_dbg_f = None
_map_slice_dbg_count = 0


def _map_slice_dbg(msg: str) -> None:
    return  # disabled — was writing 29 MB+ to disk, causing I/O lag


def _search_for_map_slice(data: bytes, gs: GameState) -> None:
    """Search packet for MAP_SLICE opcodes (0x65-0x68) to update position.

    Scans the first 50 bytes of the packet for a MAP_SLICE opcode.
    Applies at most ONE direction per packet.
    """
    global _map_slice_dbg_count
    _N = ServerOpcode.MAP_SLICE_NORTH  # 0x65
    _E = ServerOpcode.MAP_SLICE_EAST   # 0x66
    _S = ServerOpcode.MAP_SLICE_SOUTH  # 0x67
    _W = ServerOpcode.MAP_SLICE_WEST   # 0x68
    if len(data) < 1:
        return

    # Debug: log first bytes of packets that weren't handled by sequential parser
    _map_slice_dbg_count += 1
    if _map_slice_dbg_count <= 200:
        hdr = data[:min(20, len(data))].hex()
        _map_slice_dbg(f"fallback pkt len={len(data)} first20={hdr} "
                       f"pos={gs.position} pkt_pos={gs.packet_position}")

    # Search first 50 bytes for a MAP_SLICE opcode
    scan_end = min(50, len(data))
    b = None
    for i in range(scan_end):
        if data[i] in (_N, _E, _S, _W):
            b = data[i]
            if _map_slice_dbg_count <= 200:
                _map_slice_dbg(f"  FOUND 0x{b:02X} at offset {i}")
            break

    if b is None:
        return

    # Update gs.position
    x, y, z = gs.position
    if b == _N:
        gs.position = (x, y - 1, z)
    elif b == _E:
        gs.position = (x + 1, y, z)
    elif b == _S:
        gs.position = (x, y + 1, z)
    elif b == _W:
        gs.position = (x - 1, y, z)
    # Update gs.packet_position
    if gs.packet_position[0] < 100 and gs.position[0] > 100:
        gs.packet_position = gs.position
    px, py, pz = gs.packet_position
    if b == _N:
        gs.packet_position = (px, py - 1, pz)
    elif b == _E:
        gs.packet_position = (px + 1, py, pz)
    elif b == _S:
        gs.packet_position = (px, py + 1, pz)
    elif b == _W:
        gs.packet_position = (px - 1, py, pz)
    gs.last_map_time = time.time()


def _search_for_tile_updates(data: bytes, start: int, gs: GameState) -> None:
    """Brute-force search for TILE_TRANSFORM_THING (0x6B) — door open/close detection.

    Extracts position (u16 x, u16 y, u8 z = 5 bytes after opcode) and appends
    to gs.tile_updates.  Also prunes entries older than 5 seconds.

    Only 0x6B is tracked; 0x6A (TILE_ADD_THING) and 0x6C (TILE_REMOVE_THING)
    are too noisy (map refresh, floor changes) and not needed for door detection.
    """
    now = time.time()

    # Prune old entries
    while gs.tile_updates and now - gs.tile_updates[0][0] > 5.0:
        gs.tile_updates.popleft()

    _tt = PF.get("tile_transform_thing", {})
    _tt_x = _tt.get("x", 1)
    _tt_y = _tt.get("y", 3)
    _tt_z = _tt.get("z", 5)
    for i in range(start, len(data) - 5):
        if data[i] != ServerOpcode.TILE_TRANSFORM_THING:  # 0x6B
            continue
        try:
            x = struct.unpack_from('<H', data, i + _tt_x)[0]
            y = struct.unpack_from('<H', data, i + _tt_y)[0]
            z = data[i + _tt_z]
        except (struct.error, IndexError):
            continue
        # Sanity-check: valid map coordinates
        if x < 100 or x > 65000 or y < 100 or y > 65000 or z > 15:
            continue
        gs.tile_updates.append((now, x, y, z))
        gs.server_events.append((now, "tile_transform_item", {"x": x, "y": y, "z": z}))


def _check_pz_message(text: str, gs: GameState) -> None:
    """Detect Protection Zone enter/leave from server text messages."""
    lower = text.lower()
    if "protection zone" in lower:
        if "enter" in lower or "inside" in lower or "cannot attack" in lower:
            if not gs.in_protection_zone:
                log.info("PZ detected: entered protection zone")
            gs.in_protection_zone = True
        elif "left" in lower or "leave" in lower:
            if gs.in_protection_zone:
                log.info("PZ detected: left protection zone")
            gs.in_protection_zone = False


def _search_for_stats(data: bytes, start: int, gs: GameState) -> None:
    """Brute-force search for 0xA0 PLAYER_STATS after sequential scan stopped."""
    _st = PF.get("player_stats", {})
    STATS_SIZE = _st.get("size", 36)
    _st_hp = _st.get("hp", 0)
    _st_max_hp = _st.get("max_hp", 4)
    _st_level = _st.get("level", 20)
    _st_mana = _st.get("mana", 23)
    _st_max_mana = _st.get("max_mana", 27)
    _st_capacity = _st.get("capacity", 8)
    for i in range(start, len(data) - STATS_SIZE):
        if data[i] != ServerOpcode.PLAYER_STATS:
            continue
        p = i + 1
        try:
            hp = struct.unpack_from('<I', data, p + _st_hp)[0]
            max_hp = struct.unpack_from('<I', data, p + _st_max_hp)[0]
            level = struct.unpack_from('<H', data, p + _st_level)[0]
        except (struct.error, IndexError):
            continue
        # Tight sanity check to avoid false positives in map data
        if max_hp == 0 or max_hp > MAX_VALID_HP or hp > max_hp:
            continue
        if level == 0 or level > MAX_VALID_LEVEL:
            continue
        # Additional mana/capacity checks
        try:
            mana = struct.unpack_from('<I', data, p + _st_mana)[0]
            max_mana = struct.unpack_from('<I', data, p + _st_max_mana)[0]
            capacity = struct.unpack_from('<I', data, p + _st_capacity)[0]
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


def _search_for_icons(data: bytes, start: int, gs: GameState) -> None:
    """Brute-force search for 0xA2 PLAYER_ICONS after sequential scan stopped.

    PLAYER_ICONS is 3 bytes total: opcode(1) + u16 icons bitmask(2).
    We validate that the icons value is a reasonable bitmask (< 0x8000).
    """
    _ic = PF.get("player_icons", {})
    _ic_size = _ic.get("size", 2)
    for i in range(start, len(data) - _ic_size):
        if data[i] != ServerOpcode.PLAYER_ICONS:
            continue
        icons = struct.unpack_from('<H', data, i + 1)[0]
        # Reasonable icons bitmask: typically small value
        if icons < 0x8000:
            old = gs.player_icons
            gs.player_icons = icons
            if icons != old:
                log.info(f"ICONS found via fallback at offset {i}: 0x{icons:04X} (was 0x{old:04X})")
            return


def _record_kill(gs: GameState, cid: int) -> None:
    """Record a monster kill event with position and playback context."""
    creature = gs.creatures.get(cid, {})
    # Playback context from BotState (if available)
    segment = None
    loop_count = 0
    try:
        main_state = sys.modules.get("__main__")
        if main_state:
            bot_state = getattr(main_state, "state", None)
            if bot_state and getattr(bot_state, "playback_active", False):
                segment = getattr(bot_state, "playback_index", None)
                loop_count = getattr(bot_state, "playback_loop_count", 0)
    except Exception:
        pass
    gs.kill_log.append({
        "t": time.time(),
        "name": creature.get("name", ""),
        "cid": cid,
        "x": creature.get("x", 0),
        "y": creature.get("y", 0),
        "z": creature.get("z", 0),
        "px": gs.position[0],
        "py": gs.position[1],
        "pz": gs.position[2],
        "segment": segment,
        "loop": loop_count,
    })
    gs.session_kills += 1



def _parse_at(opcode: int, data: bytes, pos: int, gs: GameState) -> int:
    """Parse one message at `pos` (after opcode byte).

    Returns new position after consuming the message, or -1 if we can't
    consume (unknown opcode or variable-length map data).
    """

    # PLAYER_STATS — 36 bytes (u32 format confirmed from raw dump)
    # u32 hp, u32 max_hp, u32 capacity, u64 exp, u16 level, u8 lvl%,
    # u32 mana, u32 max_mana, u8 mlvl, u8 mlvl%, u8 soul, u16 stamina
    if opcode == ServerOpcode.PLAYER_STATS:
        _st = PF.get("player_stats", {})
        needed = _st.get("size", 36)
        if pos + needed > len(data):
            return -1
        # Raw hex dump for HP/Mana diagnosis
        raw_hex = data[pos:pos + needed].hex()
        gs.hp = struct.unpack_from('<I', data, pos + _st.get("hp", 0))[0]
        gs.max_hp = struct.unpack_from('<I', data, pos + _st.get("max_hp", 4))[0]
        gs.capacity = struct.unpack_from('<I', data, pos + _st.get("capacity", 8))[0]
        gs.experience = struct.unpack_from('<Q', data, pos + _st.get("experience", 12))[0]
        gs.level = struct.unpack_from('<H', data, pos + _st.get("level", 20))[0]
        # level_percent at _st.get("level_percent", 22)
        gs.mana = struct.unpack_from('<I', data, pos + _st.get("mana", 23))[0]
        gs.max_mana = struct.unpack_from('<I', data, pos + _st.get("max_mana", 27))[0]
        gs.magic_level = data[pos + _st.get("magic_level", 31)]
        # magic_level_percent at _st.get("magic_level_percent", 32)
        gs.soul = data[pos + _st.get("soul", 33)]
        # stamina at _st.get("stamina", 34)
        gs.stats_updated_at = time.time()
        # XP delta attribution — attach to most recent kill (within 2s)
        if gs._prev_experience > 0:
            xp_delta = gs.experience - gs._prev_experience
            if xp_delta > 0 and gs.kill_log:
                last_kill = gs.kill_log[-1]
                if time.time() - last_kill["t"] < 2.0 and "xp" not in last_kill:
                    last_kill["xp"] = xp_delta
        gs._prev_experience = gs.experience
        log.info(
            f"Stats: HP={gs.hp}/{gs.max_hp} MP={gs.mana}/{gs.max_mana} "
            f"Lv={gs.level} XP={gs.experience} ML={gs.magic_level}"
        )
        _dump_stats_debug(gs, raw_hex)
        return pos + needed

    # CREATURE_HEALTH — 5 bytes: u32 + u8
    # Only update existing creatures — never create new entries (avoids phantoms)
    if opcode == ServerOpcode.CREATURE_HEALTH:
        _ch = PF.get("creature_health", {})
        _ch_size = _ch.get("size", 5)
        if pos + _ch_size > len(data):
            return -1
        cid = struct.unpack_from('<I', data, pos + _ch.get("creature_id", 0))[0]
        health = data[pos + _ch.get("health", 4)]
        if cid in gs.creatures:
            old_health = gs.creatures[cid].get("health", -1)
            gs.creatures[cid]["health"] = health
            gs.creatures[cid]["t"] = time.time()
            # Kill detection: monster health dropped to 0
            if health == 0 and old_health > 0 and cid >= 0x40000000:
                if cid not in gs._recent_kills:
                    gs._recent_kills.add(cid)
                    _record_kill(gs, cid)
        elif health == 0 and cid >= 0x40000000:
            # Monster NOT in gs.creatures but died — count it (AOE kills)
            if cid not in gs._recent_kills:
                gs._recent_kills.add(cid)
                _record_kill(gs, cid)
        # Periodic cleanup of dedup set (every 30s, remove all)
        now = time.time()
        if now - gs._recent_kills_cleanup > 30:
            gs._recent_kills.clear()
            gs._recent_kills_cleanup = now
        return pos + _ch_size

    # CREATURE_MOVE — 11 bytes: pos(5) + u8 + pos(5)
    if opcode == ServerOpcode.CREATURE_MOVE:
        _cm_size = PF.get("creature_move", {}).get("size", 11)
        if pos + _cm_size > len(data):
            return -1
        # Skip — we just consume the bytes
        return pos + _cm_size

    # TEXT_MESSAGE — variable: u8 type + string(u16 len + chars)
    if opcode == ServerOpcode.TEXT_MESSAGE:
        _tm = PF.get("text_message", {})
        _tm_hdr = _tm.get("header", 3)
        _tm_type = _tm.get("type", 0)
        _tm_len = _tm.get("length", 1)
        _tm_text = _tm.get("text", 3)
        if pos + _tm_hdr > len(data):
            return -1
        msg_type = data[pos + _tm_type]
        str_len = struct.unpack_from('<H', data, pos + _tm_len)[0]
        end = pos + _tm_text + str_len
        if end > len(data):
            return -1
        text = data[pos + _tm_text:end].decode('latin-1', errors='replace')
        gs.messages.append({"type": msg_type, "text": text})
        if "can't throw there" in text.lower():
            gs.last_cant_throw = time.time()
        # "Creature is not reachable." — instantly blacklist current attack target
        if "not reachable" in text.lower():
            target_id = gs.attack_target_id
            if target_id and target_id >= 0x40000000:
                gs.unreachable_creatures[target_id] = time.time() + 10  # 10s blacklist
                gs.attack_target_id = 0
                log.info(f"NOT REACHABLE: blacklisted 0x{target_id:08X} for 10s")
        _check_pz_message(text, gs)
        log.info(f"TEXT_MESSAGE(type={msg_type}): {text}")
        return end

    # LOGIN_OR_PENDING — u32 player_id, u16 draw_speed, u8 can_report_bugs
    # Then MAP_DESCRIPTION with position
    if opcode == ServerOpcode.LOGIN_OR_PENDING:
        _lp = PF.get("login_or_pending", {})
        _lp_pid = _lp.get("player_id_size", 4)
        _lp_hdr = _lp.get("header_before_position", 4)
        _lp_win = _lp.get("map_description_search_window", 10)
        _pos = PF.get("position", {})
        _pos_x = _pos.get("x", 0)
        _pos_y = _pos.get("y", 2)
        _pos_z = _pos.get("z", 4)
        if pos + _lp_pid > len(data):
            return -1
        new_pid = struct.unpack_from('<I', data, pos)[0]
        # Guard: only accept player_id in valid player range (0x10xxxxxx)
        if 0x10000000 <= new_pid < 0x20000000 or gs.player_id == 0:
            gs.player_id = new_pid
            log.info(f"LOGIN: player_id=0x{gs.player_id:08X}")
        else:
            log.warning(f"LOGIN: rejected suspicious player_id=0x{new_pid:08X} "
                        f"(keeping 0x{gs.player_id:08X})")
        pos += _lp_pid
        # Search for MAP_DESCRIPTION within next N bytes (skip draw_speed/flags)
        search_end = min(pos + _lp_win, len(data) - 5)
        found_pos = False
        for i in range(pos, search_end):
            if data[i] == ServerOpcode.MAP_DESCRIPTION:
                x = struct.unpack_from('<H', data, i + 1)[0]
                y = struct.unpack_from('<H', data, i + 3)[0]
                z = data[i + 5]
                if 100 < x < 65000 and 100 < y < 65000 and z < 16:
                    gs.position = (x, y, z)
                    gs.packet_position = (x, y, z)
                    gs.creatures = {cid: info for cid, info in gs.creatures.items() if info.get("source") == "dll"}
                    gs.last_map_time = time.time()
                    log.info(f"LOGIN position: ({x}, {y}, {z})")
                    found_pos = True
                    break
        # Fallback: server may have changed the marker byte (was 0x64, now 0x4B).
        # Position is still at fixed offset: draw_speed(2) + flags(1) + marker(1) = +4
        if not found_pos and pos + _lp_hdr + 5 <= len(data):
            i = pos + _lp_hdr
            x = struct.unpack_from('<H', data, i + _pos_x)[0]
            y = struct.unpack_from('<H', data, i + _pos_y)[0]
            z = data[i + _pos_z]
            if 100 < x < 65000 and 100 < y < 65000 and z < 16:
                gs.position = (x, y, z)
                gs.packet_position = (x, y, z)
                gs.creatures = {cid: info for cid, info in gs.creatures.items() if info.get("source") == "dll"}
                gs.last_map_time = time.time()
                log.info(f"LOGIN position (fixed offset fallback): ({x}, {y}, {z})")
        return -1  # Can't skip the rest (tile data follows)

    # MAP_DESCRIPTION — read position then stop (can't skip tile data)
    if opcode == ServerOpcode.MAP_DESCRIPTION:
        _md = PF.get("map_description", {})
        if pos + 5 > len(data):
            return -1
        x = struct.unpack_from('<H', data, pos + _md.get("position_x", 0))[0]
        y = struct.unpack_from('<H', data, pos + _md.get("position_y", 2))[0]
        z = data[pos + _md.get("position_z", 4)]
        gs.position = (x, y, z)
        gs.packet_position = (x, y, z)
        gs.creatures = {cid: info for cid, info in gs.creatures.items() if info.get("source") == "dll"}
        gs.last_map_time = time.time()
        log.info(f"MAP_DESCRIPTION: pos=({x}, {y}, {z}) — creatures cleared")
        return -1  # Can't skip tile data

    # MAP_SLICE — update position, but can't skip tile data
    if opcode in (ServerOpcode.MAP_SLICE_NORTH, ServerOpcode.MAP_SLICE_EAST,
                  ServerOpcode.MAP_SLICE_SOUTH, ServerOpcode.MAP_SLICE_WEST):
        x, y, z = gs.position
        if opcode == ServerOpcode.MAP_SLICE_NORTH:
            gs.position = (x, y - 1, z)
        elif opcode == ServerOpcode.MAP_SLICE_EAST:
            gs.position = (x + 1, y, z)
        elif opcode == ServerOpcode.MAP_SLICE_SOUTH:
            gs.position = (x, y + 1, z)
        elif opcode == ServerOpcode.MAP_SLICE_WEST:
            gs.position = (x - 1, y, z)
        # Always track packet-derived position for recording accuracy
        # Seed from gs.position if packet_position looks uninitialized
        # (real game coordinates are always > 100)
        if gs.packet_position[0] < 100 and gs.position[0] > 100:
            gs.packet_position = gs.position
        px, py, pz = gs.packet_position
        if opcode == ServerOpcode.MAP_SLICE_NORTH:
            gs.packet_position = (px, py - 1, pz)
        elif opcode == ServerOpcode.MAP_SLICE_EAST:
            gs.packet_position = (px + 1, py, pz)
        elif opcode == ServerOpcode.MAP_SLICE_SOUTH:
            gs.packet_position = (px, py + 1, pz)
        elif opcode == ServerOpcode.MAP_SLICE_WEST:
            gs.packet_position = (px - 1, py, pz)
        gs.last_map_time = time.time()
        return -1  # Can't skip tile data

    # ── Fixed-size opcodes we can safely skip ──────────────────────

    # MAGIC_EFFECT — 6 bytes: pos(5) + u8 effect
    if opcode == ServerOpcode.MAGIC_EFFECT:
        _me_size = PF.get("magic_effect", {}).get("size", 6)
        return pos + _me_size if pos + _me_size <= len(data) else -1

    # SHOOT_EFFECT — 11 bytes: from_pos(5) + to_pos(5) + u8 effect
    if opcode == ServerOpcode.SHOOT_EFFECT:
        _se_size = PF.get("shoot_effect", {}).get("size", 11)
        return pos + _se_size if pos + _se_size <= len(data) else -1

    # ANIMATED_TEXT — variable: pos(5) + u8 color + string(u16 len + chars)
    # Very common during combat (damage numbers). Must handle to not break scan.
    if opcode == ServerOpcode.ANIMATED_TEXT:
        _at = PF.get("animated_text", {})
        _at_hdr = _at.get("header", 8)
        _at_slen = _at.get("string_length", 6)
        _at_text = _at.get("text", 8)
        if pos + _at_hdr > len(data):
            return -1
        str_len = struct.unpack_from('<H', data, pos + _at_slen)[0]
        end = pos + _at_text + str_len
        if end > len(data):
            return -1
        return end

    # TILE_REMOVE_THING — 6 bytes: pos(5) + u8 stack_pos
    if opcode == ServerOpcode.TILE_REMOVE_THING:
        _trt_size = PF.get("tile_remove_thing", {}).get("size", 6)
        return pos + _trt_size if pos + _trt_size <= len(data) else -1

    # CLOSE_CONTAINER — 1 byte: u8 container_id
    if opcode == ServerOpcode.CLOSE_CONTAINER:
        _cc_size = PF.get("close_container", {}).get("size", 1)
        return pos + _cc_size if pos + _cc_size <= len(data) else -1

    # REMOVE_FROM_CONTAINER — 2 bytes: u8 container_id + u8 slot
    if opcode == ServerOpcode.REMOVE_FROM_CONTAINER:
        _rfc_size = PF.get("remove_from_container", {}).get("size", 2)
        return pos + _rfc_size if pos + _rfc_size <= len(data) else -1

    # CREATURE_LIGHT — 6 bytes: u32 creature_id + u8 level + u8 color
    if opcode == ServerOpcode.CREATURE_LIGHT:
        _cl_size = PF.get("creature_light", {}).get("size", 6)
        return pos + _cl_size if pos + _cl_size <= len(data) else -1

    # CREATURE_SPEED — 6 bytes: u32 creature_id + u16 speed
    if opcode == ServerOpcode.CREATURE_SPEED:
        _cs = PF.get("creature_speed", {})
        _cs_size = _cs.get("size", 6)
        if pos + _cs_size > len(data):
            return -1
        cid = struct.unpack_from('<I', data, pos + _cs.get("creature_id", 0))[0]
        spd = struct.unpack_from('<H', data, pos + _cs.get("speed", 4))[0]
        if cid == gs.player_id:
            gs.speed = spd
        return pos + _cs_size

    # CREATURE_SKULL — 5 bytes: u32 creature_id + u8 skull
    if opcode == ServerOpcode.CREATURE_SKULL:
        _csk_size = PF.get("creature_skull", {}).get("size", 5)
        return pos + _csk_size if pos + _csk_size <= len(data) else -1

    # CREATURE_PARTY — 5 bytes: u32 creature_id + u8 shield
    if opcode == ServerOpcode.CREATURE_PARTY:
        _cp_size = PF.get("creature_party", {}).get("size", 5)
        return pos + _cp_size if pos + _cp_size <= len(data) else -1

    # PLAYER_SKILLS — variable: 7 skills × (u8 level + u8 percent) = 14 bytes
    # (standard TFS 7.x/8.x format; may differ on modified servers)
    if opcode == ServerOpcode.PLAYER_SKILLS:
        needed = PF.get("player_skills", {}).get("size", 14)
        if pos + needed > len(data):
            return -1
        # Just consume the bytes — we don't track skills yet
        return pos + needed

    # PLAYER_ICONS — 2 bytes: u16 icons bitmask
    if opcode == ServerOpcode.PLAYER_ICONS:
        _pi_size = PF.get("player_icons", {}).get("size", 2)
        if pos + _pi_size > len(data):
            return -1
        old = gs.player_icons
        gs.player_icons = struct.unpack_from('<H', data, pos)[0]
        if gs.player_icons != old:
            log.info(f"PLAYER_ICONS changed: 0x{old:04X} -> 0x{gs.player_icons:04X} "
                     f"(diff bits: 0x{old ^ gs.player_icons:04X})")
        return pos + _pi_size

    # PLAYER_CANCEL_WALK — 1 byte: u8 direction
    if opcode == ServerOpcode.PLAYER_CANCEL_WALK:
        _pcw_size = PF.get("player_cancel_walk", {}).get("size", 1)
        if pos + _pcw_size > len(data):
            return -1
        direction = data[pos]
        now = time.time()
        gs.cancel_walk_time = now
        # Revert the optimistic client-walk position update
        dx, dy = gs._last_walk_delta
        if dx != 0 or dy != 0:
            x, y, z = gs.position
            gs.position = (x - dx, y - dy, z)
            px, py, pz = gs.packet_position
            if px > 100:
                gs.packet_position = (px - dx, py - dy, pz)
            gs._last_walk_delta = (0, 0)
        gs.server_events.append((now, "cancel_walk", {"direction": direction, "pos": list(gs.position)}))
        log.info(f"CANCEL_WALK direction={direction} → reverted pos to {gs.position}")
        return pos + _pcw_size

    # NOTE: FLOOR_CHANGE_UP (0xBE) / FLOOR_CHANGE_DOWN (0xBF) are standard OT
    # opcodes but DBVictory does NOT use them. Sniffing confirmed that floor changes
    # arrive as CREATURE_MOVE (0x6D) + map data, not as standalone 0xBE/0xBF opcodes.
    # Floor change events are generated by the DLL bridge (actions/dll_bridge.py)
    # when it detects z-coordinate changes in game memory.

    # PING — 0 bytes
    if opcode == ServerOpcode.PING:
        return pos

    # PLAYER_CANCEL_ATTACK — 0 bytes
    if opcode == ServerOpcode.PLAYER_CANCEL_ATTACK:
        return pos

    # WORLD_LIGHT (0x82) — 2 bytes: u8 level + u8 color
    if opcode == 0x82:
        _wl = PF.get("world_light", {})
        _wl_size = _wl.get("size", 2)
        if pos + _wl_size > len(data):
            return -1
        gs.world_light_level = data[pos + _wl.get("level", 0)]
        gs.world_light_color = data[pos + _wl.get("color", 1)]
        return pos + _wl_size

    # DBVictory custom opcode 0xCB — 5 bytes payload (empirically observed)
    if opcode == 0xCB:
        _cb_size = PF.get("custom_0xcb", {}).get("size", 5)
        return pos + _cb_size if pos + _cb_size <= len(data) else -1

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
        _dump_stats_debug(gs, None)
    elif opcode == ServerOpcode.TEXT_MESSAGE:
        msg_type = reader.read_u8()
        text = reader.read_string()
        gs.messages.append({"type": msg_type, "text": text})
        if "can't throw there" in text.lower():
            gs.last_cant_throw = time.time()
        _check_pz_message(text, gs)
        log.info(f"TEXT_MESSAGE(type={msg_type}): {text}")
