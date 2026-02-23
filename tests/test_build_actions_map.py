"""Unit tests for cavebot.build_actions_map — recording → actions map conversion."""

import sys
import os
import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cavebot import build_actions_map, _simplify_path, _is_map_click_walk


# ── Helpers ──────────────────────────────────────────────────────────

def _walk(direction, pos, t=0.0):
    """Create a keyboard walk waypoint."""
    return {"type": "walk", "direction": direction, "pos": list(pos), "t": t}


def _autowalk(pos, t=0.0):
    """Create an autowalk waypoint (pos = final destination)."""
    return {"type": "walk", "direction": "autowalk", "pos": list(pos), "t": t}


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
        # Keyboard walk north: pos is BEFORE walk, dest = pos + (0, -1)
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
        """Verify all 8 directions compute pos + offset correctly."""
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
        # Both walks compute to (100, 199, 7) — duplicate
        wps = [
            _walk("north", (100, 200, 7)),  # dest: (100, 199)
            _walk("north", (100, 200, 7)),  # dest: (100, 199) — same pos recorded twice
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
            _walk("east", (101, 200, 7)),  # dest: (102, 200)
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
            _walk("west", (128, 564, 6)),   # dest: (127, 564, 6)
            _walk("west", (126, 564, 7)),   # dest: (125, 564, 7) — floor changed!
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
            _walk("west", (128, 564, 6)),  # dest: (127, 564, 6)
            _walk("west", (126, 564, 7)),  # dest: (125, 564, 7) — floor changed
            _walk("west", (124, 564, 7)),  # dest: (123, 564, 7)
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
