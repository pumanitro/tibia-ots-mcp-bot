"""Power Up every 1 second for healing (only when HP < 99%)."""

INTERVAL = 1  # seconds between power ups
HP_THRESHOLD = 0.99  # only heal below this percentage


async def run(bot):
    while True:
        if bot.is_connected and bot.max_hp > 0:
            hp_pct = bot.hp / bot.max_hp
            if hp_pct < HP_THRESHOLD:
                await bot.say("power up")
                bot.log(f"Said 'power up' (HP {bot.hp}/{bot.max_hp} = {hp_pct:.0%})")
        await bot.sleep(INTERVAL)
