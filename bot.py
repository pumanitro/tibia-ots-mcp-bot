"""
DBVictory Bot - Main bot with proxy integration and CLI commands.

Usage:
  1. Start this script
  2. It prints the proxy RSA key
  3. Configure the game client to connect to 127.0.0.1:7171
  4. Login through the proxy
  5. Use CLI commands to control the bot

Note: For the proxy to work, the client must use our RSA public key.
      See README for instructions on how to set this up.
"""

import asyncio
import sys
import logging
from proxy import OTProxy
from protocol import (
    Direction, ClientOpcode, ServerOpcode, PacketReader,
    build_walk_packet, build_attack_packet, build_say_packet,
    build_stop_walk_packet, build_turn_packet, build_follow_packet,
    build_set_fight_modes_packet, build_ping_packet
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    stream=sys.stderr
)
log = logging.getLogger("bot")

from constants import SERVER_HOST, LOGIN_PORT, GAME_PORT

# Server config
SERVER_LOGIN_PORT = LOGIN_PORT
SERVER_GAME_PORT = GAME_PORT
PROXY_LOGIN_PORT = LOGIN_PORT
PROXY_GAME_PORT = GAME_PORT


class GameState:
    """Track game state from server packets."""

    def __init__(self):
        self.player_pos = (0, 0, 0)  # x, y, z
        self.player_hp = 0
        self.player_max_hp = 0
        self.player_mp = 0
        self.player_max_mp = 0
        self.player_level = 0
        self.player_exp = 0
        self.player_speed = 0
        self.creatures = {}  # id -> creature info


class DBVBot:
    """Main bot class."""

    def __init__(self):
        self.proxy = OTProxy(SERVER_HOST, SERVER_GAME_PORT, PROXY_GAME_PORT)
        self.state = GameState()
        self.running = False
        self._auto_walk_task = None

    def setup_callbacks(self):
        """Set up packet inspection callbacks."""
        self.proxy.on_server_packet = self.on_server_packet
        self.proxy.on_client_packet = self.on_client_packet
        self.proxy.on_login_success = self.on_login_success

    def on_login_success(self, xtea_keys):
        log.info("=== BOT READY ===")
        log.info("Type 'help' for available commands")
        self.running = True

    def on_server_packet(self, opcode: int, reader: PacketReader):
        """Process interesting server packets to update game state."""
        try:
            if opcode == ServerOpcode.PLAYER_STATS:
                self._parse_player_stats(reader)
            elif opcode == ServerOpcode.PLAYER_CANCEL_WALK:
                log.debug("Walk cancelled by server")
            elif opcode == ServerOpcode.CREATURE_MOVE:
                pass  # Could track creature positions
            elif opcode == ServerOpcode.TEXT_MESSAGE:
                pass  # Could log messages
        except Exception:
            pass

    def on_client_packet(self, opcode: int, reader: PacketReader):
        """Log client actions."""
        try:
            name = ClientOpcode(opcode).name
            log.debug(f"Player action: {name}")
        except ValueError:
            pass

    def _parse_player_stats(self, reader: PacketReader):
        """Parse player stats packet."""
        try:
            self.state.player_hp = reader.read_u32()
            self.state.player_max_hp = reader.read_u32()
            # There are more fields but they vary by protocol version
        except Exception:
            pass

    # ============================================================
    # Bot commands
    # ============================================================

    async def walk(self, direction: str, steps: int = 1):
        """Walk in a direction."""
        dir_map = {
            'n': Direction.NORTH, 'north': Direction.NORTH,
            's': Direction.SOUTH, 'south': Direction.SOUTH,
            'e': Direction.EAST, 'east': Direction.EAST,
            'w': Direction.WEST, 'west': Direction.WEST,
            'ne': Direction.NORTHEAST, 'northeast': Direction.NORTHEAST,
            'se': Direction.SOUTHEAST, 'southeast': Direction.SOUTHEAST,
            'sw': Direction.SOUTHWEST, 'southwest': Direction.SOUTHWEST,
            'nw': Direction.NORTHWEST, 'northwest': Direction.NORTHWEST,
        }

        d = dir_map.get(direction.lower())
        if d is None:
            log.error(f"Unknown direction: {direction}")
            return

        for i in range(steps):
            packet = build_walk_packet(d)
            await self.proxy.inject_to_server(packet)
            log.info(f"Walk {d.name} ({i+1}/{steps})")
            await asyncio.sleep(0.3)  # Wait between steps

    async def turn(self, direction: str):
        """Turn to face a direction."""
        dir_map = {
            'n': Direction.NORTH, 'north': Direction.NORTH,
            's': Direction.SOUTH, 'south': Direction.SOUTH,
            'e': Direction.EAST, 'east': Direction.EAST,
            'w': Direction.WEST, 'west': Direction.WEST,
        }

        d = dir_map.get(direction.lower())
        if d is None:
            log.error(f"Unknown direction: {direction}")
            return

        packet = build_turn_packet(d)
        await self.proxy.inject_to_server(packet)
        log.info(f"Turn {d.name}")

    async def say(self, text: str):
        """Say something in game."""
        packet = build_say_packet(text)
        await self.proxy.inject_to_server(packet)
        log.info(f"Say: {text}")

    async def attack(self, creature_id: int):
        """Attack a creature by ID."""
        packet = build_attack_packet(creature_id)
        await self.proxy.inject_to_server(packet)
        log.info(f"Attack creature {creature_id}")

    async def follow(self, creature_id: int):
        """Follow a creature by ID."""
        packet = build_follow_packet(creature_id)
        await self.proxy.inject_to_server(packet)
        log.info(f"Follow creature {creature_id}")

    async def stop(self):
        """Stop walking."""
        packet = build_stop_walk_packet()
        await self.proxy.inject_to_server(packet)
        log.info("Stop walk")

    async def auto_walk(self, direction: str, steps: int = 100, delay: float = 0.5):
        """Auto-walk in a direction repeatedly."""
        if self._auto_walk_task:
            self._auto_walk_task.cancel()

        async def _walk_loop():
            for i in range(steps):
                await self.walk(direction, 1)
                await asyncio.sleep(delay)
            log.info("Auto-walk finished")

        self._auto_walk_task = asyncio.create_task(_walk_loop())
        log.info(f"Auto-walking {direction} for {steps} steps (delay={delay}s)")

    async def stop_auto(self):
        """Stop any auto-walk."""
        if self._auto_walk_task:
            self._auto_walk_task.cancel()
            self._auto_walk_task = None
            await self.stop()
            log.info("Auto-walk stopped")

    def print_status(self):
        """Print current game state."""
        s = self.state
        log.info(f"HP: {s.player_hp}/{s.player_max_hp}")
        log.info(f"Connected: {self.running}")
        log.info(f"Packets from server: {self.proxy.packets_from_server}")
        log.info(f"Packets from client: {self.proxy.packets_from_client}")

    def print_help(self):
        """Print available commands."""
        print("""
=== DBVictory Bot Commands ===

Movement:
  walk <dir> [steps]    - Walk in direction (n/s/e/w/ne/se/sw/nw)
  turn <dir>            - Turn to face direction
  stop                  - Stop walking
  autowalk <dir> [steps] [delay] - Auto-walk repeatedly

Combat:
  attack <creature_id>  - Attack a creature
  follow <creature_id>  - Follow a creature

Chat:
  say <text>            - Say something in game

Info:
  status                - Show game state
  help                  - Show this help

Control:
  stopauto              - Stop auto-walk
  quit                  - Exit bot
""")


async def cli_loop(bot: DBVBot):
    """Interactive command line loop."""
    loop = asyncio.get_event_loop()

    while True:
        try:
            # Read input in a thread to not block the event loop
            line = await loop.run_in_executor(None, lambda: input("bot> "))
            line = line.strip()

            if not line:
                continue

            parts = line.split()
            cmd = parts[0].lower()
            args = parts[1:]

            if cmd == 'help':
                bot.print_help()
            elif cmd == 'status':
                bot.print_status()
            elif cmd == 'quit' or cmd == 'exit':
                log.info("Exiting...")
                break
            elif cmd == 'walk':
                if not args:
                    print("Usage: walk <direction> [steps]")
                    continue
                steps = int(args[1]) if len(args) > 1 else 1
                await bot.walk(args[0], steps)
            elif cmd == 'turn':
                if not args:
                    print("Usage: turn <direction>")
                    continue
                await bot.turn(args[0])
            elif cmd == 'stop':
                await bot.stop()
            elif cmd == 'autowalk':
                if not args:
                    print("Usage: autowalk <direction> [steps] [delay]")
                    continue
                steps = int(args[1]) if len(args) > 1 else 100
                delay = float(args[2]) if len(args) > 2 else 0.5
                await bot.auto_walk(args[0], steps, delay)
            elif cmd == 'stopauto':
                await bot.stop_auto()
            elif cmd == 'say':
                if not args:
                    print("Usage: say <text>")
                    continue
                await bot.say(' '.join(args))
            elif cmd == 'attack':
                if not args:
                    print("Usage: attack <creature_id>")
                    continue
                await bot.attack(int(args[0]))
            elif cmd == 'follow':
                if not args:
                    print("Usage: follow <creature_id>")
                    continue
                await bot.follow(int(args[0]))
            else:
                print(f"Unknown command: {cmd}. Type 'help' for commands.")

        except EOFError:
            break
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Command error: {e}")


async def main():
    bot = DBVBot()
    bot.setup_callbacks()

    print("=" * 60)
    print("  DBVictory Bot - Network Proxy Mode")
    print("=" * 60)
    print()
    print(f"Game server: {SERVER_HOST}:{SERVER_GAME_PORT}")
    print(f"Proxy will listen on: 127.0.0.1:{PROXY_GAME_PORT}")
    print()
    print("IMPORTANT: To use this bot, you need to:")
    print("1. Replace the RSA key in the game client with the proxy's key")
    print("2. Change the client's server address to 127.0.0.1")
    print("3. Login through the proxy")
    print()
    print(f"Proxy RSA Public Key (N):")
    print(bot.proxy.get_proxy_rsa_public_key())
    print()

    # Start proxy and CLI concurrently
    proxy_task = asyncio.create_task(bot.proxy.start())
    cli_task = asyncio.create_task(cli_loop(bot))

    # Wait for either to finish
    done, pending = await asyncio.wait(
        [proxy_task, cli_task],
        return_when=asyncio.FIRST_COMPLETED
    )

    for task in pending:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
