"""Say 'power down' every 100ms."""


async def run(bot):
    while True:
        if bot.is_connected:
            await bot.say("power down")
        await bot.sleep(0.1)
