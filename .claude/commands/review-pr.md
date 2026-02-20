# Code Review Skill

You are performing a comprehensive code review for a Pull Request in the `pumanitro/tibia-ots-mcp-bot` repository.

## Input

The user provides: `$ARGUMENTS`

If `$ARGUMENTS` is a PR number (e.g., `5`), use it directly.
If `$ARGUMENTS` is a PR URL, extract the PR number from it.
If `$ARGUMENTS` is empty, run `gh pr view --json number -q .number` to get the current branch's PR number.

## Step 1: Gather PR Context

Run these commands to understand the PR:

```bash
# Get PR metadata
gh pr view <PR_NUMBER> --json title,body,baseRefName,headRefName,files,additions,deletions

# Get the full diff
gh pr diff <PR_NUMBER>

# Get list of changed files
gh pr diff <PR_NUMBER> --name-only
```

Read the PR description carefully — it contains intent and scope.

## Step 2: Parallel Review Agents

Spawn **all of the following agents in parallel** using the Task tool. Each agent gets a focused review area and the full diff context. Pass the PR number and the list of changed files to each agent.

IMPORTANT: Launch ALL agents in a single message to maximize parallelism.

### Agent 1: "Python Code Quality & Bot Conventions"

**Prompt template:**

> You are reviewing PR #<NUMBER> in pumanitro/tibia-ots-mcp-bot. Run `gh pr diff <NUMBER>` to get the full diff.
>
> **FIRST**, read the project memory for conventions:
> - Read `C:\Users\patry\.claude\projects\C--Users-patry-Desktop-dbv-exp\memory\MEMORY.md` — key architectural patterns
> - Read `C:\Users\patry\.claude\projects\C--Users-patry-Desktop-dbv-exp\memory\ot_protocol.md` if it exists — OT protocol details
>
> This is a **Tibia OT Server bot** built with Python (asyncio) + C++ (DLL injection) + Next.js/Electron (dashboard). It uses an MCP server to expose bot actions as tools for Claude Code.
>
> Review ONLY for code quality and project conventions. Check every changed file against these rules:
>
> **Project Architecture:**
> - `mcp_server.py` — MCP server entry point, exposes bot tools via FastMCP
> - `proxy.py` — TCP proxy between game client and OT server (login + game phases)
> - `protocol.py` — OT protocol packet builders, opcodes, PacketReader/PacketWriter
> - `game_state.py` — Parses server packets into live game state (HP, mana, position, creatures)
> - `crypto.py` — RSA/XTEA encryption for OT protocol
> - `dll/dbvbot.cpp` — Injected DLL for in-process creature scanning via named pipe
> - `dll_bridge.py` — Python client for the DLL's named pipe (`\\.\pipe\dbvbot`)
> - `actions/*.py` — Background action scripts with `async def run(bot)` entry point
> - `dashboard/` — Next.js 15 + Electron dashboard (React 19, Tailwind CSS 4)
> - `inject.py` — DLL injection into game process
> - `patcher.py` — Memory patching for game client
>
> **Python Rules:**
> 1. No hardcoded magic numbers — use named constants (item IDs, opcodes, offsets, intervals)
> 2. All logging must go to `stderr` (stdout is reserved for MCP JSON-RPC)
> 3. Actions must follow the pattern: `async def run(bot)` with a `while True` loop and `await bot.sleep()`
> 4. Use `bot.is_connected` guard before sending packets in action loops
> 5. Use `build_*_packet()` helpers from `protocol.py` — never hand-build raw `struct.pack` for standard packets
> 6. Use hotkey-style item use `(0xFFFF, 0, 0)` when the item position doesn't matter
> 7. Access shared state via `sys.modules["__main__"].state` in actions (not imports)
> 8. Don't leave debug files or `print()` statements — use `logging` or `bot.log()`
> 9. Packet parsing must handle truncated/malformed data with try/except
> 10. Always use `struct.pack('<...')` little-endian format for OT protocol
>
> **C++ / DLL Rules:**
> 11. Creature struct offsets must match documented layout (id@+0, name@+4, health@+28, pos@+576)
> 12. Named pipe communication must be newline-delimited JSON
> 13. Build with MinGW 32-bit: `g++ -shared -o dll/dbvbot.dll dll/dbvbot.cpp -lkernel32 -static -s -O2 -std=c++17`
> 14. Old DLLs stay loaded — new builds must use incremented filenames
>
> **Dashboard Rules (Next.js/TypeScript):**
> 15. Use Tailwind CSS classes — no inline styles or hardcoded colors
> 16. Dashboard fetches data from the bot's HTTP API (`dashboard_api.py`)
>
> For each file, list issues found with:
> - File path and line reference
> - Rule violated (reference the rule number)
> - Current code snippet
> - Suggested fix
>
> If a file has no issues, skip it. If you're unsure about something, flag it as a QUESTION.

### Agent 2: "Protocol & Packet Correctness"

**Prompt template:**

> You are reviewing PR #<NUMBER> in pumanitro/tibia-ots-mcp-bot. Run `gh pr diff <NUMBER>` to get the full diff.
>
> **FIRST**, read these reference files for protocol context:
> - Read `C:\Users\patry\Desktop\dbv_exp\protocol.py` — full OT protocol opcodes and packet builders
> - Read `C:\Users\patry\Desktop\dbv_exp\game_state.py` — server packet parsing
> - Read `C:\Users\patry\Desktop\dbv_exp\proxy.py` (first 100 lines) — proxy architecture
> - Read `C:\Users\patry\.claude\projects\C--Users-patry-Desktop-dbv-exp\memory\ot_protocol.md` if it exists
>
> This bot communicates with an Open Tibia server using a custom binary protocol over TCP. The proxy intercepts traffic between the game client and server.
>
> Review ONLY for protocol and packet correctness:
>
> **Packet Structure (CRITICAL):**
> - Are opcodes correct for the intended action? Cross-reference `ClientOpcode` and `ServerOpcode` enums
> - Are packet fields in the correct order and correct size (u8, u16, u32, string)?
> - Is endianness correct? OT protocol is little-endian (`<` in struct.pack)
> - Are string fields properly length-prefixed (u16 length + bytes)?
> - Does `PacketWriter` / `PacketReader` usage match the protocol spec?
>
> **Proxy & Encryption (HIGH):**
> - Are XTEA keys properly extracted and used for encryption/decryption?
> - Is RSA handling correct for login phase?
> - Are packet lengths correct (including the 2-byte length prefix)?
> - Is the adler32 checksum computed correctly?
>
> **Game State Parsing (HIGH):**
> - Does the parser handle all relevant server opcodes?
> - Are creature ID ranges correct? (players: 0x10000000+, monsters: 0x40000000+)
> - Are position fields parsed correctly (x: u16, y: u16, z: u8)?
> - Is the parser robust against truncated packets (try/except with proper logging)?
>
> **DLL Memory Scanning (MEDIUM):**
> - Are struct offsets correct? (id@+0, MSVC string name@+4(24B), health@+28, direction@+32, lookType@+48, NPC pos@+576, player pos@-40)
> - Is memory read safely (VirtualQuery, ReadProcessMemory, or direct access with proper page checks)?
> - Are creature ID validation ranges correct?
>
> For each finding, include:
> - File path and line reference
> - What's wrong (incorrect opcode, wrong field size, endianness issue, etc.)
> - Current code snippet
> - Suggested fix with code example
> - Impact level (CRITICAL/HIGH/MEDIUM/LOW)

### Agent 3: "Bot Logic & Regression Analysis"

**Prompt template:**

> You are reviewing PR #<NUMBER> in pumanitro/tibia-ots-mcp-bot. Run `gh pr diff <NUMBER>` to get the full diff.
> Also run `gh pr view <NUMBER> --json title,body` to understand the PR intent.
>
> This is a Tibia OT Server bot with these core subsystems:
> - **Proxy** — intercepts client↔server TCP traffic, allows packet injection
> - **Game State** — parses server packets into live state (HP, mana, position, creatures, messages)
> - **MCP Server** — exposes bot actions as tools for Claude Code (walk, attack, use item, etc.)
> - **Actions** — background scripts (eat_food, auto_attack, power_up, etc.) that run in loops
> - **DLL Bridge** — injected DLL scans game memory for creature data, sends via named pipe
> - **Dashboard** — Electron/Next.js UI showing game state
>
> **Review the PR for bot logic correctness and regression risks:**
>
> 1. **Data Flow Analysis**: Trace how data flows through:
>    - Game server → proxy → game_state parser → MCP resource/dashboard API
>    - MCP tool call → protocol packet builder → proxy → game server
>    - DLL memory scan → named pipe → dll_bridge → game_state.creatures
>
> 2. **Regression Risk Assessment**: Does this PR change behavior that could break:
>    - Login/connection flow (RSA exchange, XTEA key extraction, character list patching)?
>    - Packet injection (are packets still properly encrypted before forwarding)?
>    - Game state tracking (HP, position, creatures being lost or stale)?
>    - Action system (do existing actions still load, run, and stop correctly)?
>    - MCP tool interface (are tool signatures or return values changed)?
>    - DLL bridge communication (pipe protocol, JSON format)?
>    - Dashboard data flow (API endpoints, WebSocket updates)?
>
> 3. **Action Script Impact**: If actions are modified:
>    - Does the action still properly check `bot.is_connected`?
>    - Is the sleep interval reasonable (not too fast = spam, not too slow = unresponsive)?
>    - Could it interfere with other running actions (e.g., attacking while trying to eat)?
>
> 4. **State Consistency**: Could changes cause:
>    - Stale creature data (creatures not pruned after leaving screen)?
>    - Position desync (player position not updated after movement)?
>    - Race conditions between proxy callbacks and MCP tool handlers?
>
> **Output format:**
> - List each regression risk with severity (CRITICAL/HIGH/MEDIUM/LOW)
> - For each risk, explain: what could break, what subsystem is affected, and how to verify it works
> - If NO regressions found, explicitly state: "No regression risks identified."
> - Flag any ambiguous changes as QUESTIONS for the author

### Agent 4: "Security & Memory Safety"

**Prompt template:**

> You are reviewing PR #<NUMBER> in pumanitro/tibia-ots-mcp-bot. Run `gh pr diff <NUMBER>` to get the full diff.
>
> This is a game bot that:
> - Runs a local TCP proxy intercepting game traffic
> - Injects a DLL into the game client process
> - Reads/writes game process memory
> - Exposes MCP tools over stdio
> - Has a local dashboard on port 4747
>
> Focus ONLY on security vulnerabilities and memory safety issues:
>
> **1. Credential & Key Exposure (CRITICAL):**
> - Are game account credentials (username, password) logged or stored in plaintext?
> - Are XTEA encryption keys exposed in logs, debug files, or API responses?
> - Is the RSA private key properly scoped and not leaked?
> - Are server IPs or connection details hardcoded that should be configurable?
>
> **2. Buffer Overflows & Memory Safety — C++ DLL (CRITICAL):**
> - Are buffer sizes checked before reads/writes?
> - Can creature name strings overflow `MAX_NAME_LEN` (63 chars)?
> - Are memory addresses validated before dereferencing?
> - Is `VirtualQuery` result checked before reading memory pages?
> - Are pipe buffer sizes sufficient for the data being sent?
>
> **3. Injection & Code Execution (HIGH):**
> - Can action script filenames be manipulated to load arbitrary Python files?
> - Is `importlib` usage safe against path traversal in action names?
> - Are packet payloads validated before being processed?
> - Could a malicious game server send crafted packets that crash the parser?
>
> **4. Network Security (HIGH):**
> - Is the dashboard API (`dashboard_api.py`) bound only to localhost?
> - Are there any endpoints that accept external connections?
> - Is the MCP stdio transport properly isolated?
> - Could a MITM between proxy and game server inject packets?
>
> **5. Resource Exhaustion (MEDIUM):**
> - Can the creature dict grow unboundedly if creatures are never pruned?
> - Are there unbounded loops without sleep in action scripts?
> - Is the message ring buffer properly bounded (deque maxlen)?
> - Can the named pipe buffer overflow with too many creatures?
>
> **6. Debug Artifacts (LOW):**
> - Are debug log files (`*_debug.txt`) being written with sensitive data?
> - Are `print()` statements leaking to stdout (breaking MCP JSON-RPC)?
> - Are packet dumps (`*.bin`) being left on disk?
>
> **Output format:**
> - Categorize findings as: CRITICAL / HIGH / MEDIUM / LOW
> - For each finding include:
>   - Category (credential exposure, buffer overflow, injection, etc.)
>   - File path and line reference
>   - Attack/failure scenario
>   - Impact
>   - Current code snippet
>   - Suggested fix
> - If NO vulnerabilities found, explicitly state: "No security vulnerabilities identified in changed code."
> - Flag uncertain findings as QUESTION

## Step 3: Compile Results

After all agents complete, compile their findings into a single structured report:

```
## Code Review: PR #<NUMBER> — <TITLE>

### Summary
<1-2 sentence overview of the PR and overall review quality>

---

### 1. Python Code Quality & Bot Conventions
<Agent 1 findings, organized by file>

### 2. Protocol & Packet Correctness
<Agent 2 findings, grouped by impact level>

### 3. Bot Logic & Regression Analysis
<Agent 3 findings>

**Regression Assessment:**
- [ ] No regressions found
OR
- [ ] Potential regressions identified (see details above)

### 4. Security & Memory Safety
<Agent 4 findings>

---

### Questions for the Author
<Collect all QUESTION items from all agents into one list>

---

### Statistics
- Files reviewed: <count>
- Issues found: <count by severity>
- CRITICAL: <n> | HIGH: <n> | MEDIUM: <n> | LOW: <n>
```

## Step 4: User Confirmation (MANDATORY)

**IMPORTANT: Do NOT post anything to GitHub until the user explicitly confirms in the chat. Present the full report first, then ask and WAIT for an affirmative response.**

After presenting the full report, ask the user:

> **Would you like me to add these review comments to the GitHub PR?**
>
> Options:
> 1. Yes, post all findings as inline PR comments
> 2. Yes, but only CRITICAL and HIGH severity
> 3. No, I'll handle it manually
> 4. Let me pick which ones to post

**Wait for the user to respond with a clear confirmation (e.g., "yes", "1", "post them") before proceeding. Do NOT assume confirmation.**

## Step 5: Post as Per-File Inline Line Comments

**CRITICAL: Post inline line comments on specific files — NOT a single consolidated review comment.**

For each finding that has a file path and line reference, post an individual inline comment using:

```bash
gh api repos/pumanitro/tibia-ots-mcp-bot/pulls/<PR_NUMBER>/comments \
  -f body="<comment body>" \
  -f path="<file path>" \
  -f commit_id="$(gh pr view <PR_NUMBER> --json headRefOid -q .headRefOid)" \
  -f subject_type="line" \
  -F line=<line number> \
  -f side="RIGHT"
```

**Rules for posting:**
- Each comment should be on the specific file and line where the issue was found
- Use markdown formatting in the comment body: bold the severity, include rule reference, show code snippet and suggested fix
- Group multiple issues on the same line into a single comment for that line
- Only comment on lines that are part of the PR diff (changed lines). If a finding references a line not in the diff, post it as a general PR comment instead using `gh pr comment <NUMBER> --body "..."`
- After posting all comments, confirm to the user how many comments were posted and on which files

**Comment body format:**
```markdown
**[SEVERITY]** Rule: <rule name or number>

<Description of the issue>

```suggestion
<suggested fix code>
```
```
