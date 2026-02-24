"""Say 'transform' once after login, then stop."""


async def run(bot):
    # Wait until we're connected and have valid player data
    while not bot.is_connected or bot.max_hp <= 0:
        await bot.sleep(0.5)

    await bot.sleep(1)  # small delay to let the game settle
    await bot.say("transform")
    bot.log("Cast 'transform' (one-shot)")
