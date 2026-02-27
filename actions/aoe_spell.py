"""Cast 'Inner Flame' when 2+ monsters are within 1 sq of the player."""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constants import MONSTER_ID_MIN

SPELL = "Inner Flame"
INTERVAL = 0.2       # 200ms polling
MIN_CREATURES = 2    # minimum monsters in range
MAX_DISTANCE = 1     # Chebyshev distance (exactly 1 square)
MAX_AGE = 60         # ignore stale creature data

async def run(bot):
    state = sys.modules["__main__"].state
    gs = state.game_state

    while True:
        if bot.is_connected and bot.max_hp > 0:
            pos = gs.position
            if pos and pos[0] > 0 and pos[1] > 0:
                px, py, pz = pos
                now = time.time()
                # Periodic ID diagnostic
                if int(now) % 15 == 0 and gs.creatures:
                    ids = sorted(gs.creatures.keys())
                    bot.log(f"[AOE DIAG] {len(ids)} creatures: "
                            + " ".join(f"0x{c:X}({gs.creatures[c].get('name','?')[:8]})" for c in ids[:8]))
                count = 0
                for cid, info in gs.creatures.items():
                    if (cid >= MONSTER_ID_MIN
                            and 0 < info.get("health", 0) <= 100
                            and info.get("z") == pz
                            and now - info.get("t", 0) < MAX_AGE):
                        dist = max(abs(info.get("x", 0) - px),
                                   abs(info.get("y", 0) - py))
                        if dist <= MAX_DISTANCE:
                            count += 1
                if count >= MIN_CREATURES:
                    await bot.say(SPELL)
                    bot.log(f"Cast {SPELL} ({count} monsters within {MAX_DISTANCE} sq)")
        await bot.sleep(INTERVAL)
