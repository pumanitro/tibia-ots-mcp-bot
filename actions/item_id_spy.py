"""Debug tool: shows 'Item Id: XXXXX' in the status bar when you use or look at an item."""
import sys
import os
import asyncio
import struct

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import ClientOpcode, PacketReader, PacketWriter, ServerOpcode

# Probe: try msg_types 23-30 to find the status bar type (12-22 showed in chat, not status bar).
# Once you see the right one, set MSG_TYPE and set PROBE_MODE = False.
PROBE_MODE = False
MSG_TYPE = 26  # status bar (same line as "You are full.")


def _build_text_message(msg_type: int, text: str) -> bytes:
    pw = PacketWriter()
    pw.write_u8(ServerOpcode.TEXT_MESSAGE)
    pw.write_u8(msg_type)
    pw.write_string(text)
    return pw.data


async def run(bot):
    main = sys.modules["__main__"]
    state = main.state
    proxy = state.game_proxy
    original_cb = proxy.on_client_packet
    loop = asyncio.get_event_loop()
    def show_item_id(item_id):
        """Inject text message to client and log it."""
        bot.log(f"Item Id: {item_id}")
        payload = _build_text_message(MSG_TYPE, f"Item Id: {item_id}")
        loop.create_task(proxy.inject_to_client(payload))

    def spy(opcode, reader):
        try:
            saved_pos = reader._pos
            raw = reader._data[saved_pos:]

            # USE_ITEM (0x82): pos(5) + item_id(2) + stack(1) + index(1)
            if opcode == ClientOpcode.USE_ITEM and len(raw) >= 8:
                item_id = struct.unpack_from('<H', raw, 5)[0]
                show_item_id(item_id)

            # LOOK (0x8C): pos(5) + item_id(2) + stack(1)
            elif opcode == ClientOpcode.LOOK and len(raw) >= 7:
                item_id = struct.unpack_from('<H', raw, 5)[0]
                show_item_id(item_id)

        except Exception as e:
            bot.log(f"[SPY] Error: {e}")

        # Chain to original callback with a fresh reader
        if original_cb:
            fresh_reader = PacketReader(reader._data[reader._pos:])
            original_cb(opcode, fresh_reader)

    proxy.on_client_packet = spy
    bot.log("[SPY] Item ID spy installed - use or look at items to see their ID")

    try:
        while True:
            await bot.sleep(60)
    except BaseException:
        proxy.on_client_packet = original_cb
        bot.log("[SPY] Item ID spy removed")
