"""Auto Senzu: uses item 8465 (senzu bean) when HP drops below 50%."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import build_use_item_packet

SENZU_ID = 8465
HP_THRESHOLD = 0.50
INTERVAL = 0.2  # seconds between checks


async def run(bot):
    while True:
        if bot.is_connected and bot.max_hp > 0:
            hp_pct = bot.hp / bot.max_hp
            if hp_pct < HP_THRESHOLD:
                pkt = build_use_item_packet(0xFFFF, 0, 0, SENZU_ID, 0, 0)
                await bot.inject_to_server(pkt)
                bot.log(f"Used Senzu (HP {bot.hp}/{bot.max_hp} = {hp_pct:.0%})")
        await bot.sleep(INTERVAL)
