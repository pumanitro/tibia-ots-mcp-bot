"""DLL Bridge: reads creatures from injected dbvbot.dll via named pipe.

Auto-injects the DLL if needed, then polls creature data every 100ms
and updates game_state.creatures with authoritative memory-read data.
"""
import sys
import os
import glob
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
DEBUG_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dll_bridge_debug.txt")


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


async def _connect_with_inject(bridge, bot):
    """Try existing pipe, health-check it, re-inject if needed. Returns True on success."""
    # Try existing pipe first (DLL may still be loaded with live thread)
    await bot.sleep(1)
    if bridge.connect():
        bridge.send_command({"cmd": "init", "player_id": bot.player_id})
        bridge.send_command({"cmd": "hook_attack"})
        bridge.send_command({"cmd": "hook_send"})
        bridge.send_command({"cmd": "scan_xtea"})
        bridge.send_command({"cmd": "hook_xtea"})
        bridge.send_command({"cmd": "scan_game_attack"})
        _dbg("connected to existing pipe — sent init + hooks + scan, health checking...")
        # DLL full scan can take up to 20s on first run
        for check in range(40):
            await bot.sleep(0.5)
            test = bridge.read_creatures()
            if test is not None:
                _dbg(f"pipe healthy — got {len(test)} creatures (after {(check+1)*0.5:.1f}s)")
                return True
        _dbg("pipe dead after 20s — will inject fresh DLL")
        bridge.disconnect()

    # Inject a fresh copy
    try:
        _inject_fresh_dll()
    except Exception as e:
        _dbg(f"injection failed: {e}")
        return False

    await bot.sleep(3)

    for attempt in range(30):
        if bridge.connect():
            bridge.send_command({"cmd": "init", "player_id": bot.player_id})
            bridge.send_command({"cmd": "hook_attack"})
            bridge.send_command({"cmd": "hook_send"})
            bridge.send_command({"cmd": "scan_xtea"})
            bridge.send_command({"cmd": "hook_xtea"})
            bridge.send_command({"cmd": "scan_game_attack"})
            _dbg(f"pipe connected on attempt {attempt+1} — sent init + hooks + scan, waiting for first scan...")
            # Wait for DLL to complete first full scan before returning
            for check in range(40):
                await bot.sleep(0.5)
                test = bridge.read_creatures()
                if test is not None:
                    _dbg(f"first data received — {len(test)} creatures")
                    return True
            _dbg("pipe connected but no data after 20s — continuing anyway")
            return True
        if attempt % 5 == 0:
            _dbg(f"pipe connect attempt {attempt+1}...")
        await bot.sleep(CONNECT_RETRY)

    _dbg("FAILED to connect to pipe after injection")
    return False


async def run(bot):
    bridge = DllBridge()
    with open(DEBUG_LOG, "w") as f:
        f.write("=== dll_bridge started ===\n")

    # Wait for bot connection and player_id
    while not bot.is_connected or bot.player_id == 0:
        await bot.sleep(1)

    _dbg(f"player_id={bot.player_id:#010x} ({bot.player_id})")

    if not await _connect_with_inject(bridge, bot):
        return

    # Access game_state directly for authoritative updates
    state = sys.modules["__main__"].state
    gs = state.game_state

    # Expose bridge on game_state so other actions (auto_attack) can send commands
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
                    # Use player's own creature to keep gs.position updated
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
