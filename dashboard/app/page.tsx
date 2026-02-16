"use client";

import { useBot } from "@/lib/useBot";
import { toggleAction, restartAction } from "@/lib/api";
import type { ActionInfo, PlayerInfo, CreatureInfo } from "@/lib/api";

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
  const handleToggle = async (enabled: boolean) => {
    await toggleAction(action.name, enabled);
    onRefresh();
  };

  const handleRestart = async () => {
    await restartAction(action.name);
    onRefresh();
  };

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
          <button
            onClick={handleRestart}
            disabled={!action.enabled}
            className="rounded px-2 py-1 text-xs text-gray-300 hover:bg-gray-700 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            title="Restart action"
          >
            Restart
          </button>
          <Toggle enabled={action.enabled} onChange={handleToggle} />
        </div>
      </div>
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
              <span className="text-xs text-gray-400 tabular-nums w-24 shrink-0">
                #{c.id}
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

export default function Dashboard() {
  const { state, mcpConnected, refresh } = useBot();

  return (
    <div className="mx-auto max-w-2xl px-6 py-8">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <h1 className="text-xl font-bold tracking-tight">DBVictory Bot</h1>
        <div className="flex items-center gap-2">
          <StatusBadge label="MCP" connected={mcpConnected} />
          <StatusBadge label="Game" connected={state?.connected ?? false} />
        </div>
      </div>

      {/* Player Stats */}
      {state?.connected && state.player && (
        <PlayerStats player={state.player} />
      )}

      {/* Creatures */}
      {state?.connected && (
        <CreatureList creatures={state.creatures ?? []} />
      )}

      {/* Packet Stats */}
      {state && (
        <div className="grid grid-cols-2 gap-4 mb-8">
          <div className="rounded-lg border border-gray-700 bg-gray-800 p-4">
            <p className="text-xs text-gray-400 uppercase tracking-wider">
              Server Packets
            </p>
            <p className="text-2xl font-bold mt-1 tabular-nums">
              {state.packets_from_server.toLocaleString()}
            </p>
          </div>
          <div className="rounded-lg border border-gray-700 bg-gray-800 p-4">
            <p className="text-xs text-gray-400 uppercase tracking-wider">
              Client Packets
            </p>
            <p className="text-2xl font-bold mt-1 tabular-nums">
              {state.packets_from_client.toLocaleString()}
            </p>
          </div>
        </div>
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
