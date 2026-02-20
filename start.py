"""
DBVictory Bot Launcher

Steps:
1. Generates RSA key pair for the proxy
2. Starts login proxy (port 7171) and game proxy (port 7172)
3. Patches the running game client's memory (RSA key + server IP -> 127.0.0.1)
4. You login in the game client - traffic goes through our proxy
5. Bot CLI becomes available after login

IMPORTANT: Run this BEFORE you login! The client should be at the login screen.
Close the game if you're already logged in, restart it, then run this script.
"""

import asyncio
import sys
import os
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proxy import OTProxy
from protocol import (
    Direction, ClientOpcode, ServerOpcode, PacketReader,
    build_walk_packet, build_say_packet, build_stop_walk_packet,
    build_turn_packet, build_attack_packet, build_follow_packet,
)
from crypto import get_default_rsa_key

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    stream=sys.stderr
)
log = logging.getLogger("launcher")

SERVER_HOST = "87.98.220.215"
LOGIN_PORT = 7171
GAME_PORT = 7172


class BotController:
    """Bot with CLI commands."""

    def __init__(self, game_proxy: OTProxy):
        self.proxy = game_proxy
        self.ready = False
        self._auto_task = None

    async def walk(self, direction: str, steps: int = 1):
        dir_map = {
            'n': Direction.NORTH, 's': Direction.SOUTH,
            'e': Direction.EAST, 'w': Direction.WEST,
            'ne': Direction.NORTHEAST, 'se': Direction.SOUTHEAST,
            'sw': Direction.SOUTHWEST, 'nw': Direction.NORTHWEST,
        }
        d = dir_map.get(direction.lower())
        if d is None:
            print(f"Unknown direction: {direction}")
            return
        for i in range(steps):
            await self.proxy.inject_to_server(build_walk_packet(d))
            if steps > 1:
                await asyncio.sleep(0.3)

    async def turn(self, direction: str):
        dir_map = {'n': Direction.NORTH, 's': Direction.SOUTH,
                    'e': Direction.EAST, 'w': Direction.WEST}
        d = dir_map.get(direction.lower())
        if d:
            await self.proxy.inject_to_server(build_turn_packet(d))

    async def say(self, text: str):
        await self.proxy.inject_to_server(build_say_packet(text))

    async def attack(self, cid: int):
        await self.proxy.inject_to_server(build_attack_packet(cid))

    async def follow(self, cid: int):
        await self.proxy.inject_to_server(build_follow_packet(cid))

    async def stop(self):
        await self.proxy.inject_to_server(build_stop_walk_packet())

    async def autowalk(self, direction: str, steps: int = 100, delay: float = 0.5):
        if self._auto_task:
            self._auto_task.cancel()

        async def _loop():
            for _ in range(steps):
                await self.walk(direction)
                await asyncio.sleep(delay)

        self._auto_task = asyncio.create_task(_loop())

    async def stopauto(self):
        if self._auto_task:
            self._auto_task.cancel()
            self._auto_task = None
            await self.stop()


async def cli_loop(bot: BotController):
    """Interactive command line."""
    loop = asyncio.get_event_loop()

    help_text = """
=== Commands ===
  walk <n/s/e/w/ne/se/sw/nw> [steps]
  turn <n/s/e/w>
  autowalk <dir> [steps] [delay]
  stopauto
  stop
  say <text>
  attack <creature_id>
  follow <creature_id>
  status
  help
  quit
"""

    while True:
        try:
            line = await loop.run_in_executor(None, lambda: input("bot> "))
            line = line.strip()
            if not line:
                continue

            parts = line.split()
            cmd = parts[0].lower()
            args = parts[1:]

            if not bot.ready:
                print("Bot not ready yet. Login in the game client first.")
                continue

            if cmd == 'help':
                print(help_text)
            elif cmd == 'quit' or cmd == 'exit':
                break
            elif cmd == 'walk':
                if not args:
                    print("Usage: walk <direction> [steps]")
                else:
                    steps = int(args[1]) if len(args) > 1 else 1
                    await bot.walk(args[0], steps)
            elif cmd == 'turn':
                if args:
                    await bot.turn(args[0])
            elif cmd == 'stop':
                await bot.stop()
            elif cmd == 'autowalk':
                if args:
                    steps = int(args[1]) if len(args) > 1 else 100
                    delay = float(args[2]) if len(args) > 2 else 0.5
                    await bot.autowalk(args[0], steps, delay)
            elif cmd == 'stopauto':
                await bot.stopauto()
            elif cmd == 'say':
                if args:
                    await bot.say(' '.join(args))
            elif cmd == 'attack':
                if args:
                    await bot.attack(int(args[0]))
            elif cmd == 'follow':
                if args:
                    await bot.follow(int(args[0]))
            elif cmd == 'status':
                print(f"Ready: {bot.ready}")
                print(f"Packets S->C: {bot.proxy.packets_from_server}")
                print(f"Packets C->S: {bot.proxy.packets_from_client}")
            else:
                print(f"Unknown: {cmd}. Type 'help'.")

        except (EOFError, KeyboardInterrupt):
            break
        except Exception as e:
            print(f"Error: {e}")


def patch_client() -> bool:
    """Patch the running game client's memory - only redirect server IP."""
    try:
        import pymem
    except ImportError:
        print("[FAIL] pymem not installed")
        return False

    try:
        pm = pymem.Pymem("dbvStart.exe")
    except Exception as e:
        print(f"[FAIL] Could not attach to dbvStart.exe: {e}")
        return False

    print(f"[OK] Attached to client (PID: {pm.process_id})")

    from patcher import find_server_address_in_memory, patch_memory

    # Only patch server IP to redirect to our proxy
    # No RSA patching needed - we know the default OTClient RSA private key!
    ip_locs = find_server_address_in_memory(pm)
    localhost = b"127.0.0.1"
    patched_ip = sum(1 for addr, old in ip_locs if patch_memory(pm, addr, old, localhost))

    pm.close_process()

    print(f"[OK] Patched {patched_ip} server IP(s)")
    return patched_ip > 0


async def main():
    print("=" * 60)
    print("  DBVictory Bot")
    print("=" * 60)
    print()

    # Check game is running
    try:
        import pymem
        pm = pymem.Pymem("dbvStart.exe")
        pm.close_process()
        print("[OK] Game client detected")
    except Exception:
        print("[!!] Game client not running!")
        print("     Start the game, stay at login screen, then run this.")
        input("\nPress Enter to exit...")
        return

    # Create both proxies - they use the default OTClient RSA key (known private key)
    login_proxy = OTProxy(SERVER_HOST, LOGIN_PORT, LOGIN_PORT, is_login_proxy=True)
    game_proxy = OTProxy(SERVER_HOST, GAME_PORT, GAME_PORT, is_login_proxy=False)

    bot = BotController(game_proxy)

    # Set up callbacks
    def on_server_packet(opcode, reader):
        try:
            name = ServerOpcode(opcode).name
        except ValueError:
            name = "?"
        log.debug(f"[S->C] 0x{opcode:02X} ({name})")

    def on_client_packet(opcode, reader):
        try:
            name = ClientOpcode(opcode).name
        except ValueError:
            name = "?"
        log.debug(f"[C->S] 0x{opcode:02X} ({name})")

    def on_login_success(keys):
        bot.ready = True
        log.info("=== BOT READY - Type 'help' for commands ===")

    game_proxy.on_server_packet = on_server_packet
    game_proxy.on_client_packet = on_client_packet
    game_proxy.on_login_success = on_login_success

    # Patch client memory (only server IP, no RSA needed)
    print(f"\n[..] Patching game client...")
    success = patch_client()
    if not success:
        print("[FAIL] Could not patch client. Try running as Administrator.")
        input("\nPress Enter to exit...")
        return

    print()
    print("=" * 60)
    print("  READY! Now login in the game client.")
    print("  The traffic will go through our proxy.")
    print("=" * 60)
    print()

    # Start everything
    tasks = [
        asyncio.create_task(login_proxy.start()),
        asyncio.create_task(game_proxy.start()),
        asyncio.create_task(cli_loop(bot)),
    ]

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
