"""
Cavebot — Recording & Playback engine.

Records player navigation (walking, doors, stairs, ladders) by hooking
client packets.  Saves/loads recordings as JSON in the recordings/ folder.
"""

import json
import logging
import threading
import time
from collections import Counter
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

# Autowalk path direction bytes use 1-8 encoding (different from Direction enum 0-7)
AUTOWALK_DIR_OFFSET = {
    1: (1, 0),    # East
    2: (1, -1),   # Northeast
    3: (0, -1),   # North
    4: (-1, -1),  # Northwest
    5: (-1, 0),   # West
    6: (-1, 1),   # Southwest
    7: (0, 1),    # South
    8: (1, 1),    # Southeast
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
                "player_pos": current_pos,
                "t": t_elapsed,
            }
            state.recording_waypoints.append(wp)
            log.info(f"[REC] walk {direction} ({current_pos[0]},{current_pos[1]}) -> ({expected_pos[0]},{expected_pos[1]})")

        elif opcode == ClientOpcode.AUTO_WALK:
            # Left-click map movement — parse direction list, compute final pos
            try:
                count = reader.read_u8()
                if count == 0 or count > 50:
                    return
                x, y, z = current_pos
                for _ in range(count):
                    d = reader.read_u8()
                    offset = AUTOWALK_DIR_OFFSET.get(d)
                    if offset is None:
                        continue
                    x += offset[0]
                    y += offset[1]
                final_pos = [x, y, z]
            except Exception:
                return
            wp = {
                "type": "walk",
                "direction": "autowalk",
                "pos": final_pos,
                "player_pos": current_pos,
                "t": t_elapsed,
            }
            state.recording_waypoints.append(wp)
            log.info(f"[REC] autowalk {count} steps ({current_pos[0]},{current_pos[1]}) -> ({final_pos[0]},{final_pos[1]})")

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
                "player_pos": current_pos,
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
                "player_pos": current_pos,
                "t": t_elapsed,
            }
            state.recording_waypoints.append(wp)
            log.info(f"[REC] use_item_ex {item_id} from {from_pos} to {to_pos}")

    # Store the callback reference so we can unregister later
    state._recording_callback = _on_client_packet
    proxy.register_client_packet_callback(_on_client_packet)

    # Position tracking thread — polls game_state.position every 200ms
    # and records a waypoint whenever it changes.
    last_pos = list(pos)
    stop_event = threading.Event()

    def _poll_position():
        nonlocal last_pos
        while not stop_event.is_set():
            stop_event.wait(0.05)
            if not state.recording_active:
                break

            # Drain server events and record them as waypoints
            gs = state.game_state
            while gs.server_events:
                try:
                    ts, event_type, event_data = gs.server_events.popleft()
                except IndexError:
                    break
                t_elapsed = round(ts - state.recording_start_time, 1)
                if event_type in ("floor_change_up", "floor_change_down"):
                    direction = "up" if event_type == "floor_change_up" else "down"
                    state.recording_waypoints.append({
                        "type": "floor_change",
                        "direction": direction,
                        "pos": event_data.get("pos", list(gs.position)),
                        "z": event_data.get("z", gs.position[2]),
                        "t": t_elapsed,
                    })
                    log.info(f"[REC] floor_change {direction} z={event_data.get('z')}")
                elif event_type == "cancel_walk":
                    state.recording_waypoints.append({
                        "type": "cancel_walk",
                        "direction": event_data.get("direction", 0),
                        "pos": event_data.get("pos", list(gs.position)),
                        "t": t_elapsed,
                    })
                    log.info(f"[REC] cancel_walk dir={event_data.get('direction')}")

            current = list(gs.position)
            if current != last_pos:
                t_elapsed = round(time.time() - state.recording_start_time, 1)
                state.recording_waypoints.append({
                    "type": "position",
                    "pos": current,
                    "t": t_elapsed,
                })
                last_pos = current

    state._recording_position_stop = stop_event
    threading.Thread(target=_poll_position, daemon=True).start()

    log.info(f"Recording started: '{name}' at {pos}")
    return None


def stop_recording(state, *, discard: bool = False) -> dict | None:
    """Stop recording and optionally save.  Returns the recording dict or None."""
    if not state.recording_active:
        return None

    # Stop position tracking thread
    stop_event = getattr(state, "_recording_position_stop", None)
    if stop_event:
        stop_event.set()
    state._recording_position_stop = None

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


def remove_waypoints(name: str, indices: list[int]) -> dict | None:
    """Remove waypoints at given indices from a saved recording.

    Returns the updated recording dict, or None if not found.
    """
    rec = load_recording(name)
    if rec is None:
        return None
    waypoints = rec.get("waypoints", [])
    # Remove in reverse order so earlier indices stay valid
    for idx in sorted(set(indices), reverse=True):
        if 0 <= idx < len(waypoints):
            waypoints.pop(idx)
    rec["waypoints"] = waypoints
    save_recording(rec)
    log.info(f"Removed {len(indices)} waypoint(s) from '{name}', {len(waypoints)} remaining")
    return rec


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


def _simplify_path(points: list[tuple], max_gap: int = 3) -> list[tuple]:
    """Keep enough points so no two consecutive are more than max_gap apart.

    Each point is (x, y, z, item_id, stack_pos).

    Floor boundaries are always preserved: the last point before a Z change
    and the first point after are force-kept so stair/ramp tiles are never
    dropped by the gap filter.
    """
    if len(points) <= 1:
        return points

    # Pre-compute which indices sit at a floor boundary and must be kept.
    floor_boundary = set()
    for i in range(1, len(points)):
        if points[i][2] != points[i - 1][2]:
            floor_boundary.add(i - 1)  # last point before Z change
            floor_boundary.add(i)      # first point after Z change

    result = [points[0]]
    for i in range(1, len(points)):
        p = points[i]
        is_last = i == len(points) - 1
        dist = _manhattan(p, result[-1])
        if is_last or dist >= max_gap or i in floor_boundary:
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

        if wp_type in ("position", "floor_change", "cancel_walk"):
            # Informational-only waypoints; skip them.
            i += 1
            continue

        if wp_type == "use_item" and _is_map_click_walk(wp):
            # Collect consecutive far use_items, skipping position waypoints
            group = []
            while i < len(waypoints):
                wpi = waypoints[i]
                wpi_type = wpi.get("type", "")
                if wpi_type == "position":
                    i += 1
                    continue
                if _is_map_click_walk(wpi):
                    group.append(wpi)
                    i += 1
                else:
                    break

            # Count how many times each target (x,y,z) appears.
            # Repeated targets = real interactions (ladder/door clicked
            # multiple times from different positions as the player walks).
            # Unique targets = ground-click walks.
            target_counts = Counter(
                (g["x"], g["y"], g["z"]) for g in group
            )

            # Process in order: consecutive walk clicks get path-simplified,
            # real interactions get emitted as use_item nodes.
            walk_run = []

            def _flush_walk_run():
                if not walk_run:
                    return
                nonlocal first_ground_item_id
                path = []
                for wg in walk_run:
                    px, py, pz = wg["pos"][0], wg["pos"][1], wg["pos"][2]
                    pt = (px, py, pz, wg["item_id"], wg.get("stack_pos", 1))
                    if not path or (pt[0], pt[1], pt[2]) != (path[-1][0], path[-1][1], path[-1][2]):
                        path.append(pt)
                    if first_ground_item_id is None:
                        first_ground_item_id = wg["item_id"]
                last_wg = walk_run[-1]
                final_z = last_wg["pos"][2]
                final = (last_wg["x"], last_wg["y"], final_z,
                         last_wg["item_id"], last_wg.get("stack_pos", 1))
                if not path or (final[0], final[1], final[2]) != (path[-1][0], path[-1][1], path[-1][2]):
                    path.append(final)
                simplified = _simplify_path(path)
                for pt in simplified:
                    nodes.append({
                        "type": "walk_to",
                        "target": [pt[0], pt[1], pt[2]],
                        "item_id": pt[3],
                        "stack_pos": pt[4],
                    })
                walk_run.clear()

            for g in group:
                tgt = (g["x"], g["y"], g["z"])
                if target_counts[tgt] >= 2:
                    # Real interaction — flush any pending walks, emit use_item
                    _flush_walk_run()
                    nodes.append({
                        "type": "use_item",
                        "target": [g["x"], g["y"], g["z"]],
                        "x": g["x"], "y": g["y"], "z": g["z"],
                        "item_id": g["item_id"],
                        "stack_pos": g.get("stack_pos", 0),
                        "index": g.get("index", 0),
                        "label": g.get("label", f"Use item {g['item_id']}"),
                        "player_pos": g["pos"],
                    })
                else:
                    walk_run.append(g)

            _flush_walk_run()

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
            # Collect consecutive walks, skipping position waypoints
            walks = [wp]
            j = i + 1
            while j < len(waypoints):
                jtype = waypoints[j].get("type", "")
                if jtype == "walk":
                    walks.append(waypoints[j])
                    j += 1
                elif jtype == "position":
                    j += 1  # skip but keep collecting walks
                else:
                    break

            # Helper: find a ground tile item_id from nearby use_item waypoints
            def _find_ground_id():
                gid = first_ground_item_id or 4449
                for search_idx in range(max(0, i - 3), min(len(waypoints), j + 3)):
                    swp = waypoints[search_idx]
                    if swp.get("type") == "use_item" and _is_map_click_walk(swp):
                        return swp["item_id"]
                return gid

            # Convert walk positions to walk_to nodes (server pathfinding).
            # For both keyboard walks and autowalks, pos is the destination
            # (post-walk position).  Use it directly — no offset needed.
            ground_id = _find_ground_id()
            path = []  # (x, y, z, item_id, stack_pos)
            for w in walks:
                pt = (w["pos"][0], w["pos"][1], w["pos"][2], ground_id, 1)
                if not path or (pt[0], pt[1], pt[2]) != (path[-1][0], path[-1][1], path[-1][2]):
                    path.append(pt)

            simplified = _simplify_path(path)
            for pt in simplified:
                nodes.append({
                    "type": "walk_to",
                    "target": [pt[0], pt[1], pt[2]],
                    "item_id": pt[3],
                    "stack_pos": pt[4],
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

    # Mark walk_to nodes as exact when followed by a floor change
    for idx in range(len(nodes) - 1):
        if nodes[idx]["type"] != "walk_to":
            continue
        nxt = nodes[idx + 1]
        # Next node is use_item (stairs/door)
        if nxt["type"] in ("use_item", "use_item_ex"):
            nodes[idx]["exact"] = True
        # Next node is walk_to on a different floor
        elif nxt["type"] == "walk_to" and nxt["target"][2] != nodes[idx]["target"][2]:
            nodes[idx]["exact"] = True
        # Next node is walk_steps (floor transition)
        elif nxt["type"] == "walk_steps":
            nodes[idx]["exact"] = True

    return nodes


def actions_map_to_text(actions_map: list[dict]) -> str:
    """Render an actions map as a numbered text list for log preview."""
    lines = []
    for i, node in enumerate(actions_map):
        t = node["target"]
        pos_str = f"({t[0]},{t[1]},{t[2]})"
        ntype = node["type"]

        if ntype == "walk_to":
            exact = " [exact]" if node.get("exact") else ""
            lines.append(f"{i+1}. walk_to {pos_str}{exact}")
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
    failed_nodes: set | None = None,
) -> dict:
    """Build an ASCII minimap of the actions map.

    The grid auto-sizes to fit all nodes on the floor (plus a 1-tile margin).
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
            "width": 1,
            "height": 1,
            "origin": [int(player_pos[0]), int(player_pos[1])],
            "floor": floor,
            "floors": sorted(all_floors),
            "nodes": [],
            "player_node_index": current_index,
        }

    # Compute bounding box of ALL nodes + player on this floor
    xs = [n["target"][0] for _, n in floor_nodes]
    ys = [n["target"][1] for _, n in floor_nodes]
    if player_pos[2] == floor:
        xs.append(player_pos[0])
        ys.append(player_pos[1])

    min_x = min(xs) - 1
    max_x = max(xs) + 1
    min_y = min(ys) - 1
    max_y = max(ys) + 1

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
            is_exact = node.get("exact", False)
            if is_current:
                ch = ">"
            elif is_failed:
                ch = "X"
            elif ntype == "walk_to":
                if is_exact:
                    ch = "!" if not visited else "1"
                else:
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


def _split_into_sequences(actions_map: list[dict]) -> list[dict]:
    """Split an actions map into sequences by floor transitions.

    Each sequence is a contiguous run of nodes on the same floor.
    Returns a list of dicts: {floor, start, end, indices}.
    """
    if not actions_map:
        return []

    sequences = []
    cur_floor = actions_map[0]["target"][2]
    cur_start = 0
    cur_indices = [0]

    for i in range(1, len(actions_map)):
        node_floor = actions_map[i]["target"][2]
        if node_floor != cur_floor:
            sequences.append({
                "floor": cur_floor,
                "start": cur_start,
                "end": i - 1,
                "indices": cur_indices,
            })
            cur_floor = node_floor
            cur_start = i
            cur_indices = [i]
        else:
            cur_indices.append(i)

    # Final sequence
    sequences.append({
        "floor": cur_floor,
        "start": cur_start,
        "end": len(actions_map) - 1,
        "indices": cur_indices,
    })
    return sequences


def build_sequence_minimaps(
    actions_map: list[dict],
    current_index: int,
    player_pos: tuple | list,
    failed_nodes: set | None = None,
) -> list[dict]:
    """Build one minimap per floor-sequence instead of per floor.

    Splits the actions map into sequences at every floor (Z) change.
    Each visit to a floor gets its own fresh minimap, preventing
    overlapping backtrack lines when the path revisits the same floor.

    Returns a list of dicts, each with:
      seq_index, floor, start, end, minimap (the minimap dict)
    """
    if failed_nodes is None:
        failed_nodes = set()

    sequences = _split_into_sequences(actions_map)
    if not sequences:
        return []

    result = []
    for seq_idx, seq in enumerate(sequences):
        floor = seq["floor"]
        indices = seq["indices"]

        # Build a sub-actions-map containing only this sequence's nodes,
        # but keep original indices for visited/current/failed tracking.
        seq_nodes = [(i, actions_map[i]) for i in indices]

        # Determine which nodes in this sequence are relevant
        floor_nodes = seq_nodes  # all of them are on the same floor

        if not floor_nodes:
            continue

        # Compute bounding box
        xs = [n["target"][0] for _, n in floor_nodes]
        ys = [n["target"][1] for _, n in floor_nodes]

        # Include player position if they're on this floor AND
        # the current_index falls within this sequence
        player_in_seq = (
            int(player_pos[2]) == floor
            and seq["start"] <= current_index <= seq["end"] + 1
        )
        if player_in_seq:
            xs.append(int(player_pos[0]))
            ys.append(int(player_pos[1]))

        min_x = min(xs) - 1
        max_x = max(xs) + 1
        min_y = min(ys) - 1
        max_y = max(ys) + 1
        w = max_x - min_x + 1
        h = max_y - min_y + 1

        # Build grid
        grid = [[" "] * w for _ in range(h)]

        # Draw path lines between consecutive nodes in THIS sequence
        prev_pos = None
        for idx, node in floor_nodes:
            cx, cy = node["target"][0], node["target"][1]
            if prev_pos is not None:
                px, py = prev_pos
                step_x = 1 if cx >= px else -1
                for lx in range(px, cx, step_x):
                    gy = py - min_y
                    gx = lx - min_x
                    if 0 <= gy < h and 0 <= gx < w and grid[gy][gx] == " ":
                        grid[gy][gx] = "-"
                step_y = 1 if cy >= py else -1
                for ly in range(py, cy, step_y):
                    gy = ly - min_y
                    gx = cx - min_x
                    if 0 <= gy < h and 0 <= gx < w and grid[gy][gx] == " ":
                        grid[gy][gx] = "|"
            prev_pos = (cx, cy)

        # Place nodes
        node_info = []
        for idx, node in floor_nodes:
            tx = node["target"][0] - min_x
            ty = node["target"][1] - min_y
            if 0 <= ty < h and 0 <= tx < w:
                visited = idx < current_index
                is_current = idx == current_index
                is_failed = idx in failed_nodes
                ntype = node["type"]
                is_exact = node.get("exact", False)
                if is_current:
                    ch = ">"
                elif is_failed:
                    ch = "X"
                elif ntype == "walk_to":
                    if is_exact:
                        ch = "!" if not visited else "1"
                    else:
                        ch = "#" if visited else "o"
                else:
                    ch = "*" if visited else "+"
                grid[ty][tx] = ch
            node_info.append({
                "index": idx,
                "type": node["type"],
                "target": node["target"],
                "visited": idx < current_index,
                "failed": idx in failed_nodes,
            })

        # Ensure current target ">" wins
        for idx, node in floor_nodes:
            if idx == current_index:
                tx = node["target"][0] - min_x
                ty = node["target"][1] - min_y
                if 0 <= ty < h and 0 <= tx < w:
                    grid[ty][tx] = ">"
                break

        # Place player
        if player_in_seq:
            px = int(player_pos[0]) - min_x
            py = int(player_pos[1]) - min_y
            if 0 <= py < h and 0 <= px < w:
                grid[py][px] = "@"

        grid_lines = ["".join(row) for row in grid]

        result.append({
            "seq_index": seq_idx,
            "floor": floor,
            "start": seq["start"],
            "end": seq["end"],
            "minimap": {
                "grid": grid_lines,
                "width": w,
                "height": h,
                "origin": [min_x, min_y],
                "floor": floor,
                "floors": [floor],
                "nodes": node_info,
                "player_node_index": current_index,
            },
        })

    return result
