"use client";

import { useBot } from "@/lib/useBot";
import { useState, useEffect, useRef, useCallback } from "react";
import {
  toggleAction,
  deleteAction,
  updateActionConfig,
  startRecording,
  stopRecording,
  playRecording,
  stopPlayback,
  deleteRecording,
  fetchRecording,
  fetchActionsMap,
  removeWaypoints,
} from "@/lib/api";
import type {
  ActionInfo,
  ActionsMapNode,
  PlayerInfo,
  CreatureInfo,
  CavebotState,
  WaypointInfo,
  MinimapData,
  MinimapSequence,
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
          {action.running ? (
            <span className="h-2.5 w-2.5 rounded-full bg-emerald-400 animate-pulse" />
          ) : action.completed && !action.running ? (
            <svg className="h-3.5 w-3.5 text-emerald-400" viewBox="0 0 20 20" fill="currentColor">
              <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
            </svg>
          ) : (
            <span className="h-2.5 w-2.5 rounded-full bg-gray-600" />
          )}
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


function ConnectionSequence({ seq, onClose }: { seq: ProxySequence; onClose: () => void }) {
  const steps = [
    { key: "proxy_created", label: "Proxy Created" },
    { key: "listening", label: "Listening" },
    { key: "client_connected", label: "Client Connected" },
    { key: "server_connected", label: "Server Connected" },
    { key: "xtea_captured", label: "XTEA Captured" },
    { key: "logged_in", label: "Logged In" },
    { key: "packets_flowing", label: "Packets Flowing" },
  ] as const;

  const ts = seq.timestamps ?? {};
  const firstFail = steps.findIndex((s) => !seq[s.key]);

  return (
    <div className="flex items-center gap-1 flex-wrap mb-4">
      {steps.map((s, i) => {
        const ok = seq[s.key];
        const isBlocker = i === firstFail;
        const t = ts[s.key];
        const timeStr = t != null ? `+${t}s` : "";
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
              title={`${s.label}${timeStr ? ` (${timeStr})` : ""}`}
            >
              {s.label}{timeStr && <span className="ml-1 text-[10px] opacity-70">{timeStr}</span>}
            </div>
          </div>
        );
      })}
      <button
        onClick={onClose}
        className="text-xs text-gray-500 hover:text-gray-300 ml-1"
      >
        &times;
      </button>
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
  if (wp.type === "position") {
    const pos = wp.pos;
    return (
      <div className="text-xs text-gray-500 font-mono whitespace-nowrap">
        <span className="text-gray-600 mr-2">{index + 1}.</span>
        Pos: ({pos[0]}, {pos[1]}, {pos[2]})
      </div>
    );
  }

  let desc = "";
  if (wp.type === "walk") {
    const dest = wp.pos;
    const dir = wp.direction ?? "?";
    const cur = wp.player_pos ?? dest;
    desc = `Walk ${dir} | (${cur[0]},${cur[1]},${cur[2]}) \u2192 (${dest[0]},${dest[1]},${dest[2]})`;
  } else if (wp.type === "use_item") {
    const label = wp.label ?? `item ${wp.item_id}`;
    desc = `${label} at (${wp.x}, ${wp.y}, ${wp.z})`;
  } else if (wp.type === "use_item_ex") {
    const label = wp.label ?? `item ${wp.item_id}`;
    desc = `${label} \u2192 (${wp.to_x}, ${wp.to_y}, ${wp.to_z})`;
  } else if (wp.type === "floor_change") {
    const pos = wp.pos;
    const dir = wp.direction ?? "?";
    desc = `floor_change ${dir} \u2192 (${pos[0]},${pos[1]},${pos[2]})`;
    return (
      <div className="text-xs text-yellow-400 font-mono whitespace-nowrap">
        <span className="text-gray-500 mr-2">{index + 1}.</span>
        {desc}
      </div>
    );
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
  minimaps: MinimapSequence[] | Record<string, MinimapData>;
  nodeIndex: number;
  total: number;
}) {
  // Normalize: convert old floor-keyed format to sequence list
  const sequences: MinimapSequence[] = Array.isArray(minimaps)
    ? minimaps
    : Object.entries(minimaps).map(([floor, data], i) => ({
        seq_index: i,
        floor: Number(floor),
        start: 0,
        end: (data.nodes?.length ?? 1) - 1,
        minimap: data,
      }));

  // Find the active sequence (the one containing the current node index)
  const activeSeqIdx = sequences.findIndex(
    (s) => nodeIndex >= s.start && nodeIndex <= s.end + 1
  );
  const fallbackIdx = activeSeqIdx >= 0 ? activeSeqIdx : sequences.length - 1;

  const [viewSeqIdx, setViewSeqIdx] = useState(fallbackIdx);
  const [blink, setBlink] = useState(true);

  // Blink the player @ and > characters
  useEffect(() => {
    const id = setInterval(() => setBlink((b) => !b), 500);
    return () => clearInterval(id);
  }, []);

  // Auto-follow the active sequence as playback progresses
  useEffect(() => {
    setViewSeqIdx(fallbackIdx);
  }, [fallbackIdx]);

  // Clamp viewSeqIdx to valid range
  const safeIdx = Math.max(0, Math.min(viewSeqIdx, sequences.length - 1));
  const currentSeq = sequences[safeIdx];
  const currentMinimap = currentSeq?.minimap;

  if (!currentMinimap) {
    return (
      <div className="rounded-md bg-gray-950 border border-gray-700 p-3">
        <span className="text-xs text-gray-500">No minimap data</span>
      </div>
    );
  }

  const handlePrev = () => {
    if (safeIdx > 0) setViewSeqIdx(safeIdx - 1);
  };
  const handleNext = () => {
    if (safeIdx < sequences.length - 1) setViewSeqIdx(safeIdx + 1);
  };

  const isLiveSeq = safeIdx === fallbackIdx;

  return (
    <div className="rounded-md bg-gray-950 border border-gray-700 p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-medium text-gray-400">Minimap</span>
        <div className="flex items-center gap-2">
          <button
            onClick={handlePrev}
            disabled={safeIdx <= 0}
            className="rounded px-1.5 py-0.5 text-xs text-gray-300 hover:bg-gray-700 disabled:opacity-30 transition-colors"
          >
            &lt;
          </button>
          <span className="text-xs text-gray-400 tabular-nums text-center" style={{ minWidth: "5.5rem" }}>
            Seq {safeIdx + 1}/{sequences.length} (Z={currentSeq.floor})
          </span>
          <button
            onClick={handleNext}
            disabled={safeIdx >= sequences.length - 1}
            className="rounded px-1.5 py-0.5 text-xs text-gray-300 hover:bg-gray-700 disabled:opacity-30 transition-colors"
          >
            &gt;
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
        {!isLiveSeq && (
          <span className="text-xs text-yellow-600">(viewing seq {safeIdx + 1})</span>
        )}
      </div>
    </div>
  );
}

function CavebotPanel({
  cavebot,
  cavebotConfig,
  onRefresh,
}: {
  cavebot: CavebotState;
  cavebotConfig?: Record<string, any>;
  onRefresh: () => void;
}) {
  const [recName, setRecName] = useState("");
  const [loopEnabled, setLoopEnabled] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [previewName, setPreviewName] = useState<string | null>(null);
  const [previewWaypoints, setPreviewWaypoints] = useState<WaypointInfo[]>([]);
  const [mapPreviewName, setMapPreviewName] = useState<string | null>(null);
  const [mapPreviewText, setMapPreviewText] = useState<string>("");
  const [compareName, setCompareName] = useState<string | null>(null);
  const [compareWaypoints, setCompareWaypoints] = useState<WaypointInfo[]>([]);
  const [compareMapNodes, setCompareMapNodes] = useState<ActionsMapNode[]>([]);
  const [showPlaybackLogs, setShowPlaybackLogs] = useState(false);
  const [autoScrollLive, setAutoScrollLive] = useState(true);
  const [autoScrollLast, setAutoScrollLast] = useState(true);
  const logsContainerRef = useRef<HTMLDivElement>(null);
  const lastLogsContainerRef = useRef<HTMLDivElement>(null);

  const { recording, playback, recordings } = cavebot;

  // Scroll handlers: disable autoscroll when user scrolls away from bottom
  const handleLiveScroll = useCallback(() => {
    const el = logsContainerRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    setAutoScrollLive(atBottom);
  }, []);

  const handleLastScroll = useCallback(() => {
    const el = lastLogsContainerRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    setAutoScrollLast(atBottom);
  }, []);

  // Auto-scroll live logs
  useEffect(() => {
    if (!autoScrollLive) return;
    const el = logsContainerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [playback.logs, autoScrollLive]);

  // Auto-scroll last logs
  useEffect(() => {
    if (!autoScrollLast) return;
    const el = lastLogsContainerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [playback.logs, showPlaybackLogs, autoScrollLast]);

  // Reset autoscroll when playback starts or logs section is opened
  useEffect(() => {
    if (playback.active) setAutoScrollLive(true);
  }, [playback.active]);
  useEffect(() => {
    if (showPlaybackLogs) setAutoScrollLast(true);
  }, [showPlaybackLogs]);

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
    if (compareName === name) {
      setCompareName(null);
      setCompareWaypoints([]);
      setCompareMapNodes([]);
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
    setCompareName(null);
    setCompareWaypoints([]);
    setCompareMapNodes([]);
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
    setCompareName(null);
    setCompareWaypoints([]);
    setCompareMapNodes([]);
    const resp = await fetchActionsMap(name);
    if (resp) {
      setMapPreviewName(name);
      setMapPreviewText(resp.text_preview);
    }
  };

  const handleCompare = async (name: string) => {
    if (compareName === name) {
      setCompareName(null);
      setCompareWaypoints([]);
      setCompareMapNodes([]);
      return;
    }
    // Close individual panels
    setPreviewName(null);
    setPreviewWaypoints([]);
    setMapPreviewName(null);
    setMapPreviewText("");
    // Fetch both
    const [rec, mapResp] = await Promise.all([
      fetchRecording(name),
      fetchActionsMap(name),
    ]);
    if (rec && mapResp) {
      setCompareName(name);
      setCompareWaypoints(rec.waypoints ?? []);
      setCompareMapNodes(mapResp.actions_map ?? []);
    }
  };

  const handleRemoveWaypoint = async (name: string, index: number) => {
    const result = await removeWaypoints(name, [index]);
    if (result) {
      setPreviewWaypoints(result.waypoints);
      // If map preview is open for this recording, refresh it
      if (mapPreviewName === name) {
        const resp = await fetchActionsMap(name);
        if (resp) setMapPreviewText(resp.text_preview);
      }
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
                            onClick={() => handleCompare(r.name)}
                            className={`rounded px-2 py-1 text-xs transition-colors ${
                              compareName === r.name
                                ? "text-cyan-300 bg-cyan-900/30"
                                : "text-gray-300 hover:bg-gray-700"
                            }`}
                          >
                            Compare
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
                          <div key={i} className="flex items-start gap-1 group">
                            <button
                              onClick={() => handleRemoveWaypoint(r.name, i)}
                              className="shrink-0 mt-0.5 rounded px-1 text-xs text-red-500 opacity-0 group-hover:opacity-100 hover:bg-red-900/30 transition-all"
                              title="Remove waypoint"
                            >
                              X
                            </button>
                            <div className="flex-1 min-w-0">
                              <WaypointLine wp={wp} index={i} />
                            </div>
                          </div>
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
                  {/* Compare grouped view */}
                  {compareName === r.name && compareWaypoints.length > 0 && compareMapNodes.length > 0 && (() => {
                    // Build segments: interleave unmapped waypoints with mapped groups
                    const segments: { type: "unmapped" | "mapped"; wpStart: number; wpEnd: number; node?: ActionsMapNode; nodeIdx?: number }[] = [];
                    let cursor = 0;
                    for (let ni = 0; ni < compareMapNodes.length; ni++) {
                      const node = compareMapNodes[ni];
                      const [ws, we] = node.wp_range ?? [0, 0];
                      // Unmapped gap before this node
                      if (cursor < ws) {
                        segments.push({ type: "unmapped", wpStart: cursor, wpEnd: ws - 1 });
                      }
                      segments.push({ type: "mapped", wpStart: ws, wpEnd: we, node, nodeIdx: ni });
                      cursor = we + 1;
                    }
                    // Trailing unmapped waypoints
                    if (cursor < compareWaypoints.length) {
                      segments.push({ type: "unmapped", wpStart: cursor, wpEnd: compareWaypoints.length - 1 });
                    }
                    return (
                      <div
                        className="rounded-b-md bg-gray-950 border border-t-0 border-gray-700 overflow-y-auto"
                        style={{ maxHeight: "70vh", resize: "vertical", minHeight: "120px", height: "400px" }}
                      >
                        {/* Header row */}
                        <div className="flex sticky top-0 bg-gray-950 z-10 border-b border-gray-700">
                          <div className="flex-[3] px-2 py-1 text-[10px] text-blue-400 font-semibold uppercase tracking-wider">Recording</div>
                          <div className="flex-[2] px-2 py-1 text-[10px] text-purple-400 font-semibold uppercase tracking-wider border-l border-gray-700">Actions Map</div>
                        </div>
                        {segments.map((seg, si) => {
                          const wps = compareWaypoints.slice(seg.wpStart, seg.wpEnd + 1);
                          if (seg.type === "unmapped") {
                            return (
                              <div key={`u${si}`} className="flex border-b border-gray-800/50 bg-gray-900/10">
                                <div className="flex-[3] px-2 py-0.5 border-r border-gray-700 opacity-60">
                                  {wps.map((wp, wi) => (
                                    <WaypointLine key={seg.wpStart + wi} wp={wp} index={seg.wpStart + wi} />
                                  ))}
                                </div>
                                <div className="flex-[2] px-2 py-0.5" />
                              </div>
                            );
                          }
                          const node = seg.node!;
                          const ni = seg.nodeIdx!;
                          const t = node.target;
                          const pos = `(${t[0]},${t[1]},${t[2]})`;
                          let nodeText = `${ni + 1}. ${node.type} ${pos}`;
                          if (node.type === "use_item" || node.type === "use_item_ex") {
                            const label = node.label || `item ${node.item_id}`;
                            nodeText = `${ni + 1}. ${node.type} ${label} ${pos}`;
                          }
                          const isExact = (node as any).exact;
                          const wpStr = node.wp_range ? ` [wp ${seg.wpStart + 1}-${seg.wpEnd + 1}]` : "";
                          return (
                            <div key={`m${si}`} className={`flex border-b border-gray-800 ${ni % 2 === 0 ? "bg-gray-950" : "bg-gray-900/30"}`}>
                              <div className="flex-[3] px-2 py-1 border-r border-gray-700">
                                {wps.map((wp, wi) => (
                                  <WaypointLine key={seg.wpStart + wi} wp={wp} index={seg.wpStart + wi} />
                                ))}
                              </div>
                              <div className="flex-[2] px-2 py-1 flex items-start">
                                <span className={`font-mono text-xs ${isExact ? "text-yellow-300" : "text-purple-300"}`}>
                                  {nodeText}{isExact ? " [exact]" : ""}{wpStr}
                                </span>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    );
                  })()}
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

        {/* Loop checkbox + Targeting strategy */}
        <div className="flex items-center gap-4">
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={loopEnabled}
              onChange={(e) => setLoopEnabled(e.target.checked)}
              className="rounded border-gray-600 bg-gray-900 text-emerald-500 focus:ring-emerald-500"
            />
            <span className="text-xs text-gray-400">Loop playback</span>
          </label>
          <div className="flex items-center gap-2">
            <span className="text-xs text-gray-400">Targeting:</span>
            <select
              value={cavebotConfig?.targeting_strategy ?? "none"}
              onChange={async (e) => {
                await updateActionConfig("cavebot", {
                  targeting_strategy: e.target.value,
                });
                onRefresh();
              }}
              className="rounded-md bg-gray-900 border border-gray-600 px-2 py-1 text-xs text-gray-200 focus:border-emerald-500 focus:outline-none"
            >
              <option value="none">None</option>
              <option value="pause_on_monster">Pause on Monster</option>
            </select>
          </div>
        </div>

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
              <div>
                <div className="flex items-center justify-end mb-1">
                  <button
                    onClick={() => {
                      setAutoScrollLive(true);
                      const el = logsContainerRef.current;
                      if (el) el.scrollTop = el.scrollHeight;
                    }}
                    className={`rounded px-1.5 py-0.5 text-[10px] transition-colors ${
                      autoScrollLive
                        ? "text-emerald-400 bg-emerald-900/30"
                        : "text-gray-500 hover:text-gray-300 hover:bg-gray-700"
                    }`}
                  >
                    Auto-scroll {autoScrollLive ? "ON" : "OFF"}
                  </button>
                </div>
                <div ref={logsContainerRef} onScroll={handleLiveScroll} className="rounded-md bg-gray-900 border border-gray-700 p-2 overflow-y-auto" style={{ resize: "vertical", minHeight: "4rem", height: "10rem", maxHeight: "80vh" }}>
                  <div className="space-y-0.5 font-mono text-xs text-gray-300">
                    {playback.logs.map((line, i) => (
                      <div key={i} className={`whitespace-pre-wrap ${line?.includes("[SUCCESS]") ? "text-emerald-400" : line?.includes("[FAILURE]") ? "text-red-400" : ""}`}>{line || "\u00A0"}</div>
                    ))}
                  </div>
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
              {showPlaybackLogs && (
                <button
                  onClick={() => {
                    setAutoScrollLast(true);
                    const el = lastLogsContainerRef.current;
                    if (el) el.scrollTop = el.scrollHeight;
                  }}
                  className={`rounded px-1.5 py-0.5 text-[10px] transition-colors ${
                    autoScrollLast
                      ? "text-emerald-400 bg-emerald-900/30"
                      : "text-gray-500 hover:text-gray-300 hover:bg-gray-700"
                  }`}
                >
                  Auto-scroll {autoScrollLast ? "ON" : "OFF"}
                </button>
              )}
            </div>
            {showPlaybackLogs && (
              <div ref={lastLogsContainerRef} onScroll={handleLastScroll} className="mt-2 rounded-md bg-gray-900 border border-gray-700 p-2 overflow-y-auto" style={{ resize: "vertical", minHeight: "4rem", height: "12rem", maxHeight: "80vh" }}>
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
  const [showProxySeq, setShowProxySeq] = useState(false);

  return (
    <div className="mx-auto max-w-2xl px-6 py-8">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-xl font-bold tracking-tight">DBVictory Bot</h1>
        <div className="flex items-center gap-2">
          <StatusBadge label="MCP" connected={mcpConnected} />
          <StatusBadge label="Game Login" connected={state?.connected ?? false} />
          <StatusBadge label="DLL Injected" connected={state?.dll_injected ?? false} />
          <StatusBadge label="DLL Bridge" connected={state?.dll_bridge_connected ?? false} />
          {state?.proxy_sequence && (
            <button
              onClick={() => setShowProxySeq(!showProxySeq)}
              className={`text-xs px-1.5 py-1 rounded transition-colors ${
                showProxySeq ? "text-blue-300 bg-blue-900/30" : "text-gray-500 hover:text-gray-300"
              }`}
              title="Proxy connection details"
            >
              {showProxySeq ? "\u25B2" : "\u25BC"}
            </button>
          )}
        </div>
      </div>

      {/* Proxy Connection Sequence */}
      {showProxySeq && state?.proxy_sequence && (
        <ConnectionSequence seq={state.proxy_sequence} onClose={() => setShowProxySeq(false)} />
      )}

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
        <CavebotPanel
          cavebot={state.cavebot}
          cavebotConfig={state.actions.find((a) => a.name === "cavebot")?.config}
          onRefresh={refresh}
        />
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
