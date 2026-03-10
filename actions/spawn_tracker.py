"""Spawn Tracker — records when monsters reappear at kill locations.

Lightweight background action (1s polling). Feeds respawn interval data
into farming_telemetry.SpawnMap for HOT zone lingering accuracy.

Requires v2 telemetry: state.telemetry must be a FarmingTelemetry instance
(set by cavebot2.py). If telemetry is not available, this action idles.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constants import MONSTER_ID_MIN

GRID_SIZE = 3  # must match farming_telemetry.GRID_SIZE


async def run(bot):
    state = sys.modules["__main__"].state
    gs = state.game_state

    # Pending kill locations: {grid_key: kill_time}
    pending = {}
    last_kill_idx = 0  # track which kill_log entries we've processed

    bot.log("[SPAWN_TRACKER] Started — monitoring respawn intervals")

    while True:
        # Wait for telemetry to be available (set by cavebot2)
        telemetry = getattr(state, "telemetry", None)
        if telemetry is None:
            await bot.sleep(2)
            continue

        now = time.time()

        # Register new kills from kill_log
        kill_log = gs.kill_log
        for idx in range(last_kill_idx, len(kill_log)):
            kill = kill_log[idx]
            kx = kill.get("x", 0)
            ky = kill.get("y", 0)
            kz = kill.get("z", 0)
            if kx == 0 and ky == 0:
                continue
            gk = (kx // GRID_SIZE, ky // GRID_SIZE, kz)
            pending[gk] = kill["t"]
        last_kill_idx = len(kill_log)

        # Check for respawns: alive monster appears at a pending grid cell
        for cid, info in gs.creatures.items():
            if cid < MONSTER_ID_MIN:
                continue
            hp = info.get("health", 0)
            if hp <= 0 or hp > 100:
                continue
            cx = info.get("x", 0)
            cy = info.get("y", 0)
            cz = info.get("z", 0)
            if cx == 0 and cy == 0:
                continue
            gk = (cx // GRID_SIZE, cy // GRID_SIZE, cz)
            if gk in pending:
                interval = now - pending[gk]
                if 5 < interval < 300:
                    telemetry.record_respawn(gk, interval)
                del pending[gk]

        # Prune stale pending entries (older than 5 minutes)
        pending = {k: v for k, v in pending.items() if now - v < 300}

        await bot.sleep(1.0)
