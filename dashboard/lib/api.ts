const API_BASE = "http://127.0.0.1:8089";

export interface ActionInfo {
  name: string;
  enabled: boolean;
  running: boolean;
  description: string;
}

export interface BotState {
  connected: boolean;
  actions: ActionInfo[];
  packets_from_server: number;
  packets_from_client: number;
}

export async function fetchState(): Promise<BotState> {
  const res = await fetch(`${API_BASE}/api/state`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function toggleAction(
  name: string,
  enabled: boolean
): Promise<void> {
  await fetch(`${API_BASE}/api/actions/${name}/toggle`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

export async function restartAction(name: string): Promise<void> {
  await fetch(`${API_BASE}/api/actions/${name}/restart`, {
    method: "POST",
  });
}
