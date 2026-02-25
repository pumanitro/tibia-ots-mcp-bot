const API_BASE = "http://127.0.0.1:8089";

export interface ActionInfo {
  name: string;
  enabled: boolean;
  running: boolean;
  completed?: boolean;
  description: string;
  logs?: string[];
  config?: Record<string, any>;
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
  name: string;
  x: number;
  y: number;
  z: number;
}

export interface WaypointInfo {
  type: string;
  direction?: string;
  x?: number;
  y?: number;
  z?: number;
  item_id?: number;
  stack_pos?: number;
  index?: number;
  from_x?: number;
  from_y?: number;
  from_z?: number;
  to_x?: number;
  to_y?: number;
  to_z?: number;
  to_stack_pos?: number;
  label?: string;
  pos: number[];
  player_pos?: number[];
  t: number;
}

export interface RecordingInfo {
  name: string;
  count: number;
  created_at: string;
}

export interface MinimapNodeInfo {
  index: number;
  type: string;
  target: [number, number, number];
  visited: boolean;
}

export interface MinimapData {
  grid: string[];
  width: number;
  height: number;
  origin: [number, number];
  floor: number;
  floors: number[];
  nodes: MinimapNodeInfo[];
  player_node_index: number;
}

export interface MinimapSequence {
  seq_index: number;
  floor: number;
  start: number;
  end: number;
  minimap: MinimapData;
}

export interface ActionsMapNode {
  type: string;
  target: [number, number, number];
  item_id?: number;
  stack_pos?: number;
  index?: number;
  label?: string;
  x?: number;
  y?: number;
  z?: number;
}

export interface ActionsMapResponse {
  name: string;
  actions_map: ActionsMapNode[];
  text_preview: string;
  node_count: number;
}

export interface CavebotState {
  recording: {
    active: boolean;
    name: string;
    waypoint_count: number;
    waypoints: WaypointInfo[];
  };
  playback: {
    active: boolean;
    recording_name: string;
    index: number;
    total: number;
    loop: boolean;
    logs: string[];
    minimap: MinimapSequence[] | Record<string, MinimapData> | null;
    actions_map_count: number;
  };
  recordings: RecordingInfo[];
}

export interface ProxySequence {
  proxy_created: boolean;
  listening: boolean;
  client_connected: boolean;
  server_connected: boolean;
  xtea_captured: boolean;
  logged_in: boolean;
  packets_flowing: boolean;
  timestamps?: Record<string, number | null>;
}

export interface BotState {
  connected: boolean;
  actions: ActionInfo[];
  packets_from_server: number;
  packets_from_client: number;
  player: PlayerInfo;
  creatures: CreatureInfo[];
  dll_injected: boolean;
  dll_bridge_connected: boolean;
  cavebot?: CavebotState;
  proxy_sequence?: ProxySequence;
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


export async function deleteAction(name: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/actions/${name}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    console.error("Delete failed:", res.status, err);
  }
}

export async function updateActionConfig(
  name: string,
  config: Record<string, any>
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/actions/${name}/config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    console.error("Config update failed:", res.status, err);
  }
}

// ── Cavebot API ─────────────────────────────────────────────────

export async function startRecording(name: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/cavebot/record/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    console.error("Start recording failed:", res.status, err);
  }
}

export async function stopRecording(discard = false): Promise<void> {
  const res = await fetch(`${API_BASE}/api/cavebot/record/stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ discard }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    console.error("Stop recording failed:", res.status, err);
  }
}

export async function playRecording(
  name: string,
  loop = false
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/cavebot/play`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, loop }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    console.error("Play recording failed:", res.status, err);
  }
}

export async function stopPlayback(): Promise<void> {
  const res = await fetch(`${API_BASE}/api/cavebot/play/stop`, {
    method: "POST",
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    console.error("Stop playback failed:", res.status, err);
  }
}

export async function clearCavebotLogs(): Promise<void> {
  const res = await fetch(`${API_BASE}/api/cavebot/logs/spacer`, {
    method: "POST",
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    console.error("Spacer failed:", res.status, err);
  }
}

export async function fetchRecording(
  name: string
): Promise<{ name: string; waypoints: WaypointInfo[] } | null> {
  const res = await fetch(`${API_BASE}/api/recordings/${encodeURIComponent(name)}`);
  if (!res.ok) return null;
  return res.json();
}

export async function deleteRecording(name: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/recordings/${name}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    console.error("Delete recording failed:", res.status, err);
  }
}

export async function removeWaypoints(
  name: string,
  indices: number[]
): Promise<{ waypoints: WaypointInfo[] } | null> {
  const res = await fetch(
    `${API_BASE}/api/recordings/${encodeURIComponent(name)}/remove_waypoints`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ indices }),
    }
  );
  if (!res.ok) return null;
  return res.json();
}

export async function fetchActionsMap(
  name: string
): Promise<ActionsMapResponse | null> {
  const res = await fetch(
    `${API_BASE}/api/cavebot/actions_map/${encodeURIComponent(name)}`
  );
  if (!res.ok) return null;
  return res.json();
}
