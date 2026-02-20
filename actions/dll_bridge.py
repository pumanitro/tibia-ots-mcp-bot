"""DLL Bridge: reads creatures from injected dbvbot.dll via named pipe.

Auto-injects the DLL if needed, then polls creature data every 300ms
and updates game_state.creatures with authoritative memory-read data.
"""
import sys
import os
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
DEBUG_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dll_bridge_debug.txt")


def _dbg(msg):
    with open(DEBUG_LOG, "a") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")


async def run(bot):
    bridge = DllBridge()
    with open(DEBUG_LOG, "w") as f:
        f.write("=== dll_bridge started ===\n")

    # Wait for bot connection and player_id
    while not bot.is_connected or bot.player_id == 0:
        await bot.sleep(1)

    _dbg(f"player_id={bot.player_id:#010x} ({bot.player_id})")

    dll_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "dll", "dbvbot11.dll")

    connected = False

    # Try existing pipe first (DLL may still be loaded with live thread)
    await bot.sleep(2)  # let DLL recycle pipe from previous client
    if bridge.connect():
        bridge.send_command({"cmd": "init", "player_id": bot.player_id})
        _dbg("connected to existing pipe — health checking...")
        await bot.sleep(2)
        test = bridge.read_creatures()
        if test is not None:
            _dbg(f"pipe healthy — got {len(test)} creatures")
            connected = True
        else:
            _dbg("pipe dead — will inject fresh DLL")
            bridge.disconnect()

    if not connected:
        _dbg(f"injecting DLL: {dll_path}")
        try:
            dll_inject.inject(dll_path=dll_path)
            _dbg("DLL injected successfully")
        except Exception as e:
            _dbg(f"injection failed: {e}")

        await bot.sleep(2)

        for attempt in range(30):
            if bridge.connect():
                _dbg(f"pipe connected on attempt {attempt+1}")
                connected = True
                break
            if attempt % 5 == 0:
                _dbg(f"pipe connect attempt {attempt+1}...")
            await bot.sleep(CONNECT_RETRY)

    if not connected:
        _dbg("FAILED to connect to pipe")
        return

    # Send init command with player_id
    bridge.send_command({"cmd": "init", "player_id": bot.player_id})
    _dbg(f"sent init command with player_id={bot.player_id}")

    # Access game_state directly for authoritative updates
    state = sys.modules["__main__"].state
    gs = state.game_state

    poll_count = 0
    null_count = 0
    empty_count = 0

    try:
        while bot.is_connected:
            creatures = bridge.read_creatures()
            poll_count += 1

            if creatures is None:
                null_count += 1
            elif len(creatures) == 0:
                empty_count += 1
            else:
                _dbg(f"poll#{poll_count}: GOT {len(creatures)} creatures: {creatures}")

            # Log summary every 30 polls (~10s)
            if poll_count % 30 == 0:
                _dbg(f"poll#{poll_count}: null={null_count} empty={empty_count} gs.creatures={len(gs.creatures)}")
                # Auto-reconnect if pipe seems stale (all nulls)
                if null_count >= 30:
                    _dbg("pipe stale — reconnecting")
                    bridge.disconnect()
                    await bot.sleep(2)
                    if bridge.connect():
                        bridge.send_command({"cmd": "init", "player_id": bot.player_id})
                        _dbg("reconnected successfully")
                    else:
                        _dbg("reconnect failed — will retry next cycle")
                null_count = 0
                empty_count = 0

            now = time.time()
            if creatures is not None:
                # Get player position from packet-based game state (reliable)
                px, py, pz = gs.position if gs.position else (0, 0, 0)

                dll_creatures = {}
                raw_count = 0
                for c in creatures:
                    cid = c.get("id", 0)
                    if cid == 0:
                        continue
                    # Only accept valid OT creature IDs
                    if not ((0x10000000 <= cid <= 0x1FFFFFFF) or (0x40000000 <= cid <= 0x4FFFFFFF)):
                        continue
                    raw_count += 1
                    cx, cy, cz = c.get("x", 0), c.get("y", 0), c.get("z", 0)
                    # Use player's own creature to keep gs.position updated
                    if cid == bot.player_id:
                        if 0 < cx < 65535 and 0 < cy < 65535 and cz < 16:
                            if gs.position != (cx, cy, cz):
                                old = gs.position
                                gs.position = (cx, cy, cz)
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
            else:
                # Null response — refresh timestamps on existing DLL creatures
                # so the packet scanner doesn't prune them as stale
                for cid, info in gs.creatures.items():
                    if info.get("source") == "dll":
                        info["t"] = now

            await bot.sleep(POLL_INTERVAL)
    finally:
        # Just disconnect — don't send "stop" so the DLL pipe thread stays
        # alive and can accept a new client on next restart
        bridge.disconnect()
        _dbg("stopped (pipe thread kept alive)")
