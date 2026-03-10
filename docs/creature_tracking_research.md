# Creature Tracking Research — DBVictory Bot

## Approaches Used by OT Bots

### 1. Packet Parsing (Proxy-based)

Intercept server→client packets via a local proxy and parse creature opcodes.

| Opcode | Name | Purpose |
|--------|------|---------|
| 0x0061 | Unknown creature | First time seeing a creature — includes name, full outfit |
| 0x0062 | Known creature | Creature already seen — ID + outfit refresh only |
| 0x0063 | Creature turn | Direction update |
| 0x008C | Creature health | Health percent update |

**Pros:**
- No memory touching — completely invisible to client-side anti-cheat
- Works with any client version without needing to find memory offsets
- Full control over all game data (not just creatures)

**Cons:**
- Must perfectly match the server's binary protocol format
- One wrong byte offset cascades into broken parsing
- Custom servers (like DBVictory) deviate from standard TFS format

**Used by:** BlackD Proxy, OTClient bot modules (protocol layer), most classic-era bots

---

### 2. External Memory Reading

Use `ReadProcessMemory` (via Python `pymem`, C++ `ctypes`, etc.) to read the client's internal data structures from RAM.

**Classic Tibia Client (7.x–8.6):**
- Battle list is a flat array at a fixed offset: 150 entries × 160 bytes each
- Each entry: `id(4) + name(32) + health(4) + ...` at known offsets
- Trivial to read — just `base + index * stride + field_offset`

**OTClient-based (like dbvStart.exe):**
- Creatures stored as heap-allocated C++ objects in `std::unordered_map<uint32, CreaturePtr>`
- Must follow pointer chains: `base → creatureMap → bucket → node → Creature*`
- Key fields per Creature object: `m_id`, `m_name` (std::string with SSO), `m_healthPercent`, `m_position`, `m_speed`, `m_skull`, `m_shield`
- MSVC `std::string` layout: if len < 16, data is inline (SSO buffer); if len >= 16, first 8 bytes are a heap pointer

**Pros:**
- Battle list is always accurate — it's what the client itself renders
- No protocol parsing needed
- Immune to protocol format changes

**Cons:**
- Offsets change with every client rebuild
- Requires reverse engineering (IDA/Ghidra/x64dbg) per client version
- Can be detected by anti-cheat scanning for external readers

**Used by:** XenoBot (memory), TibiaAuto, most modern external bots

---

### 3. DLL Injection (Internal)

Inject a DLL into the client process, hook internal functions.

**Common hooks:**
- `Creature::onHealthPercent(int)` — called when health changes
- `Map::addThing(ThingPtr, Position)` — called when anything appears on map
- `Game::processCreatureMove()` — creature movement events
- `ProtocolGame::parseCreatureUnknown()` — raw packet parsing inside client

**Pros:**
- Most powerful — full access to all game objects in real-time
- Can intercept function calls with exact parameters
- Can modify game behavior (speed hack, light hack, etc.)

**Cons:**
- Most detectable approach — DLLs show up in module list
- Requires C/C++ development
- Client updates break all hooks
- Highest risk of bans

**Used by:** OTClientV8 bot modules, TibiaNG, WindBot (internal mode)

---

### 4. Pixel/Screen Analysis

Read pixels from the game window to detect creatures by their health bars or sprite patterns.

**Pros:**
- Works with any client, even heavily protected ones
- Completely external — no process interaction

**Cons:**
- Slowest and least reliable
- Cannot get creature names, IDs, or exact health
- Breaks with resolution/UI changes

**Used by:** AutoHotkey-based bots, some accessibility tools

---

### 5. Hybrid Proxy + Memory (Recommended)

Use proxy for most protocol handling, memory reading specifically for creature/battle list data.

**Pros:**
- Best of both worlds
- Proxy handles 95% of game interaction (items, movement, spells, chat)
- Memory reading fills in creature data perfectly without fighting byte formats
- Proxy is undetectable; memory reading has low detection risk

**Cons:**
- More complex setup (two data sources)
- Still needs memory offsets for the creature map

**Used by:** Advanced private server bots, some BlackD configurations

---

## DBVictory Protocol Format (Confirmed)

### 0x0061 — Unknown Creature

```
Bytes:
  u32  removeId        // creature being replaced (0 if new tile)
  u32  creatureId      // unique creature ID (>= 0x10000000)
  u16  nameLen
  char name[nameLen]   // starts A-Z, only letters/spaces/apostrophes
  u8   health          // 0-100
  u8   direction       // 0=N, 1=E, 2=S, 3=W
  u16  lookType        // outfit sprite ID

  // If lookType != 0 (outfit):
  u8   head, body, legs, feet  // color indices
  u8   addons                  // addon bitmask
  // If lookType == 0 (item appearance):
  u16  lookTypeEx              // item sprite ID

  u16  lookMount        // mount ID (0 = no mount)
  u8   mountHead, mountBody, mountLegs, mountFeet  // ALWAYS sent, even if mount=0
  u8   lightLevel
  u8   lightColor
  u16  speed
  u8   skull            // 0-5
  u8   shield           // 0-10
  u8   creatureType     // NPC/player/monster
  u8   mark             // ???
  u16  helpers          // party helpers count
```

**Trail after lookType:**
- `lookType != 0` → 22 bytes (TRAIL_OUTFIT)
- `lookType == 0` → 19 bytes (TRAIL_ITEM)

### 0x0062 — Known Creature

```
Bytes:
  u32  creatureId
  u8   health
  u8   direction
  u16  lookType
  // Same outfit/item block as 0x0061
  // Same mount/light/speed/skull/shield/type block
  // NO mark field
  // NO helpers field
```

**Trail after lookType:**
- `lookType != 0` → 22 bytes
- `lookType == 0` → 19 bytes

### Key DBVictory Deviation from Standard TFS

**Mount colors are ALWAYS sent** (4 bytes: mountHead, mountBody, mountLegs, mountFeet), even when `lookMount == 0`. Standard TFS only sends mount colors when `lookMount != 0`. This broke our initial parser.

### Real Packet Examples

```
# 0x0061 "Fight" (lookType=73, no outfit colors because monster, speed=244):
health=64(100%) dir=00(N) lookType=49,00(73)
trail: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 f4 00 00 05 00 01 00 00 00 00

# 0x0061 "Wolf" (lookType=2063, head=58, speed=170):
health=64(100%) dir=00(N) lookType=0f,08(2063)
trail: 3a 00 00 00 00 00 00 00 00 00 00 00 00 aa 00 00 05 00 01 00 00 00 00

# 0x0062 known creature (lookType=176, speed=244):
health=64(100%) dir=03(W) lookType=b0,00(176)
trail: 00 00 00 00 00 00 00 00 00 00 00 00 00 f4 00 00 00 01 00 00 00 00
```

---

## Anti-Cheat in OT Servers

### Common Server-Side Protections

| Method | Description | Effectiveness |
|--------|-------------|---------------|
| Process name scanning | Check for known bot executables | Low — trivial to rename |
| DLL module scanning | Look for injected DLLs in client process | Medium — catches injection bots |
| Memory integrity checks | CRC/hash of client code sections | Medium — catches code patches |
| Packet timing analysis | Detect impossible action speed | High — hard to fully defeat |
| Behavioral analysis | Movement patterns, click precision, reaction curves | High — requires sophisticated randomization |
| HWID bans | Hardware ID tracking for repeat offenders | Medium — can be spoofed |

### What DBVictory Likely Uses

Most private OT servers have **minimal anti-cheat**:
- Possibly checking for known bot process names
- Maybe basic packet rate limiting
- Unlikely to have sophisticated behavioral analysis
- Almost certainly no memory integrity checks on the client

### Detection Risk by Approach

| Approach | Detection Risk | Notes |
|----------|---------------|-------|
| Proxy (packet parsing) | **Very Low** | Traffic looks identical to normal play |
| External memory reading | **Low** | `ReadProcessMemory` is common; minimal footprint |
| DLL injection | **Medium-High** | Shows up in module list, detectable by scans |
| Pixel analysis | **None** | Completely external, no process interaction |

### Anti-Detection Techniques

**For proxy bots (what we use):**
- Add random jitter to action timings (50-200ms variance)
- Don't react faster than humanly possible (~150ms minimum)
- Vary movement patterns — don't walk in perfect grids
- Space out repetitive actions (eating, healing) with natural variance

**For memory reading:**
- Use `NtReadVirtualMemory` directly instead of `ReadProcessMemory` to avoid API-level hooks
- Read memory in the same thread timing as normal system calls
- Don't hold open handles longer than needed
- Use `pymem` with periodic handle refresh

**For DLL injection (if ever needed):**
- Manual mapping instead of `LoadLibrary` (avoids module list entry)
- Erase PE headers after mapping
- Hook at vtable level instead of inline hooks (harder to detect)

---

## Python Tools for Memory Reading

### pymem

```python
import pymem

pm = pymem.Pymem("dbvStart.exe")
base = pm.base_address

# Read a u32 at offset
creature_id = pm.read_uint(address)

# Read a string (MSVC std::string with SSO)
def read_std_string(pm, addr):
    buf = pm.read_bytes(addr, 32)
    length = struct.unpack_from('<Q', buf, 16)[0]
    capacity = struct.unpack_from('<Q', buf, 24)[0]
    if capacity < 16:  # SSO — data is inline
        return buf[:length].decode('utf-8')
    else:  # heap-allocated
        ptr = struct.unpack_from('<Q', buf, 0)[0]
        return pm.read_bytes(ptr, length).decode('utf-8')
```

### Finding Creature Map in OTClient

The creature map is typically accessed through:
```
Game::instance → m_creatures (std::unordered_map<uint32, CreaturePtr>)
```

To find it:
1. Open client in x64dbg/IDA
2. Search for string references like "Creature" or "creatureId"
3. Find `Game::processCreatureUnknown` or `CreatureManager::getCreatureById`
4. Trace back to the map container address
5. Read the `std::unordered_map` bucket array to enumerate all creatures

### Useful Open-Source References

- **OTClient source**: `src/client/creature.h` — defines Creature class layout
- **forgottenserver**: `src/creature.h` — server-side creature for protocol reference
- **edubart/otclient**: The original OTClient most forks derive from
- **mehah/otclient**: Modern maintained fork with protocol 13.x support

---

## Recommended Strategy for DBVictory Bot

### Short-term (now)
Apply the confirmed packet format (TRAIL_OUTFIT=22, TRAIL_ITEM=19) permanently to `game_state.py` and restart the MCP server. This gives us working creature tracking immediately.

### Medium-term
Add `pymem`-based battle list reading as a secondary/verification data source:
1. Find the creature map base address in dbvStart.exe (one-time RE task)
2. Read creature objects periodically (every 100-200ms)
3. Cross-reference with packet-parsed creatures for maximum accuracy

### Long-term
Build a full hybrid system where:
- Proxy handles all outbound actions (movement, attacks, item use, spells)
- Memory reading provides the authoritative game state (creatures, map tiles, inventory)
- Packet parsing fills gaps (chat messages, status effects, server notifications)
