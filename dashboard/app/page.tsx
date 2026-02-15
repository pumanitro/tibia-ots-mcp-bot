"use client";

import { useBot } from "@/lib/useBot";
import { toggleAction, restartAction } from "@/lib/api";
import type { ActionInfo } from "@/lib/api";

function StatusBadge({ connected }: { connected: boolean }) {
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
      {connected ? "Connected" : "Disconnected"}
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

export default function Dashboard() {
  const { state, error, refresh } = useBot();

  return (
    <div className="mx-auto max-w-2xl px-6 py-8">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <h1 className="text-xl font-bold tracking-tight">DBVictory Bot</h1>
        {state ? (
          <StatusBadge connected={state.connected} />
        ) : error ? (
          <span className="text-xs text-red-400">{error}</span>
        ) : (
          <span className="text-xs text-gray-500">Loading...</span>
        )}
      </div>

      {/* Stats */}
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
