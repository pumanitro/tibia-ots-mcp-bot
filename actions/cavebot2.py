"""Cavebot v2 — Adaptive farming with maximum lure, route optimization,
and retreat-first body-block handling.

Key differences from v1 cavebot:
  - Adaptive lure count (ramps up when fights are fast/mana-rich)
  - Dead segment skipping (after 2+ loops, skip 0-kill walk_to nodes)
  - HOT zone lingering (wait for respawns in high-kill areas)
  - Retreat-first body-block (walk away, mobs follow, path clears)
  - Highest-HP targeting integration (via auto_targeting2)
  - Telemetry integration (farming_telemetry.py)

Toggle: disable cavebot + auto_targeting, enable cavebot2 + auto_targeting2.
auto_combat is shared between v1 and v2.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import (
    Direction,
    build_use_item_packet,
    build_use_item_ex_packet,
    build_use_on_creature_packet,
    build_walk_packet,
    build_stop_walk_packet,
)
from cavebot import (
    load_recording,
    build_actions_map,
    build_sequence_minimaps,
    actions_map_to_text,
    save_recording_stats,
)
from constants import MONSTER_ID_MIN
from farming_telemetry import FarmingTelemetry

# ── Timing constants ──────────────────────────────────────────────
USE_ITEM_TIMEOUT = 5.0
WALK_TO_TOLERANCE = 2
MAX_RETRIES = 1
REACHABLE_PROBE_TIMEOUT = 0.4
PAUSE_MAX_TIMEOUT = 60
NO_DAMAGE_TIMEOUT = 3.0
MAX_CLICK_RANGE = 8
CONSECUTIVE_FAIL_RESYNC = 2

# ── Adaptive lure defaults ────────────────────────────────────────
DEFAULT_MIN_LURE = 4
DEFAULT_MAX_LURE = 10
DEFAULT_LURE_DISTANCE = 7
DEFAULT_LURE_TIMEOUT = 15
LURE_EMA_ALPHA = 0.3
LURE_STABILITY_FIGHTS = 3

# ── Route optimization ───────────────────────────────────────────
DEAD_SEGMENT_MIN_LOOPS = 2
DEAD_SEGMENT_MIN_ENTRIES = 2
LINGER_MAX = 20  # max seconds to wait for respawns in HOT zones
LINGER_MIN_KILLS_PER_ATTEMPT = 0.3

# ── Body-block handling ──────────────────────────────────────────
BLOCK_DETECT_THRESHOLD = 3  # cancel_walks at same pos before retreat
BLOCK_KILL_TIMEOUT = 5.0    # seconds stuck before killing blocker (last resort)

UNREACHABLE_EXPIRY = 30

# Direction helpers
DIR_NAME_TO_ENUM = {
    "north": Direction.NORTH, "south": Direction.SOUTH,
    "east": Direction.EAST, "west": Direction.WEST,
    "northeast": Direction.NORTHEAST, "southeast": Direction.SOUTHEAST,
    "southwest": Direction.SOUTHWEST, "northwest": Direction.NORTHWEST,
}

DIR_OFFSET = {
    "north": (0, -1), "south": (0, 1),
    "east": (1, 0), "west": (-1, 0),
    "northeast": (1, -1), "southeast": (1, 1),
    "southwest": (-1, 1), "northwest": (-1, -1),
}

OPPOSITE_DIR = {
    "north": "south", "south": "north",
    "east": "west", "west": "east",
    "northeast": "southwest", "southwest": "northeast",
    "northwest": "southeast", "southeast": "northwest",
}


def _get_state():
    return sys.modules["__main__"].state


def _get_settings():
    """Read cavebot2 settings from bot_settings.json."""
    state = _get_state()
    return state.settings.get("actions", {}).get("cavebot2", {})


def _distance(a, b):
    """Manhattan distance (ignoring z)."""
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _chebyshev(a, b):
    """Chebyshev distance (ignoring z)."""
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _count_nearby_monsters(gs, distance, targetable_only=False):
    """Count alive monsters nearby."""
    now = time.time()
    count = 0
    for cid, info in gs.creatures.items():
        if (cid >= MONSTER_ID_MIN
                and 0 < info.get("health", 0) <= 100
                and now - info.get("t", 0) < 60):
            if cid in gs.unreachable_creatures and gs.unreachable_creatures[cid] > now:
                continue
            if targetable_only:
                cx = info.get("x", 0)
                cy = info.get("y", 0)
                if cx == 0 or cy == 0:
                    continue
            count += 1
    return count


def _get_nearest_monster_on_path(gs, px, py, pz, tx, ty):
    """Find the nearest monster that's on or adjacent to the line between
    player and target. Returns creature_id or None."""
    now = time.time()
    best_cid = None
    best_dist = float("inf")
    for cid, info in gs.creatures.items():
        if cid < MONSTER_ID_MIN:
            continue
        if not (0 < info.get("health", 0) <= 100):
            continue
        if now - info.get("t", 0) > 60:
            continue
        cx = info.get("x", 0)
        cy = info.get("y", 0)
        cz = info.get("z", 0)
        if cx == 0 and cy == 0:
            continue
        if cz != pz:
            continue
        # Check if creature is within 1 tile of player
        d = max(abs(cx - px), abs(cy - py))
        if d <= 1 and d < best_dist:
            best_dist = d
            best_cid = cid
    return best_cid


def _is_next_node_floor_change(actions_map, i, player_z):
    """Return True if the node at i+1 is a floor change."""
    if i + 1 >= len(actions_map):
        return False
    next_node = actions_map[i + 1]
    ntype = next_node["type"]
    if ntype == "walk_steps":
        return True
    target = next_node.get("target")
    if target and target[2] != player_z:
        return True
    if ntype == "use_item_ex":
        to_z = next_node.get("to_z")
        if to_z is not None and to_z != player_z:
            return True
    return False


def _next_node_is_interaction(actions_map, i):
    """Return True if the next node is use_item/use_item_ex/walk_steps."""
    if i + 1 >= len(actions_map):
        return False
    return actions_map[i + 1]["type"] in ("use_item", "use_item_ex", "walk_steps")


def _node_expected_z(node):
    """Return the floor level the player should be on to execute this node."""
    if node["type"] == "walk_steps":
        start = node.get("start")
        if start:
            return start[2]
    return node["target"][2]


def _retreat_direction(px, py, tx, ty):
    """Direction AWAY from target — opposite of target direction."""
    dx = px - tx  # vector away from target
    dy = py - ty
    if dx == 0 and dy == 0:
        return "south"  # arbitrary
    # Normalize to cardinal/diagonal
    if abs(dx) > 0 and abs(dy) > 0:
        if dx > 0 and dy > 0:
            return "southeast"
        if dx > 0 and dy < 0:
            return "northeast"
        if dx < 0 and dy > 0:
            return "southwest"
        return "northwest"
    if abs(dx) >= abs(dy):
        return "east" if dx > 0 else "west"
    return "south" if dy > 0 else "north"


def _perpendicular_directions(px, py, tx, ty):
    """Return the two perpendicular directions relative to the path."""
    dx = tx - px
    dy = ty - py
    if abs(dx) >= abs(dy):
        # Moving mostly horizontally → perpendicular is N/S
        return ["north", "south"]
    else:
        # Moving mostly vertically → perpendicular is E/W
        return ["east", "west"]


# ── Adaptive Lure Count ──────────────────────────────────────────

def _compute_adaptive_lure_count(gs, telemetry, base_count, min_lure, max_lure):
    """Compute adaptive lure count based on recent fight performance.

    Ratchets upward when fights are easy and mana is plentiful.
    """
    avg_dur = telemetry.avg_fight_duration(last_n=5)
    avg_mana = telemetry.avg_mana_remaining(last_n=5)

    if avg_dur is None or avg_mana is None:
        return base_count  # not enough data yet

    computed = base_count

    # Fight duration signals
    if avg_dur < 5.0:
        computed += 2  # fights too easy, gather more
    elif avg_dur < 10.0:
        computed += 1
    elif avg_dur > 15.0:
        computed -= 1  # fights getting slow

    # Mana signals
    if avg_mana is not None and avg_mana > 30:
        computed += 1  # plenty of AOE fuel left

    # Clamp to range
    return max(min_lure, min(max_lure, computed))


# ── Position Waiting ──────────────────────────────────────────────

FLOOR_CHANGED = "floor_changed"
CANCEL_WALK = "cancel_walk"
MAX_CANCEL_WALKS = 6
CANCEL_ESCAPE_THRESHOLD = 2


async def _wait_for_position(bot, expected_pos, timeout, tolerance=0, abort_on_floor_change=False):
    """Wait until position matches expected or timeout."""
    start = time.time()
    start_z = bot.position[2]
    gs = _get_state().game_state
    while time.time() - start < timeout:
        current = bot.position
        if (abs(current[0] - expected_pos[0]) <= tolerance
                and abs(current[1] - expected_pos[1]) <= tolerance
                and current[2] == expected_pos[2]):
            return True
        if abort_on_floor_change:
            for evt in gs.server_events:
                ts, etype, edata = evt
                if ts > start and etype in ("floor_change_up", "floor_change_down"):
                    return edata
            if current[2] != start_z:
                return FLOOR_CHANGED
        if gs.cancel_walk_time > start:
            return CANCEL_WALK
        await bot.sleep(0.05)
    return False


def _is_floor_change(result):
    return result == FLOOR_CHANGED or isinstance(result, dict)


def _log_floor_change(bot, result, prefix):
    if isinstance(result, dict):
        landed = result["pos"]
        bot.log(f"{prefix}   -> ({landed[0]},{landed[1]},{landed[2]}) [floor changed]")
    else:
        after = bot.position
        bot.log(f"{prefix}   -> ({after[0]},{after[1]},{after[2]}) [floor changed]")


def _direction_toward(current, target):
    """Return the Direction enum that moves closest toward target."""
    dx = target[0] - current[0]
    dy = target[1] - current[1]
    if dx == 0 and dy == 0:
        return None
    if abs(dx) > 0 and abs(dy) > 0:
        if dx > 0 and dy > 0:
            return Direction.SOUTHEAST
        if dx > 0 and dy < 0:
            return Direction.NORTHEAST
        if dx < 0 and dy > 0:
            return Direction.SOUTHWEST
        return Direction.NORTHWEST
    if abs(dx) >= abs(dy):
        return Direction.EAST if dx > 0 else Direction.WEST
    return Direction.SOUTH if dy > 0 else Direction.NORTH


# ── Walking ───────────────────────────────────────────────────────

async def _approach_target(bot, target, item_id, prefix=""):
    """Walk toward target in intermediate ground-clicks when beyond click range."""
    for step in range(12):
        current = bot.position
        dx = target[0] - current[0]
        dy = target[1] - current[1]
        chebyshev = max(abs(dx), abs(dy))
        if chebyshev <= MAX_CLICK_RANGE:
            return True
        if current[2] != target[2]:
            return True

        scale = (MAX_CLICK_RANGE - 1) / chebyshev
        mid_x = current[0] + int(round(dx * scale))
        mid_y = current[1] + int(round(dy * scale))

        pkt = build_use_item_packet(mid_x, mid_y, target[2], item_id, 0, 0)
        await bot.inject_to_server(pkt)

        result = await _wait_for_position(
            bot, [mid_x, mid_y, target[2]], 4.0, tolerance=2,
        )
        new_pos = bot.position
        moved = (new_pos[0] != current[0] or new_pos[1] != current[1])

        if moved:
            continue

        d = _direction_toward(current, target)
        if d:
            walk_pkt = build_walk_packet(d)
            for _ in range(3):
                await bot.inject_to_server(walk_pkt)
                await bot.sleep(0.25)
                np = bot.position
                if np[0] != current[0] or np[1] != current[1]:
                    break
            else:
                return False

    cheb = max(abs(bot.position[0] - target[0]), abs(bot.position[1] - target[1]))
    return cheb <= MAX_CLICK_RANGE


async def _walk_to_exact(bot, target, max_steps=8):
    """Walk directionally to reach an exact tile position."""
    start_z = bot.position[2]
    for _ in range(max_steps):
        current = bot.position
        if current[2] != start_z:
            return False
        dx = target[0] - current[0]
        dy = target[1] - current[1]
        if dx == 0 and dy == 0 and current[2] == target[2]:
            return True
        if dx > 0 and dy < 0:
            dir_name = "northeast"
        elif dx > 0 and dy > 0:
            dir_name = "southeast"
        elif dx < 0 and dy < 0:
            dir_name = "northwest"
        elif dx < 0 and dy > 0:
            dir_name = "southwest"
        elif dx > 0:
            dir_name = "east"
        elif dx < 0:
            dir_name = "west"
        elif dy < 0:
            dir_name = "north"
        else:
            dir_name = "south"
        pkt = build_walk_packet(DIR_NAME_TO_ENUM[dir_name])
        await bot.inject_to_server(pkt)
        await bot.sleep(0.35)
    current = bot.position
    return current[0] == target[0] and current[1] == target[1] and current[2] == target[2]


# ── Body-Block Retreat ────────────────────────────────────────────

async def _retreat_unblock(bot, target, prefix=""):
    """Retreat-first body-block handler. Walk away from target to pull mobs.

    Returns True if the bot moved (path potentially cleared), False if stuck.
    """
    gs = _get_state().game_state
    px, py = bot.position[0], bot.position[1]
    tx, ty = target[0], target[1]
    stuck_start = time.time()

    # Step 1: Retreat (away from target)
    retreat_dir = _retreat_direction(px, py, tx, ty)
    bot.log(f"{prefix}   retreat {retreat_dir} (away from target)")
    pkt = build_walk_packet(DIR_NAME_TO_ENUM[retreat_dir])
    await bot.inject_to_server(pkt)
    await bot.sleep(0.4)
    new_pos = bot.position
    if new_pos[0] != px or new_pos[1] != py:
        bot.log(f"{prefix}   retreat OK -> ({new_pos[0]},{new_pos[1]})")
        await bot.sleep(0.3)  # brief pause for mobs to follow
        return True

    # Step 2: Sidestep (perpendicular)
    perp_dirs = _perpendicular_directions(px, py, tx, ty)
    for d in perp_dirs:
        bot.log(f"{prefix}   sidestep {d}")
        pkt = build_walk_packet(DIR_NAME_TO_ENUM[d])
        await bot.inject_to_server(pkt)
        await bot.sleep(0.4)
        new_pos = bot.position
        if new_pos[0] != px or new_pos[1] != py:
            bot.log(f"{prefix}   sidestep OK -> ({new_pos[0]},{new_pos[1]})")
            await bot.sleep(0.3)
            return True

    # Step 3: 8-directional sweep
    # Order: away, perpendicular, toward, remaining
    all_dirs = list(DIR_NAME_TO_ENUM.keys())
    tried = {retreat_dir} | set(perp_dirs)
    toward_dir = OPPOSITE_DIR.get(retreat_dir)
    ordered = []
    # remaining non-tried, non-toward first
    for d in all_dirs:
        if d not in tried and d != toward_dir:
            ordered.append(d)
    if toward_dir and toward_dir not in tried:
        ordered.append(toward_dir)

    for d in ordered:
        pkt = build_walk_packet(DIR_NAME_TO_ENUM[d])
        await bot.inject_to_server(pkt)
        await bot.sleep(0.35)
        new_pos = bot.position
        if new_pos[0] != px or new_pos[1] != py:
            bot.log(f"{prefix}   escape {d} OK -> ({new_pos[0]},{new_pos[1]})")
            await bot.sleep(0.3)
            return True

    # All directions failed
    bot.log(f"{prefix}   all retreat/escape failed, stuck at ({px},{py})")
    return False


async def _handle_body_block(bot, node, cancel_count, prefix=""):
    """Full body-block handler with retreat-first and kill-blocker last resort.

    Returns:
        "resolved" - block cleared, retry walk
        "killed" - had to kill blocker, retry walk
        "stuck" - couldn't resolve
    """
    gs = _get_state().game_state
    target = node["target"]

    # Try retreat up to 2 cycles
    for cycle in range(2):
        moved = await _retreat_unblock(bot, target, prefix)
        if moved:
            return "resolved"

    # Last resort: kill the blocking monster
    px, py, pz = bot.position
    blocker = _get_nearest_monster_on_path(gs, px, py, pz, target[0], target[1])
    if blocker is not None:
        info = gs.creatures.get(blocker, {})
        bot.log(f"{prefix}   LAST RESORT: killing blocker {info.get('name', '?')} "
                f"(0x{blocker:08X}) hp={info.get('health', '?')}%")
        gs.force_target = blocker
        was_luring = gs.lure_active
        gs.lure_active = False
        kill_start = time.time()
        while time.time() - kill_start < PAUSE_MAX_TIMEOUT:
            creature = gs.creatures.get(blocker)
            if not creature or creature.get("health", 0) <= 0:
                bot.log(f"{prefix}   blocker killed")
                break
            await bot.sleep(0.2)
        gs.force_target = None
        gs.lure_active = was_luring
        return "killed"

    return "stuck"


# ── Execution: walk_to ────────────────────────────────────────────

async def _execute_walk_to(bot, node, prefix="", exact=False):
    """Send use_item on ground tile (server pathfinds) and wait for arrival."""
    target = node["target"]
    item_id = node.get("item_id", 4449)
    current = bot.position
    start_z = current[2]
    dist = _distance(current, target)
    cancel_count = 0
    last_cancel_pos = None
    block_start_time = None

    bot.log(f"{prefix} walk_to ({target[0]},{target[1]},{target[2]}) dist={dist}{' [exact]' if exact else ''}")

    # Phase 0: approach if beyond click range
    chebyshev = max(abs(current[0] - target[0]), abs(current[1] - target[1]))
    if chebyshev > MAX_CLICK_RANGE and current[2] == target[2]:
        bot.log(f"{prefix}   too far (cheb={chebyshev}), approaching...")
        approached = await _approach_target(bot, target, item_id, prefix)
        if not approached:
            return False
        current = bot.position
        start_z = current[2]
        dist = _distance(current, target)

    attempt = 0
    tolerance = 0 if exact else WALK_TO_TOLERANCE

    while attempt < MAX_RETRIES and cancel_count < MAX_CANCEL_WALKS:
        current = bot.position
        dist = _distance(current, target)

        if dist <= tolerance and current[2] == target[2]:
            bot.log(f"{prefix}   -> ({current[0]},{current[1]},{current[2]})")
            return True

        if exact and current[2] != start_z:
            bot.log(f"{prefix}   -> ({current[0]},{current[1]},{current[2]}) [floor changed]")
            return True

        pkt = build_use_item_packet(
            target[0], target[1], target[2],
            item_id, 0, 0,
        )
        await bot.inject_to_server(pkt)

        timeout = max(dist * 0.3 + 2.0, 3.0)
        wait_tol = 1 if exact else WALK_TO_TOLERANCE
        result = await _wait_for_position(bot, target, timeout,
                                          tolerance=wait_tol,
                                          abort_on_floor_change=exact)

        if _is_floor_change(result):
            _log_floor_change(bot, result, prefix)
            return True

        if result == CANCEL_WALK:
            pos = bot.position
            cur_pos = (pos[0], pos[1], pos[2])
            cancel_count += 1
            bot.log(f"{prefix}   cancel_walk #{cancel_count} at ({pos[0]},{pos[1]},{pos[2]})")

            # Track how long we've been stuck at same position
            if cur_pos == last_cancel_pos:
                if block_start_time is None:
                    block_start_time = time.time()
            else:
                block_start_time = time.time()
                cancel_count = 1
            last_cancel_pos = cur_pos

            # Body-block detection: 3+ cancel_walks at same position
            if cancel_count >= BLOCK_DETECT_THRESHOLD:
                gs = _get_state().game_state
                nearby = _count_nearby_monsters(gs, 2)

                if nearby > 0:
                    # Retreat-first body-block handler
                    result_bb = await _handle_body_block(bot, node, cancel_count, prefix)
                    if result_bb in ("resolved", "killed"):
                        cancel_count = 0
                        last_cancel_pos = None
                        block_start_time = None
                        continue  # retry walk

                # No monsters blocking — try directional escape
                bot.log(f"{prefix}   no monsters, trying directional escape")
                escaped = False
                for escape_dir in ["north", "east", "south", "west"]:
                    escape_pkt = build_walk_packet(DIR_NAME_TO_ENUM[escape_dir])
                    await bot.inject_to_server(escape_pkt)
                    await bot.sleep(0.3)
                    new_pos = bot.position
                    if (new_pos[0], new_pos[1], new_pos[2]) != cur_pos:
                        bot.log(f"{prefix}   escaped {escape_dir}")
                        escaped = True
                        cancel_count = 0
                        last_cancel_pos = None
                        block_start_time = None
                        break
                if not escaped:
                    bot.log(f"{prefix}   escape failed, skipping node")
                    return False

            await bot.sleep(0.2)
            continue

        # Non-cancel result
        cancel_count = 0
        last_cancel_pos = None
        block_start_time = None

        if result is True:
            after = bot.position
            if not exact:
                bot.log(f"{prefix}   -> ({after[0]},{after[1]},{after[2]})")
                return True
            if _distance(after, target) == 0 and after[2] == target[2]:
                bot.log(f"{prefix}   -> ({after[0]},{after[1]},{after[2]})")
                return True

        # Exact: directional walk for last tiles
        if exact:
            after = bot.position
            if after[2] != start_z:
                bot.log(f"{prefix}   -> ({after[0]},{after[1]},{after[2]}) [floor changed]")
                return True
            if after[2] == target[2] and _distance(after, target) <= 5:
                ok = await _walk_to_exact(bot, target, max_steps=6)
                after = bot.position
                if after[2] != start_z:
                    bot.log(f"{prefix}   -> ({after[0]},{after[1]},{after[2]}) [floor changed]")
                    return True
                if ok:
                    bot.log(f"{prefix}   -> ({after[0]},{after[1]},{after[2]}) [directional]")
                    return True

        attempt += 1

    current = bot.position
    dist = _distance(current, target)
    bot.log(f"{prefix}   failed: at ({current[0]},{current[1]},{current[2]}) dist={dist}")
    return dist <= tolerance + 2


# ── Execution: use_item / use_item_ex ─────────────────────────────

def _check_tile_transform(bot, x, y, z, since_time):
    state = _get_state()
    gs = state.game_state
    for ts, tx, ty, tz in gs.tile_updates:
        if ts >= since_time and tx == x and ty == y and tz == z:
            return True
    return False


async def _quick_change_check(bot, gs, before, before_time):
    for evt in gs.server_events:
        ts, etype, edata = evt
        if ts > before_time and etype in ("floor_change_up", "floor_change_down"):
            return edata["pos"]
    after = bot.position
    if after[2] != before[2]:
        return after
    if abs(after[0] - before[0]) > 1 or abs(after[1] - before[1]) > 1:
        return after
    return None


async def _wait_for_change(bot, gs, before, before_time, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = await _quick_change_check(bot, gs, before, before_time)
        if result is not None:
            return result
        await bot.sleep(0.05)
    return None


async def _execute_use_item_node(bot, node, prefix=""):
    """Execute an exact use_item (stairs/doors/ladders)."""
    label = node.get("label", f"item {node['item_id']}")
    target = node["target"]
    bot.log(f"{prefix} {label} at ({target[0]},{target[1]},{target[2]})")

    current = bot.position
    dist = max(abs(current[0] - target[0]), abs(current[1] - target[1]))
    wrong_floor = current[2] != target[2]

    if dist > 1 or wrong_floor:
        walk_target = node.get("player_pos", target)
        walk_node = {"target": walk_target, "item_id": 4449, "stack_pos": 1}
        walk_ok = await _execute_walk_to(bot, walk_node, prefix + "  ", exact=True)
        if not walk_ok:
            return False

    pkt = build_use_item_packet(
        node["x"], node["y"], node["z"],
        node["item_id"], node.get("stack_pos", 0), node.get("index", 0),
    )

    current = bot.position
    is_floor_change = target[2] != current[2]
    gs = _get_state().game_state

    if is_floor_change:
        for attempt in range(MAX_RETRIES):
            before = bot.position
            before_time = time.time()
            await bot.inject_to_server(pkt)
            await bot.sleep(0.5)
            if bot.position[2] == before[2]:
                await bot.inject_to_server(pkt)
            deadline = time.time() + USE_ITEM_TIMEOUT
            while time.time() < deadline:
                for evt in gs.server_events:
                    ts, etype, edata = evt
                    if ts > before_time and etype in ("floor_change_up", "floor_change_down"):
                        landed = edata["pos"]
                        bot.log(f"{prefix}   [SUCCESS] stairs -> ({landed[0]},{landed[1]},{landed[2]})")
                        return True
                after = bot.position
                if after[2] != before[2]:
                    bot.log(f"{prefix}   [SUCCESS] -> ({after[0]},{after[1]},{after[2]})")
                    return True
                await bot.sleep(0.05)
        bot.log(f"{prefix}   [FAILURE] still at z={bot.position[2]}")
        return False
    else:
        for attempt in range(MAX_RETRIES):
            before = bot.position
            before_time = time.time()
            await bot.inject_to_server(pkt)
            await bot.sleep(0.5)
            if (not _check_tile_transform(bot, target[0], target[1], target[2], before_time)
                    and bot.position == before):
                await bot.inject_to_server(pkt)
            start = time.time()
            while time.time() - start < USE_ITEM_TIMEOUT:
                for evt in gs.server_events:
                    ts, etype, edata = evt
                    if ts > before_time and etype in ("floor_change_up", "floor_change_down"):
                        landed = edata["pos"]
                        bot.log(f"{prefix}   [SUCCESS] floor change -> ({landed[0]},{landed[1]},{landed[2]})")
                        return True
                if _check_tile_transform(bot, target[0], target[1], target[2], before_time):
                    bot.log(f"{prefix}   [SUCCESS] tile transform")
                    return True
                after = bot.position
                if after[2] != before[2]:
                    bot.log(f"{prefix}   [SUCCESS] floor change -> ({after[0]},{after[1]},{after[2]})")
                    return True
                if after[0] != before[0] or after[1] != before[1]:
                    bot.log(f"{prefix}   [SUCCESS] -> ({after[0]},{after[1]},{after[2]})")
                    return True
                await bot.sleep(0.1)
        # Fallback: USE_ITEM_EX from hotkey
        ex_pkt = build_use_item_ex_packet(
            0xFFFF, 0, 0,
            node["item_id"], 0,
            node["x"], node["y"], node["z"], 0,
        )
        before = bot.position
        before_time = time.time()
        await bot.inject_to_server(ex_pkt)
        deadline = time.time() + USE_ITEM_TIMEOUT
        while time.time() < deadline:
            for evt in gs.server_events:
                ts, etype, edata = evt
                if ts > before_time and etype in ("floor_change_up", "floor_change_down"):
                    landed = edata["pos"]
                    bot.log(f"{prefix}   [SUCCESS] fallback -> ({landed[0]},{landed[1]},{landed[2]})")
                    return True
            after = bot.position
            if after[2] != before[2] or after[0] != before[0] or after[1] != before[1]:
                bot.log(f"{prefix}   [SUCCESS] fallback -> ({after[0]},{after[1]},{after[2]})")
                return True
            if _check_tile_transform(bot, target[0], target[1], target[2], before_time):
                bot.log(f"{prefix}   [SUCCESS] fallback tile transform")
                return True
            await bot.sleep(0.1)
        bot.log(f"{prefix}   [FAILURE] no change")
        return False


async def _execute_use_item_ex_node(bot, node, prefix=""):
    """Execute a use_item_ex (rope/shovel)."""
    label = node.get("label", f"item {node['item_id']}")
    target = node["target"]
    item_id = node["item_id"]
    to_x, to_y, to_z = node["to_x"], node["to_y"], node["to_z"]
    to_sp = node.get("to_stack_pos", 0)
    from_x = node["from_x"]
    from_y = node["from_y"]
    from_z = node["from_z"]
    is_container = (from_x == 0xFFFF)

    bot.log(f"{prefix} {label} -> ({target[0]},{target[1]},{target[2]})")
    gs = _get_state().game_state
    player_id = gs.player_id

    # Attempt 1: hotkey-style
    pkt_hotkey = build_use_item_ex_packet(0xFFFF, 0, 0, item_id, 0, to_x, to_y, to_z, 0)
    before = bot.position
    before_time = time.time()
    await bot.inject_to_server(pkt_hotkey)
    await bot.sleep(0.5)
    if bot.position == before:
        await bot.inject_to_server(pkt_hotkey)
    result = await _wait_for_change(bot, gs, before, before_time, 3.0)
    if result is not None:
        bot.log(f"{prefix}   [SUCCESS] hotkey -> ({result[0]},{result[1]},{result[2]})")
        return True

    # Attempt 2: USE_ON_CREATURE on self
    if player_id:
        pkt_oc = build_use_on_creature_packet(0xFFFF, 0, 0, item_id, 0, player_id)
        before = bot.position
        before_time = time.time()
        await bot.inject_to_server(pkt_oc)
        result = await _wait_for_change(bot, gs, before, before_time, 2.0)
        if result is not None:
            bot.log(f"{prefix}   [SUCCESS] hotkey OC -> ({result[0]},{result[1]},{result[2]})")
            return True

    # Attempt 3: recorded slot
    pkt = build_use_item_ex_packet(from_x, from_y, from_z, item_id,
                                   node.get("stack_pos", 0), to_x, to_y, to_z, to_sp)
    before = bot.position
    before_time = time.time()
    await bot.inject_to_server(pkt)
    await bot.sleep(0.5)
    if bot.position == before:
        await bot.inject_to_server(pkt)
    result = await _wait_for_change(bot, gs, before, before_time, 3.0)
    if result is not None:
        bot.log(f"{prefix}   [SUCCESS] recorded -> ({result[0]},{result[1]},{result[2]})")
        return True

    # Attempt 4: USE_ON_CREATURE recorded slot
    if is_container and player_id:
        before = bot.position
        before_time = time.time()
        pkt_oc = build_use_on_creature_packet(from_x, from_y, from_z,
                                              item_id, node.get("stack_pos", 0), player_id)
        await bot.inject_to_server(pkt_oc)
        result = await _wait_for_change(bot, gs, before, before_time, 2.0)
        if result is not None:
            bot.log(f"{prefix}   [SUCCESS] OC -> ({result[0]},{result[1]},{result[2]})")
            return True

        # Attempt 5: scan container slots
        seen_cids = set()
        for cid in [from_y, 0x40, 0x41, 0x42]:
            if cid in seen_cids:
                continue
            seen_cids.add(cid)
            before = bot.position
            before_time = time.time()
            for slot in range(20):
                if cid == from_y and slot == from_z:
                    continue
                pkt2 = build_use_on_creature_packet(0xFFFF, cid, slot, item_id, slot, player_id)
                await bot.inject_to_server(pkt2)
                for _ in range(6):
                    await bot.sleep(0.05)
                    r = await _quick_change_check(bot, gs, before, before_time)
                    if r is not None:
                        bot.log(f"{prefix}   [SUCCESS] cid=0x{cid:02X} slot {slot}")
                        return True

    bot.log(f"{prefix}   [FAILURE] no change")
    return False


async def _execute_walk_steps(bot, node, prefix=""):
    """Execute raw directional walks for floor transitions."""
    steps = node["steps"]
    target = node["target"]
    start = node.get("start")
    label = node.get("label", "floor change")

    bot.log(f"{prefix} {label} -> ({target[0]},{target[1]},{target[2]}) [{len(steps)} steps]")

    if start:
        current = bot.position
        dx = start[0] - current[0]
        dy = start[1] - current[1]
        if dx != 0 or dy != 0 or current[2] != start[2]:
            ok = await _walk_to_exact(bot, start)
            if not ok:
                return False

    for step in steps:
        dir_enum = DIR_NAME_TO_ENUM.get(step["direction"])
        if dir_enum is None:
            continue
        pkt = build_walk_packet(dir_enum)
        await bot.inject_to_server(pkt)
        await bot.sleep(0.4)

    arrived = await _wait_for_position(bot, target, timeout=5.0, tolerance=2)
    after = bot.position
    if not arrived:
        if after[2] == target[2]:
            bot.log(f"{prefix}   floor changed -> ({after[0]},{after[1]},{after[2]})")
            return True
        return False
    bot.log(f"{prefix}   -> ({after[0]},{after[1]},{after[2]})")
    return True


# ── Lure Fight Loop ──────────────────────────────────────────────

async def _lure_fight(bot, gs, telemetry, lure_distance, prefix=""):
    """Execute a lure fight: disable luring, wait for all monsters to die,
    record fight summary to telemetry."""
    nearby_at_start = _count_nearby_monsters(gs, lure_distance)
    gs.fight_start_mana = gs.mana
    gs.fight_start_mana_max = gs.max_mana
    gs.lure_active = False  # enable auto_targeting + auto_combat
    fight_start = time.time()

    # Snapshot HP for unreachable detection
    hp_snapshot = {}
    for cid, info in gs.creatures.items():
        if cid >= MONSTER_ID_MIN and 0 < info.get("health", 0) <= 100:
            hp_snapshot[cid] = info.get("health", 0)

    state = _get_state()
    kills_before = gs.session_kills

    while state.playback_active and bot.is_connected:
        remaining = _count_nearby_monsters(gs, lure_distance)
        has_target = (gs.attack_target_id and gs.attack_target_id >= MONSTER_ID_MIN)
        if remaining == 0 and not has_target:
            break
        elapsed = time.time() - fight_start
        if elapsed > PAUSE_MAX_TIMEOUT:
            bot.log(f"{prefix} Fight timeout ({PAUSE_MAX_TIMEOUT}s)")
            break
        if gs.in_protection_zone:
            break
        # Unreachable detection
        if elapsed > NO_DAMAGE_TIMEOUT:
            now_check = time.time()
            for cid, info in list(gs.creatures.items()):
                if (cid >= MONSTER_ID_MIN
                        and 0 < info.get("health", 0) <= 100
                        and cid in hp_snapshot
                        and info.get("health", 0) >= hp_snapshot[cid]
                        and cid not in gs.unreachable_creatures):
                    gs.unreachable_creatures[cid] = now_check + UNREACHABLE_EXPIRY
                    bot.log(f"{prefix} {info.get('name', '?')} unreachable — blacklisting")
            hp_snapshot = {
                cid: info.get("health", 0) for cid, info in gs.creatures.items()
                if cid >= MONSTER_ID_MIN and 0 < info.get("health", 0) <= 100
                and cid not in gs.unreachable_creatures
            }
            fight_start = time.time()
        await bot.sleep(0.2)

    duration = time.time() - fight_start
    kills = gs.session_kills - kills_before
    mana_used_pct = 0
    if gs.fight_start_mana_max > 0:
        mana_used_pct = (gs.fight_start_mana - gs.mana) / gs.fight_start_mana_max * 100

    telemetry.record_fight(
        kills=kills,
        duration_s=duration,
        mana_used_pct=max(0, mana_used_pct),
        nearby_at_start=nearby_at_start,
        lure_count_used=nearby_at_start,
    )

    bot.log(f"{prefix} Fight done: {kills} kills in {duration:.1f}s, "
            f"mana used {mana_used_pct:.0f}%")

    gs.lure_active = True  # resume luring


# ── Main Playback Loop ───────────────────────────────────────────

async def _run_playback(bot):
    state = _get_state()

    if not hasattr(state, 'playback_failed_nodes'):
        state.playback_failed_nodes = set()

    settings = _get_settings()
    adaptive_lure = settings.get("adaptive_lure", True)
    min_lure = settings.get("min_lure_count", DEFAULT_MIN_LURE)
    max_lure = settings.get("max_lure_count", DEFAULT_MAX_LURE)
    lure_distance = settings.get("lure_distance", DEFAULT_LURE_DISTANCE)
    lure_timeout = settings.get("lure_timeout", DEFAULT_LURE_TIMEOUT)
    skip_dead = settings.get("skip_dead_segments", True)
    linger_hot = settings.get("linger_in_hot_zones", True)
    linger_max = settings.get("linger_max_seconds", LINGER_MAX)

    current_lure_count = min_lure
    lure_ema = float(min_lure)
    stable_count = 0

    while state.playback_active:
        if not bot.is_connected:
            await bot.sleep(1)
            continue

        rec_name = state.playback_recording_name
        rec = load_recording(rec_name)
        if rec is None:
            bot.log(f"Recording '{rec_name}' not found")
            break

        waypoints = rec.get("waypoints", [])
        if not waypoints:
            bot.log(f"Recording '{rec_name}' has no waypoints")
            break

        actions_map = build_actions_map(rec)
        state.playback_actions_map = actions_map
        state.playback_total = len(actions_map)

        # Load or create telemetry
        if not hasattr(state, 'telemetry') or state.telemetry is None:
            state.telemetry = FarmingTelemetry.load(rec_name)
        telemetry = state.telemetry

        if state.playback_start_time == 0:
            state.playback_start_time = time.time()
            state.playback_start_experience = state.game_state.experience
            state.playback_start_level = state.game_state.level
            state.playback_senzu_series = []
            state._last_senzu_sample_time = 0

        pos = bot.position
        loop_num = state.playback_loop_count
        bot.log("")
        bot.log("=" * 40)
        bot.log(f"  CAVEBOT v2: {rec_name} (loop #{loop_num})")
        bot.log("=" * 40)
        bot.log(f"Lure: {current_lure_count} (range {min_lure}-{max_lure}), "
                f"distance={lure_distance}, timeout={lure_timeout}s")
        if loop_num >= DEAD_SEGMENT_MIN_LOOPS and skip_dead:
            dead_count = sum(1 for idx in range(len(actions_map))
                             if actions_map[idx]["type"] == "walk_to"
                             and not actions_map[idx].get("exact")
                             and telemetry.segment_rating(idx) == "dead")
            bot.log(f"Dead segments to skip: {dead_count}")
        bot.log(actions_map_to_text(actions_map))

        gs = state.game_state
        gs.lure_active = True
        bot.log("Lure mode ON — suppressing auto-targeting while walking")

        lure_with_monsters_since = 0.0
        if not hasattr(state, 'segment_enter_time'):
            state.segment_enter_time = {}
        if not hasattr(state, 'segment_stats'):
            state.segment_stats = {}

        i = 0
        consecutive_walk_failures = 0
        while i < len(actions_map):
            node = actions_map[i]
            if not state.playback_active or not bot.is_connected:
                break

            state.playback_index = i
            state.segment_enter_time[i] = time.time()
            state.playback_minimap = build_sequence_minimaps(
                actions_map, i, bot.position,
                failed_nodes=state.playback_failed_nodes,
            )

            prefix = f"[{i+1}/{len(actions_map)}]"
            ntype = node["type"]

            # ── Floor skip ──
            player_pos = bot.position
            player_z = player_pos[2]
            expected_z = _node_expected_z(node)
            if player_z != expected_z:
                best_j = None
                best_dist = float("inf")
                for j in range(len(actions_map)):
                    if _node_expected_z(actions_map[j]) == player_z:
                        t = actions_map[j]["target"]
                        d = max(abs(player_pos[0] - t[0]), abs(player_pos[1] - t[1]))
                        if d < best_dist:
                            best_dist = d
                            best_j = j
                if best_j is not None:
                    bot.log(f"{prefix} Z mismatch, jumping to [{best_j+1}]")
                    i = best_j
                    continue
                else:
                    i += 1
                    continue

            # ── Dead segment skipping ──
            if (skip_dead
                    and loop_num >= DEAD_SEGMENT_MIN_LOOPS
                    and ntype == "walk_to"
                    and not node.get("exact")
                    and not _is_next_node_floor_change(actions_map, i, player_z)
                    and not _next_node_is_interaction(actions_map, i)):
                seg_stats = telemetry.segment_stats.get(i)
                if (seg_stats
                        and seg_stats.get("kills", 0) == 0
                        and seg_stats.get("entries", 0) >= DEAD_SEGMENT_MIN_ENTRIES):
                    bot.log(f"{prefix} SKIP dead segment (0 kills in {seg_stats['entries']} visits)")
                    i += 1
                    continue

            # ── Lure check (before executing node) ──
            if not gs.in_protection_zone and ntype == "walk_to":
                now = time.time()
                nearby = _count_nearby_monsters(gs, lure_distance, targetable_only=True)
                floor_change_ahead = _is_next_node_floor_change(actions_map, i, player_z)

                if nearby > 0:
                    if lure_with_monsters_since == 0.0:
                        lure_with_monsters_since = now
                else:
                    lure_with_monsters_since = 0.0

                timed_out = (nearby > 0
                             and lure_with_monsters_since > 0
                             and now - lure_with_monsters_since >= lure_timeout)

                # Double timeout: don't fight 1 lonely mob unless 2x timeout
                double_timed_out = (nearby == 1
                                    and lure_with_monsters_since > 0
                                    and now - lure_with_monsters_since >= lure_timeout * 2)

                should_fight = (
                    nearby >= current_lure_count
                    or (nearby > 0 and floor_change_ahead)
                    or (nearby >= 2 and timed_out)
                    or double_timed_out
                )

                if should_fight:
                    if nearby >= current_lure_count:
                        reason = f"{nearby}/{current_lure_count} mobs (lure full)"
                    elif floor_change_ahead:
                        reason = f"floor change ahead ({nearby} mobs)"
                    elif double_timed_out:
                        reason = f"double timeout ({nearby} mob)"
                    else:
                        reason = f"timeout {lure_timeout}s ({nearby} mobs)"
                    bot.log(f"{prefix} LURE FIGHT: {reason}")
                    await _lure_fight(bot, gs, telemetry, lure_distance, prefix)
                    lure_with_monsters_since = 0.0

                    # Adaptive lure count update
                    if adaptive_lure and len(telemetry.fight_log) >= LURE_STABILITY_FIGHTS:
                        new_count = _compute_adaptive_lure_count(
                            gs, telemetry, current_lure_count, min_lure, max_lure)
                        lure_ema = LURE_EMA_ALPHA * new_count + (1 - LURE_EMA_ALPHA) * lure_ema
                        rounded = round(lure_ema)
                        if rounded != current_lure_count:
                            stable_count += 1
                            if stable_count >= LURE_STABILITY_FIGHTS:
                                old = current_lure_count
                                current_lure_count = rounded
                                stable_count = 0
                                bot.log(f"{prefix} Adaptive lure: {old} -> {current_lure_count}")
                        else:
                            stable_count = 0

                elif nearby > 0:
                    elapsed = now - lure_with_monsters_since if lure_with_monsters_since > 0 else 0
                    bot.log(f"{prefix} Luring: {nearby}/{current_lure_count} mobs, "
                            f"timeout {elapsed:.1f}/{lure_timeout}s")
                    gs.lure_active = True

            # ── Execute node ──
            if ntype == "walk_to":
                exact = node.get("exact", False)
                success = await _execute_walk_to(bot, node, prefix, exact=exact)
            elif ntype == "use_item":
                success = await _execute_use_item_node(bot, node, prefix)
            elif ntype == "use_item_ex":
                success = await _execute_use_item_ex_node(bot, node, prefix)
            elif ntype == "walk_steps":
                success = await _execute_walk_steps(bot, node, prefix)
            else:
                bot.log(f"{prefix} Unknown node type: {ntype}")
                success = True

            # Walk failed in lure mode — fight blockers then retry
            if not success and ntype == "walk_to":
                gs_retry = state.game_state
                if not gs_retry.in_protection_zone:
                    retry_nearby = _count_nearby_monsters(gs_retry, lure_distance)
                    retry_has_target = (gs_retry.attack_target_id
                                        and gs_retry.attack_target_id >= MONSTER_ID_MIN)
                    if retry_nearby > 0 or retry_has_target:
                        bot.log(f"{prefix} Walk blocked, fighting {retry_nearby} creatures")
                        await _lure_fight(bot, gs_retry, telemetry, lure_distance, prefix)
                        lure_with_monsters_since = 0.0
                        continue  # retry same node

            if not success:
                state.playback_failed_nodes.add(i)
                if ntype == "walk_to":
                    consecutive_walk_failures += 1
                else:
                    consecutive_walk_failures = 0

                if consecutive_walk_failures >= CONSECUTIVE_FAIL_RESYNC:
                    player_pos = bot.position
                    best_idx = None
                    best_d = float("inf")
                    # Search ALL nodes (forward + backward) for nearest on same floor
                    for j in range(len(actions_map)):
                        if j == i:
                            continue  # skip current failing node
                        if _node_expected_z(actions_map[j]) == player_pos[2]:
                            t = actions_map[j]["target"]
                            d = max(abs(player_pos[0] - t[0]), abs(player_pos[1] - t[1]))
                            if d < best_d:
                                best_d = d
                                best_idx = j
                    if best_idx is not None and best_idx != i:
                        direction = "fwd" if best_idx > i else "back"
                        bot.log(f"{prefix} Re-sync ({direction}): jump [{best_idx+1}/{len(actions_map)}] cheb={best_d}")
                        i = best_idx
                        consecutive_walk_failures = 0
                        continue
            else:
                consecutive_walk_failures = 0

            # ── HOT zone lingering (after successful walk_to) ──
            if (success and linger_hot and ntype == "walk_to"
                    and not node.get("exact")
                    and telemetry.segment_rating(i) == "high"):
                respawn_est = telemetry.spawn_map.avg_respawn_at(bot.position)
                if respawn_est and respawn_est < 15:
                    linger_end = time.time() + min(respawn_est * 1.5, linger_max)
                    bot.log(f"{prefix} HOT zone — lingering up to {min(respawn_est * 1.5, linger_max):.0f}s")
                    while time.time() < linger_end:
                        if not state.playback_active or not bot.is_connected:
                            break
                        nearby = _count_nearby_monsters(gs, lure_distance, targetable_only=True)
                        if nearby > 0:
                            bot.log(f"{prefix} Respawn detected! {nearby} mobs")
                            # Keep luring from this spot — don't fight immediately
                            # unless we've reached lure count
                            if nearby >= current_lure_count:
                                await _lure_fight(bot, gs, telemetry, lure_distance, prefix)
                            break
                        await bot.sleep(0.5)

            i += 1

        # ── End of loop ──
        state.playback_minimap = build_sequence_minimaps(
            actions_map, len(actions_map), bot.position,
            failed_nodes=state.playback_failed_nodes,
        )

        if state.playback_active and state.playback_loop:
            state.playback_loop_count += 1
            loop_num = state.playback_loop_count

            # Accumulate segment stats into telemetry
            _accumulate_segment_stats(state, telemetry)
            state.segment_enter_time.clear()

            # Save telemetry and recording stats
            telemetry.save(rec_name)
            _save_stats(rec_name, telemetry)

            bot.log(f"Looping '{rec_name}'... (loop #{loop_num})")
            continue
        else:
            break

    # Cleanup
    if hasattr(state, 'telemetry') and state.telemetry is not None:
        state.telemetry.save(state.playback_recording_name)
    _cleanup_state(state)


def _accumulate_segment_stats(state, telemetry):
    """Process kill_log and segment timing into telemetry."""
    gs = state.game_state
    segment_enter_time = state.segment_enter_time

    kills_by_seg: dict[int, list[dict]] = {}
    for kill in gs.kill_log:
        seg = kill.get("segment")
        if seg is not None:
            kills_by_seg.setdefault(seg, []).append(kill)

    sorted_segs = sorted(segment_enter_time.keys())
    now = time.time()
    seg_durations: dict[int, float] = {}
    for idx, seg_idx in enumerate(sorted_segs):
        enter_t = segment_enter_time[seg_idx]
        exit_t = segment_enter_time[sorted_segs[idx + 1]] if idx + 1 < len(sorted_segs) else now
        seg_durations[seg_idx] = exit_t - enter_t

    for seg_idx in set(list(kills_by_seg.keys()) + sorted_segs):
        kills = kills_by_seg.get(seg_idx, [])
        xp = sum(k.get("xp", 0) for k in kills)
        duration = seg_durations.get(seg_idx, 0)
        telemetry.update_segment_stats(seg_idx, len(kills), xp, duration)

    gs.kill_log.clear()


def _save_stats(rec_name, telemetry):
    """Persist current playback stats into the recording JSON."""
    state = _get_state()
    gs = state.game_state
    elapsed = time.time() - state.playback_start_time if state.playback_start_time else 0
    elapsed_hours = elapsed / 3600 if elapsed > 0 else 0
    if elapsed_hours <= 0:
        return
    xp_gained = gs.experience - state.playback_start_experience if state.playback_start_experience else 0
    xp_per_hour = int(xp_gained / elapsed_hours)
    senzu_per_hour = round(getattr(state, 'playback_senzu_used', 0) / elapsed_hours, 1)

    from datetime import datetime, timezone
    stats_dict = {
        "xp_per_hour": xp_per_hour,
        "kills_per_hour": round(gs.session_kills / elapsed_hours, 1),
        "senzu_per_hour": senzu_per_hour,
        "session_seconds": int(elapsed),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "adaptive_lure_fights": len(telemetry.fight_log),
    }
    save_recording_stats(rec_name, stats_dict)


def _cleanup_state(state):
    """Reset all playback state."""
    state.playback_active = False
    state.playback_recording_name = ""
    state.playback_index = 0
    state.playback_total = 0
    state.playback_actions_map = []
    state.playback_minimap = None
    state.playback_failed_nodes = set()
    state.playback_loop_count = 0
    state.playback_kills = 0
    state.playback_senzu_used = 0
    state.playback_start_time = 0
    state.playback_start_experience = 0
    state.playback_start_level = 0
    state.playback_senzu_series = []
    state._last_senzu_sample_time = 0
    state.segment_stats = {}
    state.segment_enter_time = {}
    state.game_state.kill_log.clear()
    state.game_state.lure_active = False
    state.game_state.force_target = None
    state.telemetry = None


async def run(bot):
    """Entry point — wraps _run_playback with guaranteed lure_active cleanup."""
    try:
        await _run_playback(bot)
    finally:
        state = _get_state()
        state.game_state.lure_active = False
        state.game_state.force_target = None
