"""
DBVictory Bot — Centralized game constants.

Single source of truth for game-specific RVAs, network config, item IDs,
player icon bits, creature ID ranges, and light-patch bytes.

DLL offsets are loaded from offsets.json at import time so they don't need
to be duplicated in Python code.
"""

import json
import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent

# ── Network ────────────────────────────────────────────────────────
SERVER_HOST = os.environ.get("DBV_SERVER_HOST", "87.98.220.215")
LOGIN_PORT = 7171
GAME_PORT = 7172

# Packed binary forms used by proxy IP replacement
SERVER_IP_BYTES = bytes(int(b) for b in SERVER_HOST.split("."))  # e.g. b'\x57\x62\xdc\xd7'
SERVER_IP_STR = SERVER_HOST.encode("ascii")                      # e.g. b'87.98.220.215'

# ── Item IDs ───────────────────────────────────────────────────────
ITEM_RED_HAM = 3583
ITEM_RUNE_3165 = 3165
ITEM_BACKPACK = 2867

# ── Player icons (DBVictory-specific) ──────────────────────────────
ICONS_BASELINE = 0x0100
HASTE_ICON_BIT = 0x0002  # bit 1 — empirically verified

# ── Creature ID ranges ─────────────────────────────────────────────
MONSTER_ID_MIN = 0x40000000  # OT creature ID range: monsters start here
CREATURE_ID_MIN = 0x10000000
CREATURE_ID_MAX = 0x7FFFFFFF

# ── Light patch (full_light action) ────────────────────────────────
# JZ instruction in the light renderer at this RVA:
#   0F 84 89 01 00 00  = JZ +0x189 (skip light draw if draw_lights==0)
# Patched to unconditional JMP:
#   E9 8A 01 00 00 90  = JMP +0x18A; NOP (ALWAYS skip light draw)
LIGHT_JZ_RVA = "0x16A7EF"
LIGHT_ORIGINAL_BYTES = "0F 84 89 01 00 00"
LIGHT_PATCHED_BYTES = "E9 8A 01 00 00 90"

# ── Offsets from offsets.json ──────────────────────────────────────
# Loaded at import time so the rest of the codebase can use them directly.
# See docs/otclient_source_analysis.md for the original OTCv8 source layout.
_offsets_path = _PROJECT_ROOT / "offsets.json"
try:
    with open(_offsets_path, encoding="utf-8") as _f:
        OFFSETS = json.load(_f)
except (FileNotFoundError, json.JSONDecodeError):
    OFFSETS = {}

# g_game singleton — the Game class instance (see §4 "g_game" in source analysis)
# Source: src/client/game.h — holds m_localPlayer, m_attackingCreature, m_protocolGame, etc.
GAME_SINGLETON_RVA = OFFSETS.get("game_singleton_rva", "0x0")  # 0xB2E970

# ── Game singleton field offsets ──────────────────────────────────
# Empirical offsets from GAME_SINGLETON_RVA into the Game object.
# Source field order (game.h): m_localPlayer, m_attackingCreature,
# m_followingCreature, m_protocolGame, m_containers, m_online, ...
# DBVictory may reorder or add fields, so these are verified empirically.
#
#   Offset  Empirical   Source field (game.h)
#   0x0C    attacking   m_attackingCreature (CreaturePtr — shared_ptr, 8 bytes)
#   0x18    proto_game  m_protocolGame      (ProtocolGamePtr)
#   0x34    attack_flag (no direct source equivalent — DBV custom?)
#   0x70    seq_counter (no direct source equivalent — DBV custom?)

# ── Creature struct offsets ───────────────────────────────────────
# Inheritance: shared_object → LuaObject → Thing (packed) → Creature
#
# Original OTCv8 source layout (estimated, x86 MSVC):
#   +0x00  vtable ptr          (compiler-generated)
#   +0x04  refs                (shared_object::atomic<refcount_t>)
#   +0x08  m_fieldsTableRef    (LuaObject)
#   +0x0C  m_position.x        (Thing, #pragma pack(push,1))
#   +0x10  m_position.y        (Thing)
#   +0x14  m_position.z        (Thing, 2 bytes)
#   +0x16  m_datId             (Thing, 2 bytes)
#   +0x18  m_marked            (Thing, 1 byte)
#   +0x19  m_hidden            (Thing, 1 byte)
#   +0x1A  m_markedColor       (Thing, 4 bytes)
#   ~0x1E  m_id                (Creature — unique creature ID)
#   ~0x22  m_name              (Creature — std::string, 24 bytes MSVC SSO)
#   ~0x3A  m_healthPercent     (Creature — uint8, 0-100)
#
# DBVictory adds ~500 bytes of custom fields (Ki, power level, transformations,
# etc.) which shift offsets compared to source. Empirical values (offsets.json):
#
#   Field       Source est.  Empirical   Delta (DBV adds padding/fields)
#   vtable      +0x00        0x00        — (matches)
#   refs        +0x04        0x04        — (matches)
#   m_id        ~0x1E        0x34        +0x16 shift (DBV custom fields before ID)
#   m_name      ~0x22        0x38        +0x16 shift (consistent)
#   m_health%   ~0x3A        0x50        +0x16 shift (consistent)
#   position    ~0x0C        id-40=0x0C  — (matches source! pack(1) Thing is stable)
#   npc_pos     N/A          id+576      DBV custom field (not in original OTCv8)

# ── Function RVAs ────────────────────────────────────────────────
# These are Relative Virtual Addresses (offset from module base) for key
# game functions, found by pattern scanning and verified empirically.
#
#   RVA         Function                    Source file
#   0x3AF220    XTEA encrypt                src/framework/net/protocol.cpp
#   0x8F220     Game::attack()              src/client/game.cpp (UI red square)
#   0x89680     Game::doAttack()            src/client/game.cpp (internal)
#   0x19D100    sendAttackCreature()        src/client/protocolgamesend.cpp (network)

# ── Creature vtable RVA range ────────────────────────────────────
# Valid vtable pointers for Creature objects fall in this range.
# Used by DLL's VirtualQuery scan to identify creature structs in memory.
# Source: Creature vtable lives in .rdata section (compiler-generated).
#   Range: 0x870000 — 0x8A0000

# ── World light addresses ────────────────────────────────────────
# RVAs for the global world light values (read/written by the renderer).
# Source: likely in Map or LightView class (g_map or rendering pipeline).
#   addr_rva:        0xB2ECF8  (world light level + color)
#   render_addr_rva: 0xB2ECFC  (renderer's copy)
