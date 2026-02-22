"""Full light via code patch — skips the darkness overlay entirely.

Patches the JZ instruction at RVA 0x16A7EF to an unconditional JMP,
forcing the renderer to always skip the darkness/light overlay draw.
Instant, survives floor changes, no scanning needed.

On disable (action stop), restores the original JZ instruction so
darkness rendering resumes normally.
"""

import sys
import os
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The JZ instruction in the light renderer:
#   RVA 0x16A7EF: 0F 84 89 01 00 00  = JZ +0x189 (skip light draw if draw_lights==0)
# Patched to:
#   RVA 0x16A7EF: E9 8A 01 00 00 90  = JMP +0x18A; NOP (ALWAYS skip light draw)
LIGHT_JZ_RVA = "0x16A7EF"
ORIGINAL_BYTES = "0F 84 89 01 00 00"
PATCHED_BYTES = "E9 8A 01 00 00 90"


def _get_game_state():
    return sys.modules["__main__"].state.game_state


def _dbg(msg):
    with open(os.path.join(PROJECT_ROOT, "full_light_debug.txt"), "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")


async def run(bot):
    _dbg("=== full_light action started ===")
    bot.log("Full light: waiting for DLL bridge...")

    bridge = None
    patched = False
    try:
        gs = _get_game_state()
        while not hasattr(gs, "dll_bridge") or gs.dll_bridge is None:
            await bot.sleep(1)

        bridge = gs.dll_bridge
        _dbg(f"bridge acquired: {bridge.connected}")

        # Verify the bytes at the patch site before writing
        bot.log("Verifying patch site...")
        bridge.send_command({"cmd": "read_mem", "rva": LIGHT_JZ_RVA, "size": 8})
        await bot.sleep(0.5)

        # Apply the patch and keep re-applying every 2s
        # (game may restore original bytes on map reload / floor change)
        bridge.send_command({"cmd": "write_mem", "rva": LIGHT_JZ_RVA, "bytes": PATCHED_BYTES})
        patched = True
        bot.log("Full light ENABLED (darkness overlay skipped)")
        _dbg(f"patched JZ→JMP at {LIGHT_JZ_RVA}")

        while True:
            await bot.sleep(2)
            if bridge.connected:
                bridge.send_command({"cmd": "write_mem", "rva": LIGHT_JZ_RVA, "bytes": PATCHED_BYTES})

    except Exception as e:
        _dbg(f"EXCEPTION: {e}")
        bot.log(f"Full light error: {e}")
        import traceback
        _dbg(traceback.format_exc())
        raise
    finally:
        # Restore original JZ when action is stopped
        if patched and bridge and bridge.connected:
            bridge.send_command({"cmd": "write_mem", "rva": LIGHT_JZ_RVA, "bytes": ORIGINAL_BYTES})
            _dbg("restored original JZ (action stopped)")
            bot.log("Full light DISABLED (darkness restored)")
