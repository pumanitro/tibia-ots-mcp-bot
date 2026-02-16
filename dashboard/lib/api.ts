const API_BASE = "http://127.0.0.1:8089";

export interface ActionInfo {
  name: string;
  enabled: boolean;
  running: boolean;
  description: string;
}

export interface PlayerInfo {
  hp: number;
  max_hp: number;
  mana: number;
  max_mana: number;
  level: number;
  experience: number;
  position: [number, number, number];
  magic_level: number;
  soul: number;
}

export interface CreatureInfo {
  id: number;
  health: number;
}

export interface BotState {
  connected: boolean;
  actions: ActionInfo[];
  packets_from_server: number;
  packets_from_client: number;
  player: PlayerInfo;
  creatures: CreatureInfo[];
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
  const res = await fetch(`${API_BASE}/api/actions/${name}/toggle`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    console.error("Toggle failed:", res.status, err);
  }
}

export async function restartAction(name: string): Promise<void> {
  await fetch(`${API_BASE}/api/actions/${name}/restart`, {
    method: "POST",
  });
}
