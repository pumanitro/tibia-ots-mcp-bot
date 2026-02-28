"""Auto-targeting: targets nearest alive monster.

Two-phase attack:
  1. DLL game_attack → direct memory write to m_attackingCreature → red square
  2. Proxy inject    → ATTACK packet → network combat (server-side targeting)

Phase 1 writes the creature pointer directly to game memory (Fix 24: no Game::attack()
call, no Lua VM involvement, zero crash risk). The game's render loop reads this field
every frame to draw the red square border.
Phase 2 tells the server to start combat so spells/runes can target the creature.
"""
import sys
import os
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constants import MONSTER_ID_MIN
from protocol import build_attack_packet

INTERVAL = 0.05  # 50ms loop
MONSTER_MIN = MONSTER_ID_MIN
MAX_AGE = 60


async def run(bot):
    state = sys.modules["__main__"].state
    gs = state.game_state
    proxy = state.game_proxy

    last_target = None
    last_send_time = 0.0
    RESEND_INTERVAL = 0.5  # re-send same target every 500ms

    while True:
        if bot.is_connected:
            bridge = getattr(gs, "dll_bridge", None)
            if bridge is None:
                await bot.sleep(1)
                continue

            # Lure mode: suppress targeting while cavebot is luring
            if gs.lure_active:
                if last_target is not None:
                    bridge.send_command({"cmd": "game_cancel_attack"})
                    gs.attack_target = None
                    gs.attack_target_id = 0
                    last_target = None
                await bot.sleep(INTERVAL)
                continue

            now = time.time()
            px, py, pz = gs.position if gs.position else (0, 0, 0)

            # Log all creature IDs periodically to diagnose ID range issues
            if int(now) % 10 == 0 and now - last_send_time > 2 and gs.creatures:
                all_ids = sorted(gs.creatures.keys())
                below = [f"0x{c:08X}({gs.creatures[c].get('name','?')})" for c in all_ids if c < MONSTER_MIN]
                above = [f"0x{c:08X}({gs.creatures[c].get('name','?')})" for c in all_ids if c >= MONSTER_MIN]
                bot.log(f"[DIAG] creatures={len(all_ids)} "
                        f"below_threshold({len(below)})={below[:5]} "
                        f"above_threshold({len(above)})={above[:5]}")

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
                new_target = target != last_target
                if new_target:
                    dist = max(abs(monsters[target].get("x", 0) - px),
                               abs(monsters[target].get("y", 0) - py))
                    bot.log(f"attacking {monsters[target].get('name','?')} "
                            f"(0x{target:08X}) hp={monsters[target]['health']}% "
                            f"dist={dist}")
                    last_target = target
                # Phase 1: DLL writes creature pointer to m_attackingCreature (red square).
                # Fix 24: Memory write is always safe (no Lua VM), so resend on every cycle.
                if new_target:
                    bridge.send_command({"cmd": "game_attack", "creature_id": target})
                    if proxy and proxy.logged_in:
                        await bot.inject_to_server(build_attack_packet(target))
                    last_send_time = now
                # Resend both DLL + network every 500ms (DLL re-write is safe & idempotent)
                elif (now - last_send_time) >= RESEND_INTERVAL:
                    bridge.send_command({"cmd": "game_attack", "creature_id": target})
                    if proxy and proxy.logged_in:
                        await bot.inject_to_server(build_attack_packet(target))
                    last_send_time = now

                # Publish target for other actions (auto_rune_and_spell, aoe_spell)
                # and cavebot pause-on-monster
                gs.attack_target = target
                gs.attack_target_id = target
            else:
                if last_target is not None:
                    # Clear red square in game UI
                    bridge.send_command({"cmd": "game_cancel_attack"})
                    gs.attack_target = None
                    gs.attack_target_id = 0
                last_target = None

        await bot.sleep(INTERVAL)
