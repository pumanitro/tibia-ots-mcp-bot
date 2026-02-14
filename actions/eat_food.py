"""Eat food from backpack every 10 seconds."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import build_use_item_packet


async def run(bot):
    ITEM_ID = 3583        # red ham on DBVictory
    CONTAINER = 65        # 0x41 = backpack with food
    SLOT = 1              # slot in that container
    INTERVAL = 10         # seconds between eats

    while True:
        if bot.is_connected:
            # pos=(0xFFFF, container, slot), item_id, stack_pos=slot, index=0
            pkt = build_use_item_packet(0xFFFF, CONTAINER, SLOT, ITEM_ID, SLOT, 0)
            await bot.inject_to_server(pkt)
            bot.log(f"Ate food (item {ITEM_ID})")
        await bot.sleep(INTERVAL)
