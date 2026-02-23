"use client";

import { useBot } from "@/lib/useBot";
import { useState, useEffect, useRef } from "react";
import {
  toggleAction,
  deleteAction,
  startRecording,
  stopRecording,
  playRecording,
  stopPlayback,
  deleteRecording,
  fetchRecording,
  fetchActionsMap,
} from "@/lib/api";
import type {
  ActionInfo,
  PlayerInfo,
  CreatureInfo,
  CavebotState,
  WaypointInfo,
  MinimapData,
  ProxySequence,
} from "@/lib/api";

function StatusBadge({ label, connected }: { label: string; connected: boolean }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium ${
        connected
          ? "bg-emerald-900/50 text-emerald-300"
          : "bg-red-900/50 text-red-300"
      }`}
    >
      <span
        className={`h-2 w-2 rounded-full ${
          connected ? "bg-emerald-400 animate-pulse" : "bg-red-400"
        }`}
      />
      {label}
    </span>
  );
}

function Toggle({
  enabled,
  onChange,
}: {
  enabled: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <button
      onClick={() => onChange(!enabled)}
      className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full transition-colors ${
        enabled ? "bg-emerald-500" : "bg-gray-600"
      }`}
    >
      <span
        className={`pointer-events-none inline-block h-5 w-5 translate-y-0.5 rounded-full bg-white shadow transition-transform ${
          enabled ? "translate-x-5.5" : "translate-x-0.5"
        }`}
      />
    </button>
  );
}

function ActionCard({
  action,
  onRefresh,
}: {
  action: ActionInfo;
  onRefresh: () => void;
}) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [showLogs, setShowLogs] = useState(false);

  const handleToggle = async (enabled: boolean) => {
    await toggleAction(action.name, enabled);
    onRefresh();
  };

  const handleDelete = async () => {
    await deleteAction(action.name);
    setConfirmDelete(false);
    onRefresh();
  };

  const hasLogs = action.logs && action.logs.length > 0;

  return (
    <div className="rounded-lg border border-gray-700 bg-gray-800 p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          {/* Running indicator */}
          <span
            className={`h-2.5 w-2.5 rounded-full ${
              action.running
                ? "bg-emerald-400 animate-pulse"
                : "bg-gray-600"
            }`}
          />
          <div>
            <h3 className="font-semibold text-sm">{action.name}</h3>
            <p className="text-xs text-gray-400 mt-0.5">
              {action.description || "No description"}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          {hasLogs && (
            <button
              onClick={() => setShowLogs(!showLogs)}
              className={`rounded px-2 py-1 text-xs transition-colors ${
                showLogs
                  ? "text-blue-300 bg-blue-900/30"
                  : "text-gray-300 hover:bg-gray-700"
              }`}
              title="Toggle logs"
            >
              Logs
            </button>
          )}
          <button
            onClick={() => setConfirmDelete(true)}
            className="rounded px-2 py-1 text-xs text-red-400 hover:bg-red-900/30 transition-colors"
            title="Remove action"
          >
            Remove
          </button>
          <Toggle enabled={action.enabled} onChange={handleToggle} />
        </div>
      </div>

      {/* Logs panel */}
      {showLogs && hasLogs && (
        <div className="mt-3 rounded-md bg-gray-900 border border-gray-700 p-3 max-h-48 overflow-y-auto">
          <div className="space-y-0.5 font-mono text-xs text-gray-300">
            {action.logs!.map((line, i) => (
              <div key={i} className={`whitespace-pre-wrap ${line?.includes("[SUCCESS]") ? "text-emerald-400" : line?.includes("[FAILURE]") ? "text-red-400" : ""}`}>{line}</div>
            ))}
          </div>
        </div>
      )}

      {/* Delete confirmation */}
      {confirmDelete && (
        <div className="mt-3 flex items-center justify-between rounded-md border border-red-800 bg-red-950/40 px-3 py-2">
          <span className="text-xs text-red-300">
            Delete <strong>{action.name}</strong>? This cannot be undone.
          </span>
          <div className="flex gap-2">
            <button
              onClick={() => setConfirmDelete(false)}
              className="rounded px-2 py-1 text-xs text-gray-300 hover:bg-gray-700 transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleDelete}
              className="rounded px-2 py-1 text-xs text-white bg-red-600 hover:bg-red-500 transition-colors"
            >
              Confirm
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function StatBar({
  label,
  current,
  max,
  color,
}: {
  label: string;
  current: number;
  max: number;
  color: string;
}) {
  const pct = max > 0 ? Math.round((current / max) * 100) : 0;
  return (
    <div>
      <div className="flex justify-between text-xs mb-1">
        <span className="text-gray-400">{label}</span>
        <span className="tabular-nums">
          {current} / {max}
        </span>
      </div>
      <div className="h-3 rounded-full bg-gray-700 overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-300 ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function ConnectionSequence({ seq }: { seq: ProxySequence }) {
  const steps = [
    { key: "proxy_created", label: "Proxy Created" },
    { key: "listening", label: "Listening" },
    { key: "client_connected", label: "Client Connected" },
    { key: "server_connected", label: "Server Connected" },
    { key: "xtea_captured", label: "XTEA Keys Captured" },
    { key: "logged_in", label: "Logged In" },
    { key: "packets_flowing", label: "Packets Flowing" },
  ] as const;

  // Find the first failed step
  const firstFail = steps.findIndex((s) => !seq[s.key]);
  const allGood = firstFail === -1;

  return (
    <div className={`rounded-lg border p-3 mb-4 ${
      allGood ? "border-emerald-700 bg-emerald-950/20" : "border-yellow-700 bg-yellow-950/20"
    }`}>
      <p className="text-xs text-gray-400 uppercase tracking-wider mb-2">
        Proxy Connection Sequence
      </p>
      <div className="flex items-center gap-1">
        {steps.map((s, i) => {
          const ok = seq[s.key];
          const isBlocker = i === firstFail;
          return (
            <div key={s.key} className="flex items-center gap-1">
              {i > 0 && (
                <span className={`text-xs ${ok ? "text-emerald-600" : "text-gray-600"}`}>
                  &rarr;
                </span>
              )}
              <div
                className={`rounded px-2 py-1 text-xs font-medium ${
                  ok
                    ? "bg-emerald-900/50 text-emerald-300"
                    : isBlocker
                    ? "bg-red-900/50 text-red-300 animate-pulse"
                    : "bg-gray-800 text-gray-500"
                }`}
                title={s.label}
              >
                {s.label}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function PlayerStats({ player }: { player: PlayerInfo }) {
  const [x, y, z] = player.position;
  return (
    <div className="rounded-lg border border-gray-700 bg-gray-800 p-4 mb-8 space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-sm font-semibold text-gray-300">
          Level {player.level}
        </span>
        <span className="text-xs text-gray-500 tabular-nums">
          ({x}, {y}, {z})
        </span>
      </div>
      <StatBar label="HP" current={player.hp} max={player.max_hp} color="bg-red-500" />
      <StatBar label="Mana" current={player.mana} max={player.max_mana} color="bg-blue-500" />
    </div>
  );
}

function CreatureList({ creatures }: { creatures: CreatureInfo[] }) {
  return (
    <div className="mb-8">
      <div className="flex items-center gap-2 mb-3">
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider">
          Creatures
        </h2>
        <span className="rounded-full bg-gray-700 px-2 py-0.5 text-xs tabular-nums text-gray-300">
          {creatures.length}
        </span>
      </div>
      {creatures.length > 0 ? (
        <div className="rounded-lg border border-gray-700 bg-gray-800 divide-y divide-gray-700">
          {creatures.map((c) => (
            <div key={c.id} className="flex items-center gap-3 px-4 py-2">
              <span className="text-sm text-gray-200 w-28 shrink-0 truncate" title={`#${c.id}`}>
                {c.name || `#${c.id}`}
              </span>
              <span className="text-xs tabular-nums text-gray-500 w-24 shrink-0">
                ({c.x}, {c.y}, {c.z})
              </span>
              <div className="flex-1 h-2.5 rounded-full bg-gray-700 overflow-hidden">
                <div
                  className="h-full rounded-full transition-all duration-300 bg-gradient-to-r from-emerald-600 to-emerald-400"
                  style={{ width: `${c.health}%` }}
                />
              </div>
              <span className="text-xs tabular-nums text-gray-300 w-10 text-right">
                {c.health}%
              </span>
            </div>
          ))}
        </div>
      ) : (
        <p className="text-sm text-gray-500">No creatures nearby</p>
      )}
    </div>
  );
}

const WALK_OFFSETS: Record<string, [number, number]> = {
  north: [0, -1], south: [0, 1], east: [1, 0], west: [-1, 0],
  northeast: [1, -1], northwest: [-1, -1], southeast: [1, 1], southwest: [-1, 1],
};

function WaypointLine({ wp, index }: { wp: WaypointInfo; index: number }) {
  let desc = "";
  if (wp.type === "walk") {
    const pos = wp.pos;
    const dir = wp.direction ?? "?";
    const off = WALK_OFFSETS[dir];
    // Keyboard walks: pos is BEFORE walk, compute destination
    // Autowalks: pos is already the destination
    const dest = dir === "autowalk" || !off
      ? pos
      : [pos[0] + off[0], pos[1] + off[1], pos[2]];
    desc = `Walk ${dir} | cur: (${pos[0]},${pos[1]},${pos[2]}) \u2192 dest: (${dest[0]},${dest[1]},${dest[2]})`;
  } else if (wp.type === "use_item") {
    const label = wp.label ?? `item ${wp.item_id}`;
    const pos = wp.pos;
    desc = `${label} at (${wp.x}, ${wp.y}, ${wp.z}) \u2192 (${pos[0]}, ${pos[1]}, ${pos[2]})`;
  } else if (wp.type === "use_item_ex") {
    const label = wp.label ?? `item ${wp.item_id}`;
    const pos = wp.pos;
    desc = `${label} \u2192 (${pos[0]}, ${pos[1]}, ${pos[2]})`;
  } else {
    desc = wp.type;
  }
  return (
    <div className="text-xs text-gray-300 font-mono whitespace-nowrap">
      <span className="text-gray-500 mr-2">{index + 1}.</span>
      {desc}
    </div>
  );
}

function MinimapChar({ ch }: { ch: string }) {
  const colorMap: Record<string, string> = {
    "@": "text-blue-400",
    ">": "text-cyan-400",
    "#": "text-emerald-500",
    "o": "text-gray-400",
    "*": "text-emerald-500",
    "+": "text-red-400",
    "!": "text-yellow-300",
    "1": "text-yellow-600",
    "X": "text-red-500",
    "-": "text-gray-600",
    "|": "text-gray-600",
  };
  const color = colorMap[ch] ?? "text-gray-800";
  return <span className={color}>{ch}</span>;
}

function Minimap({ minimaps, nodeIndex, total }: {
  minimaps: Record<string, MinimapData>;
  nodeIndex: number;
  total: number;
}) {
  // Get sorted floor list and find the player's floor
  const floors = Object.keys(minimaps).map(Number).sort((a, b) => a - b);
  const playerFloor = floors.find((f) => minimaps[f].floor === f && minimaps[f].grid.some((r) => r.includes("@"))) ?? floors[0];

  const [viewFloor, setViewFloor] = useState(playerFloor);
  const [blink, setBlink] = useState(true);

  // Blink the player @ and > characters
  useEffect(() => {
    const id = setInterval(() => setBlink((b) => !b), 500);
    return () => clearInterval(id);
  }, []);

  // Sync to player floor when it changes
  useEffect(() => {
    setViewFloor(playerFloor);
  }, [playerFloor]);

  const floorIdx = floors.indexOf(viewFloor);
  const currentMinimap = minimaps[viewFloor] ?? minimaps[floors[0]];

  const handleFloorUp = () => {
    if (floorIdx > 0) setViewFloor(floors[floorIdx - 1]);
  };
  const handleFloorDown = () => {
    if (floorIdx < floors.length - 1) setViewFloor(floors[floorIdx + 1]);
  };

  const isLiveFloor = viewFloor === playerFloor;

  return (
    <div className="rounded-md bg-gray-950 border border-gray-700 p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-medium text-gray-400">Minimap</span>
        <div className="flex items-center gap-2">
          <button
            onClick={handleFloorUp}
            disabled={floorIdx <= 0}
            className="rounded px-1.5 py-0.5 text-xs text-gray-300 hover:bg-gray-700 disabled:opacity-30 transition-colors"
          >
            ^
          </button>
          <span className="text-xs text-gray-400 tabular-nums w-14 text-center">
            Floor {viewFloor}
          </span>
          <button
            onClick={handleFloorDown}
            disabled={floorIdx >= floors.length - 1}
            className="rounded px-1.5 py-0.5 text-xs text-gray-300 hover:bg-gray-700 disabled:opacity-30 transition-colors"
          >
            v
          </button>
        </div>
      </div>
      <pre className="font-mono text-xs select-none overflow-x-auto" style={{ lineHeight: "1", letterSpacing: "0.25em" }}>
        {currentMinimap.grid.map((row, y) => (
          <div key={y}>
            {[...row].map((ch, x) => {
              if (ch === "@" && !blink) {
                return <span key={x} className="text-blue-400"> </span>;
              }
              if (ch === ">" && !blink) {
                return <span key={x} className="text-cyan-400"> </span>;
              }
              return <MinimapChar key={x} ch={ch} />;
            })}
          </div>
        ))}
      </pre>
      <div className="flex items-center justify-between mt-2">
        <span className="text-xs text-gray-500 tabular-nums">
          Node {nodeIndex + 1}/{total}
        </span>
        {!isLiveFloor && (
          <span className="text-xs text-yellow-600">(viewing floor {viewFloor})</span>
        )}
      </div>
    </div>
  );
}

function CavebotPanel({
  cavebot,
  onRefresh,
}: {
  cavebot: CavebotState;
  onRefresh: () => void;
}) {
  const [recName, setRecName] = useState("");
  const [loopEnabled, setLoopEnabled] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [previewName, setPreviewName] = useState<string | null>(null);
  const [previewWaypoints, setPreviewWaypoints] = useState<WaypointInfo[]>([]);
  const [mapPreviewName, setMapPreviewName] = useState<string | null>(null);
  const [mapPreviewText, setMapPreviewText] = useState<string>("");
  const [showPlaybackLogs, setShowPlaybackLogs] = useState(false);
  const logsContainerRef = useRef<HTMLDivElement>(null);
  const lastLogsContainerRef = useRef<HTMLDivElement>(null);

  const { recording, playback, recordings } = cavebot;

  // Smart auto-scroll: only scroll if user is near the bottom
  useEffect(() => {
    const el = logsContainerRef.current;
    if (!el) return;
    const isNearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
    if (isNearBottom) el.scrollTop = el.scrollHeight;
  }, [playback.logs]);
  useEffect(() => {
    const el = lastLogsContainerRef.current;
    if (!el) return;
    const isNearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
    if (isNearBottom) el.scrollTop = el.scrollHeight;
  }, [playback.logs, showPlaybackLogs]);

  const handleStartRecording = async () => {
    if (!recName.trim()) return;
    await startRecording(recName.trim());
    setRecName("");
    onRefresh();
  };

  const handleStopRecording = async () => {
    await stopRecording();
    onRefresh();
  };

  const handleDiscard = async () => {
    await stopRecording(true);
    onRefresh();
  };

  const handlePlay = async (name: string) => {
    await playRecording(name, loopEnabled);
    onRefresh();
  };

  const handleStopPlayback = async () => {
    await stopPlayback();
    onRefresh();
  };

  const handleDelete = async (name: string) => {
    await deleteRecording(name);
    setConfirmDelete(null);
    if (previewName === name) {
      setPreviewName(null);
      setPreviewWaypoints([]);
    }
    onRefresh();
  };

  const handlePreview = async (name: string) => {
    if (previewName === name) {
      setPreviewName(null);
      setPreviewWaypoints([]);
      return;
    }
    setMapPreviewName(null);
    setMapPreviewText("");
    const rec = await fetchRecording(name);
    if (rec) {
      setPreviewName(name);
      setPreviewWaypoints(rec.waypoints ?? []);
    }
  };

  const handleMapPreview = async (name: string) => {
    if (mapPreviewName === name) {
      setMapPreviewName(null);
      setMapPreviewText("");
      return;
    }
    setPreviewName(null);
    setPreviewWaypoints([]);
    const resp = await fetchActionsMap(name);
    if (resp) {
      setMapPreviewName(name);
      setMapPreviewText(resp.text_preview);
    }
  };

  const hasPlaybackLogs = playback.logs && playback.logs.length > 0;

  return (
    <div className="mb-8">
      <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">
        Cavebot
      </h2>
      <div className="rounded-lg border border-gray-700 bg-gray-800 p-4 space-y-4">
        {/* Saved Recordings */}
        {recordings.length > 0 && (
          <div>
            <h3 className="text-xs font-medium text-gray-400 mb-2">
              Saved Recordings
            </h3>
            <div className="space-y-1">
              {recordings.map((r) => (
                <div key={r.name}>
                  <div className="flex items-center justify-between rounded-md bg-gray-900 px-3 py-2">
                    <div className="flex items-center gap-2">
                      <span className="text-sm text-gray-200">{r.name}</span>
                      <span className="text-xs text-gray-500">
                        ({r.count} wp)
                      </span>
                    </div>
                    <div className="flex items-center gap-1">
                      {confirmDelete === r.name ? (
                        <>
                          <button
                            onClick={() => setConfirmDelete(null)}
                            className="rounded px-2 py-1 text-xs text-gray-300 hover:bg-gray-700 transition-colors"
                          >
                            Cancel
                          </button>
                          <button
                            onClick={() => handleDelete(r.name)}
                            className="rounded px-2 py-1 text-xs text-white bg-red-600 hover:bg-red-500 transition-colors"
                          >
                            Confirm
                          </button>
                        </>
                      ) : (
                        <>
                          <button
                            onClick={() => handlePreview(r.name)}
                            className={`rounded px-2 py-1 text-xs transition-colors ${
                              previewName === r.name
                                ? "text-blue-300 bg-blue-900/30"
                                : "text-gray-300 hover:bg-gray-700"
                            }`}
                          >
                            Preview
                          </button>
                          <button
                            onClick={() => handleMapPreview(r.name)}
                            className={`rounded px-2 py-1 text-xs transition-colors ${
                              mapPreviewName === r.name
                                ? "text-purple-300 bg-purple-900/30"
                                : "text-gray-300 hover:bg-gray-700"
                            }`}
                          >
                            Map
                          </button>
                          <button
                            onClick={() => handlePlay(r.name)}
                            disabled={playback.active || recording.active}
                            className="rounded px-2 py-1 text-xs text-emerald-300 hover:bg-emerald-900/30 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                          >
                            Play
                          </button>
                          <button
                            onClick={() => setConfirmDelete(r.name)}
                            disabled={playback.active && playback.recording_name === r.name}
                            className="rounded px-2 py-1 text-xs text-red-400 hover:bg-red-900/30 transition-colors disabled:opacity-40"
                          >
                            Del
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                  {/* Recording waypoint preview */}
                  {previewName === r.name && previewWaypoints.length > 0 && (
                    <div className="rounded-b-md bg-gray-950 border border-t-0 border-gray-700 p-2 max-h-48 overflow-y-auto">
                      <div className="space-y-0.5">
                        {previewWaypoints.map((wp, i) => (
                          <WaypointLine key={i} wp={wp} index={i} />
                        ))}
                      </div>
                    </div>
                  )}
                  {/* Actions map preview */}
                  {mapPreviewName === r.name && mapPreviewText && (
                    <div className="rounded-b-md bg-gray-950 border border-t-0 border-gray-700 p-2 max-h-48 overflow-y-auto">
                      <pre className="font-mono text-xs whitespace-pre-wrap">
                        {mapPreviewText.split("\n").map((line, li) => (
                          <div key={li} className={line.includes("[exact]") ? "text-yellow-300" : "text-purple-300"}>
                            {line}
                          </div>
                        ))}
                      </pre>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Recording Controls */}
        {!recording.active ? (
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={recName}
              onChange={(e) => setRecName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleStartRecording()}
              placeholder="Recording name..."
              className="flex-1 rounded-md bg-gray-900 border border-gray-600 px-3 py-1.5 text-sm text-gray-200 placeholder-gray-500 focus:border-red-500 focus:outline-none"
            />
            <button
              onClick={handleStartRecording}
              disabled={!recName.trim() || playback.active}
              className="rounded-md bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-500 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Record
            </button>
          </div>
        ) : (
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <span className="h-2.5 w-2.5 rounded-full bg-red-500 animate-pulse" />
              <span className="text-sm text-red-300">
                Recording &quot;{recording.name}&quot;
              </span>
              <span className="text-xs text-gray-500">
                ({recording.waypoint_count} waypoints)
              </span>
            </div>
            <div className="flex gap-2">
              <button
                onClick={handleStopRecording}
                className="rounded-md bg-gray-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-500 transition-colors"
              >
                Stop &amp; Save
              </button>
              <button
                onClick={handleDiscard}
                className="rounded-md border border-gray-600 px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-700 transition-colors"
              >
                Discard
              </button>
            </div>
          </div>
        )}

        {/* Loop checkbox */}
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={loopEnabled}
            onChange={(e) => setLoopEnabled(e.target.checked)}
            className="rounded border-gray-600 bg-gray-900 text-emerald-500 focus:ring-emerald-500"
          />
          <span className="text-xs text-gray-400">Loop playback</span>
        </label>

        {/* Live Preview (recording) */}
        {recording.active && recording.waypoints.length > 0 && (
          <div>
            <h3 className="text-xs font-medium text-gray-400 mb-1">
              Live Preview
            </h3>
            <div className="rounded-md bg-gray-900 border border-gray-700 p-2 max-h-40 overflow-y-auto">
              <div className="space-y-0.5">
                {recording.waypoints.map((wp, i) => (
                  <WaypointLine
                    key={i}
                    wp={wp}
                    index={recording.waypoint_count - recording.waypoints.length + i}
                  />
                ))}
              </div>
            </div>
          </div>
        )}

        {/* Playback Status */}
        {playback.active && (
          <div className="space-y-2">
            <div className="flex items-center justify-between rounded-md bg-emerald-950/30 border border-emerald-800 px-3 py-2">
              <div className="flex items-center gap-2">
                <span className="h-2.5 w-2.5 rounded-full bg-emerald-400 animate-pulse" />
                <span className="text-sm text-emerald-300">
                  Playing &quot;{playback.recording_name}&quot;
                </span>
                <span className="text-xs text-gray-400 tabular-nums">
                  [{playback.index + 1}/{playback.total}]
                </span>
                {playback.loop && (
                  <span className="text-xs text-emerald-500">Loop: ON</span>
                )}
              </div>
              <button
                onClick={handleStopPlayback}
                className="rounded-md bg-gray-600 px-2 py-1 text-xs text-white hover:bg-gray-500 transition-colors"
              >
                Stop
              </button>
            </div>
            {/* Live Minimap */}
            {playback.minimap && (
              <Minimap
                minimaps={playback.minimap}
                nodeIndex={playback.index}
                total={playback.total}
              />
            )}
            {hasPlaybackLogs && (
              <div ref={logsContainerRef} className="rounded-md bg-gray-900 border border-gray-700 p-2 overflow-y-auto" style={{ resize: "vertical", minHeight: "4rem", height: "10rem", maxHeight: "80vh" }}>
                <div className="space-y-0.5 font-mono text-xs text-gray-300">
                  {playback.logs.map((line, i) => (
                    <div key={i} className={`whitespace-pre-wrap ${line?.includes("[SUCCESS]") ? "text-emerald-400" : line?.includes("[FAILURE]") ? "text-red-400" : ""}`}>{line || "\u00A0"}</div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Playback Logs (when not actively playing) */}
        {!playback.active && hasPlaybackLogs && (
          <div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setShowPlaybackLogs(!showPlaybackLogs)}
                className={`rounded px-2 py-1 text-xs transition-colors ${
                  showPlaybackLogs
                    ? "text-blue-300 bg-blue-900/30"
                    : "text-gray-300 hover:bg-gray-700"
                }`}
              >
                Last Playback Logs
              </button>
            </div>
            {showPlaybackLogs && (
              <div ref={lastLogsContainerRef} className="mt-2 rounded-md bg-gray-900 border border-gray-700 p-2 overflow-y-auto" style={{ resize: "vertical", minHeight: "4rem", height: "12rem", maxHeight: "80vh" }}>
                <div className="space-y-0.5 font-mono text-xs text-gray-300">
                  {playback.logs.map((line, i) => (
                    <div key={i} className={`whitespace-pre-wrap ${line?.includes("[SUCCESS]") ? "text-emerald-400" : line?.includes("[FAILURE]") ? "text-red-400" : ""}`}>{line || "\u00A0"}</div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

      </div>
    </div>
  );
}

export default function Dashboard() {
  const { state, mcpConnected, refresh } = useBot();

  return (
    <div className="mx-auto max-w-2xl px-6 py-8">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <h1 className="text-xl font-bold tracking-tight">DBVictory Bot</h1>
        <div className="flex items-center gap-2">
          <StatusBadge label="MCP" connected={mcpConnected} />
          <StatusBadge label="Game Login" connected={state?.connected ?? false} />
          <StatusBadge label="DLL Injected" connected={state?.dll_injected ?? false} />
          <StatusBadge label="DLL Bridge" connected={state?.dll_bridge_connected ?? false} />
        </div>
      </div>

      {/* Packet Stats */}
      {state && (
        <div className="grid grid-cols-2 gap-4 mb-4">
          <div className={`rounded-lg border p-3 ${
            (state.packets_from_server ?? 0) > 0
              ? "border-emerald-700 bg-emerald-950/30"
              : "border-red-700 bg-red-950/30"
          }`}>
            <p className="text-xs text-gray-400 uppercase tracking-wider">
              Server Packets
            </p>
            <p className={`text-xl font-bold mt-1 tabular-nums ${
              (state.packets_from_server ?? 0) > 0 ? "text-emerald-300" : "text-red-400"
            }`}>
              {(state.packets_from_server ?? 0).toLocaleString()}
            </p>
          </div>
          <div className={`rounded-lg border p-3 ${
            (state.packets_from_client ?? 0) > 0
              ? "border-emerald-700 bg-emerald-950/30"
              : "border-red-700 bg-red-950/30"
          }`}>
            <p className="text-xs text-gray-400 uppercase tracking-wider">
              Client Packets
            </p>
            <p className={`text-xl font-bold mt-1 tabular-nums ${
              (state.packets_from_client ?? 0) > 0 ? "text-emerald-300" : "text-red-400"
            }`}>
              {(state.packets_from_client ?? 0).toLocaleString()}
            </p>
          </div>
        </div>
      )}

      {/* Proxy Connection Sequence */}
      {state?.proxy_sequence && (
        <ConnectionSequence seq={state.proxy_sequence} />
      )}

      {/* Player Stats */}
      {state?.connected && state.player && (
        <PlayerStats player={state.player} />
      )}

      {/* Creatures */}
      {state?.connected && (
        <CreatureList creatures={state.creatures ?? []} />
      )}

      {/* Cavebot */}
      {state?.connected && state.cavebot && (
        <CavebotPanel cavebot={state.cavebot} onRefresh={refresh} />
      )}

      {/* Actions */}
      <div>
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">
          Actions
        </h2>
        {state && state.actions.length > 0 ? (
          <div className="space-y-3">
            {state.actions.map((a) => (
              <ActionCard key={a.name} action={a} onRefresh={refresh} />
            ))}
          </div>
        ) : state ? (
          <p className="text-sm text-gray-500">
            No actions found. Add .py files to the actions/ folder.
          </p>
        ) : null}
      </div>
    </div>
  );
}
