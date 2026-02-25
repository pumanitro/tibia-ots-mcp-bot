"""Say 'transform' once after login, then stop."""
import sys

MAX_RETRIES = 5
RETRY_DELAY = 0.5  # seconds between retries


def _proxy_ready():
    """Check that the game proxy is fully ready to inject packets."""
    st = sys.modules["__main__"].state
    gp = st.game_proxy
    return (gp is not None and gp.logged_in
            and gp.xtea_keys is not None
            and gp.server_writer is not None)


async def run(bot):
    # Wait until proxy can inject packets (no HP check — avoids stuck loop
    # when PLAYER_STATS packet hasn't been parsed yet)
    while not bot.is_connected or not _proxy_ready():
        await bot.sleep(0.2)

    for attempt in range(1, MAX_RETRIES + 1):
        if not _proxy_ready():
            bot.log(f"Attempt {attempt}/{MAX_RETRIES}: proxy not ready, waiting...")
            await bot.sleep(RETRY_DELAY)
            continue
        try:
            bot.log(f"Saying 'transform' (attempt {attempt}/{MAX_RETRIES})...")
            await bot.say("transform")
            bot.log(f"Say queued OK — check proxy logs for 'Injected to server: opcode=0x96'")
        except Exception as e:
            bot.log(f"say() FAILED: {e} (attempt {attempt}/{MAX_RETRIES})")
            await bot.sleep(RETRY_DELAY)
            continue
        break
    else:
        bot.log("Failed to say 'transform' after all retries")
