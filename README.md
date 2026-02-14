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
pip install pymem mcp
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

`actions/eat_food.py` — eats a red ham from the backpack every 10 seconds:

```python
"""Eat food from backpack every 10 seconds."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import build_use_item_packet


async def run(bot):
    ITEM_ID = 3583        # red ham on DBVictory
    CONTAINER = 65        # container ID (from packet sniffing)
    SLOT = 1              # slot in that container
    INTERVAL = 10         # seconds between eats

    while True:
        if bot.is_connected:
            pkt = build_use_item_packet(0xFFFF, CONTAINER, SLOT, ITEM_ID, SLOT, 0)
            await bot.inject_to_server(pkt)
            bot.log(f"Ate food (item {ITEM_ID})")
        await bot.sleep(INTERVAL)
```

### Discovering item IDs

Use the built-in `sniff_use_item` action to capture item IDs from the game client:

1. Enable the sniffer: `toggle_action("sniff_use_item")`
2. Right-click/use the item in-game
3. Read `sniff_log.txt` to see the captured `item_id`, `container`, and `slot`
4. Disable the sniffer when done

### MCP tools for actions

| Tool | Purpose |
|---|---|
| `list_actions` | Show all `.py` files with ON/OFF/running status and source preview |
| `toggle_action(name)` | Enable/disable an action + start/stop its background task |
| `remove_action(name)` | Delete an action script and its settings |
| `restart_action(name)` | Stop, reload `.py` from disk, and restart |
