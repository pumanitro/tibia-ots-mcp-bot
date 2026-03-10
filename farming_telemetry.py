"""
Farming Telemetry — kill density tracking and fight performance logging.

Pure data module imported by v2 cavebot/targeting components.
Tracks per-grid-cell kill density (SpawnMap) and per-fight summaries (FightLog).
"""

import json
import logging
import os
import time
from collections import deque
from pathlib import Path

log = logging.getLogger("farming_telemetry")

RECORDINGS_DIR = Path(__file__).parent / "recordings"

# Grid cell size for spawn density (3x3 tiles per cell)
GRID_SIZE = 3


class SpawnMap:
    """3x3 tile grid kill density tracker.

    Key: (tile_x // GRID_SIZE, tile_y // GRID_SIZE, z)
    Value: {"kills": int, "total_xp": int, "last_kill_t": float,
            "kill_times": deque(maxlen=20), "respawn_intervals": deque(maxlen=10)}
    """

    def __init__(self):
        self.cells: dict[tuple[int, int, int], dict] = {}

    def _key(self, x: int, y: int, z: int) -> tuple[int, int, int]:
        return (x // GRID_SIZE, y // GRID_SIZE, z)

    def record_kill(self, x: int, y: int, z: int, xp: int = 0) -> None:
        """Record a monster kill at the given position."""
        if x == 0 and y == 0:
            return  # invalid position
        gk = self._key(x, y, z)
        if gk not in self.cells:
            self.cells[gk] = {
                "kills": 0,
                "total_xp": 0,
                "last_kill_t": 0.0,
                "kill_times": deque(maxlen=20),
                "respawn_intervals": deque(maxlen=10),
            }
        cell = self.cells[gk]
        cell["kills"] += 1
        cell["total_xp"] += xp
        cell["last_kill_t"] = time.time()
        cell["kill_times"].append(time.time())

    def record_respawn(self, grid_key: tuple[int, int, int], interval: float) -> None:
        """Record a respawn interval for a grid cell."""
        if grid_key not in self.cells:
            self.cells[grid_key] = {
                "kills": 0,
                "total_xp": 0,
                "last_kill_t": 0.0,
                "kill_times": deque(maxlen=20),
                "respawn_intervals": deque(maxlen=10),
            }
        self.cells[grid_key]["respawn_intervals"].append(interval)

    def density_at(self, x: int, y: int, z: int) -> int:
        """Get kill count at a position's grid cell."""
        gk = self._key(x, y, z)
        cell = self.cells.get(gk)
        return cell["kills"] if cell else 0

    def avg_respawn_at(self, pos: tuple[int, int, int]) -> float | None:
        """Get average respawn interval at a position, or None if no data."""
        gk = self._key(pos[0], pos[1], pos[2])
        cell = self.cells.get(gk)
        if not cell:
            return None
        intervals = cell.get("respawn_intervals")
        if not intervals or len(intervals) < 2:
            return None
        return sum(intervals) / len(intervals)

    def density_ahead(self, x: int, y: int, z: int, radius: int = 2) -> float:
        """Average kill density in a radius of grid cells around a position."""
        gx, gy = x // GRID_SIZE, y // GRID_SIZE
        total = 0
        count = 0
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                gk = (gx + dx, gy + dy, z)
                cell = self.cells.get(gk)
                total += cell["kills"] if cell else 0
                count += 1
        return total / count if count > 0 else 0

    def to_dict(self) -> dict:
        """Serialize for JSON persistence (deques become lists)."""
        result = {}
        for gk, cell in self.cells.items():
            key_str = f"{gk[0]},{gk[1]},{gk[2]}"
            result[key_str] = {
                "kills": cell["kills"],
                "total_xp": cell["total_xp"],
                "last_kill_t": cell["last_kill_t"],
                "kill_times": list(cell["kill_times"]),
                "respawn_intervals": list(cell.get("respawn_intervals", [])),
            }
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "SpawnMap":
        """Deserialize from JSON."""
        sm = cls()
        for key_str, cell_data in data.items():
            parts = key_str.split(",")
            gk = (int(parts[0]), int(parts[1]), int(parts[2]))
            sm.cells[gk] = {
                "kills": cell_data.get("kills", 0),
                "total_xp": cell_data.get("total_xp", 0),
                "last_kill_t": cell_data.get("last_kill_t", 0.0),
                "kill_times": deque(cell_data.get("kill_times", []), maxlen=20),
                "respawn_intervals": deque(cell_data.get("respawn_intervals", []), maxlen=10),
            }
        return sm


class FarmingTelemetry:
    """Top-level telemetry container. One instance per session."""

    def __init__(self):
        self.spawn_map = SpawnMap()
        self.fight_log: deque = deque(maxlen=100)
        # Per-segment stats for route optimization
        self.segment_stats: dict[int, dict] = {}

    def record_kill(self, x: int, y: int, z: int, xp: int = 0) -> None:
        """Delegate kill recording to spawn_map."""
        self.spawn_map.record_kill(x, y, z, xp)

    def record_respawn(self, grid_key: tuple[int, int, int], interval: float) -> None:
        """Delegate respawn recording to spawn_map."""
        self.spawn_map.record_respawn(grid_key, interval)

    def record_fight(self, kills: int, duration_s: float, mana_used_pct: float,
                     nearby_at_start: int, lure_count_used: int) -> None:
        """Record a completed fight summary."""
        self.fight_log.append({
            "t": time.time(),
            "kills": kills,
            "duration_s": round(duration_s, 1),
            "mana_used_pct": round(mana_used_pct, 1),
            "nearby_at_start": nearby_at_start,
            "lure_count_used": lure_count_used,
        })

    def avg_fight_duration(self, last_n: int = 10) -> float | None:
        """Average duration of last N fights, or None if no data."""
        recent = list(self.fight_log)[-last_n:]
        if not recent:
            return None
        return sum(f["duration_s"] for f in recent) / len(recent)

    def avg_mana_remaining(self, last_n: int = 10) -> float | None:
        """Average mana remaining % at end of last N fights."""
        recent = list(self.fight_log)[-last_n:]
        if not recent:
            return None
        return sum(100 - f["mana_used_pct"] for f in recent) / len(recent)

    def update_segment_stats(self, seg_idx: int, kills: int, xp: int,
                             duration: float) -> None:
        """Update per-segment stats for route optimization."""
        if seg_idx not in self.segment_stats:
            self.segment_stats[seg_idx] = {
                "kills": 0, "xp": 0, "time_total": 0.0, "entries": 0,
            }
        stats = self.segment_stats[seg_idx]
        stats["kills"] += kills
        stats["xp"] += xp
        stats["time_total"] += duration
        stats["entries"] += 1

    def segment_rating(self, seg_idx: int) -> str:
        """Rate a segment: 'high', 'medium', 'low', 'dead'."""
        stats = self.segment_stats.get(seg_idx)
        if not stats or stats["entries"] == 0:
            return "unknown"
        if stats["kills"] == 0:
            return "dead"
        # Compute XP/s for this segment
        xps = stats["xp"] / stats["time_total"] if stats["time_total"] > 0 else 0
        # Compare against average
        total_xp = sum(s["xp"] for s in self.segment_stats.values())
        total_time = sum(s["time_total"] for s in self.segment_stats.values())
        avg_xps = total_xp / total_time if total_time > 0 else 0
        if avg_xps == 0:
            return "medium"
        if xps >= avg_xps * 1.2:
            return "high"
        if xps >= avg_xps * 0.5:
            return "medium"
        return "low"

    def save(self, name: str) -> None:
        """Persist telemetry to recordings/<name>_telemetry.json."""
        RECORDINGS_DIR.mkdir(exist_ok=True)
        path = RECORDINGS_DIR / f"{name}_telemetry.json"
        data = {
            "spawn_map": self.spawn_map.to_dict(),
            "fight_log": list(self.fight_log),
            "segment_stats": {str(k): v for k, v in self.segment_stats.items()},
        }
        try:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as e:
            log.warning(f"Failed to save telemetry: {e}")

    @classmethod
    def load(cls, name: str) -> "FarmingTelemetry":
        """Load telemetry from disk, or return fresh instance."""
        path = RECORDINGS_DIR / f"{name}_telemetry.json"
        ft = cls()
        if not path.exists():
            return ft
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ft.spawn_map = SpawnMap.from_dict(data.get("spawn_map", {}))
            for fight in data.get("fight_log", []):
                ft.fight_log.append(fight)
            for k, v in data.get("segment_stats", {}).items():
                ft.segment_stats[int(k)] = v
        except (json.JSONDecodeError, OSError, ValueError) as e:
            log.warning(f"Failed to load telemetry: {e}")
        return ft
