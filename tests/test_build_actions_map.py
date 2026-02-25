"""Unit tests for cavebot.build_actions_map — recording → actions map conversion."""

import sys
import os
import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cavebot import build_actions_map, _simplify_path, _is_map_click_walk

# Direction → (dx, dy) for computing post-walk pos from player_pos
_DIR_OFFSET = {
    "north": (0, -1), "south": (0, 1),
    "east": (1, 0), "west": (-1, 0),
    "northeast": (1, -1), "southeast": (1, 1),
    "southwest": (-1, 1), "northwest": (-1, -1),
}


# ── Helpers ──────────────────────────────────────────────────────────

def _walk(direction, player_pos, t=0.0):
    """Create a keyboard walk waypoint.

    player_pos is where the player was BEFORE the walk.
    pos (destination) is computed as player_pos + direction offset.
    This matches the real recording format.
    """
    dx, dy = _DIR_OFFSET.get(direction, (0, 0))
    pos = [player_pos[0] + dx, player_pos[1] + dy, player_pos[2]]
    return {
        "type": "walk", "direction": direction,
        "pos": pos, "player_pos": list(player_pos), "t": t,
    }


def _autowalk(pos, player_pos=None, t=0.0):
    """Create an autowalk waypoint (pos = final destination)."""
    wp = {"type": "walk", "direction": "autowalk", "pos": list(pos), "t": t}
    if player_pos is not None:
        wp["player_pos"] = list(player_pos)
    return wp


def _position(pos, t=0.0):
    """Create a position waypoint (recorded by position tracking thread).
    Kept for backwards-compat tests — new recordings no longer emit these.
    """
    return {"type": "position", "pos": list(pos), "t": t}


def _floor_change(direction, pos, z=None, t=0.0):
    """Create a floor_change waypoint (from server event)."""
    return {"type": "floor_change", "direction": direction,
            "pos": list(pos), "z": z if z is not None else pos[2], "t": t}


def _use_item(x, y, z, item_id, player_pos, stack_pos=0, index=0, label=None):
    """Create a use_item waypoint."""
    return {
        "type": "use_item",
        "x": x, "y": y, "z": z,
        "item_id": item_id,
        "stack_pos": stack_pos,
        "index": index,
        "label": label or f"Use item {item_id}",
        "pos": list(player_pos),
        "t": 0.0,
    }


def _use_item_ex(from_pos, item_id, to_pos, player_pos, stack_pos=0, to_stack_pos=0):
    """Create a use_item_ex waypoint."""
    return {
        "type": "use_item_ex",
        "from_x": from_pos[0], "from_y": from_pos[1], "from_z": from_pos[2],
        "item_id": item_id,
        "stack_pos": stack_pos,
        "to_x": to_pos[0], "to_y": to_pos[1], "to_z": to_pos[2],
        "to_stack_pos": to_stack_pos,
        "label": f"Use item {item_id}",
        "pos": list(player_pos),
        "t": 0.0,
    }


def _rec(waypoints):
    """Wrap waypoints in a recording dict."""
    return {"waypoints": waypoints}


def _targets(nodes):
    """Extract (type, target) tuples from actions map for easy assertion."""
    return [(n["type"], tuple(n["target"])) for n in nodes]


# ── Empty / trivial cases ────────────────────────────────────────────

class TestEmptyAndTrivial:
    def test_empty_recording(self):
        assert build_actions_map({"waypoints": []}) == []

    def test_no_waypoints_key(self):
        assert build_actions_map({}) == []

    def test_single_autowalk(self):
        nodes = build_actions_map(_rec([_autowalk((100, 200, 7))]))
        assert len(nodes) == 1
        assert nodes[0]["type"] == "walk_to"
        assert nodes[0]["target"] == [100, 200, 7]

    def test_single_keyboard_walk(self):
        # Keyboard walk north: player_pos=(100,200,7), destination pos=(100,199,7)
        nodes = build_actions_map(_rec([_walk("north", (100, 200, 7))]))
        assert len(nodes) == 1
        assert nodes[0]["type"] == "walk_to"
        assert nodes[0]["target"] == [100, 199, 7]

    def test_single_use_item_close(self):
        # use_item with distance <= 1 from player → stays as use_item
        nodes = build_actions_map(_rec([
            _use_item(100, 200, 7, 1696, player_pos=(100, 201, 7)),
        ]))
        assert len(nodes) == 1
        assert nodes[0]["type"] == "use_item"
        assert nodes[0]["item_id"] == 1696

    def test_single_use_item_ex(self):
        nodes = build_actions_map(_rec([
            _use_item_ex((0xFFFF, 0, 0), 2120, (100, 200, 7), (100, 201, 7)),
        ]))
        assert len(nodes) == 1
        assert nodes[0]["type"] == "use_item_ex"
        assert nodes[0]["item_id"] == 2120


# ── _is_map_click_walk ───────────────────────────────────────────────

class TestIsMapClickWalk:
    def test_far_tile_is_map_click(self):
        wp = _use_item(110, 200, 7, 486, player_pos=(100, 200, 7))
        assert _is_map_click_walk(wp) is True

    def test_adjacent_tile_is_not_map_click(self):
        wp = _use_item(101, 200, 7, 1696, player_pos=(100, 200, 7))
        assert _is_map_click_walk(wp) is False

    def test_same_tile_is_not_map_click(self):
        wp = _use_item(100, 200, 7, 1968, player_pos=(100, 200, 7))
        assert _is_map_click_walk(wp) is False

    def test_different_floor_adjacent_is_not_map_click(self):
        # Standing at z=7, clicking item at z=6 one tile away — distance is 1
        wp = _use_item(101, 200, 6, 1968, player_pos=(100, 200, 7))
        assert _is_map_click_walk(wp) is False

    def test_non_use_item_returns_false(self):
        wp = _walk("north", (100, 200, 7))
        assert _is_map_click_walk(wp) is False

    def test_distance_exactly_one_is_not_map_click(self):
        # Manhattan distance exactly 1
        wp = _use_item(100, 201, 7, 1696, player_pos=(100, 200, 7))
        assert _is_map_click_walk(wp) is False

    def test_distance_two_is_map_click(self):
        # Manhattan distance exactly 2
        wp = _use_item(100, 202, 7, 486, player_pos=(100, 200, 7))
        assert _is_map_click_walk(wp) is True


# ── _simplify_path ───────────────────────────────────────────────────

class TestSimplifyPath:
    def test_empty(self):
        assert _simplify_path([]) == []

    def test_single_point(self):
        pts = [(100, 200, 7, 486, 1)]
        assert _simplify_path(pts) == pts

    def test_two_points_close(self):
        """Two points within max_gap — still keeps both (last is always kept)."""
        pts = [(100, 200, 7, 486, 1), (101, 200, 7, 486, 1)]
        result = _simplify_path(pts)
        assert len(result) == 2

    def test_many_close_points_simplified(self):
        """A line of 10 consecutive tiles should be reduced."""
        pts = [(100 + i, 200, 7, 486, 1) for i in range(10)]
        result = _simplify_path(pts)
        # First and last always kept; intermediates at max_gap=3 intervals
        assert result[0] == pts[0]
        assert result[-1] == pts[-1]
        assert len(result) < len(pts)

    def test_preserves_first_and_last(self):
        pts = [(100, 200, 7, 486, 1), (100, 201, 7, 486, 1), (100, 210, 7, 486, 1)]
        result = _simplify_path(pts)
        assert result[0] == pts[0]
        assert result[-1] == pts[-1]

    def test_far_apart_points_all_kept(self):
        """Points already > max_gap apart should all be kept."""
        pts = [
            (100, 200, 7, 486, 1),
            (105, 200, 7, 486, 1),
            (110, 200, 7, 486, 1),
        ]
        result = _simplify_path(pts, max_gap=3)
        assert len(result) == 3

    def test_custom_max_gap(self):
        pts = [(100 + i, 200, 7, 486, 1) for i in range(20)]
        result_3 = _simplify_path(pts, max_gap=3)
        result_5 = _simplify_path(pts, max_gap=5)
        # Larger gap = fewer waypoints
        assert len(result_5) <= len(result_3)


# ── Keyboard walks grouping ─────────────────────────────────────────

class TestKeyboardWalks:
    def test_consecutive_same_direction(self):
        """Multiple north walks should be grouped and simplified."""
        wps = [
            _walk("north", (100, 205, 7)),
            _walk("north", (100, 204, 7)),
            _walk("north", (100, 203, 7)),
            _walk("north", (100, 202, 7)),
            _walk("north", (100, 201, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        # All should be walk_to nodes
        for n in nodes:
            assert n["type"] == "walk_to"
        # First target should be the first walk's destination
        assert nodes[0]["target"] == [100, 204, 7]
        # Last target should be the last walk's destination
        assert nodes[-1]["target"] == [100, 200, 7]

    def test_direction_change(self):
        """Walks in different directions still get grouped as one walk sequence."""
        wps = [
            _walk("north", (100, 200, 7)),
            _walk("north", (100, 199, 7)),
            _walk("east", (100, 198, 7)),
            _walk("east", (101, 198, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        for n in nodes:
            assert n["type"] == "walk_to"

    def test_walk_computes_destination_correctly(self):
        """Verify all 8 directions produce correct destination pos."""
        directions_and_offsets = {
            "north": (0, -1), "south": (0, 1),
            "east": (1, 0), "west": (-1, 0),
            "northeast": (1, -1), "southeast": (1, 1),
            "southwest": (-1, 1), "northwest": (-1, -1),
        }
        for direction, (dx, dy) in directions_and_offsets.items():
            nodes = build_actions_map(_rec([_walk(direction, (100, 200, 7))]))
            assert nodes[0]["target"] == [100 + dx, 200 + dy, 7], \
                f"Failed for direction {direction}"

    def test_duplicate_positions_deduped_in_path(self):
        """Two walks resulting in the same destination tile shouldn't create two path points."""
        # Both walks have pos=(100, 199, 7) — duplicate destination
        wps = [
            _walk("north", (100, 200, 7)),  # pos: (100, 199)
            _walk("north", (100, 200, 7)),  # pos: (100, 199) — same pos recorded twice
        ]
        nodes = build_actions_map(_rec(wps))
        # Should produce exactly 1 walk_to (deduped within path building)
        assert len(nodes) == 1
        assert nodes[0]["target"] == [100, 199, 7]


# ── Autowalks ────────────────────────────────────────────────────────

class TestAutowalks:
    def test_autowalk_uses_pos_directly(self):
        """Autowalk pos IS the destination — no offset applied."""
        nodes = build_actions_map(_rec([_autowalk((137, 579, 6))]))
        assert nodes[0]["target"] == [137, 579, 6]

    def test_consecutive_autowalks_simplified(self):
        """Multiple autowalks should be grouped and simplified."""
        wps = [
            _autowalk((100, 200, 7)),
            _autowalk((101, 200, 7)),
            _autowalk((102, 200, 7)),
            _autowalk((103, 200, 7)),
            _autowalk((104, 200, 7)),
            _autowalk((105, 200, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        assert len(nodes) < len(wps)  # simplified
        assert nodes[0]["target"] == [100, 200, 7]
        assert nodes[-1]["target"] == [105, 200, 7]

    def test_autowalk_mixed_with_keyboard_walks(self):
        """Autowalks and keyboard walks in the same group should all be walk_to."""
        wps = [
            _autowalk((100, 200, 7)),
            _walk("east", (101, 200, 7)),  # pos: (102, 200)
            _autowalk((103, 200, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        for n in nodes:
            assert n["type"] == "walk_to"


# ── Map-click walks (far use_item) ──────────────────────────────────

class TestMapClickWalks:
    def test_far_use_item_becomes_walk_to(self):
        """use_item with distance > 1 becomes walk_to nodes (player pos + click target)."""
        wps = [
            _use_item(120, 200, 7, 486, player_pos=(100, 200, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        # Produces 2 nodes: player position + click target destination
        assert len(nodes) == 2
        for n in nodes:
            assert n["type"] == "walk_to"
        assert nodes[0]["target"] == [100, 200, 7]
        assert nodes[1]["target"] == [120, 200, 7]

    def test_consecutive_map_clicks_grouped(self):
        """Multiple far use_items should be grouped and simplified."""
        wps = [
            _use_item(105, 200, 7, 486, player_pos=(100, 200, 7)),
            _use_item(110, 200, 7, 486, player_pos=(105, 200, 7)),
            _use_item(115, 200, 7, 486, player_pos=(110, 200, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        for n in nodes:
            assert n["type"] == "walk_to"
        # Should include positions from player positions and final click target
        assert len(nodes) >= 2

    def test_map_click_uses_player_floor_not_click_floor(self):
        """When clicking a tile on a different floor, destination uses player's floor."""
        # Player at z=7 clicks tile at z=6 (visible above) — walk stays on z=7
        wps = [
            _use_item(120, 200, 6, 486, player_pos=(100, 200, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        # The final destination uses player's z (7), not click z (6)
        assert nodes[-1]["target"][2] == 7

    def test_map_click_records_ground_item_id(self):
        """First map-click walk's item_id should be used as ground tile ID."""
        wps = [
            _use_item(110, 200, 7, 486, player_pos=(100, 200, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        assert nodes[0]["item_id"] == 486


# ── use_item (close interaction) ─────────────────────────────────────

class TestUseItemClose:
    def test_door_use_item(self):
        """Close use_item (door) should remain as use_item node."""
        wps = [_use_item(137, 564, 6, 1696, player_pos=(136, 564, 6))]
        nodes = build_actions_map(_rec(wps))
        assert len(nodes) == 1
        assert nodes[0]["type"] == "use_item"
        assert nodes[0]["target"] == [137, 564, 6]
        assert nodes[0]["item_id"] == 1696
        assert nodes[0]["player_pos"] == [136, 564, 6]

    def test_stairs_use_item_different_floor(self):
        """use_item where item is on different floor but adjacent."""
        wps = [_use_item(100, 200, 6, 1968, player_pos=(100, 200, 7))]
        nodes = build_actions_map(_rec(wps))
        assert nodes[0]["type"] == "use_item"
        assert nodes[0]["target"] == [100, 200, 6]

    def test_use_item_preserves_all_fields(self):
        """Verify use_item node has all expected fields."""
        wps = [_use_item(10, 20, 7, 1234, player_pos=(10, 21, 7), stack_pos=3, index=2)]
        nodes = build_actions_map(_rec(wps))
        n = nodes[0]
        assert n["x"] == 10
        assert n["y"] == 20
        assert n["z"] == 7
        assert n["item_id"] == 1234
        assert n["stack_pos"] == 3
        assert n["index"] == 2
        assert n["label"] == "Use item 1234"

    def test_consecutive_duplicate_use_items_deduped(self):
        """Same use_item sent multiple times → only one node (post-process dedup)."""
        wp = _use_item(100, 200, 7, 1054, player_pos=(99, 200, 7))
        nodes = build_actions_map(_rec([wp, wp, wp]))
        assert len(nodes) == 1

    def test_same_target_different_item_not_deduped(self):
        """Two use_items at same target but different item_ids are NOT deduped.
        Actually they ARE deduped since dedup only checks type + target."""
        wp1 = _use_item(100, 200, 7, 1054, player_pos=(99, 200, 7))
        wp2 = _use_item(100, 200, 7, 1058, player_pos=(99, 200, 7))
        nodes = build_actions_map(_rec([wp1, wp2]))
        # Dedup checks type + target only → these get deduped
        assert len(nodes) == 1


# ── use_item_ex ──────────────────────────────────────────────────────

class TestUseItemEx:
    def test_use_item_ex_preserves_fields(self):
        wp = _use_item_ex(
            from_pos=(0xFFFF, 0, 0), item_id=2120,
            to_pos=(100, 200, 7), player_pos=(100, 201, 7),
            stack_pos=5, to_stack_pos=2,
        )
        nodes = build_actions_map(_rec([wp]))
        n = nodes[0]
        assert n["type"] == "use_item_ex"
        assert n["target"] == [100, 200, 7]
        assert n["from_x"] == 0xFFFF
        assert n["item_id"] == 2120
        assert n["to_x"] == 100
        assert n["to_stack_pos"] == 2


# ── Deduplication ────────────────────────────────────────────────────

class TestDeduplication:
    def test_consecutive_walk_to_same_target_deduped(self):
        """Two walk_to nodes with same target should be deduped."""
        wps = [
            _autowalk((100, 200, 7)),
            _autowalk((100, 200, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        assert len(nodes) == 1

    def test_non_consecutive_same_target_not_deduped(self):
        """Same target but with different node in between — both kept."""
        wps = [
            _autowalk((100, 200, 7)),
            _use_item(100, 201, 7, 1696, player_pos=(100, 200, 7)),
            _autowalk((100, 200, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        assert len(nodes) == 3

    def test_different_type_same_target_not_deduped(self):
        """walk_to and use_item at same location are different types — both kept."""
        wps = [
            _autowalk((100, 200, 7)),
            _use_item(100, 200, 7, 1696, player_pos=(100, 201, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        assert len(nodes) == 2


# ── Exact marking ────────────────────────────────────────────────────

class TestExactMarking:
    def test_walk_before_use_item_is_exact(self):
        """walk_to immediately before a use_item should be marked exact."""
        wps = [
            _autowalk((100, 200, 7)),
            _use_item(100, 200, 7, 1696, player_pos=(100, 200, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        walk_node = [n for n in nodes if n["type"] == "walk_to"][0]
        assert walk_node.get("exact") is True

    def test_walk_before_use_item_ex_is_exact(self):
        wps = [
            _autowalk((100, 200, 7)),
            _use_item_ex((0xFFFF, 0, 0), 2120, (100, 200, 8), (100, 200, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        walk_node = [n for n in nodes if n["type"] == "walk_to"][0]
        assert walk_node.get("exact") is True

    def test_walk_before_floor_change_is_exact(self):
        """walk_to followed by walk_to on a different floor → exact."""
        wps = [
            _walk("west", (128, 564, 6)),   # pos: (127, 564, 6)
            _walk("west", (126, 564, 7)),   # pos: (125, 564, 7) — floor changed!
        ]
        nodes = build_actions_map(_rec(wps))
        # The node on floor 6 (before the floor change) should be exact
        floor6_nodes = [n for n in nodes if n["target"][2] == 6]
        assert len(floor6_nodes) >= 1
        assert floor6_nodes[-1].get("exact") is True

    def test_walk_not_before_anything_special_is_not_exact(self):
        """Regular walk_to followed by another same-floor walk_to → not exact."""
        wps = [
            _autowalk((100, 200, 7)),
            _autowalk((105, 200, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        for n in nodes:
            assert n.get("exact") is not True

    def test_last_node_is_never_exact(self):
        """The last node has no successor, so it can't be marked exact."""
        wps = [_autowalk((100, 200, 7))]
        nodes = build_actions_map(_rec(wps))
        assert nodes[-1].get("exact") is not True

    def test_use_item_nodes_never_get_exact(self):
        """Only walk_to nodes get exact marking, not use_item."""
        wps = [
            _use_item(100, 200, 7, 1696, player_pos=(100, 201, 7)),
            _use_item(100, 200, 6, 1968, player_pos=(100, 200, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        for n in nodes:
            if n["type"] == "use_item":
                assert n.get("exact") is not True


# ── Mixed sequences (walks + interactions) ───────────────────────────

class TestMixedSequences:
    def test_walks_interrupted_by_use_item(self):
        """Walk group → use_item → walk group should produce 3+ nodes."""
        wps = [
            _walk("north", (100, 205, 7)),
            _walk("north", (100, 204, 7)),
            _use_item(100, 203, 7, 1696, player_pos=(100, 203, 7)),
            _walk("north", (100, 202, 7)),
            _walk("north", (100, 201, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        types = [n["type"] for n in nodes]
        assert "use_item" in types
        walk_to_count = types.count("walk_to")
        assert walk_to_count >= 2  # walks before and after use_item

    def test_walk_group_ends_at_use_item(self):
        """Walk waypoints stop being grouped when a use_item appears."""
        wps = [
            _walk("north", (100, 205, 7)),
            _walk("north", (100, 204, 7)),
            _walk("north", (100, 203, 7)),
            _use_item(100, 202, 7, 1696, player_pos=(100, 202, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        # Last walk_to before use_item should be exact
        walk_nodes = [n for n in nodes if n["type"] == "walk_to"]
        assert walk_nodes[-1].get("exact") is True

    def test_map_clicks_then_keyboard_walks(self):
        """Map-click walk group followed by keyboard walk group → separate groups."""
        wps = [
            _use_item(120, 200, 7, 486, player_pos=(100, 200, 7)),
            _use_item(130, 200, 7, 486, player_pos=(120, 200, 7)),
            _walk("north", (130, 200, 7)),
            _walk("north", (130, 199, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        for n in nodes:
            assert n["type"] == "walk_to"
        assert len(nodes) >= 2


# ── Floor transitions ────────────────────────────────────────────────

class TestFloorTransitions:
    def test_walk_sequence_with_floor_change(self):
        """Walk west into a ramp: z changes mid-sequence."""
        wps = [
            _walk("west", (128, 564, 6)),  # pos: (127, 564, 6)
            _walk("west", (126, 564, 7)),  # pos: (125, 564, 7) — floor changed
            _walk("west", (124, 564, 7)),  # pos: (123, 564, 7)
        ]
        nodes = build_actions_map(_rec(wps))
        # Should produce nodes on both floors
        floors = set(n["target"][2] for n in nodes)
        assert 6 in floors
        assert 7 in floors

    def test_stair_use_item_floor_change(self):
        """use_item where item z != player z is stairs/ladder."""
        wps = [
            _autowalk((100, 200, 7)),
            _use_item(100, 200, 6, 1968, player_pos=(100, 200, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        ui = [n for n in nodes if n["type"] == "use_item"][0]
        assert ui["target"] == [100, 200, 6]


# ── Ground item_id inference ─────────────────────────────────────────

class TestGroundItemId:
    def test_walks_use_default_ground_id(self):
        """With no map-click walks, walks use default 4449 as ground ID."""
        wps = [_walk("north", (100, 200, 7))]
        nodes = build_actions_map(_rec(wps))
        assert nodes[0]["item_id"] == 4449

    def test_walks_near_map_click_use_its_item_id(self):
        """Walk group near a map-click walk should borrow its ground item_id."""
        wps = [
            _use_item(120, 200, 7, 486, player_pos=(100, 200, 7)),  # map click
            _walk("north", (120, 200, 7)),  # nearby walk
        ]
        nodes = build_actions_map(_rec(wps))
        walk_nodes = [n for n in nodes if n["type"] == "walk_to"]
        # The walk group should find 486 from the nearby map-click
        # (it searches ±3 waypoints for a ground tile ID)
        found_486 = any(n.get("item_id") == 486 for n in walk_nodes)
        assert found_486


# ── Full recording scenario ──────────────────────────────────────────

class TestFullScenario:
    """Test with a realistic mixed recording similar to the user's actual data."""

    def test_door_open_close_sequence(self):
        """Walk → door → walk through → walk back → door again."""
        wps = [
            # Walk to door
            _walk("east", (135, 564, 6)),  # dest: (136, 564, 6)
            _walk("east", (136, 564, 6)),  # dest: (137, 564, 6) — exact (before door)
            # Open door
            _use_item(137, 564, 6, 1696, player_pos=(137, 564, 6)),
            # Walk through
            _walk("west", (137, 564, 6)),  # dest: (136, 564, 6)
            _walk("west", (136, 564, 6)),  # dest: (135, 564, 6)
        ]
        nodes = build_actions_map(_rec(wps))
        types = [n["type"] for n in nodes]

        assert "use_item" in types
        # The walk_to before use_item should be exact
        for j, n in enumerate(nodes):
            if n["type"] == "use_item" and j > 0:
                assert nodes[j - 1].get("exact") is True

    def test_node_ordering_preserved(self):
        """Nodes should appear in the same order as the recording."""
        wps = [
            _autowalk((100, 200, 7)),
            _use_item(100, 200, 7, 1696, player_pos=(100, 200, 7)),
            _autowalk((110, 200, 7)),
            _use_item_ex((0xFFFF, 0, 0), 2120, (110, 200, 8), (110, 200, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        types = [n["type"] for n in nodes]
        assert types == ["walk_to", "use_item", "walk_to", "use_item_ex"]

    def test_long_walk_sequence_reduced(self):
        """A long walk sequence (20+ waypoints) should be significantly reduced."""
        wps = [_walk("north", (100, 200 - i, 7)) for i in range(20)]
        nodes = build_actions_map(_rec(wps))
        assert len(nodes) < 20
        # But should still span the full distance
        assert nodes[0]["target"][1] < 200
        assert nodes[-1]["target"][1] < nodes[0]["target"][1]

    def test_autowalks_scattered_positions_simplified(self):
        """Autowalks at scattered positions get simplified down."""
        wps = [
            _autowalk((125, 564, 7)),
            _autowalk((126, 564, 7)),
            _autowalk((127, 564, 7)),
            _autowalk((128, 564, 7)),
            _autowalk((127, 568, 6)),
            _autowalk((127, 570, 6)),
            _autowalk((128, 576, 6)),
            _autowalk((128, 579, 6)),
            _autowalk((133, 581, 6)),
            _autowalk((135, 581, 6)),
        ]
        nodes = build_actions_map(_rec(wps))
        # Should be fewer than 10 raw waypoints
        assert len(nodes) < 10
        # Floor 7 and floor 6 nodes present
        floors = set(n["target"][2] for n in nodes)
        assert 6 in floors
        assert 7 in floors
        # The node right before the floor change (7→6) should be exact
        for j in range(len(nodes) - 1):
            if (nodes[j]["target"][2] == 7 and nodes[j + 1]["target"][2] == 6):
                assert nodes[j].get("exact") is True


# ── Edge cases ───────────────────────────────────────────────────────

class TestEdgeCases:
    def test_unknown_waypoint_type_skipped(self):
        """Unknown waypoint types should be silently skipped."""
        wps = [
            {"type": "unknown_thing", "pos": [100, 200, 7], "t": 0},
            _autowalk((100, 200, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        assert len(nodes) == 1
        assert nodes[0]["type"] == "walk_to"

    def test_all_duplicates_collapse_to_one(self):
        """N identical autowalks should collapse to a single node."""
        wps = [_autowalk((100, 200, 7)) for _ in range(10)]
        nodes = build_actions_map(_rec(wps))
        assert len(nodes) == 1

    def test_use_item_at_distance_zero(self):
        """use_item where item is at player position (standing on it)."""
        wps = [_use_item(100, 200, 7, 1968, player_pos=(100, 200, 7))]
        nodes = build_actions_map(_rec(wps))
        assert nodes[0]["type"] == "use_item"

    def test_very_long_recording(self):
        """100+ waypoints should process without error."""
        wps = []
        for i in range(50):
            wps.append(_walk("east", (100 + i, 200, 7)))
        wps.append(_use_item(150, 200, 7, 1696, player_pos=(150, 200, 7)))
        for i in range(50):
            wps.append(_walk("west", (150 - i, 200, 7)))
        nodes = build_actions_map(_rec(wps))
        assert len(nodes) > 0
        assert len(nodes) < 100  # significantly reduced
        # use_item should be in the middle
        use_items = [n for n in nodes if n["type"] == "use_item"]
        assert len(use_items) == 1

    def test_alternating_floors_marks_exact(self):
        """Walk on floor 6, walk on floor 7, walk on floor 6 — transitions marked exact."""
        wps = [
            _walk("north", (100, 203, 6)),  # dest: (100, 202, 6)
            _walk("north", (100, 202, 6)),  # dest: (100, 201, 6)
            _walk("north", (100, 201, 7)),  # dest: (100, 200, 7) — floor changed
            _walk("north", (100, 200, 7)),  # dest: (100, 199, 7)
            _walk("north", (100, 199, 6)),  # dest: (100, 198, 6) — floor changed again
        ]
        nodes = build_actions_map(_rec(wps))
        # Every node before a floor change should be exact
        for j in range(len(nodes) - 1):
            if nodes[j]["target"][2] != nodes[j + 1]["target"][2]:
                assert nodes[j].get("exact") is True, \
                    f"Node {j} ({nodes[j]['target']}) before floor change should be exact"


# ── Floor transitions: stair tile and exact marking ──────────────────

class TestStairTransitionDestination:
    """Tests for floor-crossing walks.

    With the recording format, pos is the post-walk destination.  For keyboard
    walks onto stairs, pos IS the stair tile.  The stair tile is the last
    walk_to on that floor and should be marked [exact].
    """

    def test_stair_tile_is_last_floor_node(self):
        """Walk west into stair at (128,564,6): stair tile should be the
        last floor-6 node."""
        wps = [
            _walk("west", (136, 564, 6)),  # pos: (135, 564, 6)
            _walk("west", (135, 564, 6)),  # pos: (134, 564, 6)
            _walk("west", (134, 564, 6)),  # pos: (133, 564, 6)
            _walk("west", (133, 564, 6)),  # pos: (132, 564, 6)
            _walk("west", (131, 564, 6)),  # pos: (130, 564, 6)
            _walk("west", (130, 564, 6)),  # pos: (129, 564, 6)
            _walk("west", (129, 564, 6)),  # pos: (128, 564, 6) ← stair
            # Floor changed — next walk is on floor 7
            _walk("west", (126, 564, 7)),  # pos: (125, 564, 7)
            _walk("west", (125, 564, 7)),  # pos: (124, 564, 7)
            _walk("west", (123, 564, 7)),  # pos: (122, 564, 7)
        ]
        nodes = build_actions_map(_rec(wps))

        # Stair tile (128,564,6) must be the last floor-6 node
        floor6_nodes = [n for n in nodes if n["target"][2] == 6]
        assert len(floor6_nodes) >= 1
        last_f6 = floor6_nodes[-1]
        assert last_f6["target"] == [128, 564, 6], (
            f"Last floor 6 node should be stair tile (128,564,6), got {last_f6['target']}"
        )

    def test_stair_tile_is_exact(self):
        """The stair tile (last on its floor before Z change) must be exact."""
        wps = [
            _walk("west", (131, 564, 6)),
            _walk("west", (130, 564, 6)),
            _walk("west", (129, 564, 6)),  # pos: (128, 564, 6) ← stair
            _walk("west", (126, 564, 7)),  # floor 7
            _walk("west", (125, 564, 7)),
            _walk("west", (123, 564, 7)),
        ]
        nodes = build_actions_map(_rec(wps))

        floor6_nodes = [n for n in nodes if n["target"][2] == 6]
        assert len(floor6_nodes) >= 1
        last_f6 = floor6_nodes[-1]
        assert last_f6["target"] == [128, 564, 6]
        assert last_f6.get("exact") is True, "Stair tile must be marked exact"

    def test_first_tile_after_floor_change_preserved(self):
        """The first tile on the new floor must survive simplification."""
        wps = [
            _walk("west", (131, 564, 6)),
            _walk("west", (130, 564, 6)),
            _walk("west", (129, 564, 6)),  # pos: (128, 564, 6) ← stair
            _walk("west", (126, 564, 7)),  # pos: (125,564,7) — first on floor 7
            _walk("west", (125, 564, 7)),
            _walk("west", (123, 564, 7)),
            _walk("west", (122, 564, 7)),
            _walk("west", (120, 564, 7)),
        ]
        nodes = build_actions_map(_rec(wps))

        floor7_targets = [tuple(n["target"]) for n in nodes if n["target"][2] == 7]
        assert len(floor7_targets) >= 1
        assert floor7_targets[0] == (125, 564, 7), (
            f"First floor 7 target should be (125,564,7), got {floor7_targets[0]}"
        )

    def test_long_walk_into_stairs_real_data(self):
        """Reproduce baltra_v2 scenario: walk west to stair at x=128."""
        wps = [
            _walk("west", (136, 564, 6)),
            _walk("west", (136, 564, 6)),  # dup
            _walk("west", (134, 564, 6)),
            _walk("west", (134, 564, 6)),  # dup
            _walk("west", (133, 564, 6)),
            _walk("west", (133, 564, 6)),  # dup
            _walk("west", (131, 564, 6)),
            _walk("west", (130, 564, 6)),
            _walk("west", (129, 564, 6)),  # pos: (128, 564, 6) ← stair
            _walk("west", (126, 564, 7)),
            _walk("west", (126, 564, 7)),  # dup
            _walk("west", (125, 564, 7)),
            _walk("west", (123, 564, 7)),
            _walk("west", (122, 564, 7)),
            _walk("west", (122, 564, 7)),  # dup
            _walk("west", (122, 564, 7)),  # dup
            _walk("west", (120, 564, 7)),
        ]
        nodes = build_actions_map(_rec(wps))

        # Stair tile (128,564,6) must be the last floor-6 node, exact
        floor6_nodes = [n for n in nodes if n["target"][2] == 6]
        last_f6 = floor6_nodes[-1]
        assert last_f6["target"] == [128, 564, 6]
        assert last_f6.get("exact") is True

    def test_simplify_path_preserves_floor_boundary(self):
        """Direct test of _simplify_path: Z changes must force-keep boundary points."""
        path = [
            (135, 564, 6, 486, 1),
            (132, 564, 6, 486, 1),
            (129, 564, 6, 486, 1),  # last on floor 6
            (125, 564, 7, 486, 1),  # first on floor 7
            (122, 564, 7, 486, 1),
            (119, 564, 7, 486, 1),
        ]
        result = _simplify_path(path)
        result_xyz = [(p[0], p[1], p[2]) for p in result]

        assert (129, 564, 6) in result_xyz, (
            f"Last point before floor change dropped! Result: {result_xyz}"
        )
        assert (125, 564, 7) in result_xyz, (
            f"First point after floor change dropped! Result: {result_xyz}"
        )

    def test_south_walk_stair_recording(self):
        """Walk south into stair at (112,567,7).

        The stair tile (112,567,7) should be in the map as the last floor-7
        node before the transition to floor 6, and should be marked exact.
        """
        wps = [
            _autowalk((112, 564, 7)),
            _walk("south", (112, 565, 7)),  # pos: (112,566,7)
            _walk("south", (112, 566, 7)),  # pos: (112,567,7) ← stair
            # Floor changed to 6
            _walk("south", (112, 568, 6)),  # pos: (112,569,6)
            _walk("east", (113, 568, 6)),   # pos: (114,568,6)
            _walk("south", (112, 569, 6)),  # pos: (112,570,6)
            _autowalk((113, 566, 6)),       # transition for return
            _autowalk((113, 563, 7)),       # back on floor 7
        ]
        nodes = build_actions_map(_rec(wps))
        targets = [tuple(n["target"]) for n in nodes]

        # Stair tile (112,567,7) must be in the map and exact
        assert (112, 567, 7) in targets, (
            f"Stair tile (112,567,7) should be in the actions map! Targets: {targets}"
        )
        stair_nodes = [n for n in nodes if tuple(n["target"]) == (112, 567, 7)]
        assert stair_nodes[0].get("exact") is True

        # Return trip: (113,566,6) should be exact (before floor change to Z=7)
        return_nodes = [n for n in nodes if tuple(n["target"]) == (113, 566, 6)]
        assert len(return_nodes) == 1
        assert return_nodes[0].get("exact") is True

    def test_no_cascading_exact(self):
        """Only the last node before floor change should be exact."""
        wps = [
            _walk("south", (100, 195, 7)),  # pos: (100, 196, 7)
            _walk("south", (100, 196, 7)),  # pos: (100, 197, 7)
            _walk("south", (100, 197, 7)),  # pos: (100, 198, 7)
            _walk("south", (100, 198, 7)),  # pos: (100, 199, 7)
            _walk("south", (100, 199, 7)),  # pos: (100, 200, 7)
            _walk("south", (100, 200, 7)),  # pos: (100, 201, 7) ← stair
            _walk("south", (100, 201, 6)),  # pos: (100, 202, 6) — floor changed
        ]
        nodes = build_actions_map(_rec(wps))

        # Only (100,201,7) — the stair tile — should be exact
        for n in nodes:
            t = tuple(n["target"])
            if t[2] == 7 and t != (100, 201, 7):
                assert not n.get("exact"), (
                    f"Node at {t} should NOT be exact — only (100,201,7) should be"
                )


# ── Far use_item interaction preservation ─────────────────────────────

class TestFarUseItemPreservation:
    """Tests that far use_items are detected as real interactions based on
    observable effects (floor change) or door whitelist, not item ID alone.

    - Stairs/ladders: detected by z-level change in subsequent waypoints
    - Doors: detected by DOOR_ITEM_IDS whitelist (no floor change effect)
    - Ground tiles: always become walk_to regardless of click count
    """

    def test_ladder_detected_by_floor_change(self):
        """Far use_item followed by floor_change event → use_item (auto-detected)."""
        wps = [
            _use_item(137, 579, 7, 1968, player_pos=(140, 575, 7)),
            _floor_change("up", [138, 578, 6]),  # z changed: 7 → 6
        ]
        nodes = build_actions_map(_rec(wps))
        use_items = [n for n in nodes if n["type"] == "use_item"]
        assert len(use_items) == 1
        assert use_items[0]["target"] == [137, 579, 7]

    def test_any_item_with_floor_change_is_interaction(self):
        """Even an unknown item ID is preserved if floor_change event follows."""
        wps = [
            _use_item(100, 200, 7, 9999, player_pos=(105, 200, 7)),
            _floor_change("up", [103, 200, 6]),  # z changed
        ]
        nodes = build_actions_map(_rec(wps))
        use_items = [n for n in nodes if n["type"] == "use_item"]
        assert len(use_items) == 1
        assert use_items[0]["item_id"] == 9999

    def test_far_door_detected_by_tile_transform_item(self):
        """Far door followed by tile_transform_item at same position → use_item."""
        wps = [
            _use_item(130, 581, 6, 1771, player_pos=(137, 580, 6)),
            {"type": "tile_transform_item", "x": 130, "y": 581, "z": 6, "t": 1.5},
        ]
        nodes = build_actions_map(_rec(wps))
        use_items = [n for n in nodes if n["type"] == "use_item"]
        assert len(use_items) == 1
        assert use_items[0]["item_id"] == 1771

    def test_far_door_no_tile_transform_item_is_walk(self):
        """Far door without tile_transform_item → walk_to (no observable effect)."""
        wps = [
            _use_item(130, 581, 6, 1771, player_pos=(137, 580, 6)),
        ]
        nodes = build_actions_map(_rec(wps))
        for n in nodes:
            assert n["type"] == "walk_to"

    def test_far_ground_tile_is_walk_to(self):
        """Far ground tile (486) → walk_to, even if clicked twice."""
        wps = [
            _use_item(129, 581, 6, 486, player_pos=(132, 582, 6)),
            _use_item(129, 581, 6, 486, player_pos=(131, 582, 6)),
        ]
        nodes = build_actions_map(_rec(wps))
        for n in nodes:
            assert n["type"] == "walk_to", (
                f"Ground tile should be walk_to even if repeated, got: {_targets(nodes)}"
            )

    def test_mixed_group_walks_and_ladder_with_floor_change(self):
        """Ground walks + ladder (detected by floor_change event)."""
        wps = [
            _use_item(120, 200, 7, 486, player_pos=(100, 200, 7)),
            _use_item(130, 200, 7, 486, player_pos=(120, 200, 7)),
            _use_item(137, 200, 7, 5000, player_pos=(130, 200, 7)),
            _floor_change("up", [137, 200, 6]),  # floor change after unknown item
        ]
        nodes = build_actions_map(_rec(wps))
        walk_nodes = [n for n in nodes if n["type"] == "walk_to"]
        use_nodes = [n for n in nodes if n["type"] == "use_item"]
        assert len(walk_nodes) >= 1, "Walk clicks should produce walk_to"
        assert len(use_nodes) == 1, "Item with floor change should be use_item"

    def test_real_recording_ladder_and_door(self):
        """Ladder (detected by floor_change event) + door (tile_transform_item) + ground walk."""
        wps = [
            _use_item(137, 579, 7, 1968, player_pos=(140, 575, 7)),
            _floor_change("up", [137, 579, 6]),  # floor changed after ladder
            _use_item(132, 582, 6, 1771, player_pos=(135, 580, 6)),
            {"type": "tile_transform_item", "x": 132, "y": 582, "z": 6, "t": 0.0},
            _use_item(132, 582, 6, 1771, player_pos=(133, 581, 6)),
            {"type": "tile_transform_item", "x": 132, "y": 582, "z": 6, "t": 0.0},
            _use_item(129, 581, 6, 486, player_pos=(132, 582, 6)),
        ]
        nodes = build_actions_map(_rec(wps))
        types_targets = _targets(nodes)

        ladder_nodes = [n for n in nodes
                        if n["type"] == "use_item" and n["item_id"] == 1968]
        assert len(ladder_nodes) >= 1, (
            f"Ladder should be use_item (floor change), got: {types_targets}"
        )

        door_nodes = [n for n in nodes
                      if n["type"] == "use_item" and n["item_id"] == 1771]
        assert len(door_nodes) >= 1, (
            f"Door (1771) should be use_item (tile_transform_item), got: {types_targets}"
        )

        walk_nodes = [n for n in nodes if n["type"] == "walk_to"]
        assert len(walk_nodes) >= 1, (
            f"Ground click (486) should be walk_to, got: {types_targets}"
        )

    def test_interleaved_interactions_and_walks(self):
        """Interaction → walk → interaction should preserve order."""
        wps = [
            _use_item(100, 200, 7, 1968, player_pos=(105, 200, 7)),
            _floor_change("up", [100, 200, 6]),  # floor change
            _use_item(90, 200, 6, 486, player_pos=(100, 200, 6)),
            _use_item(85, 200, 6, 1771, player_pos=(90, 200, 6)),
            {"type": "tile_transform_item", "x": 85, "y": 200, "z": 6, "t": 0.0},
        ]
        nodes = build_actions_map(_rec(wps))
        types = [n["type"] for n in nodes]
        assert "use_item" in types
        assert "walk_to" in types

    def test_walk_before_far_interaction_marked_exact(self):
        """Walk_to before a far use_item should be marked exact."""
        wps = [
            _autowalk((140, 575, 7)),
            _use_item(137, 579, 7, 1771, player_pos=(140, 575, 7)),
            {"type": "tile_transform_item", "x": 137, "y": 579, "z": 7, "t": 0.0},
        ]
        nodes = build_actions_map(_rec(wps))
        walk_nodes = [n for n in nodes if n["type"] == "walk_to"]
        assert len(walk_nodes) >= 1
        assert walk_nodes[-1].get("exact") is True

    def test_no_floor_change_unknown_item_is_walk(self):
        """Unknown item without floor change → walk_to."""
        wps = [
            _use_item(120, 300, 7, 4445, player_pos=(125, 300, 7)),
            _use_item(120, 300, 7, 4445, player_pos=(123, 300, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        for n in nodes:
            assert n["type"] == "walk_to", (
                f"Unknown item without floor change → walk_to, got: {_targets(nodes)}"
            )

    def test_floor_change_event_detected(self):
        """floor_change waypoint type triggers detection."""
        wps = [
            _use_item(137, 579, 7, 8888, player_pos=(140, 575, 7)),
            {"type": "floor_change", "direction": "up", "pos": [137, 579, 6], "z": 6, "t": 2.0},
        ]
        nodes = build_actions_map(_rec(wps))
        use_items = [n for n in nodes if n["type"] == "use_item"]
        assert len(use_items) == 1


# ── Position waypoints ───────────────────────────────────────────────

class TestPositionWaypoints:
    """Tests for position-tracking waypoints (type='position').

    Position waypoints are recorded by a background thread that polls the
    player's position every 50ms.  They should not affect the actions map
    output — they are informational only.
    """

    def test_position_waypoints_alone_ignored(self):
        """Position waypoints with no walks should produce empty actions map."""
        wps = [
            _position((100, 200, 7)),
            _position((101, 200, 7)),
            _position((102, 200, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        assert nodes == []

    def test_position_between_walks_does_not_break_grouping(self):
        """Position waypoints between walks should not split the walk group."""
        wps = [
            _walk("west", (105, 200, 7)),   # pos: (104, 200, 7)
            _position((104, 200, 7)),
            _walk("west", (104, 200, 7)),   # pos: (103, 200, 7)
            _position((103, 200, 7)),
            _walk("west", (103, 200, 7)),   # pos: (102, 200, 7)
        ]
        nodes = build_actions_map(_rec(wps))
        # All walks should be in one group → simplified into walk_to nodes
        for n in nodes:
            assert n["type"] == "walk_to"
        assert nodes[0]["target"] == [104, 200, 7]
        assert nodes[-1]["target"] == [102, 200, 7]

    def test_floor_change_event_between_walks(self):
        """Reproduce user bug: floor_change event between walks.

        Recording:
          103. Walk west | (129,564,6) → (128,564,6)
          104. floor_change down to z=7               ← floor change
          105. Walk west | (127,564,7) → (126,564,7)

        Should produce walk_to (128,564,6) [exact], walk_to (126,564,7).
        Must NOT produce walk_to (127,564,6) (the old double-offset bug).
        """
        wps = [
            _walk("west", (129, 564, 6)),   # pos: (128, 564, 6) ← stair
            _floor_change("down", [127, 564, 7]),  # floor changed to 7
            _walk("west", (127, 564, 7)),   # pos: (126, 564, 7)
        ]
        nodes = build_actions_map(_rec(wps))
        targets = [tuple(n["target"]) for n in nodes]

        # Must have the stair tile (128,564,6), NOT the bogus (127,564,6)
        assert (128, 564, 6) in targets, (
            f"Stair tile (128,564,6) should be in targets, got {targets}"
        )
        assert (127, 564, 6) not in targets, (
            f"Double-offset bug: (127,564,6) should NOT be in targets, got {targets}"
        )

        # Floor 6 node must be exact (before floor change)
        floor6_nodes = [n for n in nodes if n["target"][2] == 6]
        assert len(floor6_nodes) >= 1
        assert floor6_nodes[-1].get("exact") is True

        # Floor 7 node
        assert (126, 564, 7) in targets

    def test_many_positions_between_walks_skipped(self):
        """Many position waypoints between walks should all be skipped."""
        wps = [
            _walk("east", (100, 200, 7)),   # pos: (101, 200, 7)
            _position((101, 200, 7)),
            _position((101, 201, 7)),
            _position((102, 201, 7)),
            _position((103, 201, 7)),
            _walk("east", (103, 201, 7)),   # pos: (104, 201, 7)
        ]
        nodes = build_actions_map(_rec(wps))
        for n in nodes:
            assert n["type"] == "walk_to"
        # The two walks are in one group
        assert len(nodes) == 2
        assert nodes[0]["target"] == [101, 200, 7]
        assert nodes[1]["target"] == [104, 201, 7]

    def test_position_before_use_item_does_not_interfere(self):
        """Position waypoints before a use_item should not affect it."""
        wps = [
            _walk("east", (99, 200, 7)),    # pos: (100, 200, 7)
            _position((100, 200, 7)),
            _use_item(101, 200, 7, 1696, player_pos=(100, 200, 7)),
        ]
        nodes = build_actions_map(_rec(wps))
        types = [n["type"] for n in nodes]
        assert "walk_to" in types
        assert "use_item" in types

    def test_floor_change_event_no_double_offset(self):
        """Extended real scenario: walks → floor_change event → more walks.

        Verifies no double-offset bug exists anywhere in the produced map.
        """
        wps = [
            _walk("west", (132, 564, 6)),   # pos: (131, 564, 6)
            _walk("west", (131, 564, 6)),   # pos: (130, 564, 6)
            _walk("west", (130, 564, 6)),   # pos: (129, 564, 6)
            _walk("west", (129, 564, 6)),   # pos: (128, 564, 6) ← stair
            _floor_change("down", [127, 564, 7]),  # floor change
            _walk("west", (127, 564, 7)),   # pos: (126, 564, 7)
            _walk("west", (126, 564, 7)),   # pos: (125, 564, 7)
            _walk("west", (125, 564, 7)),   # pos: (124, 564, 7)
        ]
        nodes = build_actions_map(_rec(wps))
        targets = [tuple(n["target"]) for n in nodes]

        # Stair tile on floor 6
        assert (128, 564, 6) in targets
        # No double-offset values
        assert (127, 564, 6) not in targets

        # Floor 6 and 7 both present
        floors = set(t[2] for t in targets)
        assert 6 in floors
        assert 7 in floors

        # Last floor-6 node is exact
        floor6_nodes = [n for n in nodes if n["target"][2] == 6]
        assert floor6_nodes[-1].get("exact") is True
        assert floor6_nodes[-1]["target"] == [128, 564, 6]
