"""DLL Bridge: reads creatures from injected dbvbot.dll via named pipe.

Auto-injects the DLL if needed, then polls creature data every 100ms
and updates game_state.creatures with authoritative memory-read data.
"""
import sys
import os
import glob
import json
import shutil
import time
import logging

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dll_bridge import DllBridge
import inject as dll_inject

log = logging.getLogger("action.dll_bridge")

POLL_INTERVAL = 0.016  # seconds between creature reads (~60 FPS)
INJECT_RETRY = 10     # seconds to wait before retrying injection
CONNECT_RETRY = 2     # seconds between pipe connection attempts
PROXIMITY_RANGE = 7   # max tile Chebyshev distance (visible screen range)
STALE_REINJECT = 3    # number of stale cycles before re-injecting DLL
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEBUG_LOG = os.path.join(PROJECT_ROOT, "dll_bridge_debug.txt")
OFFSETS_FILE = os.path.join(PROJECT_ROOT, "offsets.json")


def _load_offsets():
    """Load offsets.json and build a set_offsets command dict for the DLL."""
    try:
        with open(OFFSETS_FILE, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        _dbg(f"offsets.json load failed: {e}")
        return None

    # Build flat dict for DLL's parse_set_offsets()
    cmd = {"cmd": "set_offsets"}
    cmd["game_singleton_rva"] = data.get("game_singleton_rva", "0xB2E970")

    game_off = data.get("game_offsets", {})
    cmd["attacking_creature"] = game_off.get("attacking_creature", "0x0C")
    cmd["protocol_game"] = game_off.get("protocol_game", "0x18")
    cmd["attack_flag"] = game_off.get("attack_flag", "0x34")
    cmd["seq_counter"] = game_off.get("seq_counter", "0x70")

    creature_off = data.get("creature_offsets", {})
    cmd["creature_id"] = creature_off.get("id", "0x34")
    cmd["creature_name"] = creature_off.get("name", "0x38")
    cmd["creature_hp"] = creature_off.get("health", "0x50")
    cmd["creature_refs"] = creature_off.get("refs", "0x04")
    cmd["npc_pos_from_id"] = creature_off.get("npc_position_from_id", 576)
    cmd["player_pos_from_id"] = creature_off.get("player_position_from_id", -40)

    vtable_range = data.get("creature_vtable_rva_range", ["0x870000", "0x8A0000"])
    cmd["vtable_rva_min"] = vtable_range[0]
    cmd["vtable_rva_max"] = vtable_range[1]

    funcs = data.get("functions", {})
    cmd["xtea_encrypt_rva"] = funcs.get("xtea_encrypt_rva", "0x3AF220")
    cmd["game_attack_rva"] = funcs.get("game_attack_rva", "0x8F220")
    cmd["send_attack_rva"] = funcs.get("send_attack_rva", "0x19D100")
    cmd["game_doattack_rva"] = funcs.get("game_doattack_rva", "0x89680")

    return cmd


def _find_latest_dll():
    dll_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dll")
    candidates = glob.glob(os.path.join(dll_dir, "dbvbot*.dll"))
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def _dbg(msg):
    with open(DEBUG_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")


def _inject_fresh_dll():
    """Copy the DLL to a unique temp name and inject it.

    LoadLibraryA keys on filename — re-injecting the same filename just bumps
    the refcount without calling DllMain.  A unique copy forces a fresh load.
    """
    dll_source = _find_latest_dll()
    if not dll_source:
        raise FileNotFoundError("No dbvbot*.dll found in dll/ directory")
    dll_dir = os.path.dirname(dll_source)
    temp_name = f"dbvbot_live_{int(time.time())}.dll"
    temp_path = os.path.join(dll_dir, temp_name)
    shutil.copy2(dll_source, temp_path)
    _dbg(f"injecting fresh DLL copy: {temp_name}")
    dll_inject.inject(dll_path=temp_path)
    return temp_name


def _send_init_commands(bridge, player_id):
    """Send full init sequence to DLL: offsets, init, hooks, scans, map mode."""
    # 1. Send offsets first (before any hooks that use them)
    offsets_cmd = _load_offsets()
    if offsets_cmd:
        bridge.send_command(offsets_cmd)
        _dbg("sent set_offsets from offsets.json")

    # 2. Core init
    bridge.send_command({"cmd": "init", "player_id": player_id})
    bridge.send_command({"cmd": "hook_attack"})
    bridge.send_command({"cmd": "hook_send"})
    bridge.send_command({"cmd": "scan_xtea"})
    bridge.send_command({"cmd": "hook_xtea"})
    bridge.send_command({"cmd": "scan_game_attack"})

    # 3. PeekMessage hook for safe targeting at game loop boundary
    # (replaces WndProc-based targeting which crashed Lua VM)
    bridge.send_command({"cmd": "hook_peekmsg"})
    # WndProc hook kept as fallback (auto-skips targeting if PeekMsg active)
    bridge.send_command({"cmd": "hook_wndproc"})

    # 4. Creature map scanning (replaces VirtualQuery)
    bridge.send_command({"cmd": "scan_gmap"})
    bridge.send_command({"cmd": "use_map_scan", "enabled": True})

    _dbg("sent full init: offsets + hooks + peekmsg + gmap")


async def _connect_with_inject(bridge, bot):
    """Try existing pipe, health-check it, re-inject if needed. Returns True on success."""
    t0 = time.time()
    _dbg(f"[TIMING] _connect_with_inject START t=0.0s")

    # Try existing pipe first (DLL may still be loaded with live thread)
    # Short sleep — just enough for pipe thread to be ready
    await bot.sleep(0.2)

    if bridge.connect():
        _dbg(f"[TIMING] existing pipe connected t={time.time()-t0:.1f}s — sending init...")
        _send_init_commands(bridge, bot.player_id)
        # Fast health check: if pipe is alive, first read_creatures returns
        # within 1-2s. A stale pipe from a previous session will never respond.
        # 6 attempts × 0.5s = 3s max (was 40 × 0.5s = 20s)
        for check in range(6):
            await bot.sleep(0.5)
            test = bridge.read_creatures()
            if test is not None:
                _dbg(f"[TIMING] pipe healthy — {len(test)} creatures t={time.time()-t0:.1f}s")
                return True
        _dbg(f"[TIMING] pipe dead after 3s health check t={time.time()-t0:.1f}s — will inject fresh")
        bridge.disconnect()
    else:
        _dbg(f"[TIMING] no existing pipe t={time.time()-t0:.1f}s")

    # Inject a fresh copy
    _dbg(f"[TIMING] injecting fresh DLL t={time.time()-t0:.1f}s")
    try:
        _inject_fresh_dll()
    except Exception as e:
        _dbg(f"[TIMING] injection failed t={time.time()-t0:.1f}s: {e}")
        return False
    _dbg(f"[TIMING] injection done t={time.time()-t0:.1f}s — waiting for pipe...")

    # Poll for pipe instead of fixed sleep — DLL may be ready in < 1s
    for attempt in range(30):
        await bot.sleep(0.3)
        if bridge.connect():
            _dbg(f"[TIMING] pipe connected attempt {attempt+1} t={time.time()-t0:.1f}s — sending init...")
            _send_init_commands(bridge, bot.player_id)
            _dbg(f"[TIMING] init sent t={time.time()-t0:.1f}s — waiting for first scan...")
            # Wait for DLL to complete first full creature scan
            for check in range(40):
                await bot.sleep(0.5)
                test = bridge.read_creatures()
                if test is not None:
                    _dbg(f"[TIMING] first data — {len(test)} creatures t={time.time()-t0:.1f}s DONE")
                    return True
            _dbg(f"[TIMING] no data after 20s wait t={time.time()-t0:.1f}s — continuing anyway")
            return True
        if attempt % 5 == 0:
            _dbg(f"[TIMING] pipe connect attempt {attempt+1} t={time.time()-t0:.1f}s")

    _dbg(f"[TIMING] FAILED after all attempts t={time.time()-t0:.1f}s")
    return False


async def run(bot):
    bridge = DllBridge()
    t_start = time.time()
    with open(DEBUG_LOG, "w") as f:
        f.write(f"=== dll_bridge started at {time.strftime('%H:%M:%S')} ===\n")

    _dbg(f"[TIMING] run() START — waiting for connection & player_id")

    # Wait for bot connection and player_id
    while not bot.is_connected or bot.player_id == 0:
        await bot.sleep(1)

    _dbg(f"[TIMING] connected, player_id={bot.player_id:#010x} t={time.time()-t_start:.1f}s")

    if not await _connect_with_inject(bridge, bot):
        _dbg(f"[TIMING] connect_with_inject FAILED t={time.time()-t_start:.1f}s")
        return

    _dbg(f"[TIMING] DLL READY — total startup t={time.time()-t_start:.1f}s")

    # Access game_state directly for authoritative updates
    state = sys.modules["__main__"].state
    gs = state.game_state

    # Expose bridge on game_state so other actions (auto_targeting) can send commands
    gs.dll_bridge = bridge

    # Save player_id locally — gs.player_id can get corrupted mid-session
    # by mis-parsed packets (byte that looks like LOGIN_OR_PENDING opcode).
    my_player_id = bot.player_id
    _dbg(f"saved player_id=0x{my_player_id:08X} (will NOT re-read from gs)")

    # Wait for a valid position from packets before probing, then send probe_pos
    # to discover the correct creature struct position offset
    probe_sent = False
    probe_result = None
    global_pos_scanned = False

    poll_count = 0
    last_data_time = time.time()  # when we last got actual data
    last_heartbeat = time.time()  # for periodic status logging
    STALE_TIMEOUT = 30  # seconds without data before considering pipe dead
    HEARTBEAT_INTERVAL = 1  # seconds between heartbeat logs
    pipe_was_connected = True  # track transitions for disconnect alert

    try:
        while bot.is_connected:
            creatures = bridge.read_creatures()
            poll_count += 1

            if creatures is not None:
                last_data_time = time.time()

            # Detect pipe disconnect transition
            pipe_ok = bridge.connected
            if pipe_was_connected and not pipe_ok:
                age = time.time() - last_data_time
                _dbg(f"PIPE DISCONNECTED at poll#{poll_count} — last data {age:.1f}s ago")
                bot.log(f"[DLL] PIPE LOST at poll#{poll_count} — last data {age:.1f}s ago")
            pipe_was_connected = pipe_ok

            # Heartbeat every 3 seconds
            now_hb = time.time()
            if now_hb - last_heartbeat >= HEARTBEAT_INTERVAL:
                last_heartbeat = now_hb
                data_age = now_hb - last_data_time
                status = "OK" if pipe_ok else "DISCONNECTED"
                _dbg(f"HEARTBEAT pipe={status} creatures={len(gs.creatures)} "
                     f"data_age={data_age:.1f}s poll#{poll_count}")
                if not pipe_ok:
                    bot.log(f"[DLL] heartbeat: pipe={status} creatures={len(gs.creatures)} "
                            f"data_age={data_age:.0f}s")

            # Only consider pipe dead after long silence
            if time.time() - last_data_time > STALE_TIMEOUT:
                _dbg(f"pipe dead — no data for {STALE_TIMEOUT}s — re-injecting DLL")
                bot.log(f"[DLL] STALE {STALE_TIMEOUT}s — attempting re-inject")
                bridge.disconnect()
                if await _connect_with_inject(bridge, bot):
                    last_data_time = time.time()
                    pipe_was_connected = True
                    bot.log("[DLL] re-inject SUCCESS — pipe reconnected")
                else:
                    _dbg("re-injection failed — will keep trying")
                    bot.log("[DLL] re-inject FAILED — game restart needed")

            now = time.time()
            if creatures is not None and len(creatures) > 0:
                # Use packet_position as primary position source — DLL creature
                # struct position is stale for the local player (Fix 19).
                pkt = gs.packet_position
                px, py, pz = pkt if pkt[0] > 0 else gs.position if gs.position[0] > 0 else (0, 0, 0)

                # Send packet-derived position to DLL for attack distance/floor check.
                if px > 0 and py > 0 and poll_count % 6 == 0:
                    bridge.send_command({"cmd": "set_player_pos", "x": px, "y": py, "z": pz})

                # Probe position offset: send once using packet_position (clean reference).
                # Temporarily override set_player_pos with packet values so probe searches correctly.
                pkt_pos = gs.packet_position if gs.packet_position[0] > 0 else gs.position
                if not probe_sent and pkt_pos[0] > 0 and pkt_pos[1] > 0 and poll_count > 30:
                    # Send packet pos so probe searches for the correct values
                    bridge.send_command({"cmd": "set_player_pos",
                                         "x": pkt_pos[0], "y": pkt_pos[1], "z": pkt_pos[2]})
                    bridge.send_command({"cmd": "probe_pos"})
                    # Restore DLL position immediately after
                    bridge.send_command({"cmd": "set_player_pos", "x": px, "y": py, "z": pz})
                    probe_sent = True
                    _dbg(f"PROBE_POS sent — looking for pkt=({pkt_pos[0]},{pkt_pos[1]},{pkt_pos[2]}) "
                         f"dll=({px},{py},{pz}) in player creature struct")
                    bot.log(f"[DLL] probe_pos sent — scanning for ({pkt_pos[0]},{pkt_pos[1]},{pkt_pos[2]})")

                # Check for probe_pos results in extras
                if probe_sent and probe_result is None:
                    extras = bridge.pop_extras()
                    for ex in extras:
                        if "probe_pos" in ex:
                            probe_result = ex["probe_pos"]
                            _dbg(f"PROBE_POS result: {json.dumps(probe_result)}")
                            matches = probe_result.get("matches", [])
                            bot.log(f"[DLL] probe_pos: {len(matches)} position matches found")
                            for m in matches:
                                _dbg(f"  MATCH: off_from_id={m['off_from_id']} "
                                     f"off_from_base={m['off_from_base']} "
                                     f"hex_base={m.get('hex_base','?')} "
                                     f"fmt={m.get('fmt','u32u32u32')}")
                                bot.log(f"[DLL]   offset from base: {m.get('hex_base','?')} "
                                        f"(from id: {m['off_from_id']}) fmt={m.get('fmt','u32u32u32')}")

                # Send scan_global_pos once after we have a valid position
                if not global_pos_scanned and probe_sent and pkt_pos[0] > 100:
                    bridge.send_command({"cmd": "scan_global_pos"})
                    global_pos_scanned = True
                    _dbg(f"SCAN_GLOBAL_POS sent — searching for ({pkt_pos[0]},{pkt_pos[1]},{pkt_pos[2]})")
                    bot.log(f"[DLL] scan_global_pos — searching writable sections for position")

                # Check for scan_global_pos result in extras
                if global_pos_scanned:
                    extras = bridge.pop_extras()
                    for ex in extras:
                        if "scan_global_pos" in ex:
                            sgp = ex["scan_global_pos"]
                            _dbg(f"SCAN_GLOBAL_POS result: {json.dumps(sgp)}")
                            bot.log(f"[DLL] scan_global_pos: {sgp.get('matches',0)} matches, "
                                    f"addr={sgp.get('rva','?')}")

                # Update position from DLL global memory (live, not stale)
                dp = bridge.dll_pos
                if dp[0] > 100 and dp[1] > 100:
                    gs.position = dp
                    gs.packet_position = dp
                    px, py, pz = dp

                dll_creatures = {}
                raw_count = 0
                player_found = False
                for c in creatures:
                    cid = c.get("id", 0)
                    if cid == 0:
                        continue
                    # Only accept valid OT creature IDs
                    if not (0x10000000 <= cid < 0x80000000):
                        continue
                    raw_count += 1
                    cx, cy, cz = c.get("x", 0), c.get("y", 0), c.get("z", 0)
                    # Player creature: use DLL memory as authoritative position source.
                    if cid == my_player_id:
                        player_found = True
                        # Log player creature (position is stale — Fix 19)
                        if poll_count % 300 == 1:
                            _dbg(f"PLAYER FOUND: cid=0x{cid:08X} my_pid=0x{my_player_id:08X} "
                                 f"gs.pid=0x{gs.player_id:08X} dll=({cx},{cy},{cz}) pkt=({px},{py},{pz})")
                        # Update HP from memory (creature health %)
                        hp_pct = c.get("hp", 0)
                        if gs.max_hp > 0 and 0 <= hp_pct <= 100:
                            new_hp = round(hp_pct / 100 * gs.max_hp)
                            if new_hp != gs.hp:
                                _dbg(f"HP from memory: {gs.hp} -> {new_hp} ({hp_pct}%)")
                                gs.hp = new_hp
                        continue
                    # Skip dead creatures (0% HP)
                    if c.get("hp", 0) <= 0:
                        continue
                    # Skip invalid positions
                    if (cx == 0 and cy == 0) or cx > 65535 or cz > 15:
                        continue
                    # Proximity filter: same z-level, within range
                    if px > 0 and py > 0:
                        if cz != pz:
                            continue
                        if max(abs(cx - px), abs(cy - py)) > PROXIMITY_RANGE:
                            continue
                    dll_creatures[cid] = {
                        "health": c.get("hp", 0),
                        "name": c.get("name", "?"),
                        "x": cx, "y": cy, "z": cz,
                        "t": now,
                        "source": "dll",
                    }

                # Warn if player creature not found in DLL data
                if not player_found and poll_count % 60 == 1:
                    all_ids = [f"0x{c.get('id',0):08X}" for c in creatures if c.get("id", 0)]
                    ids_str = ",".join(all_ids[:8])
                    _dbg(f"PLAYER NOT FOUND! my_pid=0x{my_player_id:08X} "
                         f"gs.pid=0x{gs.player_id:08X} creature_ids=[{ids_str}]")

                if poll_count % 30 == 1:
                    refs_info = ", ".join(f"0x{c.get('id',0):X}(r={c.get('refs',0)})" for c in creatures[:5] if c.get("id",0) != my_player_id)
                    _dbg(f"filter: raw={raw_count} nearby={len(dll_creatures)} player=({px},{py},{pz}) refs=[{refs_info}]")
                # Detailed creature dump: every 5 seconds, show ALL creatures with filter reasons
                if poll_count % 300 == 1 and raw_count > 0:
                    _dbg(f"=== CREATURE DUMP (player=({px},{py},{pz})) my_pid=0x{my_player_id:08X} gs.pid=0x{gs.player_id:08X} raw={len(creatures)} ===")
                    for c in creatures:
                        cid = c.get("id", 0)
                        if cid == 0 or cid == my_player_id:
                            continue
                        name = c.get("name", "?")[:12]
                        hp = c.get("hp", 0)
                        ccx, ccy, ccz = c.get("x", 0), c.get("y", 0), c.get("z", 0)
                        refs = c.get("refs", 0)
                        # Determine filter reason
                        if not (0x10000000 <= cid < 0x80000000):
                            reason = "BAD_ID"
                        elif hp <= 0:
                            reason = "DEAD"
                        elif ccx == 0 and ccy == 0:
                            reason = "POS_ZERO"
                        elif ccx > 65535 or ccz > 15:
                            reason = "POS_INVALID"
                        elif ccz != pz:
                            reason = f"WRONG_Z({ccz}!={pz})"
                        else:
                            dist = max(abs(ccx - px), abs(ccy - py))
                            if dist > PROXIMITY_RANGE:
                                reason = f"FAR(d={dist})"
                            else:
                                reason = f"OK(d={dist})"
                        is_monster = "MON" if cid >= 0x40000000 else "PLR"
                        _dbg(f"  {is_monster} 0x{cid:08X} {name:12s} hp={hp:3d} pos=({ccx},{ccy},{ccz}) refs={refs} -> {reason}")
                    _dbg(f"=== END DUMP ===")
                # Position diagnostic: every 300 polls (~30s), show z-dist and closest
                if poll_count % 300 == 1 and raw_count > 0 and px > 0:
                    z_dist = {}
                    closest = 9999
                    closest_info = ""
                    for c in creatures:
                        cid = c.get("id", 0)
                        if cid == my_player_id or c.get("hp", 0) <= 0:
                            continue
                        cx, cy, cz = c.get("x", 0), c.get("y", 0), c.get("z", 0)
                        if cx == 0 and cy == 0:
                            continue
                        z_dist[cz] = z_dist.get(cz, 0) + 1
                        if cz == pz:
                            d = max(abs(cx - px), abs(cy - py))
                            if d < closest:
                                closest = d
                                closest_info = f"0x{cid:X} ({cx},{cy},{cz}) d={d} hp={c.get('hp',0)}"
                    _dbg(f"DIAG: z-dist={dict(sorted(z_dist.items()))} closest_same_z={closest_info or 'none'}")

                if dll_creatures:
                    for cid, info in dll_creatures.items():
                        gs.creatures[cid] = info

                # Remove DLL creatures no longer in filtered set
                stale = [
                    cid for cid, info in gs.creatures.items()
                    if info.get("source") == "dll" and cid not in dll_creatures
                ]
                for cid in stale:
                    del gs.creatures[cid]
            elif creatures is not None:
                # Empty list — no creatures nearby, clear DLL entries
                stale = [cid for cid, info in gs.creatures.items()
                         if info.get("source") == "dll"]
                for cid in stale:
                    del gs.creatures[cid]

            await bot.sleep(POLL_INTERVAL)
    finally:
        bridge.disconnect()
        _dbg("stopped (pipe thread kept alive)")
