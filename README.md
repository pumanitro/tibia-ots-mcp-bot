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
