"""Power Up every 1 second for healing."""

INTERVAL = 1  # seconds between power ups


async def run(bot):
    while True:
        if bot.is_connected:
            await bot.say("power up")
            bot.log("Said 'power up'")
        await bot.sleep(INTERVAL)
