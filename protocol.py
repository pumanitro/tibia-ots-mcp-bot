"""
OT Protocol - Packet structures and opcodes for Open Tibia protocol.
"""

import struct
import io
from enum import IntEnum


# ============================================================
# Client -> Server opcodes (actions the player/bot can perform)
# ============================================================
class ClientOpcode(IntEnum):
    # Login
    LOGIN_SERVER = 0x01
    GAME_LOGIN = 0x0A

    # Game actions
    LOGOUT = 0x14
    PING = 0x1E
    PONG = 0x1F

    # Movement
    WALK_NORTH = 0x65
    WALK_EAST = 0x66
    WALK_SOUTH = 0x67
    WALK_WEST = 0x68
    WALK_NORTHEAST = 0x6A
    WALK_SOUTHEAST = 0x6B
    WALK_SOUTHWEST = 0x6C
    WALK_NORTHWEST = 0x6D
    STOP_WALK = 0x69
    TURN_NORTH = 0x6F
    TURN_EAST = 0x70
    TURN_SOUTH = 0x71
    TURN_WEST = 0x72

    # Item/Object interaction
    MOVE_THING = 0x78
    USE_ITEM = 0x82
    USE_ITEM_EX = 0x83
    USE_ON_CREATURE = 0x84
    LOOK = 0x8C
    LOOK_IN_TRADE = 0x8E

    # Combat
    ATTACK = 0xA1
    FOLLOW = 0xA2
    CANCEL_ATTACK = 0xBE

    # Chat
    SAY = 0x96
    OPEN_CHANNEL = 0x98
    CLOSE_CHANNEL = 0x99
    OPEN_PRIVATE_CHANNEL = 0x9A

    # Container
    CLOSE_CONTAINER = 0x87
    UP_CONTAINER = 0x88

    # Trade
    REQUEST_TRADE = 0x7D
    ACCEPT_TRADE = 0x7F
    REJECT_TRADE = 0x80

    # NPC Trade
    BUY_ITEM = 0x7A
    SELL_ITEM = 0x7B
    CLOSE_NPC_TRADE = 0x7C

    # Outfit
    SET_OUTFIT = 0xD3

    # VIP
    ADD_VIP = 0xDC
    REMOVE_VIP = 0xDD

    # Misc
    SET_FIGHT_MODES = 0xA0
    REQUEST_QUEST_LOG = 0xF0
    REQUEST_QUEST_LINE = 0xF1


# ============================================================
# Server -> Client opcodes (information from the server)
# ============================================================
class ServerOpcode(IntEnum):
    # Login
    LOGIN_OR_PENDING = 0x0A
    GM_ACTIONS = 0x0B
    UPDATE_NEEDED = 0x0C
    LOGIN_ERROR = 0x14
    LOGIN_ADVISE = 0x15
    LOGIN_WAIT = 0x16
    LOGIN_SUCCESS = 0x17
    LOGIN_TOKEN = 0x0D
    CHALLENGE = 0x1F

    PING = 0x1D
    PONG = 0x1E

    # Map
    MAP_DESCRIPTION = 0x64
    MAP_SLICE_NORTH = 0x65
    MAP_SLICE_EAST = 0x66
    MAP_SLICE_SOUTH = 0x67
    MAP_SLICE_WEST = 0x68

    # Tile updates
    TILE_ADD_THING = 0x6A
    TILE_TRANSFORM_THING = 0x6B
    TILE_REMOVE_THING = 0x6C

    # Creature
    CREATURE_MOVE = 0x6D
    CREATURE_TURN = 0x6E

    # Container
    OPEN_CONTAINER = 0x6E
    CLOSE_CONTAINER = 0x6F
    CREATE_IN_CONTAINER = 0x70
    CHANGE_IN_CONTAINER = 0x71
    REMOVE_FROM_CONTAINER = 0x72

    # Effects
    MAGIC_EFFECT = 0x83
    ANIMATED_TEXT = 0x84
    SHOOT_EFFECT = 0x85

    # Creature updates
    CREATURE_LIGHT = 0x8D
    CREATURE_OUTFIT = 0x8E
    CREATURE_SPEED = 0x8F
    CREATURE_SKULL = 0x90
    CREATURE_PARTY = 0x91
    CREATURE_HEALTH = 0x8C

    # Player state
    PLAYER_STATS = 0xA0
    PLAYER_SKILLS = 0xA1
    PLAYER_ICONS = 0xA2
    PLAYER_CANCEL_ATTACK = 0xA3
    PLAYER_CANCEL_WALK = 0xB5

    # Chat
    TALK = 0xAA
    CHANNELS = 0xAB
    OPEN_CHANNEL = 0xAC
    CLOSE_CHANNEL = 0xB2
    PRIVATE_CHANNEL = 0xAD

    # Text
    TEXT_MESSAGE = 0xB4

    # VIP
    VIP_ADD = 0xD2
    VIP_STATE = 0xD3
    VIP_LOGOUT = 0xD4


# ============================================================
# Directions
# ============================================================
class Direction(IntEnum):
    NORTH = 0
    EAST = 1
    SOUTH = 2
    WEST = 3
    NORTHEAST = 4
    SOUTHEAST = 5
    SOUTHWEST = 6
    NORTHWEST = 7


# Map from direction to walk opcode
WALK_OPCODES = {
    Direction.NORTH: ClientOpcode.WALK_NORTH,
    Direction.EAST: ClientOpcode.WALK_EAST,
    Direction.SOUTH: ClientOpcode.WALK_SOUTH,
    Direction.WEST: ClientOpcode.WALK_WEST,
    Direction.NORTHEAST: ClientOpcode.WALK_NORTHEAST,
    Direction.SOUTHEAST: ClientOpcode.WALK_SOUTHEAST,
    Direction.SOUTHWEST: ClientOpcode.WALK_SOUTHWEST,
    Direction.NORTHWEST: ClientOpcode.WALK_NORTHWEST,
}


# ============================================================
# Packet Reader/Writer
# ============================================================
class PacketReader:
    """Read values from an OT protocol packet."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    @property
    def remaining(self) -> int:
        return len(self._data) - self._pos

    @property
    def position(self) -> int:
        return self._pos

    def read_bytes(self, count: int) -> bytes:
        result = self._data[self._pos:self._pos + count]
        self._pos += count
        return result

    def read_u8(self) -> int:
        val = self._data[self._pos]
        self._pos += 1
        return val

    def read_u16(self) -> int:
        val = struct.unpack_from('<H', self._data, self._pos)[0]
        self._pos += 2
        return val

    def read_u32(self) -> int:
        val = struct.unpack_from('<I', self._data, self._pos)[0]
        self._pos += 4
        return val

    def read_string(self) -> str:
        length = self.read_u16()
        data = self.read_bytes(length)
        return data.decode('latin-1')

    def read_position(self) -> tuple[int, int, int]:
        x = self.read_u16()
        y = self.read_u16()
        z = self.read_u8()
        return (x, y, z)


class PacketWriter:
    """Build an OT protocol packet."""

    def __init__(self):
        self._buf = bytearray()

    @property
    def data(self) -> bytes:
        return bytes(self._buf)

    @property
    def size(self) -> int:
        return len(self._buf)

    def write_bytes(self, data: bytes):
        self._buf.extend(data)

    def write_u8(self, value: int):
        self._buf.append(value & 0xFF)

    def write_u16(self, value: int):
        self._buf.extend(struct.pack('<H', value & 0xFFFF))

    def write_u32(self, value: int):
        self._buf.extend(struct.pack('<I', value & 0xFFFFFFFF))

    def write_string(self, text: str):
        encoded = text.encode('latin-1')
        self.write_u16(len(encoded))
        self.write_bytes(encoded)

    def write_position(self, x: int, y: int, z: int):
        self.write_u16(x)
        self.write_u16(y)
        self.write_u8(z)


def build_walk_packet(direction: Direction) -> bytes:
    """Build a walk packet for the given direction."""
    pw = PacketWriter()
    pw.write_u8(WALK_OPCODES[direction])
    return pw.data


def build_attack_packet(creature_id: int, seq: int = 0) -> bytes:
    """Build an attack packet targeting a creature."""
    pw = PacketWriter()
    pw.write_u8(ClientOpcode.ATTACK)
    pw.write_u32(creature_id)
    pw.write_u32(seq)
    return pw.data


def build_say_packet(text: str, mode: int = 1) -> bytes:
    """Build a say/chat packet. mode=1 is normal say."""
    pw = PacketWriter()
    pw.write_u8(ClientOpcode.SAY)
    pw.write_u8(mode)
    pw.write_string(text)
    return pw.data


def build_stop_walk_packet() -> bytes:
    """Build a stop walk packet."""
    pw = PacketWriter()
    pw.write_u8(ClientOpcode.STOP_WALK)
    return pw.data


def build_ping_packet() -> bytes:
    """Build a ping packet."""
    pw = PacketWriter()
    pw.write_u8(ClientOpcode.PING)
    return pw.data


def build_use_item_packet(x: int, y: int, z: int, item_id: int, stack_pos: int, index: int) -> bytes:
    """Build a use item packet."""
    pw = PacketWriter()
    pw.write_u8(ClientOpcode.USE_ITEM)
    pw.write_position(x, y, z)
    pw.write_u16(item_id)
    pw.write_u8(stack_pos)
    pw.write_u8(index)
    return pw.data


def build_move_item_packet(from_pos: tuple, item_id: int, from_stack: int, to_pos: tuple, count: int) -> bytes:
    """Build a move item packet."""
    pw = PacketWriter()
    pw.write_u8(ClientOpcode.MOVE_THING)
    pw.write_position(*from_pos)
    pw.write_u16(item_id)
    pw.write_u8(from_stack)
    pw.write_position(*to_pos)
    pw.write_u8(count)
    return pw.data


def build_look_packet(x: int, y: int, z: int, item_id: int, stack_pos: int) -> bytes:
    """Build a look at packet."""
    pw = PacketWriter()
    pw.write_u8(ClientOpcode.LOOK)
    pw.write_position(x, y, z)
    pw.write_u16(item_id)
    pw.write_u8(stack_pos)
    return pw.data


def build_follow_packet(creature_id: int, seq: int = 0) -> bytes:
    """Build a follow creature packet."""
    pw = PacketWriter()
    pw.write_u8(ClientOpcode.FOLLOW)
    pw.write_u32(creature_id)
    pw.write_u32(seq)
    return pw.data


def build_set_fight_modes_packet(fight_mode: int = 1, chase_mode: int = 0, safe_mode: int = 1) -> bytes:
    """
    Build a set fight modes packet.
    fight_mode: 1=offensive, 2=balanced, 3=defensive
    chase_mode: 0=stand, 1=chase
    safe_mode: 0=pvp, 1=safe
    """
    pw = PacketWriter()
    pw.write_u8(ClientOpcode.SET_FIGHT_MODES)
    pw.write_u8(fight_mode)
    pw.write_u8(chase_mode)
    pw.write_u8(safe_mode)
    return pw.data


def build_turn_packet(direction: Direction) -> bytes:
    """Build a turn packet for the given direction."""
    turn_opcodes = {
        Direction.NORTH: ClientOpcode.TURN_NORTH,
        Direction.EAST: ClientOpcode.TURN_EAST,
        Direction.SOUTH: ClientOpcode.TURN_SOUTH,
        Direction.WEST: ClientOpcode.TURN_WEST,
    }
    pw = PacketWriter()
    pw.write_u8(turn_opcodes[direction])
    return pw.data
