"""Sniff USE_ITEM packets from client to discover item IDs. Enable, then use an item in-game."""
import sys, os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import ClientOpcode

SNIFF_LOG = Path(__file__).parent.parent / "sniff_log.txt"


async def run(bot):
    # Access the live state from the running main module
    main = sys.modules["__main__"]
    proxy = main.state.game_proxy
    original_cb = proxy.on_client_packet

    def sniffer(opcode, reader):
        if opcode == ClientOpcode.USE_ITEM:
            try:
                pos = reader.read_position()
                item_id = reader.read_u16()
                stack_pos = reader.read_u8()
                line = f"USE_ITEM pos={pos} item_id={item_id} stack={stack_pos}\n"
                bot.log(f"[SNIFF] {line.strip()}")
                with open(SNIFF_LOG, "a") as f:
                    f.write(line)
            except Exception as e:
                with open(SNIFF_LOG, "a") as f:
                    f.write(f"ERROR: {e}\n")
        if original_cb:
            original_cb(opcode, reader)

    proxy.on_client_packet = sniffer
    with open(SNIFF_LOG, "w") as f:
        f.write("--- Sniffer started ---\n")
    bot.log("[SNIFF] Installed — use an item in-game to capture its ID")

    try:
        while True:
            await bot.sleep(60)
    except BaseException:
        proxy.on_client_packet = original_cb
        bot.log("[SNIFF] Removed")
