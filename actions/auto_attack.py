"""Auto-attack: targets nearest alive monster every tick."""
import time
from pathlib import Path
from protocol import build_attack_packet

INTERVAL = 1  # seconds between re-target attempts
LOG_FILE = Path(__file__).parent.parent / "auto_attack_debug.txt"

MONSTER_MIN = 0x40000000  # monsters start at 0x40000000
MAX_AGE = 60  # only target creatures seen in the last N seconds


def _debug(msg):
    with open(LOG_FILE, "a") as f:
        f.write(msg + "\n")


async def run(bot):
    with open(LOG_FILE, "w") as f:
        f.write("=== auto_attack started ===\n")

    last_target = None
    tick = 0
    while True:
        if bot.is_connected:
            creatures = bot.creatures
            my_id = bot.player_id
            tick += 1
            now = time.time()

            # Find alive monsters only, recently seen
            monsters = {
                cid: info for cid, info in creatures.items()
                if cid >= MONSTER_MIN
                and cid != my_id
                and 0 < info.get("health", 0) <= 100
                and now - info.get("t", 0) < MAX_AGE
            }

            if tick % 5 == 0:
                _debug(f"tick={tick} monsters={len(monsters)} "
                       f"ids={[(cid, info['health']) for cid, info in list(monsters.items())[:8]]}")

            # Pick the monster with lowest HP (most likely already in combat)
            target = None
            if monsters:
                target = min(monsters, key=lambda cid: monsters[cid]["health"])

            if target is not None:
                pkt = build_attack_packet(target)
                if target != last_target:
                    _debug(f"NEW TARGET creature={target} hp={monsters[target]['health']}%")
                await bot.inject_to_server(pkt)
                last_target = target
            else:
                if last_target is not None:
                    _debug("no alive monsters nearby")
                last_target = None
        await bot.sleep(INTERVAL)
