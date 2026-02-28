"""Mana Recovery: uses Senzu (item 8465) when mana drops to 70% or below."""
import sys
import os
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import build_use_item_packet

SENZU_ID = 8465
MANA_THRESHOLD = 70  # use Senzu when mana <= 70 (percentage-based)
INTERVAL = 0.2
COOLDOWN = 1.0  # min seconds between uses
STATE_LOG_INTERVAL = 5.0  # log state every 5s for debugging


async def run(bot):
    last_use = 0.0
    last_state_log = 0.0
    while True:
        try:
            if bot.is_connected:
                now = time.time()
                mp = bot.mana

                # Periodic state log for debugging
                if (now - last_state_log) >= STATE_LOG_INTERVAL:
                    bot.log(f"Mana Recovery: state MP={mp} connected={bot.is_connected}")
                    last_state_log = now

                if mp <= MANA_THRESHOLD and (now - last_use) >= COOLDOWN:
                    pkt = build_use_item_packet(0xFFFF, 0, 0, SENZU_ID, 0, 0)
                    await bot.inject_to_server(pkt)
                    last_use = now
                    bot.log(f"Mana Recovery: used Senzu (MP={mp})")
        except Exception as e:
            bot.log(f"Mana Recovery ERROR: {e}")
            traceback.print_exc()
        await bot.sleep(INTERVAL)
