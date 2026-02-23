"""
Cavebot — Recording & Playback engine.

Records player navigation (walking, doors, stairs, ladders) by hooking
client packets.  Saves/loads recordings as JSON in the recordings/ folder.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from protocol import ClientOpcode, PacketReader

log = logging.getLogger("cavebot")

RECORDINGS_DIR = Path(__file__).parent / "recordings"

# Walk opcode → direction name
WALK_OPCODE_TO_DIR = {
    ClientOpcode.WALK_NORTH: "north",
    ClientOpcode.WALK_EAST: "east",
    ClientOpcode.WALK_SOUTH: "south",
    ClientOpcode.WALK_WEST: "west",
    ClientOpcode.WALK_NORTHEAST: "northeast",
    ClientOpcode.WALK_SOUTHEAST: "southeast",
    ClientOpcode.WALK_SOUTHWEST: "southwest",
    ClientOpcode.WALK_NORTHWEST: "northwest",
}

WALK_OPCODES = set(WALK_OPCODE_TO_DIR.keys())

# Direction → (dx, dy) offset for position calculation
DIR_OFFSET = {
    "north": (0, -1), "south": (0, 1),
    "east": (1, 0), "west": (-1, 0),
    "northeast": (1, -1), "southeast": (1, 1),
    "southwest": (-1, 1), "northwest": (-1, -1),
}

# Item ID → label (common navigation items)
ITEM_LABELS = {
    # Ladders / stairs / holes — these vary by server, add as discovered
}


def _auto_label(item_id: int) -> str:
    """Generate a human-readable label for a use_item waypoint."""
    return ITEM_LABELS.get(item_id, f"Use item {item_id}")


# ── Recording ────────────────────────────────────────────────────────

def start_recording(state, name: str) -> str | None:
    """Begin recording waypoints.  Returns error string or None on success."""
    if state.recording_active:
        return f"Already recording '{state.recording_name}'. Stop it first."
    if not name or not all(c.isalnum() or c in "-_" for c in name):
        return "Invalid name. Use alphanumeric, hyphens, underscores only."

    gs = state.game_state
    pos = gs.position
    state.recording_active = True
    state.recording_name = name
    state.recording_waypoints = []
    state.recording_start_pos = pos
    state.recording_start_time = time.time()

    # Register client packet callback on the game proxy
    proxy = state.game_proxy
    if proxy is None:
        state.recording_active = False
        return "Game proxy not available."

    def _on_client_packet(opcode, reader: PacketReader):
        if not state.recording_active:
            return
        t_elapsed = round(time.time() - state.recording_start_time, 1)
        current_pos = list(state.game_state.position)

        if opcode in WALK_OPCODES:
            direction = WALK_OPCODE_TO_DIR[opcode]
            # Calculate expected position AFTER the walk completes
            dx, dy = DIR_OFFSET[direction]
            expected_pos = [current_pos[0] + dx, current_pos[1] + dy, current_pos[2]]
            wp = {
                "type": "walk",
                "direction": direction,
                "pos": expected_pos,
                "t": t_elapsed,
            }
            state.recording_waypoints.append(wp)
            log.info(f"[REC] walk {direction} ({current_pos[0]},{current_pos[1]}) -> ({expected_pos[0]},{expected_pos[1]})")

        elif opcode == ClientOpcode.USE_ITEM:
            try:
                pos_tuple = reader.read_position()
                item_id = reader.read_u16()
                stack_pos = reader.read_u8()
                index = reader.read_u8() if reader.remaining >= 1 else 0
            except Exception:
                return
            wp = {
                "type": "use_item",
                "x": pos_tuple[0],
                "y": pos_tuple[1],
                "z": pos_tuple[2],
                "item_id": item_id,
                "stack_pos": stack_pos,
                "index": index,
                "label": _auto_label(item_id),
                "pos": current_pos,
                "t": t_elapsed,
            }
            state.recording_waypoints.append(wp)
            log.info(f"[REC] use_item {item_id} at ({pos_tuple[0]},{pos_tuple[1]},{pos_tuple[2]})")

        elif opcode == ClientOpcode.USE_ITEM_EX:
            try:
                from_pos = reader.read_position()
                item_id = reader.read_u16()
                stack_pos = reader.read_u8()
                to_pos = reader.read_position()
                to_stack_pos = reader.read_u8() if reader.remaining >= 1 else 0
            except Exception:
                return
            wp = {
                "type": "use_item_ex",
                "from_x": from_pos[0],
                "from_y": from_pos[1],
                "from_z": from_pos[2],
                "item_id": item_id,
                "stack_pos": stack_pos,
                "to_x": to_pos[0],
                "to_y": to_pos[1],
                "to_z": to_pos[2],
                "to_stack_pos": to_stack_pos,
                "label": _auto_label(item_id),
                "pos": current_pos,
                "t": t_elapsed,
            }
            state.recording_waypoints.append(wp)
            log.info(f"[REC] use_item_ex {item_id} from {from_pos} to {to_pos}")

    # Store the callback reference so we can unregister later
    state._recording_callback = _on_client_packet
    proxy.register_client_packet_callback(_on_client_packet)
    log.info(f"Recording started: '{name}' at {pos}")
    return None


def stop_recording(state, *, discard: bool = False) -> dict | None:
    """Stop recording and optionally save.  Returns the recording dict or None."""
    if not state.recording_active:
        return None

    # Unregister callback
    cb = getattr(state, "_recording_callback", None)
    if cb and state.game_proxy:
        state.game_proxy.unregister_client_packet_callback(cb)
    state._recording_callback = None

    recording = {
        "name": state.recording_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "version": 1,
        "start_position": list(state.recording_start_pos),
        "waypoints": state.recording_waypoints,
    }

    state.recording_active = False
    name = state.recording_name
    count = len(state.recording_waypoints)
    state.recording_name = ""
    state.recording_waypoints = []
    state.recording_start_pos = (0, 0, 0)
    state.recording_start_time = 0

    if discard:
        log.info(f"Recording '{name}' discarded ({count} waypoints)")
        return None

    save_recording(recording)
    log.info(f"Recording '{name}' saved ({count} waypoints)")
    return recording


# ── File I/O ─────────────────────────────────────────────────────────

def save_recording(recording: dict) -> None:
    """Write recording dict to recordings/<name>.json."""
    RECORDINGS_DIR.mkdir(exist_ok=True)
    path = RECORDINGS_DIR / f"{recording['name']}.json"
    path.write_text(json.dumps(recording, indent=2), encoding="utf-8")


def load_recording(name: str) -> dict | None:
    """Load a recording by name.  Returns None if not found."""
    path = RECORDINGS_DIR / f"{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"Failed to load recording '{name}': {e}")
        return None


def list_recordings() -> list[dict]:
    """List all saved recordings with summary info."""
    RECORDINGS_DIR.mkdir(exist_ok=True)
    results = []
    for path in sorted(RECORDINGS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            results.append({
                "name": data.get("name", path.stem),
                "count": len(data.get("waypoints", [])),
                "created_at": data.get("created_at", ""),
            })
        except (json.JSONDecodeError, OSError):
            continue
    return results


def delete_recording(name: str) -> bool:
    """Delete a recording file.  Returns True if deleted."""
    path = RECORDINGS_DIR / f"{name}.json"
    if path.exists():
        path.unlink()
        log.info(f"Deleted recording '{name}'")
        return True
    return False


# ── Actions Map ──────────────────────────────────────────────────────

def _manhattan(a, b) -> int:
    """Manhattan distance between two (x,y,z) or [x,y,z] points (ignoring z)."""
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _is_map_click_walk(wp: dict) -> bool:
    """True if this use_item is a map-click walk (far tile, any floor).

    When right-clicking to walk, the client sends use_item targeting the
    visible tile — which may be on a different floor than the player
    (e.g. clicking on the floor above/below).  Distance > 1 means the
    player can't be interacting with stairs/doors, so it's always a walk.
    """
    if wp.get("type") != "use_item":
        return False
    item_pos = (wp["x"], wp["y"], wp["z"])
    player_pos = wp["pos"]
    return _manhattan(item_pos, player_pos) > 1


def _simplify_path(points: list[tuple], max_gap: int = 8) -> list[tuple]:
    """Keep enough points so no two consecutive are more than max_gap apart.

    Each point is (x, y, z, item_id, stack_pos).
    """
    if len(points) <= 1:
        return points
    result = [points[0]]
    for i in range(1, len(points)):
        p = points[i]
        is_last = i == len(points) - 1
        dist = _manhattan(p, result[-1])
        if is_last or dist >= max_gap:
            result.append(p)
    return result


def build_actions_map(recording: dict) -> list[dict]:
    """Convert a raw recording into a simplified actions map.

    Reduces 80+ raw waypoints into ~15-20 high-level navigation nodes:
    - walk_to: server-pathfinded walk (right-click on ground tile)
    - use_item: exact interaction (stairs/doors/ladders)
    - use_item_ex: extended use (rope/shovel)
    """
    waypoints = recording.get("waypoints", [])
    if not waypoints:
        return []

    nodes = []
    i = 0
    # Track a fallback ground tile item_id from the recording
    first_ground_item_id = None

    while i < len(waypoints):
        wp = waypoints[i]
        wp_type = wp.get("type", "")

        if wp_type == "use_item" and _is_map_click_walk(wp):
            # Collect consecutive map-click walks
            group = []
            while i < len(waypoints) and _is_map_click_walk(waypoints[i]):
                group.append(waypoints[i])
                if first_ground_item_id is None:
                    first_ground_item_id = waypoints[i]["item_id"]
                i += 1

            # Build path from PLAYER positions (real, on correct floor)
            # plus the final click target as the last destination.
            path = []  # (x, y, z, item_id, stack_pos)
            for wp_g in group:
                px, py, pz = wp_g["pos"][0], wp_g["pos"][1], wp_g["pos"][2]
                pt = (px, py, pz, wp_g["item_id"], wp_g.get("stack_pos", 1))
                if not path or (pt[0], pt[1], pt[2]) != (path[-1][0], path[-1][1], path[-1][2]):
                    path.append(pt)

            # Add final click target (on player's floor) as the destination
            last = group[-1]
            final_z = last["pos"][2]
            final = (last["x"], last["y"], final_z, last["item_id"], last.get("stack_pos", 1))
            if not path or (final[0], final[1], final[2]) != (path[-1][0], path[-1][1], path[-1][2]):
                path.append(final)

            # Simplify: keep points with max_gap between them
            simplified = _simplify_path(path)

            for pt in simplified:
                nodes.append({
                    "type": "walk_to",
                    "target": [pt[0], pt[1], pt[2]],
                    "item_id": pt[3],
                    "stack_pos": pt[4],
                })

        elif wp_type == "use_item":
            # Non-map-click use_item: stairs/doors/close interaction
            nodes.append({
                "type": "use_item",
                "target": [wp["x"], wp["y"], wp["z"]],
                "x": wp["x"],
                "y": wp["y"],
                "z": wp["z"],
                "item_id": wp["item_id"],
                "stack_pos": wp.get("stack_pos", 0),
                "index": wp.get("index", 0),
                "label": wp.get("label", f"Use item {wp['item_id']}"),
                "player_pos": wp["pos"],
            })
            i += 1

        elif wp_type == "walk":
            # Collect consecutive walks
            walks = [wp]
            j = i + 1
            while j < len(waypoints) and waypoints[j].get("type") == "walk":
                walks.append(waypoints[j])
                j += 1

            # Helper: find a ground tile item_id from nearby use_item waypoints
            def _find_ground_id():
                gid = first_ground_item_id or 4449
                for search_idx in range(max(0, i - 3), min(len(waypoints), j + 3)):
                    swp = waypoints[search_idx]
                    if swp.get("type") == "use_item" and _is_map_click_walk(swp):
                        return swp["item_id"]
                return gid

            # Check for floor transitions (z-change between consecutive walks)
            floor_change_idx = None
            for k in range(1, len(walks)):
                if walks[k]["pos"][2] != walks[k - 1]["pos"][2]:
                    floor_change_idx = k
                    break

            if floor_change_idx is None:
                # No floor change — normal walk_to at final position
                final_pos = list(walks[-1]["pos"])
                nodes.append({
                    "type": "walk_to",
                    "target": final_pos,
                    "item_id": _find_ground_id(),
                    "stack_pos": 1,
                })
            else:
                # Floor change detected — all walks become walk_steps
                # Include entire walk sequence so directional approach is exact
                old_z = walks[floor_change_idx - 1]["pos"][2]
                new_z = walks[floor_change_idx]["pos"][2]

                steps = [{"direction": w["direction"]} for w in walks]
                final_pos = list(walks[-1]["pos"])

                # Calculate start position (where player must be before first walk)
                first_dir = walks[0]["direction"]
                dx, dy = DIR_OFFSET[first_dir]
                first_pos = walks[0]["pos"]
                start_pos = [first_pos[0] - dx, first_pos[1] - dy, first_pos[2]]

                # Add precise walk_to for start position before walk_steps
                nodes.append({
                    "type": "walk_to",
                    "target": start_pos,
                    "item_id": first_ground_item_id or 4449,
                    "stack_pos": 1,
                })

                nodes.append({
                    "type": "walk_steps",
                    "target": final_pos,
                    "start": start_pos,
                    "steps": steps,
                    "label": f"Walk floor {old_z}\u2192{new_z}",
                })

            i = j

        elif wp_type == "use_item_ex":
            nodes.append({
                "type": "use_item_ex",
                "target": [wp["to_x"], wp["to_y"], wp["to_z"]],
                "from_x": wp["from_x"],
                "from_y": wp["from_y"],
                "from_z": wp["from_z"],
                "item_id": wp["item_id"],
                "stack_pos": wp.get("stack_pos", 0),
                "to_x": wp["to_x"],
                "to_y": wp["to_y"],
                "to_z": wp["to_z"],
                "to_stack_pos": wp.get("to_stack_pos", 0),
                "label": wp.get("label", f"Use item {wp['item_id']}"),
                "player_pos": wp["pos"],
            })
            i += 1

        else:
            i += 1

    # Post-process: deduplicate consecutive nodes with same type + target
    if len(nodes) > 1:
        deduped = [nodes[0]]
        for n in nodes[1:]:
            prev = deduped[-1]
            if (n["type"] == prev["type"]
                    and n["target"] == prev["target"]):
                continue  # skip duplicate
            deduped.append(n)
        nodes = deduped

    return nodes


def actions_map_to_text(actions_map: list[dict]) -> str:
    """Render an actions map as a numbered text list for log preview."""
    lines = []
    for i, node in enumerate(actions_map):
        t = node["target"]
        pos_str = f"({t[0]},{t[1]},{t[2]})"
        ntype = node["type"]

        if ntype == "walk_to":
            lines.append(f"{i+1}. walk_to {pos_str}")
        elif ntype == "use_item":
            label = node.get("label", f"item {node['item_id']}")
            # Detect floor change: if target z != player z
            ppos = node.get("player_pos", t)
            if t[2] != ppos[2]:
                lines.append(f"{i+1}. use_item {label} {pos_str} *floor change*")
            else:
                lines.append(f"{i+1}. use_item {label} {pos_str}")
        elif ntype == "use_item_ex":
            label = node.get("label", f"item {node['item_id']}")
            lines.append(f"{i+1}. use_item_ex {label} {pos_str}")
        elif ntype == "walk_steps":
            label = node.get("label", "floor change")
            dirs = ", ".join(s["direction"] for s in node.get("steps", []))
            lines.append(f"{i+1}. walk_steps {pos_str} *{label}* [{dirs}]")

    return "\n".join(lines)


def build_minimap(
    actions_map: list[dict],
    current_index: int,
    player_pos: tuple | list,
    floor: int,
    width: int = 40,
    height: int = 25,
    failed_nodes: set | None = None,
) -> dict:
    """Build an ASCII minimap of the actions map.

    Returns a dict with grid (list of strings), dimensions, metadata.
    Characters: @ = player, > = current target, # = visited walk_to,
                o = unvisited walk_to, * = visited use_item/stairs,
                + = unvisited use_item/stairs, X = failed node, space = empty.
    """
    if failed_nodes is None:
        failed_nodes = set()
    # Collect all nodes and their floors
    all_floors = set()
    for node in actions_map:
        all_floors.add(node["target"][2])
    all_floors.add(floor)

    # Filter nodes to requested floor
    floor_nodes = []
    for i, node in enumerate(actions_map):
        if node["target"][2] == floor:
            floor_nodes.append((i, node))

    if not floor_nodes:
        # No nodes on this floor — return minimal grid
        return {
            "grid": ["  @ (no nodes on this floor)"],
            "width": width,
            "height": 1,
            "origin": [int(player_pos[0]), int(player_pos[1])],
            "floor": floor,
            "floors": sorted(all_floors),
            "nodes": [],
            "player_node_index": current_index,
        }

    # Compute bounding box of nodes + player
    xs = [n["target"][0] for _, n in floor_nodes]
    ys = [n["target"][1] for _, n in floor_nodes]
    if player_pos[2] == floor:
        xs.append(player_pos[0])
        ys.append(player_pos[1])

    min_x = min(xs) - 1
    max_x = max(xs) + 1
    min_y = min(ys) - 1
    max_y = max(ys) + 1

    # If bounding box is larger than viewport, center on player
    bbox_w = max_x - min_x + 1
    bbox_h = max_y - min_y + 1

    if bbox_w > width or bbox_h > height:
        cx = int(player_pos[0]) if player_pos[2] == floor else (min_x + max_x) // 2
        cy = int(player_pos[1]) if player_pos[2] == floor else (min_y + max_y) // 2
        min_x = cx - width // 2
        min_y = cy - height // 2
        max_x = min_x + width - 1
        max_y = min_y + height - 1

    actual_w = max_x - min_x + 1
    actual_h = max_y - min_y + 1

    # Build grid
    grid = [[" "] * actual_w for _ in range(actual_h)]

    # Draw path lines between consecutive same-floor nodes
    prev_node_pos = None
    for idx, node in floor_nodes:
        cx, cy = node["target"][0], node["target"][1]
        if prev_node_pos is not None:
            # Draw simple line (horizontal then vertical)
            px, py = prev_node_pos
            # Horizontal segment
            step_x = 1 if cx >= px else -1
            for lx in range(px, cx, step_x):
                gy = py - min_y
                gx = lx - min_x
                if 0 <= gy < actual_h and 0 <= gx < actual_w and grid[gy][gx] == " ":
                    grid[gy][gx] = "-"
            # Vertical segment
            step_y = 1 if cy >= py else -1
            for ly in range(py, cy, step_y):
                gy = ly - min_y
                gx = cx - min_x
                if 0 <= gy < actual_h and 0 <= gx < actual_w and grid[gy][gx] == " ":
                    grid[gy][gx] = "|"
        prev_node_pos = (cx, cy)

    # Place nodes
    node_info = []
    for idx, node in floor_nodes:
        tx = node["target"][0] - min_x
        ty = node["target"][1] - min_y
        if 0 <= ty < actual_h and 0 <= tx < actual_w:
            visited = idx < current_index
            is_current = idx == current_index
            is_failed = idx in failed_nodes
            ntype = node["type"]
            if is_current:
                ch = ">"
            elif is_failed:
                ch = "X"
            elif ntype == "walk_to":
                ch = "#" if visited else "o"
            else:  # use_item, use_item_ex, walk_steps
                ch = "*" if visited else "+"
            grid[ty][tx] = ch
        node_info.append({
            "index": idx,
            "type": node["type"],
            "target": node["target"],
            "visited": idx < current_index,
            "failed": idx in failed_nodes,
        })

    # Ensure current target ">" is visible even if another node shares the cell
    for idx, node in floor_nodes:
        if idx == current_index:
            tx = node["target"][0] - min_x
            ty = node["target"][1] - min_y
            if 0 <= ty < actual_h and 0 <= tx < actual_w:
                grid[ty][tx] = ">"
            break

    # Place player
    if player_pos[2] == floor:
        px = int(player_pos[0]) - min_x
        py = int(player_pos[1]) - min_y
        if 0 <= py < actual_h and 0 <= px < actual_w:
            grid[py][px] = "@"

    # Convert grid to strings
    grid_lines = ["".join(row) for row in grid]

    return {
        "grid": grid_lines,
        "width": actual_w,
        "height": actual_h,
        "origin": [min_x, min_y],
        "floor": floor,
        "floors": sorted(all_floors),
        "nodes": node_info,
        "player_node_index": current_index,
    }


def build_all_minimaps(
    actions_map: list[dict],
    current_index: int,
    player_pos: tuple | list,
    failed_nodes: set | None = None,
) -> dict:
    """Build minimaps for ALL floors in the actions map.

    Returns a dict keyed by floor number, each value is a minimap dict.
    The player's current floor is included even if no nodes are on it.
    """
    all_floors = set()
    for node in actions_map:
        all_floors.add(node["target"][2])
    all_floors.add(int(player_pos[2]))

    result = {}
    for floor in sorted(all_floors):
        result[floor] = build_minimap(
            actions_map, current_index, player_pos, floor,
            failed_nodes=failed_nodes,
        )
    return result
