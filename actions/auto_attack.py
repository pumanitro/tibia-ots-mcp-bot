"""Auto-attack: targets nearest alive monster using DLL internal attack call."""
import sys
import os
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

INTERVAL = 0.2  # 200ms loop
MONSTER_MIN = 0x40000000
MAX_AGE = 60


async def run(bot):
    state = sys.modules["__main__"].state
    gs = state.game_state

    last_target = None
    while True:
        if bot.is_connected:
            bridge = getattr(gs, "dll_bridge", None)
            if bridge is None:
                await bot.sleep(1)
                continue

            now = time.time()
            monsters = {
                cid: info for cid, info in gs.creatures.items()
                if cid >= MONSTER_MIN
                and 0 < info.get("health", 0) <= 100
                and now - info.get("t", 0) < MAX_AGE
            }

            target = None
            if monsters:
                target = min(monsters, key=lambda cid: monsters[cid]["health"])

            if target is not None:
                if target != last_target:
                    bot.log(f"attacking {monsters[target].get('name','?')} "
                            f"(0x{target:08X}) hp={monsters[target]['health']}%")
                bridge.send_command({"cmd": "game_attack", "creature_id": target})
                last_target = target
            else:
                last_target = None

        await bot.sleep(INTERVAL)
