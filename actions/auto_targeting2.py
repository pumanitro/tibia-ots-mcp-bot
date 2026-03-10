"""Auto-targeting v2: highest-HP-first targeting with force_target support.

Targets the highest-HP alive monster first (to maximize AOE lure value),
with nearest distance as tiebreaker. Only targets creatures with valid
positions (not cached/despawned). Respects force_target override from
body-block handler.

Two-phase attack (same as v1):
  1. DLL game_attack -> direct memory write to m_attackingCreature -> red square
  2. Proxy inject    -> ATTACK packet -> network combat (server-side targeting)
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constants import MONSTER_ID_MIN
from protocol import build_attack_packet

INTERVAL = 0.05  # 50ms loop
MAX_AGE = 60


def _pick_target(creatures, px, py, pz, gs):
    """Select best target: force_target > highest HP > nearest.

    Returns creature_id or None.
    """
    now = time.time()

    # Force target override (body-block last resort)
    ft = gs.force_target
    if ft is not None:
        info = creatures.get(ft)
        if info and 0 < info.get("health", 0) <= 100:
            return ft
        # Force target dead or gone — clear it
        gs.force_target = None

    candidates = []
    for cid, info in creatures.items():
        if cid < MONSTER_ID_MIN:
            continue
        hp = info.get("health", 0)
        if hp <= 0 or hp > 100:
            continue
        age = now - info.get("t", 0)
        if age > MAX_AGE:
            continue
        # Skip unreachable creatures
        if cid in gs.unreachable_creatures and gs.unreachable_creatures[cid] > now:
            continue
        x = info.get("x", 0)
        y = info.get("y", 0)
        z = info.get("z", 0)
        has_pos = not (x == 0 and y == 0)
        same_z = (z == pz) if has_pos else True  # trust creatures without pos
        if has_pos and not same_z:
            continue  # skip creatures confirmed on different floor
        dist = max(abs(x - px), abs(y - py)) if has_pos else 999
        candidates.append((cid, hp, dist, has_pos))

    if not candidates:
        return None

    # Sort: prefer creatures with valid position, then highest HP, then nearest
    candidates.sort(key=lambda c: (not c[3], -c[1], c[2]))
    return candidates[0][0]


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
                    bot.log("[TARGET2] lure_active=True, cancelling target")
                    bridge.send_command({"cmd": "game_cancel_attack"})
                    gs.attack_target = None
                    gs.attack_target_id = 0
                    last_target = None
                await bot.sleep(INTERVAL)
                continue

            now = time.time()
            px, py, pz = gs.position if gs.position else (0, 0, 0)

            target = None
            if px > 0 and py > 0:
                target = _pick_target(gs.creatures, px, py, pz, gs)

            if target is not None:
                new_target = target != last_target
                if new_target:
                    info = gs.creatures.get(target, {})
                    dist = max(abs(info.get("x", 0) - px),
                               abs(info.get("y", 0) - py))
                    bot.log(f"[TARGET2] attacking {info.get('name', '?')} "
                            f"(0x{target:08X}) hp={info.get('health', '?')}% "
                            f"dist={dist} [highest-HP]")
                    last_target = target

                # Phase 1: DLL writes creature pointer (red square)
                if new_target:
                    bridge.send_command({"cmd": "game_attack", "creature_id": target})
                    if proxy and proxy.logged_in:
                        await bot.inject_to_server(build_attack_packet(target))
                    last_send_time = now
                # Resend both DLL + network every 500ms
                elif (now - last_send_time) >= RESEND_INTERVAL:
                    bridge.send_command({"cmd": "game_attack", "creature_id": target})
                    if proxy and proxy.logged_in:
                        await bot.inject_to_server(build_attack_packet(target))
                    last_send_time = now

                # Publish target for other actions
                gs.attack_target = target
                gs.attack_target_id = target
            else:
                if last_target is not None:
                    bridge.send_command({"cmd": "game_cancel_attack"})
                    gs.attack_target = None
                    gs.attack_target_id = 0
                last_target = None

        await bot.sleep(INTERVAL)
