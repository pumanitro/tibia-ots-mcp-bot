"""Auto-rune & spell: casts Kiaiho when mana >= 5%, otherwise uses rune 3165 on target."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import ClientOpcode, build_use_on_creature_packet
from constants import ITEM_RUNE_3165

RUNE_ID = ITEM_RUNE_3165
INTERVAL = 1.0  # seconds between casts
SPELL_TEXT = "Kiaiho"
SPELL_MANA_PCT_THRESHOLD = 5  # cast spell when mana >= 5%


async def run(bot):
    state = sys.modules["__main__"].state
    proxy = state.game_proxy
    gs = state.game_state
    current_target = [None]

    def track_target(opcode, reader):
        """Watch ATTACK packets to track the current target.

        The DLL's game_attack calls Game::attack() (UI red square) AND
        sendAttackCreature() (network packet).  In DBVictory, Game::attack()
        only handles UI — sendAttackCreature() is required for the network
        packet to exist.  The packet flows through the proxy, so we intercept
        it here.
        """
        if opcode == ClientOpcode.ATTACK:
            try:
                cid = reader.read_u32()
                old = current_target[0]
                current_target[0] = cid if cid != 0 else None
                if current_target[0] != old:
                    if current_target[0]:
                        bot.log(f"target set: 0x{cid:08X}")
                    else:
                        bot.log("target cleared")
            except Exception:
                pass

    proxy.register_client_packet_callback(track_target)
    bot.log("[RUNE] Tracking attack target, will use rune on targeted creature")

    try:
        while True:
            if bot.is_connected and state.game_proxy and current_target[0]:
                cid = current_target[0]
                # Verify target is still alive before wasting a rune/spell
                creature = gs.creatures.get(cid)
                if creature and creature.get("health", 0) > 0:
                    mana_pct = (gs.mana / gs.max_mana * 100) if gs.max_mana > 0 else 100
                    if mana_pct >= SPELL_MANA_PCT_THRESHOLD:
                        await bot.say(SPELL_TEXT)
                    else:
                        pkt = build_use_on_creature_packet(0xFFFF, 0, 0, RUNE_ID, 0, cid)
                        await bot.inject_to_server(pkt)
            await bot.sleep(INTERVAL)
    except BaseException:
        proxy.unregister_client_packet_callback(track_target)
        bot.log("[RUNE] Stopped")
