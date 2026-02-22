"""Auto-targeting: targets nearest alive monster using DLL internal attack call."""
import sys
import os
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

INTERVAL = 0.1  # 100ms loop (map scan provides data every 100ms)
MONSTER_MIN = 0x40000000  # OT creature ID range: monsters start at 0x40000000
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
            px, py, pz = gs.position if gs.position else (0, 0, 0)

            monsters = {
                cid: info for cid, info in gs.creatures.items()
                if cid >= MONSTER_MIN
                and 0 < info.get("health", 0) <= 100
                and now - info.get("t", 0) < MAX_AGE
            }

            target = None
            if monsters and px > 0 and py > 0:
                target = min(monsters, key=lambda cid: max(
                    abs(monsters[cid].get("x", 0) - px),
                    abs(monsters[cid].get("y", 0) - py),
                ))

            if target is not None:
                if target != last_target:
                    dist = max(abs(monsters[target].get("x", 0) - px),
                               abs(monsters[target].get("y", 0) - py))
                    bot.log(f"attacking {monsters[target].get('name','?')} "
                            f"(0x{target:08X}) hp={monsters[target]['health']}% "
                            f"dist={dist}")
                    # DLL calls Game::attack (UI) + sendAttackCreature (network).
                    # The network packet flows through the proxy, so other actions
                    # (e.g. auto_rune) can intercept the ATTACK opcode.
                    bridge.send_command({"cmd": "game_attack", "creature_id": target})
                    last_target = target
            else:
                last_target = None

        await bot.sleep(INTERVAL)
