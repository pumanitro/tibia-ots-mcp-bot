"""Cavebot playback — replays a recorded navigation path using actions_map."""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import (
    Direction,
    build_use_item_packet,
    build_use_item_ex_packet,
    build_walk_packet,
)
from cavebot import load_recording, build_actions_map, build_all_minimaps, build_sequence_minimaps, actions_map_to_text

USE_ITEM_TIMEOUT = 5.0
WALK_TO_TOLERANCE = 2   # tiles — close enough for walk_to nodes
MAX_RETRIES = 5

# Map direction name strings to Direction enum values
DIR_NAME_TO_ENUM = {
    "north": Direction.NORTH, "south": Direction.SOUTH,
    "east": Direction.EAST, "west": Direction.WEST,
    "northeast": Direction.NORTHEAST, "southeast": Direction.SOUTHEAST,
    "southwest": Direction.SOUTHWEST, "northwest": Direction.NORTHWEST,
}


def _get_state():
    return sys.modules["__main__"].state


def _distance(a, b):
    """Manhattan distance (ignoring z)."""
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


FLOOR_CHANGED = "floor_changed"

async def _wait_for_position(bot, expected_pos, timeout, tolerance=0, abort_on_floor_change=False):
    """Wait until game_state.position is within tolerance of expected_pos (or timeout).
    Returns True if arrived, FLOOR_CHANGED if floor changed (when abort_on_floor_change), else False.
    """
    start = time.time()
    start_z = bot.position[2]
    while time.time() - start < timeout:
        current = bot.position
        if (abs(current[0] - expected_pos[0]) <= tolerance
                and abs(current[1] - expected_pos[1]) <= tolerance
                and current[2] == expected_pos[2]):
            return True
        if abort_on_floor_change and current[2] != start_z:
            return FLOOR_CHANGED
        await bot.sleep(0.05)
    return False


async def _execute_walk_to(bot, node, prefix="", exact=False):
    """Send use_item on ground tile (server pathfinds) and wait for arrival."""
    target = node["target"]
    item_id = node.get("item_id", 4449)
    stack_pos = node.get("stack_pos", 1)
    current = bot.position
    start_z = current[2]
    dist = _distance(current, target)

    bot.log(f"{prefix} walk_to ({target[0]},{target[1]},{target[2]}) dist={dist}{' [exact]' if exact else ''}")

    for attempt in range(MAX_RETRIES):
        current = bot.position
        dist = _distance(current, target)

        # Already at target (or close enough for non-exact)
        tolerance = 0 if exact else WALK_TO_TOLERANCE
        if dist <= tolerance and current[2] == target[2]:
            bot.log(f"{prefix}   -> ({current[0]},{current[1]},{current[2]})")
            return True

        # Floor already changed (stairs auto-triggered) — success
        if exact and current[2] != start_z:
            bot.log(f"{prefix}   -> ({current[0]},{current[1]},{current[2]}) [floor changed]")
            return True

        # Step 1: pathfind walk (use normal tolerance — get close)
        pkt = build_use_item_packet(
            target[0], target[1], target[2],
            item_id, stack_pos, 0,
        )
        await bot.inject_to_server(pkt)

        timeout = max(dist * 0.3 + 2.0, 3.0)
        result = await _wait_for_position(bot, target, timeout,
                                          tolerance=WALK_TO_TOLERANCE,
                                          abort_on_floor_change=exact)

        if result == FLOOR_CHANGED:
            after = bot.position
            bot.log(f"{prefix}   -> ({after[0]},{after[1]},{after[2]}) [floor changed]")
            return True

        if result is True:
            after = bot.position
            # For non-exact, close enough is fine
            if not exact:
                bot.log(f"{prefix}   -> ({after[0]},{after[1]},{after[2]})")
                return True
            # For exact, check if we're actually on the tile
            if _distance(after, target) == 0 and after[2] == target[2]:
                bot.log(f"{prefix}   -> ({after[0]},{after[1]},{after[2]})")
                return True

        # Step 2 (exact only): directional walk for the last 1-2 tiles
        if exact:
            after = bot.position
            if after[2] != start_z:
                bot.log(f"{prefix}   -> ({after[0]},{after[1]},{after[2]}) [floor changed]")
                return True
            if after[2] == target[2] and _distance(after, target) <= 3:
                ok = await _walk_to_exact(bot, target, max_steps=6)
                after = bot.position
                if after[2] != start_z:
                    bot.log(f"{prefix}   -> ({after[0]},{after[1]},{after[2]}) [floor changed]")
                    return True
                if ok:
                    bot.log(f"{prefix}   -> ({after[0]},{after[1]},{after[2]}) [directional]")
                    return True

        if attempt < MAX_RETRIES - 1:
            current = bot.position
            dist = _distance(current, target)
            bot.log(f"{prefix}   retry {attempt+1}/{MAX_RETRIES} at ({current[0]},{current[1]},{current[2]}) dist={dist}")

    current = bot.position
    dist = _distance(current, target)
    bot.log(f"{prefix}   failed: at ({current[0]},{current[1]},{current[2]}) dist={dist}")
    # Continue if reasonably close
    return dist <= tolerance + 2


def _check_tile_update(bot, x, y, z, since_time):
    """Check if game_state.tile_updates has an entry at (x,y,z) after since_time."""
    state = _get_state()
    gs = state.game_state
    for ts, tx, ty, tz in gs.tile_updates:
        if ts >= since_time and tx == x and ty == y and tz == z:
            return True
    return False


async def _execute_use_item_node(bot, node, prefix=""):
    """Execute an exact use_item (stairs/doors/ladders)."""
    label = node.get("label", f"item {node['item_id']}")
    target = node["target"]
    player_pos = node.get("player_pos", target)

    bot.log(f"{prefix} {label} at ({target[0]},{target[1]},{target[2]})")

    pkt = build_use_item_packet(
        node["x"], node["y"], node["z"],
        node["item_id"], node.get("stack_pos", 0), node.get("index", 0),
    )

    current = bot.position
    is_floor_change = target[2] != current[2]

    if is_floor_change:
        # Floor change: send packet, wait for z to change, retry if needed
        for attempt in range(MAX_RETRIES):
            before = bot.position
            await bot.inject_to_server(pkt)
            arrived = await _wait_for_position(bot, target, USE_ITEM_TIMEOUT, tolerance=0)
            after = bot.position
            if arrived or after[2] != before[2]:
                bot.log(f"{prefix}   [SUCCESS] -> ({after[0]},{after[1]},{after[2]})")
                return True
            if attempt < MAX_RETRIES - 1:
                bot.log(f"{prefix}   retry {attempt+1}/{MAX_RETRIES} at ({after[0]},{after[1]},{after[2]})")
        after = bot.position
        bot.log(f"{prefix}   [FAILURE] still at z={after[2]}")
        return False
    else:
        # Same-floor interaction (door etc): send packet, verify via server
        # tile update at target coords, with position-change fallback.
        for attempt in range(MAX_RETRIES):
            before = bot.position
            before_time = time.time()
            await bot.inject_to_server(pkt)
            # Poll up to 2s for tile update at target OR position change
            success = False
            start = time.time()
            while time.time() - start < 2.0:
                # Check for server tile update at target coords
                if _check_tile_update(bot, target[0], target[1], target[2], before_time):
                    after = bot.position
                    bot.log(f"{prefix}   [SUCCESS] tile update at ({target[0]},{target[1]},{target[2]})")
                    return True
                # Fallback: position changed (player walked through)
                after = bot.position
                if after[0] != before[0] or after[1] != before[1]:
                    bot.log(f"{prefix}   [SUCCESS] -> ({after[0]},{after[1]},{after[2]})")
                    return True
                await bot.sleep(0.1)
            if attempt < MAX_RETRIES - 1:
                after = bot.position
                bot.log(f"{prefix}   retry {attempt+1}/{MAX_RETRIES} at ({after[0]},{after[1]},{after[2]})")
        after = bot.position
        bot.log(f"{prefix}   [FAILURE] no tile update or movement at ({after[0]},{after[1]},{after[2]})")
        return False


async def _execute_use_item_ex_node(bot, node, prefix=""):
    """Execute a use_item_ex (rope/shovel) and wait for position."""
    label = node.get("label", f"item {node['item_id']}")
    target = node["target"]

    bot.log(f"{prefix} {label} -> ({target[0]},{target[1]},{target[2]})")

    pkt = build_use_item_ex_packet(
        node["from_x"], node["from_y"], node["from_z"],
        node["item_id"], node.get("stack_pos", 0),
        node["to_x"], node["to_y"], node["to_z"],
        node.get("to_stack_pos", 0),
    )
    await bot.inject_to_server(pkt)

    arrived = await _wait_for_position(bot, target, USE_ITEM_TIMEOUT, tolerance=1)
    after = bot.position
    if not arrived:
        bot.log(f"{prefix}   [FAILURE] missed: at ({after[0]},{after[1]},{after[2]})")
    else:
        bot.log(f"{prefix}   [SUCCESS] -> ({after[0]},{after[1]},{after[2]})")
    return arrived


async def _walk_to_exact(bot, target, max_steps=8):
    """Walk directionally to reach an exact tile position."""
    for _ in range(max_steps):
        current = bot.position
        dx = target[0] - current[0]
        dy = target[1] - current[1]
        if dx == 0 and dy == 0 and current[2] == target[2]:
            return True
        # Pick best direction (prefer diagonal when both axes need correction)
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


async def _execute_walk_steps(bot, node, prefix=""):
    """Execute raw directional walks for floor transitions.

    If not at the exact start position, uses directional walk
    corrections to reach it before sending the recorded walk sequence.
    """
    steps = node["steps"]
    target = node["target"]
    start = node.get("start")
    label = node.get("label", "floor change")

    bot.log(f"{prefix} {label} -> ({target[0]},{target[1]},{target[2]}) [{len(steps)} steps]")

    # Directional correction to reach exact start position
    if start:
        current = bot.position
        dx = start[0] - current[0]
        dy = start[1] - current[1]
        if dx != 0 or dy != 0 or current[2] != start[2]:
            bot.log(f"{prefix}   correct to ({start[0]},{start[1]},{start[2]}) dx={dx} dy={dy}")
            ok = await _walk_to_exact(bot, start)
            after = bot.position
            if not ok:
                bot.log(f"{prefix}   correction failed: at ({after[0]},{after[1]},{after[2]})")
                return False
            bot.log(f"{prefix}   at ({after[0]},{after[1]},{after[2]})")

    # Send directional walk packets
    for step in steps:
        dir_enum = DIR_NAME_TO_ENUM.get(step["direction"])
        if dir_enum is None:
            bot.log(f"{prefix}   unknown direction: {step['direction']}")
            continue
        pkt = build_walk_packet(dir_enum)
        await bot.inject_to_server(pkt)
        await bot.sleep(0.4)

    # Wait for arrival at target (with tolerance for floor changes)
    arrived = await _wait_for_position(bot, target, timeout=5.0, tolerance=2)
    after = bot.position
    if not arrived:
        # For floor changes, check that z changed to expected floor
        if after[2] == target[2]:
            bot.log(f"{prefix}   floor changed -> ({after[0]},{after[1]},{after[2]})")
            return True
        bot.log(f"{prefix}   failed: at ({after[0]},{after[1]},{after[2]})")
        return False
    bot.log(f"{prefix}   -> ({after[0]},{after[1]},{after[2]})")
    return True


async def run(bot):
    state = _get_state()

    # Ensure failed_nodes set exists (may be missing if mcp_server hasn't reloaded)
    if not hasattr(state, 'playback_failed_nodes'):
        state.playback_failed_nodes = set()

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

        # Build actions map from raw waypoints
        actions_map = build_actions_map(rec)
        state.playback_actions_map = actions_map
        state.playback_total = len(actions_map)

        pos = bot.position
        bot.log("")
        bot.log("=" * 40)
        bot.log(f"  PLAYBACK: {rec_name}")
        bot.log("=" * 40)
        bot.log("")
        bot.log(
            f"Playing '{rec_name}' ({len(waypoints)} wp -> {len(actions_map)} nodes) "
            f"from ({pos[0]},{pos[1]},{pos[2]})"
        )
        bot.log(actions_map_to_text(actions_map))

        aborted = False
        for i, node in enumerate(actions_map):
            if not state.playback_active or not bot.is_connected:
                break

            state.playback_index = i
            state.playback_minimap = build_sequence_minimaps(
                actions_map, i, bot.position,
                failed_nodes=state.playback_failed_nodes,
            )

            prefix = f"[{i+1}/{len(actions_map)}]"
            ntype = node["type"]

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

            if not success:
                state.playback_failed_nodes.add(i)
                bot.log(f"{prefix} Node failed, continuing...")

        # Update minimap one final time
        state.playback_minimap = build_sequence_minimaps(
            actions_map, len(actions_map), bot.position,
            failed_nodes=state.playback_failed_nodes,
        )

        if state.playback_active and state.playback_loop and not aborted:
            bot.log(f"Looping '{rec_name}'...")
            continue
        else:
            break

    state.playback_active = False
    state.playback_recording_name = ""
    state.playback_index = 0
    state.playback_total = 0
    state.playback_actions_map = []
    state.playback_minimap = None
    state.playback_failed_nodes = set()
    bot.log("Playback finished")
