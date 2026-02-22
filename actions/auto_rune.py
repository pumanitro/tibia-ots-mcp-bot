"""Auto-rune: uses rune 3165 on the currently targeted creature every 1 second."""
import sys
import os
import struct

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import ClientOpcode, PacketReader

RUNE_ID = 3165
INTERVAL = 1.0  # seconds between casts


def _build_use_on_creature(item_id: int, creature_id: int) -> bytes:
    """Build USE_ON_CREATURE (0x84) with hotkey-style position."""
    buf = bytearray()
    buf.append(0x84)
    buf.extend(struct.pack('<H', 0xFFFF))  # x
    buf.extend(struct.pack('<H', 0))       # y
    buf.append(0)                          # z
    buf.extend(struct.pack('<H', item_id))
    buf.append(0)                          # stack_pos
    buf.extend(struct.pack('<I', creature_id))
    return bytes(buf)


async def run(bot):
    state = sys.modules["__main__"].state
    proxy = state.game_proxy
    original_cb = proxy.on_client_packet
    current_target = [None]

    def track_target(opcode, reader):
        """Watch ATTACK packets to track the current target."""
        if opcode == ClientOpcode.ATTACK:
            try:
                raw = reader._data[reader._pos:]
                if len(raw) >= 4:
                    cid = struct.unpack_from('<I', raw, 0)[0]
                    old = current_target[0]
                    current_target[0] = cid if cid != 0 else None
                    if current_target[0] != old:
                        if current_target[0]:
                            bot.log(f"target set: 0x{cid:08X}")
                        else:
                            bot.log("target cleared")
            except Exception:
                pass

        if original_cb:
            fresh_reader = PacketReader(reader._data[reader._pos:])
            original_cb(opcode, fresh_reader)

    proxy.on_client_packet = track_target
    bot.log("[RUNE] Tracking attack target, will use rune on targeted creature")

    try:
        while True:
            if bot.is_connected and state.game_proxy and current_target[0]:
                pkt = _build_use_on_creature(RUNE_ID, current_target[0])
                await bot.inject_to_server(pkt)
            await bot.sleep(INTERVAL)
    except BaseException:
        proxy.on_client_packet = original_cb
        bot.log("[RUNE] Stopped")
