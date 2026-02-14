"""Eat food every 10 seconds. Uses hotkey-style packet — works from any backpack/slot."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import build_use_item_packet

FOOD_ID = 3583    # red ham on DBVictory
INTERVAL = 10     # seconds between eats


async def run(bot):
    while True:
        if bot.is_connected:
            # Hotkey-style: pos=(0xFFFF, 0, 0) — server finds the item automatically
            pkt = build_use_item_packet(0xFFFF, 0, 0, FOOD_ID, 0, 0)
            await bot.inject_to_server(pkt)
            bot.log(f"Ate food (item {FOOD_ID})")
        await bot.sleep(INTERVAL)
