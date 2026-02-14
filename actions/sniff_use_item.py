"""Sniff ALL client packets to discover what hotkeys send. Enable, then press the hotkey."""
import sys, os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import ClientOpcode

SNIFF_LOG = Path(__file__).parent.parent / "sniff_log.txt"


async def run(bot):
    main = sys.modules["__main__"]
    proxy = main.state.game_proxy
    original_cb = proxy.on_client_packet

    def sniffer(opcode, reader):
        try:
            try:
                name = ClientOpcode(opcode).name
            except ValueError:
                name = "?"

            # Save reader position to read raw bytes
            raw = reader.read_bytes(reader.remaining) if reader.remaining > 0 else b""
            hex_dump = raw.hex(" ") if raw else "(empty)"

            line = f"0x{opcode:02X} ({name}) [{len(raw)} bytes] {hex_dump}\n"
            bot.log(f"[SNIFF] {line.strip()}")
            with open(SNIFF_LOG, "a") as f:
                f.write(line)

            # Also parse known packet types for readability
            if opcode == ClientOpcode.USE_ITEM and len(raw) >= 8:
                import struct
                x = struct.unpack_from('<H', raw, 0)[0]
                y = struct.unpack_from('<H', raw, 2)[0]
                z = raw[4]
                item_id = struct.unpack_from('<H', raw, 5)[0]
                stack = raw[7]
                detail = f"  -> USE_ITEM pos=({x}, {y}, {z}) item_id={item_id} stack={stack}\n"
                with open(SNIFF_LOG, "a") as f:
                    f.write(detail)

        except Exception as e:
            with open(SNIFF_LOG, "a") as f:
                f.write(f"ERROR: {e}\n")

        if original_cb:
            original_cb(opcode, reader)

    proxy.on_client_packet = sniffer
    with open(SNIFF_LOG, "w") as f:
        f.write("--- Full packet sniffer started ---\n")
    bot.log("[SNIFF] Full packet sniffer installed — press your hotkey now")

    try:
        while True:
            await bot.sleep(60)
    except BaseException:
        proxy.on_client_packet = original_cb
        bot.log("[SNIFF] Removed")
