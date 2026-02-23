# DBVictory Bot

A Tibia OTS bot powered by Claude Code via MCP (Model Context Protocol). Controls a game character through a MITM proxy, injected DLL, and automated action scripts.

## Dashboard

![Dashboard](dashboard.png)

The Electron dashboard provides real-time monitoring and control:

- **Player Stats** — HP, Mana, Level, Position
- **Creatures** — Nearby creatures with health bars
- **Cavebot** — Record and replay navigation paths with a live minimap
- **Actions** — Toggle automated scripts (healing, haste, auto-targeting, food, runes)
- **Packet Counters** — Server/client packet throughput

## Architecture

- **MCP Server** (`mcp_server.py`) — Exposes bot tools to Claude Code via stdio JSON-RPC
- **MITM Proxy** (`proxy.py`) — Intercepts client-server traffic for packet parsing and injection
- **DLL Bridge** (`dll/dbvbot.cpp`) — Injected DLL for creature scanning, in-game targeting, and memory operations
- **Dashboard** (`dashboard/`) — Next.js + Electron app with WebSocket real-time state push
- **Actions** (`actions/`) — Modular async scripts (eat_food, speed_up, power_up, auto_targeting, auto_rune, full_light, cavebot)
- **Cavebot** (`cavebot.py`) — Records player navigation, replays via actions map with minimap visualization

## Cavebot TODO

The cavebot is a first working version but not yet fully functional. Known issues and planned features:

### Bugs
1. **Stairs recording is incorrect** — floor transitions via stairs/ramps are not always captured properly in recordings, causing playback to fail at floor changes

### Planned Features
2. **Monster combat strategies** — move-and-attack behavior that keeps 1 square distance from the monster while fighting
3. **Real item usage support** — shovel, rope, and other usable items during navigation
4. **Terrain testing** — verify and fix playback on doors, ladders, and other interactive map objects

### Future
- **Anti-bot / Intelligent responder** — automated system to detect and respond to anti-bot checks or GM interactions
