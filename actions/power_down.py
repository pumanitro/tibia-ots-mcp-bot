"""Say 'power down' every 1 second."""


async def run(bot):
    while True:
        if bot.is_connected:
            await bot.say("power down")
        await bot.sleep(1)
