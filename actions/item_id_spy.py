"""Debug tool: shows 'Item Id: XXXXX' in the status bar when you use or look at an item."""
import sys
import os
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import ClientOpcode, PacketWriter, ServerOpcode

MSG_TYPE = 26  # status bar (same line as "You are full.")


def _build_text_message(msg_type: int, text: str) -> bytes:
    pw = PacketWriter()
    pw.write_u8(ServerOpcode.TEXT_MESSAGE)
    pw.write_u8(msg_type)
    pw.write_string(text)
    return pw.data


async def run(bot):
    state = sys.modules["__main__"].state
    proxy = state.game_proxy
    loop = asyncio.get_running_loop()

    def show_item_id(item_id):
        """Inject text message to client and log it."""
        bot.log(f"Item Id: {item_id}")
        payload = _build_text_message(MSG_TYPE, f"Item Id: {item_id}")
        try:
            loop.create_task(proxy.inject_to_client(payload))
        except Exception:
            pass

    def spy(opcode, reader):
        try:
            # USE_ITEM (0x82): pos(5) + item_id(2) + stack(1) + index(1)
            if opcode == ClientOpcode.USE_ITEM:
                reader.read_position()  # skip pos
                item_id = reader.read_u16()
                show_item_id(item_id)

            # LOOK (0x8C): pos(5) + item_id(2) + stack(1)
            elif opcode == ClientOpcode.LOOK:
                reader.read_position()  # skip pos
                item_id = reader.read_u16()
                show_item_id(item_id)

        except Exception as e:
            bot.log(f"[SPY] Error: {e}")

    proxy.register_client_packet_callback(spy)
    bot.log("[SPY] Item ID spy installed - use or look at items to see their ID")

    try:
        while True:
            await bot.sleep(60)
    except BaseException:
        proxy.unregister_client_packet_callback(spy)
        bot.log("[SPY] Item ID spy removed")
