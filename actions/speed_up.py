"""Cast 'speed up' when not hasted (checks player icons bitmask)."""
import sys

INTERVAL = 1  # seconds between checks
HASTE_ICON_BIT = 0x0002  # bit 1 — empirically verified on DBVictory


def _get_game_state():
    return sys.modules["__main__"].state.game_state


async def run(bot):
    while True:
        if bot.is_connected and bot.max_hp > 0:
            gs = _get_game_state()
            icons = getattr(gs, 'player_icons', 0)
            is_hasted = (icons & HASTE_ICON_BIT) != 0
            if not is_hasted:
                await bot.say("speed up")
                bot.log(f"Cast 'speed up' (icons=0x{icons:04X})")
        await bot.sleep(INTERVAL)
