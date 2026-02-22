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
_offsets_path = _PROJECT_ROOT / "offsets.json"
try:
    with open(_offsets_path, encoding="utf-8") as _f:
        OFFSETS = json.load(_f)
except (FileNotFoundError, json.JSONDecodeError):
    OFFSETS = {}

GAME_SINGLETON_RVA = OFFSETS.get("game_singleton_rva", "0x0")
