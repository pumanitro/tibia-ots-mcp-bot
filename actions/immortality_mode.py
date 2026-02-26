"""Immortality Mode — emergency server disconnect when HP drops critically low.

When HP falls below 20%, the proxy drops the server-side TCP connection.
The server thinks the player disconnected — monsters lose aggro for ~30s.
After a brief pause the proxy reconnects by replaying the saved login packet.
"""
import sys
import os
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HP_THRESHOLD = 0.20       # trigger at 20% HP
POLL_INTERVAL = 0.1       # check HP every 100ms — must be fast for survival
DISCONNECT_WAIT = 1.5     # seconds before reconnecting
RECONNECT_TIMEOUT = 10    # seconds to wait for logged_in after reconnect
MAX_RETRIES = 3
COOLDOWN = 30             # seconds after reconnect before next trigger


async def run(bot):
    state = sys.modules["__main__"].state
    proxy = state.game_proxy

    # Wait for connection and saved login packet
    while not bot.is_connected:
        await bot.sleep(1)

    while proxy._saved_login_packet is None:
        bot.log("Waiting for login packet to be captured...")
        await bot.sleep(2)

    bot.log(f"Immortality Mode active — disconnect at <{HP_THRESHOLD:.0%} HP")
    triggers = 0

    try:
        while True:
            await bot.sleep(POLL_INTERVAL)

            if not bot.is_connected or bot.max_hp <= 0:
                continue

            hp_pct = bot.hp / bot.max_hp
            if hp_pct >= HP_THRESHOLD:
                continue

            # HP critical — trigger emergency disconnect
            triggers += 1
            bot.log(f"[#{triggers}] HP CRITICAL: {bot.hp}/{bot.max_hp} ({hp_pct:.0%}) — disconnecting!")

            await proxy.ghost_disconnect()
            bot.log(f"[#{triggers}] Server disconnected — monsters losing aggro")

            await bot.sleep(DISCONNECT_WAIT)

            # Reconnect with retry
            success = False
            for attempt in range(1, MAX_RETRIES + 1):
                bot.log(f"[#{triggers}] Reconnecting (attempt {attempt}/{MAX_RETRIES})...")
                if await proxy.ghost_reconnect():
                    success = True
                    break
                await bot.sleep(1)

            if not success:
                bot.log(f"[#{triggers}] All reconnect attempts FAILED")
                continue

            # Wait for server to respond
            elapsed = 0.0
            while not proxy.logged_in and elapsed < RECONNECT_TIMEOUT:
                await bot.sleep(0.2)
                elapsed += 0.2

            if proxy.logged_in:
                bot.log(f"[#{triggers}] Reconnected ({elapsed:.1f}s) — cooldown {COOLDOWN}s")
            else:
                bot.log(f"[#{triggers}] Reconnect timeout — logged_in still False")

            # Cooldown — don't re-trigger immediately after reconnect
            await bot.sleep(COOLDOWN)

    except asyncio.CancelledError:
        if not proxy.logged_in and proxy._saved_login_packet is not None:
            bot.log("Cancelled during disconnect — scheduling emergency reconnect")

            async def _emergency_reconnect():
                try:
                    if await proxy.ghost_reconnect():
                        for _ in range(50):
                            if proxy.logged_in:
                                break
                            await asyncio.sleep(0.2)
                except Exception:
                    pass
            asyncio.ensure_future(_emergency_reconnect())
        raise
