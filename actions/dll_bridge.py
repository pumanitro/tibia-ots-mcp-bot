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

POLL_INTERVAL = 0.1   # seconds between creature reads
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
    with open(DEBUG_LOG, "a") as f:
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

    # 3. WndProc hook for fast targeting (~16ms instead of ~1s XTEA)
    bridge.send_command({"cmd": "hook_wndproc"})

    # 4. Creature map scanning (replaces VirtualQuery)
    bridge.send_command({"cmd": "scan_gmap"})
    bridge.send_command({"cmd": "use_map_scan", "enabled": True})

    _dbg("sent full init: offsets + hooks + wndproc + gmap")


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

    poll_count = 0
    last_data_time = time.time()  # when we last got actual data
    STALE_TIMEOUT = 30  # seconds without data before considering pipe dead

    try:
        while bot.is_connected:
            creatures = bridge.read_creatures()
            poll_count += 1

            if creatures is not None:
                last_data_time = time.time()

            # Log summary every 100 polls (~10s)
            if poll_count % 100 == 0:
                age = time.time() - last_data_time
                _dbg(f"poll#{poll_count}: gs.creatures={len(gs.creatures)} "
                     f"last_data={age:.0f}s ago")

            # Only consider pipe dead after long silence
            if time.time() - last_data_time > STALE_TIMEOUT:
                _dbg(f"pipe dead — no data for {STALE_TIMEOUT}s — re-injecting DLL")
                bridge.disconnect()
                if await _connect_with_inject(bridge, bot):
                    last_data_time = time.time()
                else:
                    _dbg("re-injection failed — will keep trying")

            now = time.time()
            if creatures is not None and len(creatures) > 0:
                # Get player position from packet-based game state (reliable)
                px, py, pz = gs.position if gs.position else (0, 0, 0)

                dll_creatures = {}
                raw_count = 0
                for c in creatures:
                    cid = c.get("id", 0)
                    if cid == 0:
                        continue
                    # Only accept valid OT creature IDs
                    if not (0x10000000 <= cid < 0x80000000):
                        continue
                    raw_count += 1
                    cx, cy, cz = c.get("x", 0), c.get("y", 0), c.get("z", 0)
                    # Use player's own creature to keep gs.position and HP updated
                    if cid == bot.player_id:
                        if 0 < cx < 65535 and 0 < cy < 65535 and cz < 16:
                            if gs.position != (cx, cy, cz):
                                old = gs.position
                                gs.position = (cx, cy, cz)
                                try:
                                    gs.dll_position_active = True
                                except AttributeError:
                                    pass
                                px, py, pz = cx, cy, cz
                                if old[2] != cz:
                                    _dbg(f"player z changed: {old} -> ({cx},{cy},{cz})")
                        # Update HP from memory (creature health %) — much more
                        # reliable than packet parsing during combat
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

                if poll_count % 30 == 1:
                    _dbg(f"filter: raw={raw_count} nearby={len(dll_creatures)} player=({px},{py},{pz})")

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
