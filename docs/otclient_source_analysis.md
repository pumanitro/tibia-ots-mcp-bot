# OTClient Source Code Analysis — DBVictory Client

## 1. Client Identification

**DBVictory Client (0.4.2)** = custom fork of **OTCv8** (OTClientV8)

Evidence:
- Login screen says: "(OTClient modified by Davi)"
- Has DirectX9/OpenGL runtime toggle — only OTCv8 has native DirectX9
- Protocol 8.54 (confirmed on OTLand)
- Polish Dragon Ball Z themed OT server

### Source Code Repositories

| Fork | URL | Notes |
|------|-----|-------|
| Original (edubart) | github.com/edubart/otclient | C++11, OpenGL only, inactive |
| Redemption (mehah) | github.com/mehah/otclient | C++20, OpenGL, actively maintained |
| **OTCv8** | **github.com/OTCv8/otcv8-dev** | **C++17, DirectX9+OpenGL — THIS IS THE BASE** |
| DBVictory | private "modified by Davi" | Adds DBZ features (Ki, power, transformations) |

---

## 2. Class Inheritance Hierarchy

```
stdext::shared_object          ← reference counting (atomic refcount)
  └─ LuaObject                 ← Lua scripting bridge (m_fieldsTableRef)
       └─ Thing                ← position, datId (#pragma pack(push,1))
            └─ Creature        ← id, name, health, direction, outfit, movement...
                 ├─ Player     ← (empty, just type overrides)
                 │    └─ LocalPlayer  ← health, mana, level, skills, inventory
                 ├─ Npc
                 └─ Monster
```

---

## 3. Complete Memory Layout

### 3.1 Base Classes

**shared_object** (src/framework/stdext/shared_object.h)
```cpp
class shared_object {
    std::atomic<refcount_t> refs;    // atomic int, 4 bytes
    // + vtable pointer at offset 0 (compiler-generated, 4 bytes on x86)
};
```

**LuaObject** (src/framework/luaengine/luaobject.h) — inherits shared_object
```cpp
class LuaObject : public stdext::shared_object {
    int m_fieldsTableRef;            // 4 bytes — Lua table reference
};
```

**Thing** (src/client/thing.h) — inherits LuaObject
```cpp
#pragma pack(push,1)                 // ← PACKED! No padding between fields
class Thing : public LuaObject {
    Position m_position;             // 10 bytes (int x + int y + short z)
    uint16   m_datId;                // 2 bytes
    bool     m_marked;               // 1 byte
    bool     m_hidden;               // 1 byte
    Color    m_markedColor;          // 4 bytes (RGBA)
};
#pragma pack(pop)
```

### 3.2 Position Struct

```cpp
class Position {
    int   x;     // 4 bytes — horizontal coordinate (default 65535 = invalid)
    int   y;     // 4 bytes — vertical coordinate   (default 65535 = invalid)
    short z;     // 2 bytes — floor level            (default 255 = invalid)
};
// Total: 10 bytes (no packing needed — naturally aligned within pack(1) Thing)
```

### 3.3 Creature Class (src/client/creature.h)

Inherits Thing. This is the big one — ALL creatures (players, NPCs, monsters) use this.

```cpp
class Creature : public Thing {
    // === Identity ===
    uint32      m_id;                    // 4 bytes  — unique creature ID
    std::string m_name;                  // 24 bytes (MSVC SSO) or 32 bytes (GCC)
    uint8       m_healthPercent;         // 1 byte   — 0-100
    int8        m_manaPercent;           // 1 byte

    // === Direction ===
    Otc::Direction m_direction;          // 4 bytes (enum/int)
    Otc::Direction m_walkDirection;      // 4 bytes

    // === Appearance ===
    Outfit      m_outfit;               // ~80+ bytes (see §3.4)
    Light       m_light;                // 8 bytes (color + intensity)
    uint16      m_speed;                // 2 bytes
    double      m_baseSpeed;            // 8 bytes

    // === Status Icons ===
    uint8       m_skull;                // 1 byte
    uint8       m_shield;               // 1 byte
    uint8       m_emblem;               // 1 byte
    uint8       m_type;                 // 1 byte
    uint8       m_icon;                 // 1 byte

    // === Textures (shared_ptr = 8 bytes each on x86) ===
    TexturePtr  m_skullTexture;         // 8 bytes
    TexturePtr  m_shieldTexture;        // 8 bytes
    TexturePtr  m_emblemTexture;        // 8 bytes
    TexturePtr  m_typeTexture;          // 8 bytes
    TexturePtr  m_iconTexture;          // 8 bytes

    // === Display Flags ===
    stdext::boolean<true>  m_showShieldTexture;   // 1 byte
    stdext::boolean<false> m_shieldBlink;          // 1 byte
    stdext::boolean<false> m_passable;             // 1 byte
    Color       m_timedSquareColor;     // 4 bytes
    Color       m_staticSquareColor;    // 4 bytes
    Color       m_nameColor;            // 4 bytes
    stdext::boolean<false> m_showTimedSquare;      // 1 byte
    stdext::boolean<false> m_showStaticSquare;     // 1 byte
    stdext::boolean<true>  m_removed;              // 1 byte

    // === Cached UI Text ===
    CachedText  m_nameCache;            // variable (string + texture)
    Color       m_informationColor;     // 4 bytes
    bool        m_useCustomInformationColor; // 1 byte
    Point       m_informationOffset;    // 8 bytes (int x, int y)
    Color       m_outfitColor;          // 4 bytes
    ScheduledEventPtr m_outfitColorUpdateEvent;  // 8 bytes (shared_ptr)
    Timer       m_outfitColorTimer;     // 8+ bytes

    // === Title ===
    CachedText  m_titleCache;           // variable
    Color       m_titleColor;           // 4 bytes

    // === Speed Formula (STATIC — not per-instance) ===
    // static std::array<double, Otc::LastSpeedFormula> m_speedFormula;

    // === Walking State ===
    int         m_walkAnimationPhase;   // 4 bytes
    uint8       m_walkedPixels;         // 1 byte
    uint        m_footStep;             // 4 bytes
    Timer       m_walkTimer;            // 8+ bytes
    ticks_t     m_footLastStep;         // 8 bytes (int64 or similar)
    TilePtr     m_walkingTile;          // 8 bytes (shared_ptr)
    stdext::boolean<false> m_walking;   // 1 byte
    stdext::boolean<false> m_allowAppearWalk; // 1 byte
    ScheduledEventPtr m_walkUpdateEvent;     // 8 bytes
    ScheduledEventPtr m_walkFinishAnimEvent; // 8 bytes
    EventPtr    m_disappearEvent;       // 8 bytes

    // === Walk Positions ===
    Point       m_walkOffset;               // 8 bytes (int x, int y)
    Point       m_walkOffsetInNextFrame;    // 8 bytes
    Otc::Direction m_lastStepDirection;     // 4 bytes
    Position    m_lastStepFromPosition;     // 10 bytes
    Position    m_lastStepToPosition;       // 10 bytes
    Position    m_oldPosition;              // 10 bytes

    // === Jump/Elevation ===
    uint8       m_elevation;            // 1 byte
    uint16      m_stepDuration;         // 2 bytes
    float       m_jumpHeight;           // 4 bytes
    float       m_jumpDuration;         // 4 bytes
    PointF      m_jumpOffset;           // 8 bytes (float x, float y)
    Timer       m_jumpTimer;            // 8+ bytes

    // === Attached UI ===
    StaticTextPtr m_text;                       // 8 bytes
    std::list<UIWidgetPtr> m_bottomWidgets;     // variable (list container)
    std::list<UIWidgetPtr> m_directionalWidgets;
    std::list<UIWidgetPtr> m_topWidgets;

    // === Progress Bar ===
    uint8       m_progressBarPercent;          // 1 byte
    ScheduledEventPtr m_progressBarUpdateEvent;// 8 bytes
    Timer       m_progressBarTimer;            // 8+ bytes
};
```

### 3.4 Outfit Struct (src/client/outfit.h)

```cpp
class Outfit {
    ThingCategory m_category;   // 4 bytes (enum)
    int m_id;                   // 4 bytes — this is "lookType"
    int m_auxId;                // 4 bytes
    int m_head;                 // 4 bytes — head color
    int m_body;                 // 4 bytes — body color
    int m_legs;                 // 4 bytes — legs color
    int m_feet;                 // 4 bytes — feet color
    int m_addons;               // 4 bytes
    int m_mount;                // 4 bytes
    int m_wings;                // 4 bytes (OTCv8 addition)
    int m_aura;                 // 4 bytes (OTCv8 addition)
    int m_healthBar;            // 4 bytes (OTCv8 addition)
    int m_manaBar;              // 4 bytes (OTCv8 addition)
    std::string m_shader;       // 24 bytes (MSVC) — shader name
    bool m_center;              // 1 byte
};
// Approx total: 77 bytes + padding
```

### 3.5 LocalPlayer Class (src/client/localplayer.h)

Inherits Player → Creature → Thing. Adds:

```cpp
class LocalPlayer : public Player {
    // === Auto-walk ===
    Position m_autoWalkDestination;           // 10 bytes
    Position m_lastAutoWalkPosition;          // 10 bytes
    int      m_lastAutoWalkRetries;           // 4 bytes
    ScheduledEventPtr m_serverWalkEndEvent;   // 8 bytes
    ScheduledEventPtr m_autoWalkContinueEvent;// 8 bytes
    ticks_t  m_walkLockExpiration;            // 8 bytes

    // === Pre-walking ===
    std::list<Position> m_preWalking;         // variable
    bool     m_serverWalking;                 // 1 byte
    bool     m_lastPrewalkDone;               // 1 byte
    WalkMatrix m_walkMatrix;                  // unknown size

    // === Account Status ===
    bool     m_premium;                       // 1 byte
    bool     m_known;                         // 1 byte
    bool     m_pending;                       // 1 byte

    // === Inventory ===
    ItemPtr  m_inventoryItems[Otc::LastInventorySlot]; // 15 x 8 = 120 bytes

    // === Idle Timer ===
    Timer    m_idleTimer;                     // 8+ bytes

    // === Skills ===
    std::vector<int> m_skillsLevel;           // 12 bytes (vec header)
    std::vector<int> m_skillsBaseLevel;       // 12 bytes
    std::vector<int> m_skillsLevelPercent;    // 12 bytes
    std::vector<int> m_spells;                // 12 bytes

    // === Character Stats (all doubles = 8 bytes each) ===
    int      m_states;                        // 4 bytes
    int      m_vocation;                      // 4 bytes
    int      m_blessings;                     // 4 bytes
    double   m_health;                        // 8 bytes
    double   m_maxHealth;                     // 8 bytes
    double   m_freeCapacity;                  // 8 bytes
    double   m_totalCapacity;                 // 8 bytes
    double   m_experience;                    // 8 bytes
    double   m_level;                         // 8 bytes
    double   m_levelPercent;                  // 8 bytes
    double   m_mana;                          // 8 bytes
    double   m_maxMana;                       // 8 bytes
    double   m_magicLevel;                    // 8 bytes
    double   m_magicLevelPercent;             // 8 bytes
    double   m_baseMagicLevel;               // 8 bytes
    double   m_soul;                          // 8 bytes
    double   m_stamina;                       // 8 bytes
    double   m_regenerationTime;              // 8 bytes
    double   m_offlineTrainingTime;           // 8 bytes
};
```

---

## 4. Key Global Singletons

These are the entry points for reading game state from memory:

### g_map (Map singleton)
```cpp
// src/client/map.h
class Map {
    std::map<uint32, CreaturePtr> m_knownCreatures;  // ALL creatures by ID
    Position m_centralPosition;                       // camera center = player pos
    // ...
};
// Access: g_map.m_knownCreatures gives us every creature the client knows about
```

### g_game (Game singleton)
```cpp
// src/client/game.h
class Game {
    LocalPlayerPtr   m_localPlayer;          // our player (full stats)
    CreaturePtr      m_attackingCreature;     // current attack target
    CreaturePtr      m_followingCreature;     // current follow target
    ProtocolGamePtr  m_protocolGame;          // network handler (has XTEA key!)
    std::map<int, ContainerPtr> m_containers; // open containers (backpack etc.)
    bool             m_online;
    Otc::FightModes  m_fightMode;            // offensive/balanced/defensive
    Otc::ChaseModes  m_chaseMode;            // stand/chase
    // ...
};
```

### g_lua (Lua VM)
```cpp
// The Lua state — could be used to call Lua functions from injected code
```

---

## 5. XTEA Encryption

### Key Storage
```cpp
// src/framework/net/protocol.h
class Protocol {
    uint32 m_xteaKey[4];               // 128-bit key = 4 x 32-bit words
    bool   m_xteaEncryptionEnabled;
    bool   m_checksumEnabled;
    bool   m_sequencedPackets;
    bool   m_compression;
};
```

### Algorithm
- Delta: `0x9E3779B9` in source (but compiled binary uses negated `0x61C88647` with SUB instruction — mathematically equivalent)
- Rounds: 32
- Block size: 8 bytes
- Our hook location: VA `0x0060F220` (confirmed)

---

## 6. Enums & Constants

### Direction (src/client/const.h)
```
North=0, East=1, South=2, West=3
NorthEast=4, SouthEast=5, SouthWest=6, NorthWest=7
InvalidDirection=8
```

### Fight Modes
```
FightOffensive=1, FightBalanced=2, FightDefensive=3
```

### Chase Modes
```
DontChase=0, ChaseOpponent=1
```

### Inventory Slots
```
Head, Necklace, Backpack, Armor, RightHand, LeftHand,
Legs, Feet, Ring, Ammo, Purse, Ext1, Ext2, Ext3, Ext4
```

---

## 7. Estimated Memory Layout (x86, MSVC)

### Creature Object — Approximate Byte Offsets

**IMPORTANT:** These are estimates based on source field order + MSVC alignment.
The DBVictory fork adds custom fields that shift everything after base Creature fields.
Our empirical offsets (from DLL testing) are the ground truth.

```
Offset  Size  Field                    Source
──────  ────  ───────────────────────  ──────────────
+0x00   4     vtable pointer           (compiler)
+0x04   4     refs (atomic refcount)   shared_object
+0x08   4     m_fieldsTableRef         LuaObject
                                       ─── Thing (packed) ───
+0x0C   4     m_position.x             Thing
+0x10   4     m_position.y             Thing
+0x14   2     m_position.z             Thing
+0x16   2     m_datId                  Thing
+0x18   1     m_marked                 Thing
+0x19   1     m_hidden                 Thing
+0x1A   4     m_markedColor            Thing
                                       ─── Creature ───
+0x1E   4     m_id                     Creature        ◄── creature ID
+0x22   24    m_name (MSVC string)     Creature        ◄── name
+0x3A   1     m_healthPercent          Creature        ◄── HP %
+0x3B   1     m_manaPercent            Creature
+0x3C   4     m_direction              Creature        ◄── facing
+0x40   4     m_walkDirection          Creature
+0x44   ~80   m_outfit                 Creature        ◄── lookType at +0x48
+0xA0   ...   (everything else)        Creature
```

**NOTE:** The `#pragma pack(push,1)` on Thing means no padding within Thing's fields.
But Creature does NOT have pack(1), so the compiler may insert padding at the
Thing→Creature boundary. The exact gap depends on compiler settings.

### Cross-Reference With Our Empirical Offsets

| Field | Estimated from source | Our DLL found | Notes |
|-------|----------------------|---------------|-------|
| m_position (in Thing) | ~0x0C from object start | ID - 0x28 = -40 from ID | If ID is at 0x1E, then pos at 0x0C is -0x12 from ID... doesn't match -40. Suggests padding or extra fields |
| m_id | ~0x1E from object start | +0 (our baseline) | — |
| m_name | ~0x22 from object start | +4 from ID | Close: 0x22 - 0x1E = +4 ✓ |
| m_healthPercent | ~0x3A from object start | +28 from ID | 0x3A - 0x1E = 0x1C = +28 ✓ |
| m_direction | ~0x3C from object start | +32 from ID | 0x3C - 0x1E = 0x1E = +30 ≈ close (padding) |
| m_outfit.m_id (lookType) | ~0x48 from object start | +48 from ID | 0x48 - 0x1E = 0x2A = +42 ≈ close |

**The name and health offsets match perfectly.** Direction and lookType are close
but shifted slightly, likely due to DBVictory's custom modifications or alignment.

---

## 8. What DBVictory Likely Added

The +576 offset for NPC position (which doesn't exist in base OTClient) suggests
DBVictory added ~500 bytes of custom fields to the Creature class. Possible additions:

- Ki level / Ki max (Dragon Ball power system)
- Power level
- Transformation state (Super Saiyan levels)
- Custom animations / aura data
- Server-specific attributes
- Custom UI bar data

These custom fields sit between the standard Creature fields and the end of the object,
which is why our empirical offsets diverge from source estimates at higher offsets.

---

## 9. Potential Improvements to Our DLL

### Current approach: VirtualQuery full memory scan
- Scans all process memory looking for creature patterns
- Slow, unreliable, misses creatures sometimes

### Better approach: Find g_map singleton
1. Find the `g_map` global in the binary (look for `Map::addCreature` references)
2. Read `g_map.m_knownCreatures` (std::map<uint32, CreaturePtr>)
3. Walk the red-black tree to enumerate all creatures
4. Direct O(1) access by creature ID

### Even better: Find g_game singleton
1. Locate `g_game` global
2. Read `m_localPlayer` → get full player stats (health, mana, level, exp, skills)
3. Read `m_attackingCreature` → know current target
4. Read `m_containers` → read inventory contents
5. Read `m_protocolGame` → access XTEA key directly (no hook needed!)

### How to find globals in binary:
- Search for known strings like "knownCreatures" or error messages from map.cpp
- Find functions that reference these strings → trace back to find the global address
- Or: search for the vtable pointer pattern of Map/Game class objects

---

## 10. Protocol Information

- Server: dbv.dbvictory.eu:7171
- Protocol version: 8.54
- Packet format: [2-byte length] [4-byte checksum] [encrypted payload]
- Encryption: XTEA (32 rounds, 8-byte blocks)
- Login: RSA + XTEA key exchange

### Packet Parsing Source
`src/client/protocolgameparse.cpp` — shows exactly how the client decodes every
server packet. This is the definitive reference for understanding the packet format
that flows through our proxy.

---

## 11. File Reference

| Source File | What It Contains |
|---|---|
| `src/client/creature.h` | Creature class — all creature fields |
| `src/client/creature.cpp` | Creature methods — movement, rendering |
| `src/client/thing.h` | Thing base class — position, datId |
| `src/client/localplayer.h` | LocalPlayer — health, mana, skills, inventory |
| `src/client/game.h` | Game singleton — player, targets, containers |
| `src/client/map.h` | Map singleton — creature registry, tiles |
| `src/client/outfit.h` | Outfit struct — lookType, colors, mount |
| `src/client/position.h` | Position struct — x, y, z |
| `src/client/const.h` | Enums — Direction, FightMode, InventorySlot |
| `src/client/tile.h` | Tile — items and creatures on a map square |
| `src/framework/net/protocol.h` | Protocol — XTEA key, encryption flags |
| `src/framework/net/protocol.cpp` | XTEA encrypt/decrypt implementation |
| `src/client/protocolgameparse.cpp` | All server→client packet parsing |
| `src/client/protocolgamesend.cpp` | All client→server packet building |
| `src/framework/stdext/shared_object.h` | Reference counting base class |
| `src/framework/luaengine/luaobject.h` | Lua bridge base class |
