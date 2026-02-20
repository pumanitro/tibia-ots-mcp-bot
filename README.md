# DBVictory Bot

A man-in-the-middle proxy bot for the DBVictory game, controlled through Claude Code via MCP (Model Context Protocol).

## How It Works

1. The game client (`dbvStart.exe`) normally connects to `87.98.220.215`
2. The bot patches the client's memory to redirect traffic through `127.0.0.1`
3. A local proxy intercepts login/game traffic, extracts XTEA encryption keys, and injects packets on demand
4. Claude Code sends commands to the MCP server, which translates them into game packets

## Project Structure

| File | Description |
|---|---|
| `mcp_server.py` | MCP server exposing bot actions as tools for Claude Code |
| `proxy.py` | TCP proxy handling login and game protocol traffic |
| `protocol.py` | OT protocol packet builders and opcodes |
| `crypto.py` | RSA and XTEA encryption/decryption |
| `patcher.py` | Memory patcher for redirecting client connections |
| `bot.py` | Bot logic and helpers |
| `start.py` | Standalone startup script |
| `xtea_finder.py` | Utility for locating XTEA keys in memory |
| `actions/` | Automated action scripts (Python) |

## Setup

### Requirements

- Python 3.10+
- Windows (required for memory patching)
- DBVictory game client (`dbvStart.exe`)

### Install Dependencies

```bash
pip install pymem mcp websockets
```

## Usage

> **Important:** Claude Code must be running as Administrator for memory patching to work.

Follow these steps in order:

### Step 1: Start Claude Code as Administrator

Right-click your terminal and select "Run as Administrator", then launch Claude Code from the project directory. The MCP server (`.mcp.json`) starts automatically as a subprocess — it inherits admin privileges from the terminal.

### Step 2: Launch the game client

Open `dbvStart.exe` and wait at the **login screen**. Do **not** log in yet — the bot needs to patch the client's memory before you authenticate.

### Step 3: Ask Claude to `start_bot`

This patches the client's memory (redirects server IP to `127.0.0.1`) and starts the local login + game proxies. Claude Code will be unresponsive for up to 2 minutes while it waits for you to log in.

### Step 4: Log in through the game client

Enter your credentials in the game client as normal. The proxy intercepts the connection, extracts encryption keys, and establishes the session. Once login succeeds, Claude returns control.

### Step 5: Control your character

You can now give Claude natural language commands like "walk north 5 steps", "say hello", or "attack creature 12345".

## Available Commands

### Movement
- **walk** — Move in a direction (n/s/e/w/ne/se/sw/nw)
- **autowalk** — Walk continuously in a direction
- **stop** — Stop walking
- **turn** — Face a cardinal direction

### Combat
- **attack** — Attack a creature by ID
- **follow** — Follow a creature by ID
- **set_fight_modes** — Set offensive/balanced/defensive, chase/stand, PvP/safe

### Interaction
- **say** — Send a chat message
- **use_item** — Use an item at a map position
- **move_item** — Move an item between positions
- **look_at** — Inspect a tile or item

### Utility
- **get_status** — Check connection state and packet stats
- **send_ping** — Ping the game server
- **logout** — Disconnect from the server

## Automated Actions

The bot supports automated background actions — small Python scripts that run continuously while the bot is connected. Each action is a `.py` file in the `actions/` folder with an `async def run(bot)` entry point.

### How it works

1. **Create** an action by writing a `.py` file to `actions/` (Claude can do this for you via natural language)
2. **Enable** it with `toggle_action` — it starts running as a background task
3. **Disable** it with `toggle_action` — it stops immediately
4. **Edit** the `.py` file and call `restart_action` to reload changes at runtime
5. Enabled actions **auto-start** when `start_bot` connects to the game

Settings (which actions are enabled) persist in `bot_settings.json` across sessions.

### Action API

Each action receives a `bot` object with:

```python
bot.use_item_in_container(item_id, container, slot)  # use item from backpack
bot.use_item_on_map(x, y, z, item_id, stack_pos)     # use item on ground
bot.say(text)                                          # send chat message
bot.walk(direction, steps, delay)                      # walk in a direction
bot.inject_to_server(packet)                           # send raw packet
bot.sleep(seconds)                                     # async sleep
bot.is_connected                                       # connection status
bot.log(msg)                                           # log to stderr
```

### Example: Auto-eat food

`actions/eat_food.py` — uses a hotkey-style packet so the server finds the food in any backpack/slot:

```python
"""Eat food every 10 seconds. Uses hotkey-style packet — works from any backpack/slot."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import build_use_item_packet

FOOD_ID = 3583    # red ham on DBVictory
INTERVAL = 10

async def run(bot):
    while True:
        if bot.is_connected:
            # Hotkey-style: pos=(0xFFFF, 0, 0) — server finds the item automatically
            pkt = build_use_item_packet(0xFFFF, 0, 0, FOOD_ID, 0, 0)
            await bot.inject_to_server(pkt)
        await bot.sleep(INTERVAL)
```

### Discovering item IDs

Use the built-in `packet_sniffer` action to capture packets from the game client:

1. Enable the sniffer: `toggle_action("packet_sniffer")`
2. Right-click/use the item in-game (or press a hotkey)
3. Read `sniff_log.txt` to see captured opcodes, positions, item IDs
4. Disable the sniffer when done

### MCP tools for actions

| Tool | Purpose |
|---|---|
| `list_actions` | Show all `.py` files with ON/OFF/running status and source preview |
| `toggle_action(name)` | Enable/disable an action + start/stop its background task |
| `remove_action(name)` | Delete an action script and its settings |
| `restart_action(name)` | Stop, reload `.py` from disk, and restart |

## DLL Injection — Live Battle List

The bot can inject a DLL into the game client to read the battle list (creature data) directly from process memory. This gives accurate, real-time creature tracking that matches the in-game battle list exactly.

### Architecture

```
dbvStart.exe (with injected DLL)  <-->  Real Game Server
         |
    DLL scans process memory
    for creature structs
         |
    Named Pipe (\\.\pipe\dbvbot)
         |
    Python dll_bridge.py
         |
    game_state.creatures  -->  Dashboard UI
```

The proxy remains for sending commands (walk, attack, say). The DLL supplements it with authoritative creature reading.

### Build Prerequisites

**MinGW-w64 (32-bit)** is required to compile the DLL:

```bash
winget install -e --id MingW-w64.MinGW-w64
```

Or download the `i686-*-posix-dwarf` build from [mingw-builds-binaries](https://github.com/niXman/mingw-builds-binaries/releases), extract to `C:\mingw32\`, and add `C:\mingw32\bin` to PATH.

Verify with `g++ --version` — must be i686 (32-bit) since dbvStart.exe is 32-bit.

### Build

```bash
cd dll && make
```

This produces `dll/dbvbot.dll` (~20-50KB).

### Usage

1. Start the game client and connect via `start_bot` as usual
2. Enable the DLL bridge action: `toggle_action("dll_bridge")`
3. The action will automatically:
   - Inject `dll/dbvbot.dll` into dbvStart.exe
   - Connect to the named pipe
   - Poll creature data every 100ms
   - Update `game_state.creatures` with authoritative data

Check with `list_actions` — should show "dll_bridge >>> RUNNING".

## Real-Time Dashboard (WebSocket)

The Electron dashboard receives game state updates via WebSocket for low-latency display.

### Architecture

```
DLL (100ms poll) → Python game_state → WebSocket push (100ms) → Electron Dashboard
```

- **WebSocket server** runs on `ws://127.0.0.1:8090` and pushes state to all connected clients every 100ms
- **HTTP API** on `http://127.0.0.1:8089` handles action mutations (toggle, restart, delete)
- The dashboard auto-connects to WebSocket and falls back to HTTP polling if unavailable

### Component Status

`start_bot` and `get_status` report health of all components:

```
[OK] MCP Server: running
[OK] Game Client: dbvStart.exe detected
[OK] Proxy: connected (server=1234 client=567 packets)
[OK] DLL Pipe: dbvbot pipe available
[OK] Dashboard: Electron running on port 4747
[OK] Player: id=0x10000001 pos=(123,456,7) HP=100/100 creatures=3
```

## Roadmap

Planned features, roughly in order of priority:

### Server packet parsing — game state awareness
Parse server-to-client packets to give actions access to real-time game state. The server opcodes are already defined in `protocol.py`. DBV uses a custom format with u32 HP/mana and u64 experience (see `game_state.py`).

- [x] **Player stats** (`PLAYER_STATS` 0xA0) — HP, max HP, mana, max mana, level, XP, cap, magic level, soul
- [x] **Player position** — extracted from `MAP_DESCRIPTION` (0x64) and map slice packets (0x65-0x68)
- [x] **Creatures on screen** — from `CREATURE_MOVE` (0x6D), `CREATURE_HEALTH` (0x8C)
- [x] **Text messages** (`TEXT_MESSAGE` 0xB4) — capture server messages, loot drops, damage

This unlocks the BotContext API: `bot.hp`, `bot.max_hp`, `bot.mana`, `bot.position`, `bot.creatures`, `bot.messages`, etc.

### Auto-healing
- [x] **Power Up** — `actions/power_up.py` says "power up" every 1 second for healing

### Waypoint recording & playback (auto-hunting)
Record player movement and replay it in a loop for AFK experience grinding:

- [ ] **Record waypoints** — capture walk packets + player position into a route file
- [ ] **Playback loop** — walk the recorded route continuously
- [ ] **Auto-attack** — target nearest creature from `bot.creatures` and send `ATTACK` packet
- [ ] **Spell rotation** — cast attack spells on cooldown during combat
- [ ] **Loot pickup** — open corpses and move loot to backpack via `MOVE_THING` (0x78)
- [ ] **Death handling** — detect death, pause actions, resume after respawn
