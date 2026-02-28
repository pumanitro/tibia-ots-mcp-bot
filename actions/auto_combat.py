"""Auto combat: 2+ monsters nearby → Inner Flame, else Kiaiho (mana>=5%) or rune 3165 on target.

Replaces auto_rune_and_spell + aoe_spell with a single unified action.

Priority each tick (200ms):
  1. AOE   — 2+ alive monsters within 1 sq (Chebyshev) → cast 'Inner Flame'
  2. Spell — has attack target and mana >= 5% → cast 'Kiaiho'
  3. Rune  — has attack target and mana < 5% → use rune 3165 on target

Target is read from gs.attack_target (set by auto_targeting) with fallback to
proxy ATTACK packet callback for manual/DLL attacks. Creature proximity uses
gs.creatures dict, filtered by MONSTER_ID_MIN, health > 0, same Z level,
and data freshness < 60s.
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import ClientOpcode, build_use_on_creature_packet
from constants import ITEM_RUNE_3165, MONSTER_ID_MIN

INTERVAL = 0.2

# AOE settings
AOE_SPELL = "Inner Flame"
AOE_MIN_CREATURES = 2
AOE_MAX_DISTANCE = 1  # Chebyshev distance (1 square)
AOE_MAX_AGE = 60      # ignore stale creature data (seconds)

# Single-target settings
SPELL_TEXT = "Kiaiho"
SPELL_MANA_PCT = 5  # cast spell when mana >= 5%
RUNE_ID = ITEM_RUNE_3165


def _count_nearby_monsters(gs, now):
    """Count alive monsters within AOE_MAX_DISTANCE of the player."""
    pos = gs.position
    if not pos or pos[0] <= 0 or pos[1] <= 0:
        return 0
    px, py, pz = pos
    count = 0
    for cid, info in gs.creatures.items():
        if (cid >= MONSTER_ID_MIN
                and 0 < info.get("health", 0) <= 100
                and info.get("z") == pz
                and now - info.get("t", 0) < AOE_MAX_AGE):
            dist = max(abs(info.get("x", 0) - px),
                       abs(info.get("y", 0) - py))
            if dist <= AOE_MAX_DISTANCE:
                count += 1
    return count


async def run(bot):
    state = sys.modules["__main__"].state
    proxy = state.game_proxy
    gs = state.game_state
    callback_target = [None]

    def track_target(opcode, reader):
        if opcode == ClientOpcode.ATTACK:
            try:
                cid = reader.read_u32()
                callback_target[0] = cid if cid != 0 else None
            except Exception:
                pass

    proxy.register_client_packet_callback(track_target)
    bot.log("[COMBAT] Started — AOE > Spell > Rune")

    try:
        while True:
            if bot.is_connected and state.game_proxy:
                # Respect lure mode — only fight when cavebot allows it
                if gs.lure_active:
                    await bot.sleep(INTERVAL)
                    continue

                now = time.time()
                nearby = _count_nearby_monsters(gs, now)

                # Priority 1: AOE when 2+ monsters close
                if nearby >= AOE_MIN_CREATURES:
                    await bot.say(AOE_SPELL)
                else:
                    # Priority 2 & 3: single-target spell or rune
                    cid = getattr(gs, "attack_target", None) or callback_target[0]
                    if cid:
                        creature = gs.creatures.get(cid)
                        if not creature or creature.get("health", 0) > 0:
                            mana_pct = (gs.mana / gs.max_mana * 100) if gs.max_mana > 0 else 100
                            if mana_pct >= SPELL_MANA_PCT:
                                await bot.say(SPELL_TEXT)
                            else:
                                pkt = build_use_on_creature_packet(
                                    0xFFFF, 0, 0, RUNE_ID, 0, cid)
                                await bot.inject_to_server(pkt)

            await bot.sleep(INTERVAL)
    except BaseException:
        proxy.unregister_client_packet_callback(track_target)
        bot.log("[COMBAT] Stopped")
