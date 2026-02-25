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
from constants import MONSTER_ID_MIN

USE_ITEM_TIMEOUT = 5.0
WALK_TO_TOLERANCE = 2   # tiles — close enough for walk_to nodes
MAX_RETRIES = 5
REACHABLE_PROBE_TIMEOUT = 0.4  # seconds — one walk attempt, fast bail
PAUSE_MAX_TIMEOUT = 60  # seconds — safety cap on monster-fight pause

# Map direction name strings to Direction enum values
DIR_NAME_TO_ENUM = {
    "north": Direction.NORTH, "south": Direction.SOUTH,
    "east": Direction.EAST, "west": Direction.WEST,
    "northeast": Direction.NORTHEAST, "southeast": Direction.SOUTHEAST,
    "southwest": Direction.SOUTHWEST, "northwest": Direction.NORTHWEST,
}


def _get_state():
    return sys.modules["__main__"].state


def _get_targeting_strategy():
    """Read the targeting_strategy from bot_settings.json for the cavebot action."""
    state = _get_state()
    cfg = state.settings.get("actions", {}).get("cavebot", {})
    return cfg.get("targeting_strategy", "none")


def _get_nearby_monsters(gs):
    """Return list of alive monsters from game_state.creatures.

    Filters for creature IDs >= MONSTER_ID_MIN with health between 1-100
    and seen within the last 60 seconds.
    """
    now = time.time()
    monsters = []
    for cid, info in dict(gs.creatures).items():
        if cid < MONSTER_ID_MIN:
            continue
        health = info.get("health", 0)
        if health <= 0 or health > 100:
            continue
        age = now - info.get("last_seen", now)
        if age > 60:
            continue
        monsters.append(info)
    return monsters


def _distance(a, b):
    """Manhattan distance (ignoring z)."""
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


async def _is_reachable(bot, target_x, target_y, target_z):
    """Check if a map position is reachable via server pathfinding.

    Sends a ground-click (use_item) toward the target tile. If the server
    finds a valid path, the character starts walking — detected as a
    position change. This is the server-side equivalent of OTClientV8's
    findPath() reachability check.

    Returns True if the character moved (path exists), False otherwise.
    Side effect: if reachable, the character starts walking toward the target.
    """
    gs = _get_state().game_state
    px, py, pz = gs.position

    # Different floor = unreachable
    if pz != target_z:
        return False

    # Already adjacent or on the tile = definitely reachable
    dist = max(abs(target_x - px), abs(target_y - py))
    if dist <= 1:
        return True

    # Send ground click to target tile (triggers server pathfinding)
    pkt = build_use_item_packet(target_x, target_y, target_z, 4449, 1, 0)
    await bot.inject_to_server(pkt)

    # Watch for position change (character started walking = path exists)
    start = time.time()
    start_pos = (px, py)
    while time.time() - start < REACHABLE_PROBE_TIMEOUT:
        await bot.sleep(0.1)
        cx, cy = gs.position[0], gs.position[1]
        if (cx, cy) != start_pos:
            return True

    return False


FLOOR_CHANGED = "floor_changed"
CANCEL_WALK = "cancel_walk"

async def _wait_for_position(bot, expected_pos, timeout, tolerance=0, abort_on_floor_change=False):
    """Wait until game_state.position is within tolerance of expected_pos (or timeout).
    Returns True if arrived, a dict (event data with "pos") or FLOOR_CHANGED if floor changed
    (when abort_on_floor_change), CANCEL_WALK if server rejected a walk, else False.
    """
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
            # Event-driven: scan server_events for floor_change with position data
            for evt in gs.server_events:
                ts, etype, edata = evt
                if ts > start and etype in ("floor_change_up", "floor_change_down"):
                    return edata  # {"pos": [x, y, new_z], "z": new_z}
            # Fallback: position polling
            if current[2] != start_z:
                return FLOOR_CHANGED
        # Server rejected the walk — bail immediately for fast retry
        if gs.cancel_walk_time > start:
            return CANCEL_WALK
        await bot.sleep(0.05)
    return False


def _is_floor_change(result):
    """Check if a _wait_for_position result indicates a floor change."""
    return result == FLOOR_CHANGED or isinstance(result, dict)


def _log_floor_change(bot, result, prefix):
    """Log a floor change result from _wait_for_position."""
    if isinstance(result, dict):
        landed = result["pos"]
        bot.log(f"{prefix}   -> ({landed[0]},{landed[1]},{landed[2]}) [floor changed]")
    else:
        after = bot.position
        bot.log(f"{prefix}   -> ({after[0]},{after[1]},{after[2]}) [floor changed]")


async def _execute_walk_to(bot, node, prefix="", exact=False):
    """Send use_item on ground tile (server pathfinds) and wait for arrival."""
    target = node["target"]
    item_id = node.get("item_id", 4449)
    stack_pos = node.get("stack_pos", 1)
    current = bot.position
    start_z = current[2]
    dist = _distance(current, target)
    cancel_count = 0
    last_cancel_pos = None

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
        # For exact nodes, use tolerance=1 so we actually wait for the
        # pathfind to move the character close, instead of returning
        # immediately when per-axis distance happens to be <= 2.
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
            bot.log(f"{prefix}   cancel_walk #{cancel_count} at ({pos[0]},{pos[1]},{pos[2]}) target=({target[0]},{target[1]},{target[2]})")
            # After 3 consecutive cancel_walks at same position: try directional escape
            if cancel_count >= 3 and last_cancel_pos == cur_pos:
                bot.log(f"{prefix}   stuck at same position, trying directional escape")
                escaped = False
                for escape_dir in ["north", "east", "south", "west"]:
                    escape_pkt = build_walk_packet(DIR_NAME_TO_ENUM[escape_dir])
                    await bot.inject_to_server(escape_pkt)
                    await bot.sleep(0.3)
                    new_pos = bot.position
                    if (new_pos[0], new_pos[1], new_pos[2]) != cur_pos:
                        bot.log(f"{prefix}   escaped {escape_dir} to ({new_pos[0]},{new_pos[1]},{new_pos[2]})")
                        escaped = True
                        cancel_count = 0
                        last_cancel_pos = None
                        break
                if not escaped:
                    bot.log(f"{prefix}   escape failed, skipping node")
                    return False
            else:
                if cur_pos != last_cancel_pos:
                    cancel_count = 1
                last_cancel_pos = cur_pos
            await bot.sleep(0.2)
            continue

        # Non-cancel result: reset counter
        cancel_count = 0
        last_cancel_pos = None

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
            if after[2] == target[2] and _distance(after, target) <= 5:
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


def _check_tile_transform(bot, x, y, z, since_time):
    """Check if game_state.tile_updates has a transform at (x,y,z) after since_time."""
    state = _get_state()
    gs = state.game_state
    for ts, tx, ty, tz in gs.tile_updates:
        if ts >= since_time and tx == x and ty == y and tz == z:
            return True
    return False


async def _execute_use_item_node(bot, node, prefix=""):
    """Execute an exact use_item (stairs/doors/ladders).

    For far targets, the server auto-walks the player then uses the item.
    For floor-change targets (different z), waits for z to change.
    For same-floor targets (doors), waits for tile update or movement.
    Also detects unexpected floor changes (ladders at same z that go up/down).
    """
    label = node.get("label", f"item {node['item_id']}")
    target = node["target"]

    bot.log(f"{prefix} {label} at ({target[0]},{target[1]},{target[2]})")

    # --- Pre-position: walk to adjacency before interacting ---
    current = bot.position
    dist = max(abs(current[0] - target[0]), abs(current[1] - target[1]))  # Chebyshev
    wrong_floor = current[2] != target[2]

    if dist > 1 or wrong_floor:
        walk_target = node.get("player_pos", target)
        bot.log(f"{prefix}   pre-walk to ({walk_target[0]},{walk_target[1]},{walk_target[2]}) dist={dist}")
        walk_node = {
            "target": walk_target,
            "item_id": 4449,
            "stack_pos": 1,
        }
        walk_ok = await _execute_walk_to(bot, walk_node, prefix + "  ", exact=True)
        if not walk_ok:
            bot.log(f"{prefix}   pre-walk failed")
            return False

    # --- Proceed with use_item interaction ---
    pkt = build_use_item_packet(
        node["x"], node["y"], node["z"],
        node["item_id"], node.get("stack_pos", 0), node.get("index", 0),
    )

    current = bot.position  # re-read after potential walk
    is_floor_change = target[2] != current[2]

    gs = _get_state().game_state

    if is_floor_change:
        # Floor change: send packet, check server events first, then poll z
        for attempt in range(MAX_RETRIES):
            before = bot.position
            before_time = time.time()
            await bot.inject_to_server(pkt)

            deadline = time.time() + USE_ITEM_TIMEOUT
            while time.time() < deadline:
                # Event-driven: check for floor_change event with position
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

            if attempt < MAX_RETRIES - 1:
                after = bot.position
                bot.log(f"{prefix}   retry {attempt+1}/{MAX_RETRIES} at ({after[0]},{after[1]},{after[2]})")
        after = bot.position
        bot.log(f"{prefix}   [FAILURE] still at z={after[2]}")
        return False
    else:
        # Same-floor interaction: send packet, verify via tile update,
        # position change, or floor change (ladder at same z that goes up/down).
        for attempt in range(MAX_RETRIES):
            before = bot.position
            before_time = time.time()
            await bot.inject_to_server(pkt)
            start = time.time()
            while time.time() - start < USE_ITEM_TIMEOUT:
                # Event-driven floor change check (unexpected stairs at same z)
                for evt in gs.server_events:
                    ts, etype, edata = evt
                    if ts > before_time and etype in ("floor_change_up", "floor_change_down"):
                        landed = edata["pos"]
                        bot.log(f"{prefix}   [SUCCESS] floor change event -> ({landed[0]},{landed[1]},{landed[2]})")
                        return True
                # Check for server tile update at target coords (door opened)
                if _check_tile_transform(bot, target[0], target[1], target[2], before_time):
                    after = bot.position
                    bot.log(f"{prefix}   [SUCCESS] tile transform at ({target[0]},{target[1]},{target[2]})")
                    return True
                after = bot.position
                # Floor changed (ladder/stairs at same z going up/down)
                if after[2] != before[2]:
                    bot.log(f"{prefix}   [SUCCESS] floor change -> ({after[0]},{after[1]},{after[2]})")
                    return True
                # Position changed on same floor (walked through door)
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
    start_z = bot.position[2]
    for _ in range(max_steps):
        current = bot.position
        # Floor changed (stair triggered) — exit immediately so the caller
        # can detect it; don't keep walking on the wrong floor.
        if current[2] != start_z:
            return False
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
        i = 0
        while i < len(actions_map):
            node = actions_map[i]
            if not state.playback_active or not bot.is_connected:
                break

            state.playback_index = i
            state.playback_minimap = build_sequence_minimaps(
                actions_map, i, bot.position,
                failed_nodes=state.playback_failed_nodes,
            )

            prefix = f"[{i+1}/{len(actions_map)}]"

            # Targeting strategy: pause while actively fighting a monster
            # Uses server pathfinding reachability probe instead of reactive
            # "can't throw" message — equivalent to OTClientV8's findPath() check.
            strategy = _get_targeting_strategy()
            if strategy == "pause_on_monster":
                gs = state.game_state
                target_id = gs.attack_target_id
                if target_id and target_id >= MONSTER_ID_MIN:
                    creature = gs.creatures.get(target_id)
                    if creature and 0 < creature.get("health", 0) <= 100:
                        cx = creature.get("x", 0)
                        cy = creature.get("y", 0)
                        cz = creature.get("z", 0)
                        name = creature.get("name", "?")
                        hp = creature.get("health", 100)

                        # Pathfinding reachability check: send ground-click to
                        # monster's tile, see if the server pathfinds us there.
                        reachable = await _is_reachable(bot, cx, cy, cz)

                        if reachable:
                            bot.log(f"{prefix} Pausing — fighting {name} (0x{target_id:08X}) hp={hp}%")
                            pause_start = time.time()
                            last_checked_target = target_id
                            while state.playback_active and bot.is_connected:
                                target_id = gs.attack_target_id
                                if not target_id:
                                    break
                                creature = gs.creatures.get(target_id)
                                if not creature or creature.get("health", 0) <= 0:
                                    break
                                # Safety timeout — don't pause forever
                                if time.time() - pause_start > PAUSE_MAX_TIMEOUT:
                                    bot.log(f"{prefix} Resuming — pause timeout ({PAUSE_MAX_TIMEOUT}s)")
                                    break
                                # If auto_targeting switched to a NEW monster,
                                # re-check reachability before continuing to pause.
                                if target_id != last_checked_target:
                                    new_c = gs.creatures.get(target_id)
                                    if new_c:
                                        nx = new_c.get("x", 0)
                                        ny = new_c.get("y", 0)
                                        nz = new_c.get("z", 0)
                                        if not await _is_reachable(bot, nx, ny, nz):
                                            bot.log(f"{prefix} New target unreachable, resuming")
                                            break
                                    last_checked_target = target_id
                                await bot.sleep(0.2)
                        else:
                            bot.log(f"{prefix} Monster {name} unreachable (no path), skipping")

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

            i += 1

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
