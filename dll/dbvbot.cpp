/*
 * dbvbot.dll — In-process creature scanner for DBVictory
 *
 * v50: Direct creature map reading + WndProc hook for fast targeting.
 *   - Map scan (~100ms): walks g_map.m_knownCreatures red-black tree
 *   - Full scan (~5s):   VirtualQuery fallback (auto if map unavailable)
 *   - WndProc hook:      executes targeting in ~16ms (one frame)
 *
 * Build (MinGW 32-bit):
 *   g++ -shared -o dbvbot.dll dbvbot.cpp -lkernel32 -luser32 -static -s -O2 -std=c++17
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winsock2.h>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <cstdarg>
#include <cstdint>
#include <csetjmp>

// ── Safe memory copy (replaces deprecated IsBadReadPtr) ─────────────
// MinGW does not support MSVC __try/__except in C++.  Use
// VirtualQuery to check readability instead (no TOCTOU with
// IsBadReadPtr's page-guard side-effects).
static BOOL safe_readable(const void* ptr, size_t len) {
    if (!ptr) return FALSE;
    MEMORY_BASIC_INFORMATION mbi;
    const uint8_t* p = (const uint8_t*)ptr;
    const uint8_t* end = p + len;
    while (p < end) {
        if (VirtualQuery(p, &mbi, sizeof(mbi)) == 0) return FALSE;
        if (mbi.State != MEM_COMMIT) return FALSE;
        DWORD prot = mbi.Protect & ~(PAGE_GUARD | PAGE_NOCACHE | PAGE_WRITECOMBINE);
        if (!(prot == PAGE_READONLY || prot == PAGE_READWRITE ||
              prot == PAGE_EXECUTE_READ || prot == PAGE_EXECUTE_READWRITE ||
              prot == PAGE_WRITECOPY || prot == PAGE_EXECUTE_WRITECOPY))
            return FALSE;
        uintptr_t region_end = (uintptr_t)mbi.BaseAddress + mbi.RegionSize;
        p = (const uint8_t*)region_end;
    }
    return TRUE;
}

static BOOL safe_memcpy(void* dst, const void* src, size_t len) {
    // Use ReadProcessMemory on ourselves — it handles page faults atomically,
    // avoiding the TOCTOU race where VirtualQuery says "readable" but the game
    // thread frees the memory before our memcpy executes.
    SIZE_T bytes_read = 0;
    if (!ReadProcessMemory(GetCurrentProcess(), src, dst, len, &bytes_read))
        return FALSE;
    return bytes_read == len;
}

// ── Constants ───────────────────────────────────────────────────────
#define MIN_CREATURE_ID 0x10000000u
#define MAX_CREATURE_ID 0x80000000u
#define PIPE_NAME       "\\\\.\\pipe\\dbvbot"
#define PIPE_BUF_SIZE   65536
#define MAX_CREATURES   200
#define MAX_NAME_LEN    63
#define FULL_SCAN_INTERVAL 5000  // ms between full VirtualQuery scans
#define FAST_SCAN_INTERVAL 200   // ms between fast re-reads of cached addrs
#define MAP_SCAN_INTERVAL  16    // ms between creature map tree walks (~60 FPS)
#define SEND_INTERVAL      16    // ms between JSON sends (~60 FPS)

// ── Configurable offsets (loaded from pipe "set_offsets" command) ────
// Defaults match known DBVictory layout; overridden at runtime.
static uint32_t OFF_GAME_SINGLETON_RVA   = 0xB2E970u;
static uint32_t OFF_GAME_ATTACKING       = 0x0C;
static uint32_t OFF_GAME_PROTOCOL        = 0x18;
static uint32_t OFF_GAME_ATKFLAG         = 0x34;
static uint32_t OFF_GAME_SEQ             = 0x70;
static uint32_t OFF_CREATURE_VTABLE      = 0x00;
static uint32_t OFF_CREATURE_REFS        = 0x04;
static uint32_t OFF_CREATURE_ID          = 0x34;
static uint32_t OFF_CREATURE_NAME        = 0x38;
static uint32_t OFF_CREATURE_HP          = 0x50;
static int32_t  OFF_NPC_POS_FROM_ID      = 576;
static int32_t  OFF_PLAYER_POS_FROM_ID   = -40;
static uint32_t OFF_VTABLE_RVA_MIN       = 0x870000u;
static uint32_t OFF_VTABLE_RVA_MAX       = 0x8A0000u;
static uint32_t OFF_XTEA_ENCRYPT_RVA     = 0x3AF220u;
static uint32_t OFF_GAME_ATTACK_RVA      = 0x8F220u;
static uint32_t OFF_SEND_ATTACK_RVA      = 0x19D100u;
static uint32_t OFF_GAME_DOATTACK_RVA    = 0x89680u;


// ── Creature data ───────────────────────────────────────────────────
struct CachedCreature {
    uint8_t*  addr;     // memory address of the creature's ID field
    uint32_t  id;
    char      name[MAX_NAME_LEN + 1];
    uint8_t   health;
    uint32_t  x, y, z;
};

// ── Global state ────────────────────────────────────────────────────
static HANDLE  g_thread = NULL;
static volatile BOOL g_running = FALSE;
static volatile uint32_t g_player_id = 0;

// Address cache: creatures found by full scan, re-read by fast scan
static CachedCreature g_addrs[MAX_CREATURES];
static int            g_addr_count = 0;

// Output cache: creatures sent to Python
static CachedCreature g_output[MAX_CREATURES];
static int            g_output_count = 0;
static CRITICAL_SECTION g_cs;

static char g_dll_dir[MAX_PATH] = {0};
static int  g_scan_count = 0;

// ── Creature map (g_map) state ──────────────────────────────────────
static uintptr_t g_map_addr = 0;           // address of the std::map header
static volatile BOOL g_use_map_scan = FALSE; // feature flag: use tree walk vs VirtualQuery
static int g_map_scan_count = 0;

// ── Crash recovery (setjmp/longjmp + VEH) ──────────────────────────
// MinGW doesn't support MSVC __try/__except.  Instead, setjmp saves
// the call point and the VEH handler longjmp's back on access violation.
// Thread IDs prevent cross-thread longjmp (undefined behavior).
static jmp_buf  g_scan_jmpbuf;               // scan thread recovery point
static volatile BOOL g_scan_recovery = FALSE; // armed when scan is in progress
static DWORD    g_scan_thread_id = 0;         // pipe/scan thread ID

static jmp_buf  g_attack_jmpbuf;              // game thread recovery point
static volatile BOOL g_attack_recovery = FALSE;
static DWORD    g_attack_thread_id = 0;       // game thread ID (from WndProc)

// ── Map stability tracking (Fix 11) ─────────────────────────────────
// Track AV timestamps and creature count changes to detect unstable map
// state.  When the map is unstable, skip Game::attack to avoid AVs
// that corrupt Lua state via longjmp recovery.
#define MAP_STABILITY_COOLDOWN_MS 2000  // skip attacks for 2s after any AV
#define COUNT_CHANGE_COOLDOWN_MS  1000  // skip attacks for 1s after big count change
#define COUNT_CHANGE_THRESHOLD    5     // creature count change >= 5 = "big"
static volatile DWORD g_last_scan_av_tick    = 0;  // GetTickCount when scan thread last AVed
static volatile DWORD g_last_attack_av_tick  = 0;  // GetTickCount when game thread last AVed during attack
static volatile int   g_prev_creature_count  = 0;  // previous scan cycle creature count
static volatile DWORD g_last_count_change_tick = 0; // when creature count changed significantly

// ── WndProc hook state ──────────────────────────────────────────────
#define WM_BOT_TARGET (WM_USER + 100)
static HWND    g_game_hwnd = NULL;
static WNDPROC g_orig_wndproc = NULL;
static volatile BOOL g_wndproc_hooked = FALSE;

// ── Full light state ─────────────────────────────────────────────────
static volatile BOOL g_full_light = FALSE;
static uintptr_t g_light_addr = 0;      // absolute address of world light level
static int       g_light_format = 0;     // 0=u8 pair (level,color), 1=u32 pair
// Rendering light structure base (RVA 0xB2ECF0 = base + game_singleton_rva + offset)
// Layout discovered via xref analysis:
//   +0x00 (0xB2ECF0): u32 cleared by packet handler
//   +0x04 (0xB2ECF4): u32 cleared by packet handler
//   +0x08 (0xB2ECF8): u8 world light level (written by 0x82 opcode)
//   +0x09 (0xB2ECF9): u8 world light color
//   +0x0C (0xB2ECFC): u32 rendering param #1 (7 xrefs in tile renderers)
//   +0x10 (0xB2ED00): u32 rendering param #2 (7 xrefs in tile renderers)
//   +0x14 (0xB2ED04): u16 rendering param #3
static uintptr_t g_light_render_base = 0;  // absolute addr of 0xB2ECFC

// ── Pipe handle (for scan responses) ─────────────────────────────────
static HANDLE g_active_pipe = INVALID_HANDLE_VALUE;

// ── Debug log ───────────────────────────────────────────────────────
static FILE* g_dbg = NULL;

static void dbg_open(void) {
    if (g_dbg) return;
    char path[MAX_PATH];
    int n = _snprintf(path, sizeof(path), "%s\\dbvbot_debug.txt", g_dll_dir);
    if (n < 0 || n >= (int)sizeof(path)) return;
    g_dbg = fopen(path, "a");
    if (g_dbg) {
        fprintf(g_dbg, "=== dbvbot.dll v50 (map scan + WndProc hook) ===\n");
        fflush(g_dbg);
    }
}

static void dbg(const char* fmt, ...) {
    if (!g_dbg) return;
    va_list ap;
    va_start(ap, fmt);
    vfprintf(g_dbg, fmt, ap);
    va_end(ap);
    fprintf(g_dbg, "\n");
    fflush(g_dbg);
}

// ── Name validation ─────────────────────────────────────────────────

static BOOL is_name_char(char c) {
    return c == ' ' || c == '\'' || c == '-' || c == '.' ||
           (c >= '0' && c <= '9') ||
           (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z');
}

static BOOL validate_name(const char* s, size_t len) {
    if (len < 3 || len > 30) return FALSE;
    if (!(s[0] >= 'A' && s[0] <= 'Z'))
        return FALSE;
    BOOL has_lower = FALSE;
    for (size_t i = 0; i < len; i++) {
        if (!is_name_char(s[i])) return FALSE;
        if (s[i] >= 'a' && s[i] <= 'z') has_lower = TRUE;
        if (i > 0 && (s[i-1] >= 'a' && s[i-1] <= 'z') && (s[i] >= 'A' && s[i] <= 'Z'))
            return FALSE;
    }
    if (!has_lower) return FALSE;
    return TRUE;
}

// ── MSVC string reader ──────────────────────────────────────────────

static BOOL try_read_name(const uint8_t* base, char* out, size_t out_sz) {
    uint32_t str_size, str_cap;
    memcpy(&str_size, base + 16, 4);
    memcpy(&str_cap, base + 20, 4);

    if (str_size == 0 || str_size > 30) return FALSE;
    if (str_cap < str_size || str_cap >= 256) return FALSE;

    const char* data = NULL;

    if (str_cap < 16) {
        data = (const char*)base;
    } else {
        uintptr_t ptr;
        memcpy(&ptr, base, 4);
        if (ptr < 0x10000 || ptr >= 0x7FFE0000) return FALSE;
        // Use a stack buffer to safely copy the heap string data
        static thread_local char heap_buf[64];
        if (str_size >= sizeof(heap_buf)) return FALSE;
        if (!safe_memcpy(heap_buf, (void*)ptr, str_size)) return FALSE;
        heap_buf[str_size] = '\0';
        data = heap_buf;
    }

    if (!validate_name(data, str_size)) return FALSE;

    size_t n = str_size < (out_sz - 1) ? str_size : (out_sz - 1);
    memcpy(out, data, n);
    out[n] = '\0';
    return TRUE;
}

// ── Read creature position ──────────────────────────────────────────

static BOOL read_position_at(const uint8_t* id_ptr, int offset, uint32_t* x, uint32_t* y, uint32_t* z) {
    const uint8_t* pos_ptr = id_ptr + offset;
    uint8_t pos_buf[12];
    if (!safe_memcpy(pos_buf, pos_ptr, 12)) return FALSE;

    memcpy(x, pos_buf, 4);
    memcpy(y, pos_buf + 4, 4);
    memcpy(z, pos_buf + 8, 4);

    if (*x > 65535 || *y > 65535 || *z > 15) return FALSE;
    return TRUE;
}

static BOOL read_position(const uint8_t* id_ptr, uint32_t id, uint32_t* x, uint32_t* y, uint32_t* z) {
    // Player creature stores position at a different offset
    if (g_player_id != 0 && id == g_player_id) {
        return read_position_at(id_ptr, OFF_PLAYER_POS_FROM_ID, x, y, z);
    }
    return read_position_at(id_ptr, OFF_NPC_POS_FROM_ID, x, y, z);
}

// ── Try to read a creature at a known address ───────────────────────
// Returns TRUE if the address still holds a valid creature with the expected ID.

static BOOL reread_creature(CachedCreature* cc) {
    uint8_t snap[32];
    if (!safe_memcpy(snap, cc->addr, 32)) return FALSE;

    // Verify the ID is still the same
    uint32_t id;
    memcpy(&id, snap, 4);
    if (id != cc->id) return FALSE;

    // Re-read health
    uint32_t hp_word;
    memcpy(&hp_word, snap + 28, 4);
    if (hp_word > 100) return FALSE;
    cc->health = (uint8_t)hp_word;

    // Re-read position
    uint32_t x = 0, y = 0, z = 0;
    if (read_position(cc->addr, cc->id, &x, &y, &z)) {
        cc->x = x;
        cc->y = y;
        cc->z = z;
    }

    return TRUE;
}

// ── Copy all creatures to output (filtering done in Python) ──────────

static void copy_to_output(void) {
    EnterCriticalSection(&g_cs);
    memcpy(g_output, g_addrs, sizeof(CachedCreature) * g_addr_count);
    g_output_count = g_addr_count;
    LeaveCriticalSection(&g_cs);
}

// ── Fast scan: re-read cached addresses ─────────────────────────────

static void fast_scan(void) {
    int valid = 0;
    for (int i = 0; i < g_addr_count; i++) {
        if (reread_creature(&g_addrs[i])) {
            if (valid != i)
                g_addrs[valid] = g_addrs[i];
            valid++;
        }
    }
    g_addr_count = valid;
    copy_to_output();
}

// ── Full memory scan ────────────────────────────────────────────────

static void full_scan(void) {
    g_scan_count++;

    CachedCreature found[MAX_CREATURES];
    int found_count = 0;
    int regions_scanned = 0;
    int pages_scanned = 0;
    int pages_bad = 0;
    uintptr_t max_addr_reached = 0;

    MEMORY_BASIC_INFORMATION mbi;
    uintptr_t addr = 0x10000;

    while (addr < 0x7FFE0000u && found_count < MAX_CREATURES) {
        if (VirtualQuery((void*)addr, &mbi, sizeof(mbi)) == 0) {
            dbg("VirtualQuery failed at 0x%08X err=%lu", (unsigned)addr, GetLastError());
            break;
        }

        uintptr_t rstart = (uintptr_t)mbi.BaseAddress;
        uintptr_t rend = rstart + mbi.RegionSize;
        max_addr_reached = rend;

        if (mbi.State == MEM_COMMIT &&
            (mbi.Protect == PAGE_READWRITE ||
             mbi.Protect == PAGE_EXECUTE_READWRITE) &&
            mbi.RegionSize >= 32) {

            regions_scanned++;
            for (uintptr_t page = rstart; page < rend && found_count < MAX_CREATURES; page += 4096) {
                uint8_t probe;
                if (!safe_memcpy(&probe, (void*)page, 1)) { pages_bad++; continue; }
                pages_scanned++;

                uintptr_t page_end = page + 4096;
                if (page_end > rend) page_end = rend;
                if (page_end - page < 32) continue;

                uint32_t* base = (uint32_t*)page;
                int max_idx = (int)((page_end - page - 32) / 4);

                for (int i = 0; i < max_idx && found_count < MAX_CREATURES; i++) {
                    uint32_t id = base[i];

                    if (id < MIN_CREATURE_ID || id >= MAX_CREATURE_ID) continue;

                    uint32_t str_size = base[i + 5];
                    if (str_size == 0 || str_size > 30) continue;

                    uint32_t str_cap = base[i + 6];
                    if (str_cap < str_size || str_cap >= 256) continue;

                    uint32_t hp_word = base[i + 7];
                    if (hp_word > 100) continue;

                    uint8_t* id_ptr = (uint8_t*)&base[i];
                    char name[64] = {0};
                    if (!try_read_name(id_ptr + 4, name, sizeof(name))) continue;

                    // Dedup by id
                    BOOL dup = FALSE;
                    for (int j = 0; j < found_count; j++) {
                        if (found[j].id == id) { dup = TRUE; break; }
                    }
                    if (dup) continue;

                    // Read position
                    uint32_t cx = 0, cy = 0, cz = 0;
                    read_position(id_ptr, id, &cx, &cy, &cz);

                    CachedCreature* c = &found[found_count++];
                    c->addr = id_ptr;  // cache the memory address!
                    c->id = id;
                    strncpy(c->name, name, MAX_NAME_LEN);
                    c->name[MAX_NAME_LEN] = '\0';
                    c->health = (uint8_t)hp_word;
                    c->x = cx;
                    c->y = cy;
                    c->z = cz;

                    if (g_scan_count <= 3) {
                        dbg("  FOUND id=0x%08X name=\"%s\" hp=%d pos=(%u,%u,%u) addr=%p",
                            id, name, (int)hp_word, cx, cy, cz, id_ptr);
                    }
                }
            }
        }
        addr = rend;
    }

    // Replace address cache
    memcpy(g_addrs, found, sizeof(CachedCreature) * found_count);
    g_addr_count = found_count;

    // Apply proximity filter to update output
    copy_to_output();

    dbg("full_scan#%d: raw=%d nearby=%d regions=%d pages=%d bad_pages=%d maxaddr=0x%08X",
        g_scan_count, found_count, g_output_count,
        regions_scanned, pages_scanned, pages_bad, (unsigned)max_addr_reached);
}

// ── JSON builder ────────────────────────────────────────────────────
// validate_name() guarantees [A-Za-z0-9 '.-] — no JSON escaping needed

static int build_json(char* buf, size_t buf_sz) {
    EnterCriticalSection(&g_cs);
    int written = _snprintf(buf, buf_sz, "{\"creatures\":[");
    if (written < 0 || written >= (int)buf_sz) {
        LeaveCriticalSection(&g_cs);
        return -1;
    }
    int pos = written;
    for (int i = 0; i < g_output_count; i++) {
        if (i > 0) {
            if (pos + 1 >= (int)buf_sz) break;
            buf[pos++] = ',';
        }
        written = _snprintf(buf + pos, buf_sz - pos,
            "{\"id\":%u,\"name\":\"%s\",\"hp\":%d,\"x\":%u,\"y\":%u,\"z\":%u}",
            g_output[i].id, g_output[i].name, g_output[i].health,
            g_output[i].x, g_output[i].y, g_output[i].z);
        if (written < 0 || written >= (int)(buf_sz - pos)) break;
        pos += written;
    }
    written = _snprintf(buf + pos, buf_sz - pos, "]}\n");
    if (written < 0 || written >= (int)(buf_sz - pos)) {
        LeaveCriticalSection(&g_cs);
        return -1;
    }
    pos += written;
    LeaveCriticalSection(&g_cs);
    return pos;
}

// ── IAT Hook: intercept Winsock WSASend() to capture call stacks ────

typedef int (WSAAPI *WSASend_fn)(SOCKET, LPWSABUF, DWORD, LPDWORD, DWORD, LPWSAOVERLAPPED, LPWSAOVERLAPPED_COMPLETION_ROUTINE);
static WSASend_fn    g_original_WSASend = NULL;
static volatile BOOL g_hook_active  = FALSE;
static FILE*         g_hook_log     = NULL;
static SOCKET        g_game_socket  = INVALID_SOCKET;  // captured from WSASend calls

static int WSAAPI hooked_WSASend(SOCKET s, LPWSABUF lpBuffers, DWORD dwBufferCount,
                                  LPDWORD lpNumberOfBytesSent, DWORD dwFlags,
                                  LPWSAOVERLAPPED lpOverlapped,
                                  LPWSAOVERLAPPED_COMPLETION_ROUTINE lpCompletionRoutine) {
    // Capture the game socket for later use
    if (g_game_socket == INVALID_SOCKET && dwBufferCount > 0 && lpBuffers[0].len == 14) {
        g_game_socket = s;
        dbg("Captured game socket: %u", (unsigned)s);
    }

    if (g_hook_active && g_hook_log && dwBufferCount > 0 && lpBuffers[0].len > 0) {
        // Use __builtin_return_address for reliable caller capture
        void* ret_addr = __builtin_return_address(0);
        HMODULE game_base = GetModuleHandle(NULL);
        DWORD caller_rva = (DWORD)((uintptr_t)ret_addr - (uintptr_t)game_base);

        int total_len = 0;
        for (DWORD b = 0; b < dwBufferCount; b++) total_len += lpBuffers[b].len;

        fprintf(g_hook_log, "WSASend(%d bytes, %lu bufs) caller:+0x%X", total_len, dwBufferCount, caller_rva);

        // Dump first 64 bytes of first buffer
        const char* buf = lpBuffers[0].buf;
        int buf_len = lpBuffers[0].len;
        int dump_len = buf_len < 64 ? buf_len : 64;
        fprintf(g_hook_log, " data[%d]:", buf_len);
        for (int i = 0; i < dump_len; i++) {
            fprintf(g_hook_log, " %02X", (unsigned char)buf[i]);
        }
        if (buf_len > dump_len) fprintf(g_hook_log, " ...");
        fprintf(g_hook_log, "\n");
        fflush(g_hook_log);
    }
    return g_original_WSASend(s, lpBuffers, dwBufferCount, lpNumberOfBytesSent,
                               dwFlags, lpOverlapped, lpCompletionRoutine);
}

// ── XTEA constant scanner: find encryption function in game code ────
#define XTEA_DELTA 0x9E3779B9u

// Storage for found XTEA locations
static uintptr_t g_xtea_addrs[16];
static int        g_xtea_count = 0;
static uintptr_t  g_xtea_func_entry = 0;  // detected function entry point

// Find function entry by scanning backwards for prologue patterns
static uintptr_t find_func_entry(uintptr_t addr_in_func) {
    const uint8_t* p = (const uint8_t*)addr_in_func;
    // Scan backwards up to 2048 bytes looking for common MSVC function prologues
    for (int i = 1; i < 2048; i++) {
        const uint8_t* check = p - i;
        // push ebp / mov ebp, esp  (55 8B EC)
        if (check[0] == 0x55 && check[1] == 0x8B && check[2] == 0xEC) {
            // Must be preceded by alignment padding (CC), NOP (90), ret (C3), or null
            uint8_t prev = *(check - 1);
            if (prev == 0xCC || prev == 0x90 || prev == 0xC3 || prev == 0x00)
                return (uintptr_t)check;
            // Otherwise keep scanning — this is a false positive in the function body
        }
    }
    return 0;
}

static void scan_xtea_constant(void) {
    HMODULE game = GetModuleHandle(NULL);
    if (!game) { dbg("scan_xtea: no game module"); return; }

    PIMAGE_DOS_HEADER dos = (PIMAGE_DOS_HEADER)game;
    PIMAGE_NT_HEADERS nt = (PIMAGE_NT_HEADERS)((BYTE*)game + dos->e_lfanew);

    // Get .text section bounds (first code section)
    PIMAGE_SECTION_HEADER sec = IMAGE_FIRST_SECTION(nt);
    uintptr_t code_start = 0, code_end = 0;
    for (int i = 0; i < nt->FileHeader.NumberOfSections; i++) {
        if (sec[i].Characteristics & IMAGE_SCN_CNT_CODE) {
            code_start = (uintptr_t)game + sec[i].VirtualAddress;
            code_end = code_start + sec[i].Misc.VirtualSize;
            dbg("scan_xtea: code section '%s' at 0x%08X - 0x%08X (%u bytes)",
                sec[i].Name, (unsigned)code_start, (unsigned)code_end,
                sec[i].Misc.VirtualSize);
            break;
        }
    }

    if (!code_start) {
        code_start = (uintptr_t)game + 0x1000;
        code_end = (uintptr_t)game + nt->OptionalHeader.SizeOfImage;
        dbg("scan_xtea: no .text found, scanning full image");
    }

    g_xtea_count = 0;

    // Search for BOTH XTEA delta forms:
    //   0x9E3779B9 (standard delta, LE: B9 79 37 9E)
    //   0x61C88647 (negated delta used in some OT implementations, LE: 47 86 C8 61)
    // The OT client uses 0x61C88647 with SUB for encrypt (sum += delta)
    const uint8_t needle1[4] = { 0xB9, 0x79, 0x37, 0x9E };
    const uint8_t needle2[4] = { 0x47, 0x86, 0xC8, 0x61 };

    for (uintptr_t addr = code_start; addr + 4 <= code_end; addr++) {
        const uint8_t* p = (const uint8_t*)addr;
        BOOL match1 = (p[0] == needle1[0] && p[1] == needle1[1] && p[2] == needle1[2] && p[3] == needle1[3]);
        BOOL match2 = (p[0] == needle2[0] && p[1] == needle2[1] && p[2] == needle2[2] && p[3] == needle2[3]);

        if (!match1 && !match2) continue;

        DWORD rva = (DWORD)(addr - (uintptr_t)game);
        const char* delta_name = match1 ? "0x9E3779B9" : "0x61C88647";

        // Check the instruction that uses this constant
        // SUB reg, 0x61C88647 = encrypt (sum += delta) → opcode 81 EA/E9/...
        // ADD reg, 0x61C88647 = decrypt (sum -= delta) → opcode 81 C2/C1/...
        BOOL is_encrypt = FALSE;
        if (match2 && addr >= 2) {
            uint8_t op1 = *(uint8_t*)(addr - 2);
            uint8_t op2 = *(uint8_t*)(addr - 1);
            if (op1 == 0x81 && (op2 >= 0xE8 && op2 <= 0xEF)) {
                is_encrypt = TRUE;  // SUB reg, imm32 → encrypt
            }
        }

        dbg("XTEA delta %s at RVA +0x%08X (VA 0x%08X)%s",
            delta_name, rva, (unsigned)addr,
            is_encrypt ? " [ENCRYPT - SUB]" : "");

        uintptr_t entry = find_func_entry(addr);
        if (entry) {
            DWORD entry_rva = (DWORD)(entry - (uintptr_t)game);
            dbg("  function entry at RVA +0x%08X (VA 0x%08X)", entry_rva, (unsigned)entry);
        }

        if (g_xtea_count < 16) {
            g_xtea_addrs[g_xtea_count++] = addr;
        }

        // Prefer the encrypt function (SUB with 0x61C88647)
        if (is_encrypt && entry && !g_xtea_func_entry) {
            g_xtea_func_entry = entry;
            dbg(">>> Selected XTEA ENCRYPT function at VA 0x%08X (RVA +0x%08X)",
                (unsigned)entry, (unsigned)(entry - (uintptr_t)game));
        }
    }

    dbg("scan_xtea: total %d matches, encrypt func=%s",
        g_xtea_count, g_xtea_func_entry ? "FOUND" : "not found");
}

// ── Inline hook on XTEA encrypt to capture pre-encryption data ──────

// Trampoline: executable memory with saved original bytes + JMP back
static uint8_t* g_xtea_trampoline = NULL;
static uint8_t  g_xtea_saved[16];  // saved original bytes
static int      g_xtea_patch_len = 0;
static volatile BOOL g_xtea_hook_active = FALSE;
static FILE*    g_xtea_log = NULL;

// The XTEA encrypt function at VA 0x00675A60 takes 4 cdecl params:
//   [ebp+8]=p1, [ebp+C]=p2, [ebp+10]=p3, [ebp+14]=p4
// We log all params and any data they point to, then call the original.

// Lightweight XTEA hook — raw machine code, zero calling convention risk.
// Saves all regs, checks caller RVA, stores non-keepalive callers in a buffer.
// No fprintf in hot path. Pipe thread flushes buffer to log file.

#define KEEPALIVE_CALLER_RVA  0x19A4B5u
#define KEEPALIVE_CALLER_RVA2 0x8E938u
#define MAX_XTEA_CAPTURES 4096

struct XteaCapture {
    DWORD caller_rva;
    DWORD grandcaller_rva;
};
static volatile LONG g_xtea_write_idx = 0;
static XteaCapture g_xtea_captures[MAX_XTEA_CAPTURES];
static LONG g_xtea_read_idx = 0;  // read by pipe thread

// Flush captured callers to log (called from pipe thread, not from hook)
static void flush_xtea_captures(void) {
    if (!g_xtea_log) return;
    LONG write_idx = g_xtea_write_idx;
    while (g_xtea_read_idx < write_idx && g_xtea_read_idx < MAX_XTEA_CAPTURES) {
        XteaCapture* c = &g_xtea_captures[g_xtea_read_idx];
        fprintf(g_xtea_log, "XTEA caller:+0x%X grandcaller:+0x%X\n",
                c->caller_rva, c->grandcaller_rva);
        g_xtea_read_idx++;
    }
    if (g_xtea_read_idx > 0) fflush(g_xtea_log);
}

// Forward declarations for attack replay (defined later, used by XTEA cave)
static volatile uintptr_t g_protocol_this;    // captured ProtocolGame 'this'
static volatile uint32_t  g_attack_request;   // creature_id to attack (0 = no request)
static volatile LONG      g_attack_done;      // set to 1 when attack completes
static uint8_t* g_attack_trampoline;          // original attack bytes + JMP back
static volatile uintptr_t g_attack_caller_ret; // return addr of whoever calls sendAttackCreature
static volatile uintptr_t g_game_this = 0;        // captured Game object 'this' (EBX in targeting func)
static volatile uint32_t  g_last_attack_cid = 0;  // last creature_id passed to sendAttackCreature

// Forward declaration: game target updater called from XTEA hook cave (game thread)
static void __cdecl do_game_target_update(void);
static volatile LONG g_target_update_calls = 0;  // debug counter: how many times cave called us

// Build the hook code cave as raw x86 machine code.
// No filtering — captures ALL XTEA encrypt calls with caller + grandcaller RVA.
// pushad order: EAX ECX EDX EBX ESP EBP ESI EDI (pushed in that order, so on stack:
//   [ESP+0]=EDI [ESP+4]=ESI [ESP+8]=EBP [ESP+12]=ESP [ESP+16]=EBX
//   [ESP+20]=EDX [ESP+24]=ECX [ESP+28]=EAX [ESP+32]=EFLAGS [ESP+36]=retaddr)
static uint8_t* build_xtea_hook_cave(uintptr_t game_base, uint8_t* orig_bytes,
                                       int patch_len, uintptr_t jump_back_addr) {
    uint8_t* cave = (uint8_t*)VirtualAlloc(NULL, 512,
        MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!cave) return NULL;

    int p = 0;

    // pushfd
    cave[p++] = 0x9C;
    // pushad
    cave[p++] = 0x60;

    // --- Get caller RVA ---
    // mov eax, [esp+36]  — return address
    cave[p++] = 0x8B; cave[p++] = 0x44; cave[p++] = 0x24; cave[p++] = 36;
    // sub eax, game_base
    cave[p++] = 0x2D;
    memcpy(&cave[p], &game_base, 4); p += 4;

    // --- Get grandcaller RVA via EBP chain ---
    // mov ebx, [esp+8]   — saved EBP (caller's frame pointer)
    cave[p++] = 0x8B; cave[p++] = 0x5C; cave[p++] = 0x24; cave[p++] = 8;
    // mov ebx, [ebx+4]   — grandcaller return address ([EBP+4])
    cave[p++] = 0x8B; cave[p++] = 0x5B; cave[p++] = 0x04;
    // sub ebx, game_base
    cave[p++] = 0x81; cave[p++] = 0xEB;
    memcpy(&cave[p], &game_base, 4); p += 4;

    // --- Atomic alloc slot in ring buffer ---
    // push eax (save caller_rva)
    cave[p++] = 0x50;
    // push ebx (save grandcaller_rva)
    cave[p++] = 0x53;
    // mov ecx, 1
    cave[p++] = 0xB9; uint32_t one = 1; memcpy(&cave[p], &one, 4); p += 4;
    // lock xadd [g_xtea_write_idx], ecx
    cave[p++] = 0xF0; cave[p++] = 0x0F; cave[p++] = 0xC1;
    cave[p++] = 0x0D;
    uintptr_t write_idx_addr = (uintptr_t)&g_xtea_write_idx;
    memcpy(&cave[p], &write_idx_addr, 4); p += 4;
    // ecx = old index. Check < MAX_XTEA_CAPTURES
    cave[p++] = 0x81; cave[p++] = 0xF9;
    uint32_t max_cap = MAX_XTEA_CAPTURES;
    memcpy(&cave[p], &max_cap, 4); p += 4;
    // jge skip_full (buffer full)
    cave[p++] = 0x7D;
    int jge_offset_pos = p;
    cave[p++] = 0x00;  // placeholder

    // --- Store capture ---
    // pop ebx (grandcaller_rva)
    cave[p++] = 0x5B;
    // pop eax (caller_rva)
    cave[p++] = 0x58;
    // imul edx, ecx, 8  (each XteaCapture = 8 bytes)
    cave[p++] = 0x6B; cave[p++] = 0xD1; cave[p++] = 0x08;
    // add edx, &g_xtea_captures
    cave[p++] = 0x81; cave[p++] = 0xC2;
    uintptr_t captures_addr = (uintptr_t)&g_xtea_captures;
    memcpy(&cave[p], &captures_addr, 4); p += 4;
    // mov [edx], eax     (store caller_rva)
    cave[p++] = 0x89; cave[p++] = 0x02;
    // mov [edx+4], ebx   (store grandcaller_rva)
    cave[p++] = 0x89; cave[p++] = 0x5A; cave[p++] = 0x04;
    // jmp done
    cave[p++] = 0xEB;
    int jmp_done_pos = p;
    cave[p++] = 0x00;  // placeholder

    // skip_full: (buffer full, just pop saved regs)
    cave[jge_offset_pos] = (uint8_t)(p - jge_offset_pos - 1);
    cave[p++] = 0x5B;  // pop ebx
    cave[p++] = 0x58;  // pop eax

    // done:
    int done_target = p;
    cave[jmp_done_pos] = (uint8_t)(done_target - jmp_done_pos - 1);

    // --- Attack replay: triggered by g_attack_request ---
    // We're still inside pushad/pushfd, so all registers are saved.
    // The XTEA hook fires constantly (keepalive ~every 1-2s), making this
    // a reliable trigger for bot-initiated attacks.

    // mov eax, [g_attack_request]
    cave[p++] = 0xA1;
    uintptr_t atk_req_addr = (uintptr_t)&g_attack_request;
    memcpy(&cave[p], &atk_req_addr, 4); p += 4;
    // test eax, eax
    cave[p++] = 0x85; cave[p++] = 0xC0;
    // jz no_attack
    cave[p++] = 0x74;
    int jz_no_atk_pos = p;
    cave[p++] = 0x00;  // placeholder

    // Clear request BEFORE calling to prevent recursion via nested XTEA call
    // mov dword [g_attack_request], 0
    cave[p++] = 0xC7; cave[p++] = 0x05;
    memcpy(&cave[p], &atk_req_addr, 4); p += 4;
    uint32_t zero_v = 0;
    memcpy(&cave[p], &zero_v, 4); p += 4;

    // Check trampoline exists: mov edx, [g_attack_trampoline]
    cave[p++] = 0x8B; cave[p++] = 0x15;
    uintptr_t tramp_ptr_addr = (uintptr_t)&g_attack_trampoline;
    memcpy(&cave[p], &tramp_ptr_addr, 4); p += 4;
    // test edx, edx
    cave[p++] = 0x85; cave[p++] = 0xD2;
    // jz no_attack
    cave[p++] = 0x74;
    int jz_no_tramp_pos = p;
    cave[p++] = 0x00;  // placeholder

    // Check this ptr captured: mov ecx, [g_protocol_this]
    cave[p++] = 0x8B; cave[p++] = 0x0D;
    uintptr_t pthis_addr = (uintptr_t)&g_protocol_this;
    memcpy(&cave[p], &pthis_addr, 4); p += 4;
    // test ecx, ecx
    cave[p++] = 0x85; cave[p++] = 0xC9;
    // jz no_attack
    cave[p++] = 0x74;
    int jz_no_this_pos = p;
    cave[p++] = 0x00;  // placeholder

    // Call attack: __thiscall(ECX=this, creature_id, seq=0)
    // push 0 (sequence number)
    cave[p++] = 0x6A; cave[p++] = 0x00;
    // push eax (creature_id)
    cave[p++] = 0x50;
    // call edx (attack trampoline — original prologue + JMP back)
    cave[p++] = 0xFF; cave[p++] = 0xD2;
    // ret 8 in attack function cleaned our 2 pushes

    // Set done flag: mov dword [g_attack_done], 1
    cave[p++] = 0xC7; cave[p++] = 0x05;
    uintptr_t atk_done_addr = (uintptr_t)&g_attack_done;
    memcpy(&cave[p], &atk_done_addr, 4); p += 4;
    uint32_t one_v2 = 1;
    memcpy(&cave[p], &one_v2, 4); p += 4;

    // no_attack: (all jz targets converge here)
    int no_attack_target = p;
    cave[jz_no_atk_pos]   = (uint8_t)(no_attack_target - jz_no_atk_pos - 1);
    cave[jz_no_tramp_pos] = (uint8_t)(no_attack_target - jz_no_tramp_pos - 1);
    cave[jz_no_this_pos]  = (uint8_t)(no_attack_target - jz_no_this_pos - 1);

    // --- Call game target updater (visual targeting on game thread) ---
    // mov eax, <do_game_target_update>
    cave[p++] = 0xB8;
    uintptr_t update_fn_addr = (uintptr_t)&do_game_target_update;
    memcpy(&cave[p], &update_fn_addr, 4); p += 4;
    // call eax
    cave[p++] = 0xFF; cave[p++] = 0xD0;

    // popad
    cave[p++] = 0x61;
    // popfd
    cave[p++] = 0x9D;

    // Original prologue bytes
    memcpy(&cave[p], orig_bytes, patch_len);
    p += patch_len;

    // jmp back to original+patch_len
    cave[p++] = 0xE9;
    int32_t jmp_back = (int32_t)(jump_back_addr - (uintptr_t)&cave[p + 4]);
    memcpy(&cave[p], &jmp_back, 4); p += 4;

    dbg("  hook cave at %p, %d bytes, jumps back to %p", cave, p, (void*)jump_back_addr);
    return cave;
}

static BOOL install_xtea_hook(void) {
    if (!g_xtea_func_entry) {
        dbg("install_xtea_hook: no XTEA function found (run scan_xtea first)");
        return FALSE;
    }
    if (g_xtea_trampoline) {
        dbg("install_xtea_hook: already installed");
        return TRUE;
    }

    uint8_t* target = (uint8_t*)g_xtea_func_entry;
    HMODULE game_base = GetModuleHandle(NULL);

    g_xtea_patch_len = 5;  // minimum for JMP rel32

    if (target[0] != 0x55) {
        dbg("install_xtea_hook: unexpected prologue byte 0x%02X (expected 0x55=push ebp)", target[0]);
    }

    // Determine safe patch length: must overwrite complete instructions (>= 5 bytes)
    if (target[0] == 0x55 && target[1] == 0x8B && target[2] == 0xEC) {
        if (target[3] == 0x83 && target[4] == 0xEC) {
            g_xtea_patch_len = 6;  // push ebp + mov ebp,esp + sub esp,imm8
        } else if (target[3] == 0x81 && target[4] == 0xEC) {
            g_xtea_patch_len = 9;  // push ebp + mov ebp,esp + sub esp,imm32
        } else if (target[3] == 0x6A) {
            g_xtea_patch_len = 5;  // push ebp + mov ebp,esp + push imm8
        } else {
            g_xtea_patch_len = 5;
        }
    }

    dbg("install_xtea_hook: target=%p patch_len=%d game_base=%p",
        target, g_xtea_patch_len, game_base);
    dbg("  original bytes: %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X",
        target[0], target[1], target[2], target[3], target[4],
        target[5], target[6], target[7], target[8], target[9]);

    // Save original bytes
    memcpy(g_xtea_saved, target, g_xtea_patch_len);

    // Build hook cave: pushad/popad + ring buffer capture + original bytes + JMP back
    uintptr_t jump_back = (uintptr_t)(target + g_xtea_patch_len);
    g_xtea_trampoline = build_xtea_hook_cave((uintptr_t)game_base, g_xtea_saved,
                                               g_xtea_patch_len, jump_back);
    if (!g_xtea_trampoline) {
        dbg("install_xtea_hook: build_xtea_hook_cave FAILED");
        return FALSE;
    }

    // Patch the target: overwrite with JMP to our hook cave
    DWORD old_prot;
    VirtualProtect(target, g_xtea_patch_len, PAGE_EXECUTE_READWRITE, &old_prot);

    target[0] = 0xE9;  // JMP rel32
    int32_t jmp_to_cave = (int32_t)((uintptr_t)g_xtea_trampoline -
                                     (uintptr_t)(target + 5));
    memcpy(target + 1, &jmp_to_cave, 4);

    // NOP any remaining bytes
    for (int i = 5; i < g_xtea_patch_len; i++) {
        target[i] = 0x90;
    }

    VirtualProtect(target, g_xtea_patch_len, old_prot, &old_prot);
    FlushInstructionCache(GetCurrentProcess(), target, g_xtea_patch_len);

    dbg("install_xtea_hook: SUCCESS — raw x86 cave hook, zero calling convention risk");
    return TRUE;
}

static void open_xtea_log(void) {
    if (g_xtea_log) return;
    char path[MAX_PATH];
    int n = _snprintf(path, sizeof(path), "%s\\xtea_hook_log.txt", g_dll_dir);
    if (n < 0 || n >= (int)sizeof(path)) return;
    g_xtea_log = fopen(path, "a");
    if (g_xtea_log) {
        fprintf(g_xtea_log, "=== XTEA pre-encryption hook log ===\n");
        fflush(g_xtea_log);
    }
}

static BOOL install_send_hook(void) {
    HMODULE game = GetModuleHandle(NULL);
    if (!game) return FALSE;

    PIMAGE_DOS_HEADER dos = (PIMAGE_DOS_HEADER)game;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return FALSE;

    PIMAGE_NT_HEADERS nt = (PIMAGE_NT_HEADERS)((BYTE*)game + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) return FALSE;

    DWORD import_rva = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT].VirtualAddress;
    if (import_rva == 0) return FALSE;

    PIMAGE_IMPORT_DESCRIPTOR imports = (PIMAGE_IMPORT_DESCRIPTOR)((BYTE*)game + import_rva);

    for (; imports->Name; imports++) {
        const char* dll_name = (const char*)((BYTE*)game + imports->Name);
        if (_stricmp(dll_name, "ws2_32.dll") != 0 && _stricmp(dll_name, "wsock32.dll") != 0)
            continue;

        // Method 1: scan OriginalFirstThunk for WSASend by name
        PIMAGE_THUNK_DATA thunk = (PIMAGE_THUNK_DATA)((BYTE*)game + imports->FirstThunk);
        PIMAGE_THUNK_DATA orig  = imports->OriginalFirstThunk
            ? (PIMAGE_THUNK_DATA)((BYTE*)game + imports->OriginalFirstThunk)
            : thunk;

        for (; thunk->u1.Function; thunk++, orig++) {
            if (orig->u1.Ordinal & IMAGE_ORDINAL_FLAG) continue;

            PIMAGE_IMPORT_BY_NAME name_entry =
                (PIMAGE_IMPORT_BY_NAME)((BYTE*)game + orig->u1.AddressOfData);

            if (strcmp(name_entry->Name, "WSASend") == 0) {
                DWORD old_prot;
                VirtualProtect(&thunk->u1.Function, sizeof(void*), PAGE_READWRITE, &old_prot);
                g_original_WSASend = (WSASend_fn)thunk->u1.Function;
                thunk->u1.Function = (DWORD_PTR)hooked_WSASend;
                VirtualProtect(&thunk->u1.Function, sizeof(void*), old_prot, &old_prot);

                dbg("IAT hook installed (by name): WSASend() at %p", g_original_WSASend);
                return TRUE;
            }
        }

        // Method 2: find WSASend by matching its resolved address in the IAT
        HMODULE ws2 = GetModuleHandleA("ws2_32.dll");
        if (ws2) {
            FARPROC real_addr = GetProcAddress(ws2, "WSASend");
            if (real_addr) {
                dbg("  method2: real WSASend=%p, scanning FirstThunk...", real_addr);
                thunk = (PIMAGE_THUNK_DATA)((BYTE*)game + imports->FirstThunk);
                for (int idx = 0; thunk->u1.Function; thunk++, idx++) {
                    if ((FARPROC)thunk->u1.Function == real_addr) {
                        DWORD old_prot;
                        VirtualProtect(&thunk->u1.Function, sizeof(void*), PAGE_READWRITE, &old_prot);
                        g_original_WSASend = (WSASend_fn)thunk->u1.Function;
                        thunk->u1.Function = (DWORD_PTR)hooked_WSASend;
                        VirtualProtect(&thunk->u1.Function, sizeof(void*), old_prot, &old_prot);

                        dbg("IAT hook installed (by addr): WSASend() idx=%d", idx);
                        return TRUE;
                    }
                }
            }
        }
    }

    dbg("IAT hook FAILED: could not find WSASend() in any import table");
    return FALSE;
}

static void open_hook_log(void) {
    if (g_hook_log) return;
    char path[MAX_PATH];
    int n = _snprintf(path, sizeof(path), "%s\\send_hook_log.txt", g_dll_dir);
    if (n < 0 || n >= (int)sizeof(path)) return;
    g_hook_log = fopen(path, "a");
    if (g_hook_log) {
        fprintf(g_hook_log, "=== send() IAT hook log ===\n");
        fflush(g_hook_log);
    }
}

// ── Attack function hook: capture 'this', replay attacks on game thread ──

// OFF_SEND_ATTACK_RVA now uses OFF_SEND_ATTACK_RVA from offsets config

// g_protocol_this, g_attack_request, g_attack_done, g_attack_trampoline
// are forward-declared above (before XTEA cave builder)
static uint8_t* g_attack_cave = NULL;
static uint8_t  g_attack_saved[8];
static int      g_attack_patch_len = 0;

// The hook cave runs on the GAME THREAD (inside the attack function).
// It captures ECX (this ptr) every time, and if g_attack_request is set,
// it replays an extra attack call using the captured this pointer.
//
// Flow: game calls attack(this, creature_id, seq)
//   → JMP to cave
//   → cave stores ECX to g_protocol_this
//   → cave checks g_attack_request
//   → if set: pushes request args, calls trampoline (original func), clears request
//   → runs original prologue bytes
//   → JMP back to original+5
//
// This ensures the attack call happens on the correct thread.

static uint8_t* build_attack_hook(uint8_t* orig_bytes, int patch_len,
                                   uintptr_t jump_back_addr) {
    // Build trampoline: original bytes + JMP back (used by XTEA cave for replays)
    g_attack_trampoline = (uint8_t*)VirtualAlloc(NULL, 32,
        MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!g_attack_trampoline) return NULL;

    int tp = 0;
    memcpy(&g_attack_trampoline[tp], orig_bytes, patch_len); tp += patch_len;
    g_attack_trampoline[tp++] = 0xE9;
    int32_t tramp_jmp = (int32_t)(jump_back_addr - (uintptr_t)&g_attack_trampoline[tp + 4]);
    memcpy(&g_attack_trampoline[tp], &tramp_jmp, 4); tp += 4;

    // Minimal hook cave: capture ECX (ProtocolGame this), EBX (Game this),
    // EAX (creature_id), and caller return address.
    // Attack replay is triggered from the XTEA hook cave instead (fires constantly).
    uint8_t* cave = (uint8_t*)VirtualAlloc(NULL, 64,
        MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!cave) return NULL;

    int p = 0;
    // mov [g_protocol_this], ecx  (6 bytes, no flags modified)
    cave[p++] = 0x89; cave[p++] = 0x0D;
    uintptr_t this_addr = (uintptr_t)&g_protocol_this;
    memcpy(&cave[p], &this_addr, 4); p += 4;

    // mov [g_game_this], ebx  (6 bytes — EBX = Game 'this' in targeting func)
    cave[p++] = 0x89; cave[p++] = 0x1D;
    uintptr_t game_this_addr = (uintptr_t)&g_game_this;
    memcpy(&cave[p], &game_this_addr, 4); p += 4;

    // mov [g_last_attack_cid], eax  (5 bytes — EAX = creature_id at call site)
    cave[p++] = 0xA3;
    uintptr_t last_cid_addr = (uintptr_t)&g_last_attack_cid;
    memcpy(&cave[p], &last_cid_addr, 4); p += 4;

    // Capture caller return address: push eax; mov eax,[esp+4]; mov [g_attack_caller_ret],eax; pop eax
    cave[p++] = 0x50;  // push eax
    cave[p++] = 0x8B; cave[p++] = 0x44; cave[p++] = 0x24; cave[p++] = 0x04;  // mov eax,[esp+4]
    cave[p++] = 0xA3;  // mov [g_attack_caller_ret], eax
    uintptr_t caller_addr = (uintptr_t)&g_attack_caller_ret;
    memcpy(&cave[p], &caller_addr, 4); p += 4;
    cave[p++] = 0x58;  // pop eax

    // Original prologue bytes
    memcpy(&cave[p], orig_bytes, patch_len);
    p += patch_len;

    // JMP back to original+patch_len
    cave[p++] = 0xE9;
    int32_t jmp_back = (int32_t)(jump_back_addr - (uintptr_t)&cave[p + 4]);
    memcpy(&cave[p], &jmp_back, 4); p += 4;

    dbg("  attack cave at %p, %d bytes (ECX+EBX+EAX capture), trampoline at %p",
        cave, p, g_attack_trampoline);
    return cave;
}

static BOOL install_attack_hook(void) {
    if (g_attack_cave) {
        dbg("install_attack_hook: already installed");
        return TRUE;
    }

    HMODULE game = GetModuleHandle(NULL);
    uint8_t* target = (uint8_t*)((uintptr_t)game + OFF_SEND_ATTACK_RVA);

    dbg("install_attack_hook: target=%p", target);
    dbg("  bytes: %02X %02X %02X %02X %02X %02X %02X %02X",
        target[0], target[1], target[2], target[3],
        target[4], target[5], target[6], target[7]);

    // Handle case where old DLL already patched the prologue with a JMP
    if (target[0] == 0xE9) {
        dbg("install_attack_hook: target already patched by old DLL (E9 JMP), restoring original prologue");
        static const uint8_t orig_prologue[] = {0x55, 0x8B, 0xEC, 0x6A, 0xFF};
        DWORD old_prot;
        VirtualProtect(target, 5, PAGE_EXECUTE_READWRITE, &old_prot);
        memcpy(target, orig_prologue, 5);
        VirtualProtect(target, 5, old_prot, &old_prot);
        FlushInstructionCache(GetCurrentProcess(), target, 5);
        dbg("  restored: %02X %02X %02X %02X %02X",
            target[0], target[1], target[2], target[3], target[4]);
    }

    if (target[0] != 0x55 || target[1] != 0x8B || target[2] != 0xEC) {
        dbg("install_attack_hook: unexpected prologue!");
        return FALSE;
    }
    g_attack_patch_len = 5;  // push ebp + mov ebp,esp + push -1

    memcpy(g_attack_saved, target, g_attack_patch_len);

    uintptr_t jump_back = (uintptr_t)(target + g_attack_patch_len);
    g_attack_cave = build_attack_hook(g_attack_saved, g_attack_patch_len, jump_back);
    if (!g_attack_cave) {
        dbg("install_attack_hook: cave alloc failed");
        return FALSE;
    }

    DWORD old_prot;
    VirtualProtect(target, g_attack_patch_len, PAGE_EXECUTE_READWRITE, &old_prot);
    target[0] = 0xE9;
    int32_t jmp_to_cave = (int32_t)((uintptr_t)g_attack_cave - (uintptr_t)(target + 5));
    memcpy(target + 1, &jmp_to_cave, 4);
    VirtualProtect(target, g_attack_patch_len, old_prot, &old_prot);
    FlushInstructionCache(GetCurrentProcess(), target, g_attack_patch_len);

    dbg("install_attack_hook: SUCCESS — ECX+EBX+EAX capture (replay via XTEA hook)");
    return TRUE;
}

// ── Game object targeting: write to Game singleton to trigger in-game attack ──

// All offsets now come from OFF_* variables (loaded via "set_offsets" pipe command)

static inline bool is_valid_creature_vtable(uint32_t vtable) {
    uintptr_t base = (uintptr_t)GetModuleHandle(NULL);
    uintptr_t rva = (uintptr_t)vtable - base;
    return rva >= OFF_VTABLE_RVA_MIN && rva < OFF_VTABLE_RVA_MAX;
}

// Function pointer type for calling Game::attack via __thiscall convention.
// ECX = Game* (this), stack param = const CreaturePtr& (pointer to 4-byte Creature*).
// The callee cleans the stack (ret 4).
typedef void (__attribute__((thiscall)) *Game_attack_fn)(void* game_this, const void* creature_ref);

// Pending target: set by pipe thread, consumed by XTEA hook on game thread
static volatile uintptr_t g_pending_creature_ptr = 0;
static volatile uint32_t  g_pending_creature_id  = 0;
static volatile LONG g_pending_game_attack = 0;

// Cache: last successfully found Creature*
static uint32_t  g_cached_target_cid = 0;
static uintptr_t g_cached_target_ptr = 0;

// Forward declaration (defined after the g_map scanning section)
static uintptr_t find_creature_in_map(uint32_t creature_id);

static uintptr_t find_creature_ptr(uint32_t creature_id) {
    // Check cache first
    if (g_cached_target_cid == creature_id && g_cached_target_ptr) {
        uint32_t check_vtable = 0, check_id = 0;
        if (safe_memcpy(&check_vtable, (void*)g_cached_target_ptr, 4) &&
            safe_memcpy(&check_id, (void*)(g_cached_target_ptr + OFF_CREATURE_ID), 4) &&
            is_valid_creature_vtable(check_vtable) &&
            check_id == creature_id) {
            return g_cached_target_ptr;
        }
        g_cached_target_cid = 0;
        g_cached_target_ptr = 0;
    }

    // Check creature map tree (O(log n) — instant).
    // Guarded by setjmp: tree walk can AV on stale pointers during floor
    // changes or heavy creature churn.  VEH longjmp's back here on crash.
    if (g_map_addr) {
        g_scan_recovery = TRUE;
        if (setjmp(g_scan_jmpbuf) != 0) {
            // VEH recovered from AV during tree search — fall through to cache
            g_scan_recovery = FALSE;
            dbg("find_creature_ptr: VEH recovered from AV searching for 0x%08X", creature_id);
        } else {
            uintptr_t map_result = find_creature_in_map(creature_id);
            g_scan_recovery = FALSE;
            if (map_result) {
                g_cached_target_cid = creature_id;
                g_cached_target_ptr = map_result;
                dbg("find_creature_ptr: 0x%08X -> map tree, Creature* %p",
                    creature_id, (void*)map_result);
                return map_result;
            }
        }
    }

    // Check creature scan cache (g_addrs) — avoids expensive heap scan
    for (int i = 0; i < g_addr_count; i++) {
        if (g_addrs[i].id == creature_id) {
            uintptr_t obj_addr = (uintptr_t)g_addrs[i].addr - OFF_CREATURE_ID;
            uint32_t vtable = 0;
            if (safe_memcpy(&vtable, (void*)obj_addr, 4) &&
                is_valid_creature_vtable(vtable)) {
                g_cached_target_cid = creature_id;
                g_cached_target_ptr = obj_addr;
                dbg("find_creature_ptr: 0x%08X -> scan cache, Creature* %p vtable=0x%08X",
                    creature_id, (void*)obj_addr, vtable);
                return obj_addr;
            }
        }
    }

    // LEGACY FALLBACK: Full heap scan — rarely triggers now that map tree walk
    // (find_creature_in_map) is the primary lookup.  Only reached if the creature
    // is absent from both the map tree and the scan cache (g_addrs).
    // Consider removing if map scan proves fully reliable.
    // Scan heap page-by-page using safe_memcpy (never dereference directly)
    MEMORY_BASIC_INFORMATION mbi;
    uintptr_t addr = 0x10000;

    while (addr < 0x7FFE0000u) {
        if (VirtualQuery((void*)addr, &mbi, sizeof(mbi)) == 0) break;
        uintptr_t rstart = (uintptr_t)mbi.BaseAddress;
        uintptr_t rend = rstart + mbi.RegionSize;

        if (mbi.State == MEM_COMMIT &&
            (mbi.Protect == PAGE_READWRITE || mbi.Protect == PAGE_EXECUTE_READWRITE) &&
            mbi.RegionSize >= 0x60) {

            // Read page-by-page with safe_memcpy
            for (uintptr_t page = rstart; page < rend; page += 4096) {
                uint8_t buf[4096];
                uintptr_t page_end = page + 4096;
                if (page_end > rend) page_end = rend;
                size_t page_sz = page_end - page;
                if (page_sz < 0x60) continue;
                if (!safe_memcpy(buf, (void*)page, page_sz)) continue;

                // Search this page buffer for creature_id
                int max_off = (int)(page_sz - 4);
                for (int off = 0; off <= max_off; off += 4) {
                    uint32_t val;
                    memcpy(&val, &buf[off], 4);
                    if (val != creature_id) continue;

                    uintptr_t cid_addr = page + off;
                    uintptr_t obj_addr = cid_addr - OFF_CREATURE_ID;
                    if (obj_addr < 0x10000) continue;

                    uint32_t vtable;
                    if (!safe_memcpy(&vtable, (void*)obj_addr, 4)) continue;
                    if (is_valid_creature_vtable(vtable)) {
                        g_cached_target_cid = creature_id;
                        g_cached_target_ptr = obj_addr;
                        dbg("find_creature_ptr: 0x%08X -> Creature* %p vtable=0x%08X",
                            creature_id, (void*)obj_addr, vtable);
                        return obj_addr;
                    }
                }
            }
        }
        addr = rend;
    }
    return 0;
}

// Called from XTEA hook cave on the GAME THREAD.
// Calls Game::attack(const CreaturePtr&) at RVA +0x8F220 to trigger
// in-game targeting (red square, battle list, follow, attack packet).
// GAME_ATTACK_RVA now uses OFF_GAME_ATTACK_RVA from offsets config

static volatile uint32_t g_last_attack_target_cid = 0; // track what we last attacked

static void __cdecl do_game_target_update(void) {
    // Fast path: nothing pending — just return immediately
    if (!g_pending_game_attack)
        return;

    if (!InterlockedExchange(&g_pending_game_attack, 0))
        return;

    uint32_t cid = g_pending_creature_id;
    g_pending_creature_ptr = 0;
    g_pending_creature_id  = 0;
    if (!cid) return;

    // ── Fix 11: Skip attack if map is unstable ──
    // If a scan thread AV, attack AV, or big creature count change happened
    // recently, the creature map is in flux and Game::attack is likely to AV
    // (which corrupts Lua state via longjmp recovery).  Better to skip.
    {
        DWORD now = GetTickCount();
        DWORD scan_av = g_last_scan_av_tick;
        DWORD atk_av  = g_last_attack_av_tick;
        DWORD count_chg = g_last_count_change_tick;
        if (scan_av && (now - scan_av) < MAP_STABILITY_COOLDOWN_MS) {
            dbg("[GTUPD] SKIP attack 0x%08X — map unstable (scan AV %ums ago)", cid, now - scan_av);
            g_last_attack_target_cid = 0;
            return;
        }
        if (atk_av && (now - atk_av) < MAP_STABILITY_COOLDOWN_MS) {
            dbg("[GTUPD] SKIP attack 0x%08X — map unstable (attack AV %ums ago)", cid, now - atk_av);
            g_last_attack_target_cid = 0;
            return;
        }
        if (count_chg && (now - count_chg) < COUNT_CHANGE_COOLDOWN_MS) {
            dbg("[GTUPD] SKIP attack 0x%08X — map unstable (count change %ums ago)", cid, now - count_chg);
            g_last_attack_target_cid = 0;
            return;
        }
    }

    // ── Fix 7: Re-lookup Creature* on game thread ──
    // The pipe thread found a Creature* earlier, but by the time WndProc
    // fires (~16ms later), the creature may have been freed/moved.
    // Re-finding on the game thread eliminates the race: the game thread
    // owns the creature map so it can't be modified mid-lookup.
    uintptr_t creature_ptr = 0;
    if (g_map_addr) {
        creature_ptr = find_creature_in_map(cid);
    }
    // Fallback to pipe thread's cached pointer if map lookup fails
    if (!creature_ptr) {
        creature_ptr = g_cached_target_ptr;
        if (g_cached_target_cid != cid)
            creature_ptr = 0;
    }
    if (!creature_ptr) {
        dbg("[GTUPD] Creature* not found for 0x%08X on game thread", cid);
        return;
    }

    // Validate the fresh pointer
    uint32_t vtable = 0, read_cid = 0, hp = 0;
    if (!safe_memcpy(&vtable, (void*)creature_ptr, 4) ||
        !safe_memcpy(&read_cid, (void*)(creature_ptr + OFF_CREATURE_ID), 4) ||
        !safe_memcpy(&hp, (void*)(creature_ptr + OFF_CREATURE_HP), 4))
        return;
    if (!is_valid_creature_vtable(vtable) || read_cid != cid || hp == 0 || hp > 100) {
        dbg("[GTUPD] stale Creature* %p for 0x%08X (vtable=%08X cid=%08X hp=%u)",
            (void*)creature_ptr, cid, vtable, read_cid, hp);
        g_cached_target_cid = 0;
        g_cached_target_ptr = 0;
        return;
    }

    HMODULE game_mod = GetModuleHandle(NULL);
    uintptr_t base = (uintptr_t)game_mod;
    uintptr_t game_obj = base + OFF_GAME_SINGLETON_RVA;
    uintptr_t func_addr = base + OFF_GAME_ATTACK_RVA;

    // Check if game still has our target — if game cleared it (Z change, etc.), re-target
    if (cid == g_last_attack_target_cid) {
        uintptr_t cur = 0;
        safe_memcpy(&cur, (void*)(game_obj + OFF_GAME_ATTACKING), 4);
        if (cur != 0) return;  // game still targeting something, skip
        // Game cleared the target — re-send
        dbg("[GTUPD] re-target 0x%08X (game cleared target)", cid);
    }

    dbg("[GTUPD] Game::attack(&%p) id=0x%08X hp=%u", (void*)creature_ptr, cid, hp);

    // ── Fix 7+9: Safe Game::attack() call ──
    // Fix 7: Re-lookup on game thread prevents stale pointers (above).
    // Fix 9: VEH-based catch for MSVC C++ exceptions (0xE06D7363).
    //   MinGW's try/catch can't catch MSVC exceptions (incompatible ABI).
    //   Instead, arm setjmp + VEH handler to longjmp on 0xE06D7363.
    //   Safe because Lua cleans up its state BEFORE throwing the C++ exception,
    //   unlike an AV mid-Lua-call where longjmp corrupts Lua state.
    //   AVs (0xC0000005) are NOT caught here — let them crash cleanly.

    g_attack_thread_id = GetCurrentThreadId();
    g_attack_recovery = TRUE;
    if (setjmp(g_attack_jmpbuf) != 0) {
        // VEH caught a Lua C++ exception during Game::attack/sendAttackCreature
        dbg("[GTUPD] VEH caught Lua exception during Game::attack for 0x%08X — swallowed", cid);
        g_last_attack_target_cid = 0;
        return;
    }

    // 1. Call Game::attack for UI (red square, battle list, Lua callback)
    uintptr_t creature_ref = creature_ptr;
    typedef void (__attribute__((thiscall)) *Game_attack_fn)(void* game_this, uintptr_t* creature_ref);
    Game_attack_fn attack_fn = (Game_attack_fn)func_addr;
    attack_fn((void*)game_obj, &creature_ref);

    // 2. Call sendAttackCreature for network (actual combat + follow).
    //    In DBVictory, Game::attack() only updates UI — it does NOT send
    //    the network packet internally (unlike standard OTClient).
    //    This explicit call is required for combat to work and for the
    //    proxy to see ATTACK packets (used by auto_rune, etc.).
    uintptr_t proto = 0;
    safe_memcpy(&proto, (void*)(game_obj + OFF_GAME_PROTOCOL), 4);
    if (proto > 0x10000) {
        volatile uint32_t* seq_ptr = (volatile uint32_t*)(game_obj + OFF_GAME_SEQ);
        uint32_t seq = InterlockedIncrement((volatile LONG*)seq_ptr);

        typedef void (__attribute__((thiscall)) *SendAttack_fn)(void* proto_this, uint32_t creature_id, uint32_t seq);
        SendAttack_fn send_fn = (SendAttack_fn)(base + OFF_SEND_ATTACK_RVA);
        send_fn((void*)proto, cid, seq);
        dbg("[GTUPD] sendAttackCreature(0x%08X, seq=%u) via protocol=%p", cid, seq, (void*)proto);
    } else {
        dbg("[GTUPD] no protocol — skipped sendAttackCreature");
    }

    g_attack_recovery = FALSE;  // disarm

    g_last_attack_target_cid = cid;
    dbg("[GTUPD] target locked 0x%08X", cid);
}

// Request an attack (called from pipe thread).
// Finds the Creature* via scan cache / heap scan and queues for game thread.
static void request_game_attack(uint32_t creature_id) {
    // Skip if already attacking this creature (unless game cleared it)
    if (creature_id == g_last_attack_target_cid) {
        HMODULE gm = GetModuleHandle(NULL);
        uintptr_t go = (uintptr_t)gm + OFF_GAME_SINGLETON_RVA;
        uintptr_t cur = 0;
        safe_memcpy(&cur, (void*)(go + OFF_GAME_ATTACKING), 4);
        if (cur != 0) return;  // still targeting, skip
        // Game cleared target — allow re-queue
    }

    // Find the Creature* object in game memory
    uintptr_t creature_ptr = find_creature_ptr(creature_id);
    if (!creature_ptr) {
        dbg("[GATK] Creature* not found for 0x%08X", creature_id);
        return;
    }

    // Quick validation before queuing
    uint32_t vtable = 0, hp = 0;
    if (!safe_memcpy(&vtable, (void*)creature_ptr, 4) ||
        !safe_memcpy(&hp, (void*)(creature_ptr + OFF_CREATURE_HP), 4))
        return;
    if (!is_valid_creature_vtable(vtable) || hp == 0 || hp > 100) {
        g_cached_target_cid = 0;
        g_cached_target_ptr = 0;
        return;
    }

    dbg("[GATK] new target 0x%08X -> Creature* %p hp=%u", creature_id, (void*)creature_ptr, hp);

    // Queue for game thread (pass both ID and ptr — game thread will re-lookup)
    g_pending_creature_id  = creature_id;
    g_pending_creature_ptr = creature_ptr;
    InterlockedExchange(&g_pending_game_attack, 1);

    // Trigger immediate execution via WndProc hook (~16ms) instead of
    // waiting for next XTEA hook fire (~1s). XTEA hook remains as backup.
    if (g_wndproc_hooked && g_game_hwnd) {
        PostMessage(g_game_hwnd, WM_BOT_TARGET, 0, 0);
    }
}

// ── Creature map (g_map) scanning ───────────────────────────────────
// MSVC std::map<uint32, CreaturePtr> uses a red-black tree:
//   Map header:  +0x00 = sentinel node*, +0x04 = element count
//   Tree node:   +0x00 = left*, +0x04 = parent*, +0x08 = right*,
//                +0x0C = color(1), +0x0D = isnil(1), +0x0E = pad(2),
//                +0x10 = key (creature_id), +0x14 = Creature*

// Validate that a pointer looks like an MSVC std::map sentinel node:
// sentinel->isnil == 1, and left/right/parent are readable pointers.
static BOOL validate_map_sentinel(uintptr_t sentinel_addr) {
    uint8_t buf[16];
    if (!safe_memcpy(buf, (void*)sentinel_addr, 16)) return FALSE;

    // Check isnil byte at +0x0D
    if (buf[0x0D] != 1) return FALSE;

    // Check left/parent/right are readable pointers
    uintptr_t left, parent, right;
    memcpy(&left,   buf + 0x00, 4);
    memcpy(&parent, buf + 0x04, 4);
    memcpy(&right,  buf + 0x08, 4);

    if (left   < 0x10000 || left   >= 0x7FFE0000u) return FALSE;
    if (parent < 0x10000 || parent >= 0x7FFE0000u) return FALSE;
    if (right  < 0x10000 || right  >= 0x7FFE0000u) return FALSE;

    return TRUE;
}

// Validate that a std::map looks like it contains creatures.
// Checks: sentinel valid, count > 0, first few nodes contain valid creature IDs.
static BOOL validate_creature_map(uintptr_t map_addr, int expected_min) {
    uint8_t hdr[8];
    if (!safe_memcpy(hdr, (void*)map_addr, 8)) return FALSE;

    uintptr_t sentinel;
    uint32_t count;
    memcpy(&sentinel, hdr, 4);
    memcpy(&count, hdr + 4, 4);

    if (count == 0 || count > 500) return FALSE;  // sanity check
    if (!validate_map_sentinel(sentinel)) return FALSE;

    // Walk first 3 nodes and check they have valid creature IDs
    // Start from sentinel->left (smallest key = leftmost node)
    uintptr_t node;
    if (!safe_memcpy(&node, (void*)(sentinel + 0x00), 4)) return FALSE;
    if (node == sentinel) return FALSE;  // empty tree

    int valid_count = 0;
    for (int i = 0; i < 3 && node != sentinel; i++) {
        uint8_t nbuf[0x18];
        if (!safe_memcpy(nbuf, (void*)node, 0x18)) break;

        uint8_t isnil = nbuf[0x0D];
        if (isnil) break;  // hit sentinel

        uint32_t key;
        memcpy(&key, nbuf + 0x10, 4);
        if (key >= MIN_CREATURE_ID && key < MAX_CREATURE_ID)
            valid_count++;

        // In-order successor: if right child exists, go to leftmost of right subtree
        uintptr_t right_child;
        memcpy(&right_child, nbuf + 0x08, 4);
        if (right_child != sentinel) {
            node = right_child;
            // Go leftmost
            for (int safety = 0; safety < 500; safety++) {
                uintptr_t lc;
                if (!safe_memcpy(&lc, (void*)(node + 0x00), 4)) break;
                if (lc == sentinel) break;
                node = lc;
            }
        } else {
            // Go up until we come from a left child
            uintptr_t parent_node;
            memcpy(&parent_node, nbuf + 0x04, 4);
            uintptr_t cur = node;
            node = parent_node;
            for (int safety = 0; safety < 500; safety++) {
                if (node == sentinel) break;
                uintptr_t n_right;
                if (!safe_memcpy(&n_right, (void*)(node + 0x08), 4)) { node = sentinel; break; }
                if (n_right != cur) break;  // came from left child
                cur = node;
                if (!safe_memcpy(&node, (void*)(node + 0x04), 4)) { node = sentinel; break; }
            }
        }
    }

    return valid_count >= 1;
}

// Scan Game::attack function code to find g_map address by looking for
// MOV/LEA instructions that reference global variables, then checking
// if any of those globals is a valid creature std::map.
static void scan_gmap(void) {
    HMODULE game = GetModuleHandle(NULL);
    uintptr_t base = (uintptr_t)game;
    uintptr_t func_addr = base + OFF_GAME_ATTACK_RVA;

    dbg("[GMAP] scanning for g_map from Game::attack at VA 0x%08X...", (unsigned)func_addr);

    // Read 512 bytes of the function
    uint8_t code[512];
    if (!safe_memcpy(code, (void*)func_addr, 512)) {
        dbg("[GMAP] failed to read Game::attack code");
        return;
    }

    // Extract absolute addresses from MOV/LEA instructions
    // Common patterns: 8B 0D XX XX XX XX  (mov ecx, [addr])
    //                  8B 15 XX XX XX XX  (mov edx, [addr])
    //                  A1 XX XX XX XX     (mov eax, [addr])
    //                  8D 05 XX XX XX XX  (lea eax, [addr])
    //                  B8 XX XX XX XX     (mov eax, imm32) — could be a global addr
    uintptr_t candidates[64];
    int ncand = 0;

    for (int i = 0; i < 512 - 6 && ncand < 64; i++) {
        uintptr_t addr = 0;

        if (code[i] == 0xA1) {
            // mov eax, [imm32]
            memcpy(&addr, &code[i+1], 4);
            i += 4;
        } else if (code[i] == 0x8B && (code[i+1] & 0xC7) == 0x05) {
            // mov reg, [imm32] — 8B mod=00 r/m=101 (disp32)
            memcpy(&addr, &code[i+2], 4);
            i += 5;
        } else if (code[i] == 0x8B && (code[i+1] & 0xC7) == 0x0D) {
            // mov reg, [imm32] — 8B 0D pattern
            memcpy(&addr, &code[i+2], 4);
            i += 5;
        } else if (code[i] == 0x8B && (code[i+1] & 0xC7) == 0x15) {
            // mov reg, [imm32] — 8B 15 pattern
            memcpy(&addr, &code[i+2], 4);
            i += 5;
        } else if (code[i] == 0x8D && (code[i+1] & 0xC7) == 0x05) {
            // lea reg, [imm32]
            memcpy(&addr, &code[i+2], 4);
            i += 5;
        } else if (code[i] == 0x8D && (code[i+1] & 0xC7) == 0x0D) {
            // lea reg, [imm32]
            memcpy(&addr, &code[i+2], 4);
            i += 5;
        } else if (code[i] == 0xB8 || code[i] == 0xB9 || code[i] == 0xBB) {
            // mov eax/ecx/ebx, imm32
            memcpy(&addr, &code[i+1], 4);
            i += 4;
        } else if (code[i] == 0x68) {
            // push imm32
            memcpy(&addr, &code[i+1], 4);
            i += 4;
        }

        if (addr >= 0x10000 && addr < 0x7FFE0000u) {
            // Dedup
            BOOL dup = FALSE;
            for (int j = 0; j < ncand; j++) {
                if (candidates[j] == addr) { dup = TRUE; break; }
            }
            if (!dup) {
                candidates[ncand++] = addr;
            }
        }
    }

    dbg("[GMAP] found %d candidate addresses in Game::attack", ncand);

    // Also scan a range around each candidate (maps might be at addr or addr+offset)
    int expected_creature_count = g_addr_count;  // from VirtualQuery scan
    dbg("[GMAP] current VQ scan has %d creatures for cross-check", expected_creature_count);

    for (int i = 0; i < ncand; i++) {
        uintptr_t cand = candidates[i];
        // Try the address directly as a map header
        // Also try reading it as a pointer-to-map
        uintptr_t try_addrs[3] = { cand, 0, 0 };
        int ntry = 1;

        // Also dereference it (pointer to map)
        uintptr_t deref = 0;
        if (safe_memcpy(&deref, (void*)cand, 4) && deref >= 0x10000 && deref < 0x7FFE0000u) {
            try_addrs[ntry++] = deref;
        }

        for (int t = 0; t < ntry; t++) {
            uintptr_t try_addr = try_addrs[t];
            if (validate_creature_map(try_addr, expected_creature_count > 0 ? 1 : 0)) {
                uint32_t count = 0;
                safe_memcpy(&count, (void*)(try_addr + 4), 4);
                dbg("[GMAP] FOUND creature map at 0x%08X (count=%u) via candidate 0x%08X%s",
                    (unsigned)try_addr, count, (unsigned)cand,
                    t == 0 ? " (direct)" : " (deref)");
                g_map_addr = try_addr;
                return;
            }
        }
    }

    // Broader scan: check globals in writable data sections only (.data, .bss)
    // IMPORTANT: Skip .rdata — it contains read-only constants; dereferencing
    // arbitrary values there as pointers causes access violations.
    dbg("[GMAP] no map found in Game::attack refs, scanning writable sections...");
    PIMAGE_DOS_HEADER dos = (PIMAGE_DOS_HEADER)game;
    PIMAGE_NT_HEADERS nt = (PIMAGE_NT_HEADERS)((BYTE*)game + dos->e_lfanew);
    PIMAGE_SECTION_HEADER sec = IMAGE_FIRST_SECTION(nt);
    for (int s = 0; s < nt->FileHeader.NumberOfSections; s++) {
        // Only scan writable data sections (.data, .bss)
        if (!(sec[s].Characteristics & IMAGE_SCN_MEM_WRITE)) continue;
        if (sec[s].Characteristics & IMAGE_SCN_CNT_CODE) continue;
        uintptr_t sec_start = base + sec[s].VirtualAddress;
        uintptr_t sec_end = sec_start + sec[s].Misc.VirtualSize;
        dbg("[GMAP] scanning section '%s' (0x%08X - 0x%08X)...",
            sec[s].Name, (unsigned)sec_start, (unsigned)sec_end);

        // Pre-filter: only check addresses that look like a plausible map header
        // (first 4 bytes = pointer in heap range, next 4 = small count 1..500)
        for (uintptr_t addr = sec_start; addr + 8 <= sec_end; addr += 4) {
            uint8_t peek[8];
            if (!safe_memcpy(peek, (void*)addr, 8)) continue;
            uintptr_t sentinel_candidate;
            uint32_t count_candidate;
            memcpy(&sentinel_candidate, peek, 4);
            memcpy(&count_candidate, peek + 4, 4);
            // Quick reject: sentinel must be a heap pointer, count must be 1..500
            if (sentinel_candidate < 0x10000 || sentinel_candidate >= 0x7FFE0000u) continue;
            if (count_candidate == 0 || count_candidate > 500) continue;

            if (validate_creature_map(addr, expected_creature_count > 0 ? 1 : 0)) {
                dbg("[GMAP] FOUND creature map at 0x%08X (count=%u) in section '%s'",
                    (unsigned)addr, count_candidate, sec[s].Name);
                g_map_addr = addr;
                return;
            }
        }
    }
    dbg("[GMAP] creature map NOT FOUND");
}

// Walk the creature map tree and populate g_addrs[].
// Returns the number of creatures found.
static int walk_creature_map_inner(void) {
    if (!g_map_addr) return -1;

    uint8_t hdr[8];
    if (!safe_memcpy(hdr, (void*)g_map_addr, 8)) return -1;

    uintptr_t sentinel;
    uint32_t count;
    memcpy(&sentinel, hdr, 4);
    memcpy(&count, hdr + 4, 4);

    // Validate sentinel still looks correct
    if (count == 0 || count > 500 || !validate_map_sentinel(sentinel)) {
        dbg("[MAP] map validation failed (count=%u sentinel=0x%08X)", count, (unsigned)sentinel);
        return -1;
    }

    // Find leftmost node (smallest key)
    uintptr_t node = sentinel;
    {
        uintptr_t left;
        if (!safe_memcpy(&left, (void*)(sentinel + 0x00), 4)) return -1;
        node = left;
    }
    if (node == sentinel) return 0;  // empty tree

    // Go to leftmost
    for (int safety = 0; safety < 500; safety++) {
        uintptr_t lc;
        if (!safe_memcpy(&lc, (void*)(node + 0x00), 4)) break;
        if (lc == sentinel) break;
        node = lc;
    }

    // In-order traversal
    CachedCreature found[MAX_CREATURES];
    int found_count = 0;

    for (int iter = 0; iter < 500 && node != sentinel && found_count < MAX_CREATURES; iter++) {
        uint8_t nbuf[0x18];
        if (!safe_memcpy(nbuf, (void*)node, 0x18)) break;

        uint8_t isnil = nbuf[0x0D];
        if (isnil) break;  // hit sentinel

        uint32_t key;
        uintptr_t creature_ptr;
        memcpy(&key, nbuf + 0x10, 4);
        memcpy(&creature_ptr, nbuf + 0x14, 4);

        // Read creature data from Creature* object
        if (key >= MIN_CREATURE_ID && key < MAX_CREATURE_ID &&
            creature_ptr >= 0x10000 && creature_ptr < 0x7FFE0000u) {
            // Validate vtable
            uint32_t vtable = 0;
            if (safe_memcpy(&vtable, (void*)creature_ptr, 4) &&
                is_valid_creature_vtable(vtable)) {

                // Read creature_id from object to confirm
                uint32_t obj_id = 0;
                safe_memcpy(&obj_id, (void*)(creature_ptr + OFF_CREATURE_ID), 4);

                if (obj_id == key) {
                    // Read health
                    uint32_t hp = 0;
                    safe_memcpy(&hp, (void*)(creature_ptr + OFF_CREATURE_HP), 4);

                    // Read name from object (name field is at Creature* + OFF_CREATURE_NAME)
                    char name[64] = {0};
                    uint8_t name_raw[24];
                    uintptr_t name_addr = creature_ptr + OFF_CREATURE_NAME;
                    if (safe_memcpy(name_raw, (void*)name_addr, 24)) {
                        try_read_name(name_raw, name, sizeof(name));
                    }

                    // Read position (using id_ptr = Creature* + OFF_CREATURE_ID)
                    uint8_t* id_ptr = (uint8_t*)(creature_ptr + OFF_CREATURE_ID);
                    uint32_t cx = 0, cy = 0, cz = 0;
                    read_position(id_ptr, key, &cx, &cy, &cz);

                    CachedCreature* c = &found[found_count++];
                    c->addr = id_ptr;
                    c->id = key;
                    strncpy(c->name, name, MAX_NAME_LEN);
                    c->name[MAX_NAME_LEN] = '\0';
                    c->health = (hp <= 100) ? (uint8_t)hp : 0;
                    c->x = cx;
                    c->y = cy;
                    c->z = cz;
                }
            }
        }

        // In-order successor
        uintptr_t right_child;
        memcpy(&right_child, nbuf + 0x08, 4);
        if (right_child != sentinel) {
            node = right_child;
            for (int safety = 0; safety < 500; safety++) {
                uintptr_t lc;
                if (!safe_memcpy(&lc, (void*)(node + 0x00), 4)) break;
                if (lc == sentinel) break;
                node = lc;
            }
        } else {
            uintptr_t parent_node;
            memcpy(&parent_node, nbuf + 0x04, 4);
            uintptr_t cur = node;
            node = parent_node;
            for (int safety = 0; safety < 500; safety++) {
                if (node == sentinel) break;
                uintptr_t n_right;
                if (!safe_memcpy(&n_right, (void*)(node + 0x08), 4)) { node = sentinel; break; }
                if (n_right != cur) break;
                cur = node;
                if (!safe_memcpy(&node, (void*)(node + 0x04), 4)) { node = sentinel; break; }
            }
        }
    }

    // Replace address cache
    memcpy(g_addrs, found, sizeof(CachedCreature) * found_count);
    g_addr_count = found_count;
    copy_to_output();
    return found_count;
}

// Crash-safe wrapper: uses setjmp + VEH longjmp to recover from access
// violations caused by stale tree pointers during the scan.  Without this,
// a single race condition between the scan thread and the game thread
// modifying the red-black tree brings down the entire game process.
static int walk_creature_map(void) {
    g_scan_recovery = TRUE;
    if (setjmp(g_scan_jmpbuf) != 0) {
        // VEH handler caught an AV and longjmp'd back here
        dbg("[MAP] VEH recovered from AV during tree walk — skipping cycle");
        return -1;
    }
    int result = walk_creature_map_inner();
    g_scan_recovery = FALSE;

    // Fix 11: Track creature count changes for stability detection
    if (result >= 0) {
        int prev = g_prev_creature_count;
        int delta = result - prev;
        if (delta < 0) delta = -delta;
        if (delta >= COUNT_CHANGE_THRESHOLD && prev > 0) {
            g_last_count_change_tick = GetTickCount();
            dbg("[MAP] creature count changed %d -> %d (delta=%d) — map unstable",
                prev, result, delta);
        }
        g_prev_creature_count = result;
    }
    return result;
}

// Find a specific creature by ID using the map tree (O(log n) binary search).
static uintptr_t find_creature_in_map(uint32_t creature_id) {
    if (!g_map_addr) return 0;

    uint8_t hdr[8];
    if (!safe_memcpy(hdr, (void*)g_map_addr, 8)) return 0;

    uintptr_t sentinel;
    uint32_t count;
    memcpy(&sentinel, hdr, 4);
    memcpy(&count, hdr + 4, 4);
    if (count == 0 || !validate_map_sentinel(sentinel)) return 0;

    // Start from root: sentinel->parent is the root node
    uintptr_t node;
    if (!safe_memcpy(&node, (void*)(sentinel + 0x04), 4)) return 0;

    for (int i = 0; i < 30 && node != sentinel; i++) {  // log2(500) < 10, 30 is generous
        uint8_t nbuf[0x18];
        if (!safe_memcpy(nbuf, (void*)node, 0x18)) return 0;
        if (nbuf[0x0D]) return 0;  // isnil — hit sentinel

        uint32_t key;
        memcpy(&key, nbuf + 0x10, 4);

        if (creature_id == key) {
            uintptr_t creature_ptr;
            memcpy(&creature_ptr, nbuf + 0x14, 4);
            if (creature_ptr >= 0x10000 && creature_ptr < 0x7FFE0000u) {
                uint32_t vtable = 0;
                if (safe_memcpy(&vtable, (void*)creature_ptr, 4) &&
                    is_valid_creature_vtable(vtable)) {
                    return creature_ptr;
                }
            }
            return 0;
        } else if (creature_id < key) {
            memcpy(&node, nbuf + 0x00, 4);  // go left
        } else {
            memcpy(&node, nbuf + 0x08, 4);  // go right
        }
    }
    return 0;
}

// ── WndProc hook: execute targeting on the game thread in ~16ms ─────

static LRESULT CALLBACK bot_wndproc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    if (msg == WM_BOT_TARGET) {
        // Capture game thread ID on first call (WndProc runs on the game's UI thread)
        if (!g_attack_thread_id)
            g_attack_thread_id = GetCurrentThreadId();
        do_game_target_update();
        return 0;
    }
    return CallWindowProc(g_orig_wndproc, hwnd, msg, wParam, lParam);
}

// EnumWindows callback to find the game window (matches the game process)
static BOOL CALLBACK find_game_window_cb(HWND hwnd, LPARAM lParam) {
    DWORD pid = 0;
    GetWindowThreadProcessId(hwnd, &pid);
    if (pid == GetCurrentProcessId()) {
        // Only match visible top-level windows with a title
        if (IsWindowVisible(hwnd)) {
            char title[128] = {0};
            GetWindowTextA(hwnd, title, sizeof(title));
            if (title[0] != '\0') {
                *(HWND*)lParam = hwnd;
                return FALSE;  // stop enumeration
            }
        }
    }
    return TRUE;
}

static BOOL install_wndproc_hook(void) {
    if (g_wndproc_hooked) return TRUE;

    // Find the game window
    g_game_hwnd = NULL;
    EnumWindows(find_game_window_cb, (LPARAM)&g_game_hwnd);
    if (!g_game_hwnd) {
        dbg("[WNDPROC] game window not found");
        return FALSE;
    }

    char title[128] = {0};
    GetWindowTextA(g_game_hwnd, title, sizeof(title));
    dbg("[WNDPROC] found game window: hwnd=%p title='%s'", g_game_hwnd, title);

    // Subclass the window procedure
    g_orig_wndproc = (WNDPROC)SetWindowLongPtr(g_game_hwnd, GWLP_WNDPROC, (LONG_PTR)bot_wndproc);
    if (!g_orig_wndproc) {
        dbg("[WNDPROC] SetWindowLongPtr failed (err=%lu)", GetLastError());
        return FALSE;
    }

    g_wndproc_hooked = TRUE;
    dbg("[WNDPROC] hook installed — targeting via PostMessage(WM_USER+100)");
    return TRUE;
}

// ── Parse hex string helper ─────────────────────────────────────────
static uint32_t parse_hex_or_dec(const char* s) {
    while (*s == ' ' || *s == '"' || *s == ':') s++;
    if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X'))
        return (uint32_t)strtoul(s, NULL, 16);
    return (uint32_t)strtoul(s, NULL, 10);
}

// ── set_offsets parser: update all OFF_* variables from JSON ─────────
static void parse_set_offsets(const char* line) {
    // Simple key-value extraction from the JSON string
    // Format: {"cmd":"set_offsets","game_singleton_rva":"0xB2E970",...}
    auto get_val = [&](const char* key) -> uint32_t {
        const char* p = strstr(line, key);
        if (!p) return 0xFFFFFFFF;
        p = strchr(p + strlen(key), ':');
        if (!p) return 0xFFFFFFFF;
        return parse_hex_or_dec(p + 1);
    };

    uint32_t v;
    if ((v = get_val("\"game_singleton_rva\"")) != 0xFFFFFFFF) { OFF_GAME_SINGLETON_RVA = v; dbg("[OFF] game_singleton_rva=0x%X", v); }
    if ((v = get_val("\"attacking_creature\"")) != 0xFFFFFFFF) { OFF_GAME_ATTACKING = v; }
    if ((v = get_val("\"protocol_game\"")) != 0xFFFFFFFF) { OFF_GAME_PROTOCOL = v; }
    if ((v = get_val("\"attack_flag\"")) != 0xFFFFFFFF) { OFF_GAME_ATKFLAG = v; }
    if ((v = get_val("\"seq_counter\"")) != 0xFFFFFFFF) { OFF_GAME_SEQ = v; }
    if ((v = get_val("\"creature_id\"")) != 0xFFFFFFFF) { OFF_CREATURE_ID = v; }
    if ((v = get_val("\"creature_name\"")) != 0xFFFFFFFF) { OFF_CREATURE_NAME = v; }
    if ((v = get_val("\"creature_hp\"")) != 0xFFFFFFFF) { OFF_CREATURE_HP = v; }
    if ((v = get_val("\"creature_refs\"")) != 0xFFFFFFFF) { OFF_CREATURE_REFS = v; }
    if ((v = get_val("\"vtable_rva_min\"")) != 0xFFFFFFFF) { OFF_VTABLE_RVA_MIN = v; }
    if ((v = get_val("\"vtable_rva_max\"")) != 0xFFFFFFFF) { OFF_VTABLE_RVA_MAX = v; }
    if ((v = get_val("\"xtea_encrypt_rva\"")) != 0xFFFFFFFF) { OFF_XTEA_ENCRYPT_RVA = v; }
    if ((v = get_val("\"game_attack_rva\"")) != 0xFFFFFFFF) { OFF_GAME_ATTACK_RVA = v; }
    if ((v = get_val("\"send_attack_rva\"")) != 0xFFFFFFFF) { OFF_SEND_ATTACK_RVA = v; }
    if ((v = get_val("\"game_doattack_rva\"")) != 0xFFFFFFFF) { OFF_GAME_DOATTACK_RVA = v; }

    // Signed offsets
    const char* npc_pos = strstr(line, "\"npc_pos_from_id\"");
    if (npc_pos) {
        npc_pos = strchr(npc_pos + 17, ':');
        if (npc_pos) OFF_NPC_POS_FROM_ID = (int32_t)atoi(npc_pos + 1);
    }
    const char* pl_pos = strstr(line, "\"player_pos_from_id\"");
    if (pl_pos) {
        pl_pos = strchr(pl_pos + 19, ':');
        if (pl_pos) OFF_PLAYER_POS_FROM_ID = (int32_t)atoi(pl_pos + 1);
    }

    dbg("[OFF] offsets updated from pipe command");
}

// ── Light memory scanner ─────────────────────────────────────────────

#define MAX_LIGHT_CANDIDATES 256

// Snapshot for differential scan
static uintptr_t g_snap_addrs[MAX_LIGHT_CANDIDATES];
static int       g_snap_fmts[MAX_LIGHT_CANDIDATES];
static int       g_snap_count = 0;

static void scan_light_memory(uint8_t level, uint8_t color) {
    HMODULE game = GetModuleHandle(NULL);
    uintptr_t base = (uintptr_t)game;

    // Get image size from PE header
    IMAGE_DOS_HEADER* dos = (IMAGE_DOS_HEADER*)base;
    if (!safe_readable(dos, sizeof(*dos)) || dos->e_magic != IMAGE_DOS_SIGNATURE) {
        dbg("[LIGHT] bad DOS header");
        return;
    }
    IMAGE_NT_HEADERS* nt = (IMAGE_NT_HEADERS*)(base + dos->e_lfanew);
    if (!safe_readable(nt, sizeof(*nt)) || nt->Signature != IMAGE_NT_SIGNATURE) {
        dbg("[LIGHT] bad NT header");
        return;
    }
    uintptr_t end = base + nt->OptionalHeader.SizeOfImage;

    struct { uintptr_t addr; int format; } candidates[MAX_LIGHT_CANDIDATES];
    int count = 0;

    MEMORY_BASIC_INFORMATION mbi;
    uintptr_t addr = base;

    while (addr < end && count < MAX_LIGHT_CANDIDATES) {
        if (VirtualQuery((void*)addr, &mbi, sizeof(mbi)) == 0) break;
        uintptr_t rstart = (uintptr_t)mbi.BaseAddress;
        uintptr_t rend = rstart + mbi.RegionSize;
        if (rend > end) rend = end;

        DWORD prot = mbi.Protect & ~(PAGE_GUARD | PAGE_NOCACHE | PAGE_WRITECOMBINE);
        BOOL writable = (prot == PAGE_READWRITE || prot == PAGE_EXECUTE_READWRITE ||
                         prot == PAGE_WRITECOPY || prot == PAGE_EXECUTE_WRITECOPY);

        if (mbi.State == MEM_COMMIT && writable) {
            uint8_t buf[4096];
            for (uintptr_t page = rstart; page < rend && count < MAX_LIGHT_CANDIDATES; page += 4096) {
                size_t chunk = 4096;
                if (page + chunk > rend) chunk = rend - page;
                if (chunk < 2) continue;
                if (!safe_memcpy(buf, (void*)page, chunk)) continue;

                // fmt 0: u8 pair level,color
                for (size_t i = 0; i + 1 < chunk && count < MAX_LIGHT_CANDIDATES; i++) {
                    if (buf[i] == level && buf[i + 1] == color) {
                        candidates[count].addr = page + i;
                        candidates[count].format = 0;
                        count++;
                    }
                }

                // fmt 1: u8 pair color,level (reversed)
                for (size_t i = 0; i + 1 < chunk && count < MAX_LIGHT_CANDIDATES; i++) {
                    if (buf[i] == color && buf[i + 1] == level) {
                        candidates[count].addr = page + i;
                        candidates[count].format = 1;
                        count++;
                    }
                }

                // fmt 2: u32 pair level,color (4-byte aligned)
                for (size_t i = 0; i + 7 < chunk && count < MAX_LIGHT_CANDIDATES; i += 4) {
                    uint32_t v1, v2;
                    memcpy(&v1, &buf[i], 4);
                    memcpy(&v2, &buf[i + 4], 4);
                    if (v1 == (uint32_t)level && v2 == (uint32_t)color) {
                        candidates[count].addr = page + i;
                        candidates[count].format = 2;
                        count++;
                    }
                }

                // fmt 3: u32 pair color,level (reversed, 4-byte aligned)
                for (size_t i = 0; i + 7 < chunk && count < MAX_LIGHT_CANDIDATES; i += 4) {
                    uint32_t v1, v2;
                    memcpy(&v1, &buf[i], 4);
                    memcpy(&v2, &buf[i + 4], 4);
                    if (v1 == (uint32_t)color && v2 == (uint32_t)level) {
                        candidates[count].addr = page + i;
                        candidates[count].format = 3;
                        count++;
                    }
                }
            }
        }

        addr = rend;
    }

    dbg("[LIGHT] scan found %d candidates for level=%d color=%d", count, level, color);
    for (int i = 0; i < count; i++) {
        uintptr_t rva = candidates[i].addr - base;
        // Read 8 bytes context around the match
        uint8_t ctx[8] = {0};
        safe_memcpy(ctx, (void*)candidates[i].addr, 8);
        const char* fmt_names[] = {"u8:lc", "u8:cl", "u32:lc", "u32:cl"};
        dbg("[LIGHT]   #%d: RVA=0x%X fmt=%s bytes=[%02X %02X %02X %02X %02X %02X %02X %02X]",
            i, (unsigned)rva, fmt_names[candidates[i].format & 3],
            ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7]);
    }

    // Write results as JSON to the pipe
    if (g_active_pipe != INVALID_HANDLE_VALUE) {
        char resp[8192];
        int pos = _snprintf(resp, sizeof(resp), "{\"scan_light_results\":[");
        for (int i = 0; i < count && pos < (int)sizeof(resp) - 100; i++) {
            if (i > 0) resp[pos++] = ',';
            uintptr_t rva = candidates[i].addr - base;
            pos += _snprintf(resp + pos, sizeof(resp) - pos,
                "{\"rva\":\"0x%X\",\"fmt\":%d}", (unsigned)rva, candidates[i].format);
        }
        pos += _snprintf(resp + pos, sizeof(resp) - pos, "],\"count\":%d}\n", count);

        DWORD written = 0;
        WriteFile(g_active_pipe, resp, (DWORD)pos, &written, NULL);
    }
}

// ── Full memory snapshot for format-agnostic diff ────────────────────
// Copies ALL writable committed pages in the process, then compares
// byte-by-byte after the user casts aura.  Finds the light address
// regardless of storage format (u8, u32, float, etc.).

#define FSNAP_MAX_REGIONS 16384
#define FSNAP_MAX_BYTES   (256u * 1024u * 1024u)  // 256 MB cap

struct FSnapRegion {
    uintptr_t addr;
    size_t    size;
    size_t    offset;   // byte offset into g_fsnap_buf
};

static FSnapRegion g_fsnap_regions[FSNAP_MAX_REGIONS];
static int         g_fsnap_nregions = 0;
static size_t      g_fsnap_total = 0;

static BOOL is_writable_committed(const MEMORY_BASIC_INFORMATION* mbi) {
    if (mbi->State != MEM_COMMIT) return FALSE;
    DWORD p = mbi->Protect & ~(PAGE_GUARD | PAGE_NOCACHE | PAGE_WRITECOMBINE);
    return (p == PAGE_READWRITE || p == PAGE_EXECUTE_READWRITE);
}

static void do_full_snap() {
    g_fsnap_nregions = 0;
    g_fsnap_total = 0;

    // Scan only WRITABLE PE sections (.data, .bss) — NOT the full module.
    // The full module has 160MB+ of writable regions (heap mapped inside)
    // which crashes the game when we try to copy it all.
    HMODULE game = GetModuleHandle(NULL);
    uintptr_t base = (uintptr_t)game;
    IMAGE_DOS_HEADER* dos = (IMAGE_DOS_HEADER*)base;
    if (!safe_readable(dos, sizeof(*dos)) || dos->e_magic != IMAGE_DOS_SIGNATURE) {
        dbg("[FSNAP] bad DOS header"); return;
    }
    IMAGE_NT_HEADERS* nt = (IMAGE_NT_HEADERS*)(base + dos->e_lfanew);
    if (!safe_readable(nt, sizeof(*nt)) || nt->Signature != IMAGE_NT_SIGNATURE) {
        dbg("[FSNAP] bad NT header"); return;
    }

    IMAGE_SECTION_HEADER* sec = IMAGE_FIRST_SECTION(nt);
    int nsec = nt->FileHeader.NumberOfSections;
    dbg("[FSNAP] game base=0x%08X, %d PE sections", (unsigned)base, nsec);

    // Open binary snapshot file
    char path[MAX_PATH];
    _snprintf(path, sizeof(path), "%s\\fsnap.bin", g_dll_dir);
    FILE* fp = fopen(path, "wb");
    if (!fp) { dbg("[FSNAP] can't open %s", path); return; }

    // Write header: region count placeholder
    int placeholder = 0;
    fwrite(&placeholder, 4, 1, fp);

    size_t total_bytes = 0;
    uint8_t buf[4096];

    for (int s = 0; s < nsec && g_fsnap_nregions < FSNAP_MAX_REGIONS; s++) {
        // IMAGE_SCN_MEM_WRITE = 0x80000000
        if (!(sec[s].Characteristics & 0x80000000u)) continue;

        uintptr_t sec_start = base + sec[s].VirtualAddress;
        size_t    sec_size  = sec[s].Misc.VirtualSize;
        if (sec_size == 0) sec_size = sec[s].SizeOfRawData;
        if (sec_size > 16u * 1024u * 1024u) sec_size = 16u * 1024u * 1024u;

        char name[9] = {0};
        memcpy(name, sec[s].Name, 8);
        dbg("[FSNAP] section '%s': 0x%08X size=%zu KB (writable)",
            name, (unsigned)sec_start, sec_size / 1024);

        // Write region descriptor: addr(4) + size(4)
        uint32_t ra = (uint32_t)sec_start;
        uint32_t rs = (uint32_t)sec_size;
        fwrite(&ra, 4, 1, fp);
        fwrite(&rs, 4, 1, fp);

        // Write section bytes in 4KB chunks
        for (size_t pg = 0; pg < sec_size; pg += 4096) {
            size_t csz = 4096;
            if (pg + csz > sec_size) csz = sec_size - pg;
            if (safe_memcpy(buf, (void*)(sec_start + pg), csz)) {
                fwrite(buf, 1, csz, fp);
            } else {
                memset(buf, 0, csz);
                fwrite(buf, 1, csz, fp);
            }
        }

        g_fsnap_regions[g_fsnap_nregions].addr   = sec_start;
        g_fsnap_regions[g_fsnap_nregions].size   = sec_size;
        g_fsnap_regions[g_fsnap_nregions].offset = total_bytes;
        g_fsnap_nregions++;
        total_bytes += sec_size;
    }

    fseek(fp, 0, SEEK_SET);
    fwrite(&g_fsnap_nregions, 4, 1, fp);
    fclose(fp);

    g_fsnap_total = total_bytes;
    dbg("[FSNAP] wrote %d sections, %zu bytes to fsnap.bin", g_fsnap_nregions, g_fsnap_total);
}

static void do_full_diff() {
    if (g_fsnap_nregions == 0) {
        dbg("[FDIFF] no snapshot — run full_snap first");
        return;
    }

    // Read snapshot back from file
    char snap_path[MAX_PATH];
    _snprintf(snap_path, sizeof(snap_path), "%s\\fsnap.bin", g_dll_dir);
    FILE* snap_fp = fopen(snap_path, "rb");
    if (!snap_fp) { dbg("[FDIFF] can't open %s", snap_path); return; }

    // Skip header (region count)
    fseek(snap_fp, 4, SEEK_SET);

    // Open results file
    char res_path[MAX_PATH];
    _snprintf(res_path, sizeof(res_path), "%s\\full_diff_results.txt", g_dll_dir);
    FILE* fp = fopen(res_path, "w");
    if (!fp) { fclose(snap_fp); dbg("[FDIFF] can't open results"); return; }

    HMODULE game = GetModuleHandle(NULL);
    uintptr_t gbase = (uintptr_t)game;

    fprintf(fp, "=== Full Memory Diff (game module only) ===\n");
    fprintf(fp, "Snapshot: %d regions, %zu bytes\n", g_fsnap_nregions, g_fsnap_total);
    fprintf(fp, "Game base: 0x%08X\n\n", (unsigned)gbase);

    int total_changed = 0;
    int byte_cands = 0;
    int float_cands = 0;

    fprintf(fp, "=== BYTE CANDIDATES (|delta| >= 80) ===\n");

    uint8_t old_buf[4096];
    uint8_t new_buf[4096];

    // Process each region: read descriptor from file, read old bytes, compare with current
    for (int r = 0; r < g_fsnap_nregions && byte_cands < 500; r++) {
        uint32_t raddr_u32, rsize_u32;
        if (fread(&raddr_u32, 4, 1, snap_fp) != 1) break;
        if (fread(&rsize_u32, 4, 1, snap_fp) != 1) break;
        uintptr_t raddr = (uintptr_t)raddr_u32;
        size_t    rsize = (size_t)rsize_u32;

        for (size_t pg = 0; pg < rsize && byte_cands < 500; pg += 4096) {
            size_t csz = 4096;
            if (pg + csz > rsize) csz = rsize - pg;

            // Read old bytes from snapshot file
            if (fread(old_buf, 1, csz, snap_fp) != csz) goto done;

            // Read current bytes from memory
            if (!safe_memcpy(new_buf, (void*)(raddr + pg), csz)) continue;

            for (size_t i = 0; i < csz; i++) {
                if (new_buf[i] != old_buf[i]) {
                    total_changed++;
                    int delta = (int)new_buf[i] - (int)old_buf[i];
                    if (delta < 0) delta = -delta;
                    if (delta >= 80 && byte_cands < 500) {
                        uintptr_t va = raddr + pg + i;
                        uintptr_t rva = va - gbase;
                        uint8_t nb_old = (i + 1 < csz) ? old_buf[i+1] : 0;
                        uint8_t nb_new = (i + 1 < csz) ? new_buf[i+1] : 0;
                        fprintf(fp, "#%d VA=0x%08X RVA=0x%X old=%d new=%d delta=%+d nb_old=%d nb_new=%d\n",
                            byte_cands, (unsigned)va, (unsigned)rva,
                            old_buf[i], new_buf[i], (int)new_buf[i] - (int)old_buf[i],
                            nb_old, nb_new);
                        fprintf(fp, "  old:");
                        for (int c = 0; c < 8 && i + c < csz; c++)
                            fprintf(fp, " %02X", old_buf[i + c]);
                        fprintf(fp, "\n  new:");
                        for (int c = 0; c < 8 && i + c < csz; c++)
                            fprintf(fp, " %02X", new_buf[i + c]);
                        fprintf(fp, "\n\n");
                        byte_cands++;
                    }
                }
            }
        }
    }

    // Second pass for floats: re-read file from start
    fseek(snap_fp, 4, SEEK_SET);
    fprintf(fp, "\n=== FLOAT CANDIDATES (4-byte aligned, |delta| > 50.0) ===\n");

    for (int r = 0; r < g_fsnap_nregions && float_cands < 200; r++) {
        uint32_t raddr_u32, rsize_u32;
        if (fread(&raddr_u32, 4, 1, snap_fp) != 1) break;
        if (fread(&rsize_u32, 4, 1, snap_fp) != 1) break;
        uintptr_t raddr = (uintptr_t)raddr_u32;
        size_t    rsize = (size_t)rsize_u32;

        for (size_t pg = 0; pg < rsize && float_cands < 200; pg += 4096) {
            size_t csz = 4096;
            if (pg + csz > rsize) csz = rsize - pg;
            if (fread(old_buf, 1, csz, snap_fp) != csz) goto done;
            if (!safe_memcpy(new_buf, (void*)(raddr + pg), csz)) continue;

            for (size_t i = 0; i + 3 < csz && float_cands < 200; i += 4) {
                float fold, fnew;
                memcpy(&fold, &old_buf[i], 4);
                memcpy(&fnew, &new_buf[i], 4);
                if (fold != fold || fnew != fnew) continue;
                if (fold < -1000.0f || fold > 1000.0f) continue;
                if (fnew < -1000.0f || fnew > 1000.0f) continue;
                float fdelta = fnew - fold;
                if (fdelta < 0) fdelta = -fdelta;
                if (fdelta > 50.0f) {
                    uintptr_t va = raddr + pg + i;
                    uintptr_t rva = va - gbase;
                    fprintf(fp, "#%d VA=0x%08X RVA=0x%X old=%.2f new=%.2f delta=%+.2f\n",
                        float_cands, (unsigned)va, (unsigned)rva,
                        fold, fnew, fnew - fold);
                    fprintf(fp, "  old_hex:");
                    for (int c = 0; c < 8 && i + c < csz; c++)
                        fprintf(fp, " %02X", old_buf[i + c]);
                    fprintf(fp, "\n  new_hex:");
                    for (int c = 0; c < 8 && i + c < csz; c++)
                        fprintf(fp, " %02X", new_buf[i + c]);
                    fprintf(fp, "\n\n");
                    float_cands++;
                }
            }
        }
    }

done:
    fprintf(fp, "\n=== SUMMARY ===\n");
    fprintf(fp, "Total changed bytes: %d\n", total_changed);
    fprintf(fp, "Byte candidates (|delta|>=80): %d\n", byte_cands);
    fprintf(fp, "Float candidates (|delta|>50): %d\n", float_cands);
    fclose(fp);
    fclose(snap_fp);

    dbg("[FDIFF] done: %d changed, %d byte-cands, %d float-cands — see full_diff_results.txt",
        total_changed, byte_cands, float_cands);

    if (g_active_pipe != INVALID_HANDLE_VALUE) {
        char resp[256];
        int len = _snprintf(resp, sizeof(resp),
            "{\"full_diff\":{\"total_changed\":%d,\"byte_candidates\":%d,\"float_candidates\":%d}}\n",
            total_changed, byte_cands, float_cands);
        DWORD written = 0;
        WriteFile(g_active_pipe, resp, (DWORD)len, &written, NULL);
    }
}

// ── Command parser ──────────────────────────────────────────────────

static void parse_command(const char* line) {
    if (!strstr(line, "\"cmd\"")) return;

    if (strstr(line, "\"init\"")) {
        const char* pid = strstr(line, "\"player_id\"");
        if (pid) {
            pid = strchr(pid + 11, ':');
            if (pid) {
                g_player_id = (uint32_t)strtoul(pid + 1, NULL, 10);
                dbg("CMD init: player_id=0x%08X (%u)", g_player_id, g_player_id);
            }
        }
    } else if (strstr(line, "\"hook_send\"")) {
        dbg("CMD hook_send");
        open_hook_log();
        if (!g_original_WSASend) {
            install_send_hook();
        }
        g_hook_active = TRUE;
        dbg("send() hook ACTIVE — logging to send_hook_log.txt");
    } else if (strstr(line, "\"unhook_send\"")) {
        dbg("CMD unhook_send");
        g_hook_active = FALSE;
        dbg("send() hook PAUSED");
    } else if (strstr(line, "\"scan_xtea\"")) {
        dbg("CMD scan_xtea");
        scan_xtea_constant();
    } else if (strstr(line, "\"hook_xtea\"")) {
        dbg("CMD hook_xtea");
        // Use hardcoded known address (prologue scan fails if old hook patched it)
        HMODULE game = GetModuleHandle(NULL);
        uintptr_t known_entry = (uintptr_t)game + OFF_XTEA_ENCRYPT_RVA;
        if (g_xtea_func_entry == 0) {
            g_xtea_func_entry = known_entry;
            dbg("Using hardcoded XTEA encrypt at VA 0x%08X (RVA +0x%08X)",
                (unsigned)known_entry, OFF_XTEA_ENCRYPT_RVA);
        }
        if (g_xtea_func_entry != 0) {
            open_xtea_log();
            g_xtea_hook_active = TRUE;
            if (!g_xtea_trampoline) {
                install_xtea_hook();
            }
            dbg("XTEA hook ACTIVE — logging pre-encryption data to xtea_hook_log.txt");
        }
    } else if (strstr(line, "\"reset_xtea\"")) {
        dbg("CMD reset_xtea — clearing capture buffer");
        g_xtea_read_idx = 0;
        g_xtea_write_idx = 0;
        dbg("XTEA capture buffer reset (ready for %d new captures)", MAX_XTEA_CAPTURES);
    } else if (strstr(line, "\"unhook_xtea\"")) {
        dbg("CMD unhook_xtea");
        g_xtea_hook_active = FALSE;
        dbg("XTEA hook PAUSED");
    } else if (strstr(line, "\"hook_attack\"")) {
        dbg("CMD hook_attack");
        install_attack_hook();
        if (g_protocol_this) {
            dbg("  'this' pointer already captured: %p", (void*)g_protocol_this);
        } else {
            dbg("  Waiting for user to attack a creature to capture 'this' pointer...");
        }
    } else if (strstr(line, "\"query_attack\"")) {
        HMODULE game = GetModuleHandle(NULL);
        uintptr_t base = (uintptr_t)game;
        dbg("CMD query_attack:");
        dbg("  protocol_this = %p", (void*)g_protocol_this);
        dbg("  attack_caller_ret = %p (RVA +0x%X)",
            (void*)g_attack_caller_ret,
            g_attack_caller_ret ? (unsigned)(g_attack_caller_ret - base) : 0);
        dbg("  attack_trampoline = %p", (void*)g_attack_trampoline);
        dbg("  attack_cave = %p", (void*)g_attack_cave);
    } else if (strstr(line, "\"query_game\"")) {
        HMODULE game = GetModuleHandle(NULL);
        uintptr_t base = (uintptr_t)game;
        dbg("CMD query_game:");
        dbg("  target_update_calls = %d (times XTEA cave called do_game_target_update)",
            (int)g_target_update_calls);
        dbg("  pending_game_attack = %d, pending_creature_ptr = %p",
            (int)g_pending_game_attack, (void*)g_pending_creature_ptr);
        dbg("  game_this = %p", (void*)g_game_this);
        dbg("  protocol_this = %p", (void*)g_protocol_this);
        dbg("  last_attack_cid = 0x%08X (%u)", g_last_attack_cid, g_last_attack_cid);
        dbg("  attack_caller_ret = %p (RVA +0x%X)",
            (void*)g_attack_caller_ret,
            g_attack_caller_ret ? (unsigned)(g_attack_caller_ret - base) : 0);
        if (g_game_this) {
            // Dump 128 bytes around offset 0x20-0x9F of the Game object
            uint8_t buf[128];
            if (safe_memcpy(buf, (void*)(g_game_this + 0x20), 128)) {
                dbg("  Game object dump (+0x20 to +0x9F):");
                for (int i = 0; i < 128; i += 16) {
                    char hex[80] = {0};
                    int hp = 0;
                    for (int j = 0; j < 16 && i+j < 128; j++) {
                        hp += _snprintf(hex+hp, sizeof(hex)-hp, "%02X ", buf[i+j]);
                    }
                    dbg("    +0x%02X: %s", 0x20 + i, hex);
                }
                // Also show as uint32s for easier creature_id spotting
                dbg("  As uint32s:");
                for (int i = 0; i < 128; i += 4) {
                    uint32_t val;
                    memcpy(&val, &buf[i], 4);
                    if (val >= MIN_CREATURE_ID && val < MAX_CREATURE_ID) {
                        dbg("    +0x%02X: 0x%08X  <-- CREATURE ID!", 0x20 + i, val);
                    }
                }
            }
        }
    } else if (strstr(line, "\"dump_mem\"")) {
        // {"cmd":"dump_mem","address":12345,"length":64}
        const char* addr_s = strstr(line, "\"address\"");
        const char* len_s = strstr(line, "\"length\"");
        if (addr_s && len_s) {
            addr_s = strchr(addr_s + 9, ':');
            len_s = strchr(len_s + 8, ':');
            if (addr_s && len_s) {
                uintptr_t addr = (uintptr_t)strtoul(addr_s + 1, NULL, 10);
                int length = atoi(len_s + 1);
                if (length > 512) length = 512;
                if (length > 0 && safe_readable((void*)addr, length)) {
                    dbg("CMD dump_mem: addr=0x%08X len=%d", (unsigned)addr, length);
                    uint8_t dumpbuf[512];
                    memcpy(dumpbuf, (void*)addr, length);
                    for (int i = 0; i < length; i += 16) {
                        char hex[80] = {0};
                        int hp = 0;
                        for (int j = 0; j < 16 && i+j < length; j++) {
                            hp += _snprintf(hex+hp, sizeof(hex)-hp, "%02X ", dumpbuf[i+j]);
                        }
                        dbg("  0x%08X: %s", (unsigned)(addr + i), hex);
                    }
                }
            }
        }
    } else if (strstr(line, "\"game_attack\"")) {
        // Parse creature_id from: {"cmd":"game_attack","creature_id":12345}
        const char* cid = strstr(line, "\"creature_id\"");
        if (cid) {
            cid = strchr(cid + 13, ':');
            if (cid) {
                uint32_t creature_id = (uint32_t)strtoul(cid + 1, NULL, 10);
                request_game_attack(creature_id);
            }
        }
    } else if (strstr(line, "\"scan_game_attack\"")) {
        // Runs on PIPE THREAD (not game thread) — safe to do slow scans
        dbg("[SCAN] v35 scanning for Game::attack function (pipe thread)...");
        HMODULE game_mod = GetModuleHandle(NULL);
        uintptr_t base = (uintptr_t)game_mod;
        uintptr_t scan_end = base + 0x01000000; // 16MB from base
        MEMORY_BASIC_INFORMATION mbi;

        // 1. Search for string "onAttackingCreatureChange"
        uintptr_t str_addr = 0;
        const char* needles[] = {"onAttackingCreatureChange", "onFollowingCreatureChange"};
        for (int n = 0; n < 2; n++) {
            const char* needle = needles[n];
            size_t needle_len = strlen(needle);
            uintptr_t scan_addr = base;
            while (scan_addr < scan_end) {
                if (VirtualQuery((void*)scan_addr, &mbi, sizeof(mbi)) == 0) break;
                uintptr_t rstart = (uintptr_t)mbi.BaseAddress;
                uintptr_t rend = rstart + mbi.RegionSize;
                if (mbi.State == MEM_COMMIT) {
                    // Read in 4K pages for efficiency
                    for (uintptr_t page = rstart; page < rend; page += 4096) {
                        uint8_t buf[4096];
                        size_t chunk = 4096;
                        if (page + chunk > rend) chunk = rend - page;
                        if (chunk < needle_len) continue;
                        if (!safe_memcpy(buf, (void*)page, chunk)) continue;
                        for (size_t i = 0; i + needle_len <= chunk; i++) {
                            if (memcmp(&buf[i], needle, needle_len) == 0) {
                                uintptr_t found_addr = page + i;
                                dbg("[SCAN] FOUND '%s' at VA=0x%08X (RVA +0x%X)",
                                    needle, (uint32_t)found_addr, (uint32_t)(found_addr - base));
                                if (n == 0) str_addr = found_addr;
                                goto next_needle;
                            }
                        }
                    }
                }
                scan_addr = rend;
            }
            dbg("[SCAN] '%s' NOT FOUND", needle);
            next_needle:;
        }

        // 2. Scan for CALL instructions to sendAttackCreature (RVA +0x19D100)
        uintptr_t target_func = base + OFF_SEND_ATTACK_RVA;
        dbg("[SCAN] Scanning for CALL to sendAttackCreature VA=0x%08X...", (uint32_t)target_func);
        int call_count = 0;
        uintptr_t scan_addr = base;
        while (scan_addr < scan_end && call_count < 20) {
            if (VirtualQuery((void*)scan_addr, &mbi, sizeof(mbi)) == 0) break;
            uintptr_t rstart = (uintptr_t)mbi.BaseAddress;
            uintptr_t rend = rstart + mbi.RegionSize;
            DWORD prot = mbi.Protect & ~(PAGE_GUARD | PAGE_NOCACHE | PAGE_WRITECOMBINE);
            if (mbi.State == MEM_COMMIT &&
                (prot == PAGE_EXECUTE_READ || prot == PAGE_EXECUTE_READWRITE ||
                 prot == PAGE_EXECUTE || prot == PAGE_EXECUTE_WRITECOPY)) {
                uint8_t buf[4096];
                for (uintptr_t page = rstart; page + 5 <= rend; page += 4096) {
                    size_t chunk = 4096;
                    if (page + chunk > rend) chunk = rend - page;
                    if (chunk < 5) continue;
                    if (!safe_memcpy(buf, (void*)page, chunk)) continue;
                    for (size_t i = 0; i + 5 <= chunk; i++) {
                        if (buf[i] == 0xE8) {
                            int32_t rel;
                            memcpy(&rel, &buf[i+1], 4);
                            uintptr_t call_src = page + i;
                            uintptr_t call_target = call_src + 5 + (int32_t)rel;
                            if (call_target == target_func) {
                                uintptr_t rva = call_src - base;
                                dbg("[SCAN] CALL sendAttackCreature at RVA +0x%05X", (uint32_t)rva);
                                // Dump 64 bytes context around the CALL
                                uintptr_t ctx_start = call_src - 48;
                                uint8_t ctx[80];
                                if (safe_memcpy(ctx, (void*)ctx_start, 80)) {
                                    dbg("[SCAN]   context (-48 to +32):");
                                    for (int r = 0; r < 80; r += 16) {
                                        char hex[80] = {0};
                                        int hp2 = 0;
                                        for (int c = 0; c < 16; c++)
                                            hp2 += _snprintf(hex+hp2, sizeof(hex)-hp2, "%02X ", ctx[r+c]);
                                        dbg("[SCAN]     +%02X: %s", r, hex);
                                    }
                                }
                                // Find function start (scan back for 55 8B EC)
                                uint8_t backbuf[512];
                                uintptr_t bk_start = (call_src > base + 512) ? call_src - 512 : base;
                                size_t bk_len = call_src - bk_start;
                                if (bk_len > 2 && safe_memcpy(backbuf, (void*)bk_start, bk_len)) {
                                    for (int j = (int)bk_len - 1; j >= 2; j--) {
                                        if (backbuf[j] == 0x55 && backbuf[j+1] == 0x8B && backbuf[j+2] == 0xEC) {
                                            uintptr_t fs = bk_start + j;
                                            dbg("[SCAN]   func start: RVA +0x%05X (%d bytes before CALL)",
                                                (uint32_t)(fs - base), (int)(call_src - fs));
                                            break;
                                        }
                                    }
                                }
                                call_count++;
                            }
                        }
                    }
                }
            }
            scan_addr = rend;
        }
        dbg("[SCAN] Found %d CALL(s) to sendAttackCreature", call_count);

        // 3. If string found, search for PUSH <string_addr> in code
        if (str_addr) {
            dbg("[SCAN] Searching for PUSH 0x%08X (onAttackingCreatureChange ref)...", (uint32_t)str_addr);
            uint8_t push_pat[5];
            push_pat[0] = 0x68;
            memcpy(&push_pat[1], &str_addr, 4);
            int push_count = 0;
            scan_addr = base;
            while (scan_addr < scan_end && push_count < 10) {
                if (VirtualQuery((void*)scan_addr, &mbi, sizeof(mbi)) == 0) break;
                uintptr_t rstart = (uintptr_t)mbi.BaseAddress;
                uintptr_t rend = rstart + mbi.RegionSize;
                if (mbi.State == MEM_COMMIT) {
                    uint8_t buf[4096];
                    for (uintptr_t page = rstart; page + 5 <= rend; page += 4096) {
                        size_t chunk = 4096;
                        if (page + chunk > rend) chunk = rend - page;
                        if (chunk < 5) continue;
                        if (!safe_memcpy(buf, (void*)page, chunk)) continue;
                        for (size_t i = 0; i + 5 <= chunk; i++) {
                            if (memcmp(&buf[i], push_pat, 5) == 0) {
                                uintptr_t ref = page + i;
                                dbg("[SCAN] PUSH ref at RVA +0x%05X", (uint32_t)(ref - base));
                                // Find function start
                                uint8_t bb[512];
                                uintptr_t bs = (ref > base + 512) ? ref - 512 : base;
                                size_t bl = ref - bs;
                                if (bl > 2 && safe_memcpy(bb, (void*)bs, bl)) {
                                    for (int j = (int)bl - 1; j >= 2; j--) {
                                        if (bb[j] == 0x55 && bb[j+1] == 0x8B && bb[j+2] == 0xEC) {
                                            uintptr_t fs = bs + j;
                                            dbg("[SCAN]   func start: RVA +0x%05X",
                                                (uint32_t)(fs - base));
                                            // Dump 128 bytes of this function
                                            uint8_t fd[128];
                                            if (safe_memcpy(fd, (void*)fs, 128)) {
                                                dbg("[SCAN]   func dump (128B):");
                                                for (int r = 0; r < 128; r += 16) {
                                                    char hex[80] = {0};
                                                    int hp2 = 0;
                                                    for (int c = 0; c < 16; c++)
                                                        hp2 += _snprintf(hex+hp2, sizeof(hex)-hp2, "%02X ", fd[r+c]);
                                                    dbg("[SCAN]     +%02X: %s", r, hex);
                                                }
                                            }
                                            break;
                                        }
                                    }
                                }
                                push_count++;
                            }
                        }
                    }
                }
                scan_addr = rend;
            }
            dbg("[SCAN] Found %d PUSH references", push_count);
        }
        dbg("[SCAN] === scan complete ===");
    } else if (strstr(line, "\"set_offsets\"")) {
        dbg("CMD set_offsets");
        parse_set_offsets(line);
    } else if (strstr(line, "\"scan_gmap\"")) {
        dbg("CMD scan_gmap");
        scan_gmap();
        if (g_map_addr) {
            uint32_t count = 0;
            safe_memcpy(&count, (void*)(g_map_addr + 4), 4);
            dbg("[GMAP] map ready at 0x%08X with %u creatures", (unsigned)g_map_addr, count);
        }
    } else if (strstr(line, "\"use_map_scan\"")) {
        const char* en = strstr(line, "\"enabled\"");
        BOOL enable = TRUE;
        if (en) {
            en = strchr(en + 9, ':');
            if (en) {
                while (*++en == ' ');
                enable = (*en == 't' || *en == '1') ? TRUE : FALSE;
            }
        }
        if (enable && !g_map_addr) {
            dbg("CMD use_map_scan: REJECTED — g_map not found yet (run scan_gmap first)");
        } else {
            g_use_map_scan = enable;
            dbg("CMD use_map_scan: %s", enable ? "ENABLED" : "DISABLED");
        }
    } else if (strstr(line, "\"hook_wndproc\"")) {
        dbg("CMD hook_wndproc");
        install_wndproc_hook();
    } else if (strstr(line, "\"scan_light\"")) {
        // {"cmd":"scan_light","level":250,"color":215}
        const char* lv = strstr(line, "\"level\"");
        const char* cl = strstr(line, "\"color\"");
        if (lv && cl) {
            lv = strchr(lv + 7, ':');
            cl = strchr(cl + 7, ':');
            if (lv && cl) {
                uint8_t level = (uint8_t)atoi(lv + 1);
                uint8_t color = (uint8_t)atoi(cl + 1);
                dbg("CMD scan_light: level=%d color=%d", level, color);
                scan_light_memory(level, color);
            }
        }
    } else if (strstr(line, "\"snap_light\"")) {
        // {"cmd":"snap_light","level":40,"color":215}
        // Scan and save candidates internally for later diff
        const char* lv = strstr(line, "\"level\"");
        const char* cl = strstr(line, "\"color\"");
        if (lv && cl) {
            lv = strchr(lv + 7, ':');
            cl = strchr(cl + 7, ':');
            if (lv && cl) {
                uint8_t level = (uint8_t)atoi(lv + 1);
                uint8_t color = (uint8_t)atoi(cl + 1);
                dbg("CMD snap_light: level=%d color=%d", level, color);
                scan_light_memory(level, color);
                // Copy results to snapshot (scan_light_memory logged them already)
                // Re-scan into snapshot arrays
                HMODULE game = GetModuleHandle(NULL);
                uintptr_t base = (uintptr_t)game;
                IMAGE_DOS_HEADER* dos = (IMAGE_DOS_HEADER*)base;
                IMAGE_NT_HEADERS* nt = (IMAGE_NT_HEADERS*)(base + dos->e_lfanew);
                uintptr_t end = base + nt->OptionalHeader.SizeOfImage;
                g_snap_count = 0;
                MEMORY_BASIC_INFORMATION mbi;
                uintptr_t addr = base;
                while (addr < end && g_snap_count < MAX_LIGHT_CANDIDATES) {
                    if (VirtualQuery((void*)addr, &mbi, sizeof(mbi)) == 0) break;
                    uintptr_t rstart = (uintptr_t)mbi.BaseAddress;
                    uintptr_t rend = rstart + mbi.RegionSize;
                    if (rend > end) rend = end;
                    DWORD prot = mbi.Protect & ~(PAGE_GUARD | PAGE_NOCACHE | PAGE_WRITECOMBINE);
                    BOOL writable = (prot == PAGE_READWRITE || prot == PAGE_EXECUTE_READWRITE ||
                                     prot == PAGE_WRITECOPY || prot == PAGE_EXECUTE_WRITECOPY);
                    if (mbi.State == MEM_COMMIT && writable) {
                        uint8_t buf[4096];
                        for (uintptr_t page = rstart; page < rend && g_snap_count < MAX_LIGHT_CANDIDATES; page += 4096) {
                            size_t chunk = 4096;
                            if (page + chunk > rend) chunk = rend - page;
                            if (chunk < 2) continue;
                            if (!safe_memcpy(buf, (void*)page, chunk)) continue;
                            for (size_t i = 0; i + 1 < chunk && g_snap_count < MAX_LIGHT_CANDIDATES; i++) {
                                if (buf[i] == level && buf[i+1] == color) {
                                    g_snap_addrs[g_snap_count] = page + i;
                                    g_snap_fmts[g_snap_count] = 0;
                                    g_snap_count++;
                                }
                            }
                            for (size_t i = 0; i + 1 < chunk && g_snap_count < MAX_LIGHT_CANDIDATES; i++) {
                                if (buf[i] == color && buf[i+1] == level) {
                                    g_snap_addrs[g_snap_count] = page + i;
                                    g_snap_fmts[g_snap_count] = 1;
                                    g_snap_count++;
                                }
                            }
                        }
                    }
                    addr = rend;
                }
                dbg("[SNAP] saved %d candidates", g_snap_count);
            }
        }
    } else if (strstr(line, "\"diff_light\"")) {
        // {"cmd":"diff_light","level":250,"color":215}
        // Rescan, intersect with snapshot, report addresses present in both
        const char* lv = strstr(line, "\"level\"");
        const char* cl = strstr(line, "\"color\"");
        if (lv && cl) {
            lv = strchr(lv + 7, ':');
            cl = strchr(cl + 7, ':');
            if (lv && cl) {
                uint8_t level = (uint8_t)atoi(lv + 1);
                uint8_t color = (uint8_t)atoi(cl + 1);
                dbg("CMD diff_light: level=%d color=%d (snap has %d)", level, color, g_snap_count);
                if (g_snap_count == 0) {
                    dbg("[DIFF] no snapshot — run snap_light first");
                } else {
                    HMODULE game = GetModuleHandle(NULL);
                    uintptr_t base = (uintptr_t)game;
                    // Check each snapped address: does it now contain new values?
                    int match_count = 0;
                    for (int i = 0; i < g_snap_count; i++) {
                        uintptr_t a = g_snap_addrs[i];
                        int fmt = g_snap_fmts[i];
                        uint8_t cur[2];
                        if (!safe_memcpy(cur, (void*)a, 2)) continue;
                        BOOL matches = FALSE;
                        if (fmt == 0 && cur[0] == level && cur[1] == color) matches = TRUE;
                        if (fmt == 1 && cur[0] == color && cur[1] == level) matches = TRUE;
                        if (matches) {
                            uintptr_t rva = a - base;
                            uint8_t ctx[8] = {0};
                            safe_memcpy(ctx, (void*)a, 8);
                            dbg("[DIFF] MATCH #%d: RVA=0x%X fmt=%d bytes=[%02X %02X %02X %02X %02X %02X %02X %02X]",
                                match_count, (unsigned)rva, fmt,
                                ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7]);
                            match_count++;
                        }
                    }
                    dbg("[DIFF] %d of %d snap addresses now contain level=%d color=%d",
                        match_count, g_snap_count, level, color);
                }
            }
        }
    } else if (strstr(line, "\"check_snap\"")) {
        // {"cmd":"check_snap"} — reads all snapped addresses, reports which changed
        dbg("CMD check_snap (snap has %d)", g_snap_count);
        if (g_snap_count == 0) {
            dbg("[CHECK] no snapshot — run snap_light first");
        } else {
            HMODULE game = GetModuleHandle(NULL);
            uintptr_t base = (uintptr_t)game;
            int changed = 0;
            for (int i = 0; i < g_snap_count; i++) {
                uintptr_t a = g_snap_addrs[i];
                int fmt = g_snap_fmts[i];
                uint8_t cur[8] = {0};
                if (!safe_memcpy(cur, (void*)a, 8)) continue;
                // Check if value CHANGED from what we snapped (250/215 in some order)
                BOOL still_same = FALSE;
                if (fmt == 0 && cur[0] == 250 && cur[1] == 215) still_same = TRUE;  // u8:lc
                if (fmt == 1 && cur[0] == 215 && cur[1] == 250) still_same = TRUE;  // u8:cl
                uintptr_t rva = a - base;
                if (!still_same) {
                    dbg("[CHECK] CHANGED #%d: RVA=0x%X fmt=%d now=[%02X %02X %02X %02X %02X %02X %02X %02X]",
                        changed, (unsigned)rva, fmt,
                        cur[0], cur[1], cur[2], cur[3], cur[4], cur[5], cur[6], cur[7]);
                    changed++;
                } else {
                    dbg("[CHECK] same   : RVA=0x%X fmt=%d still=[%02X %02X]",
                        (unsigned)rva, fmt, cur[0], cur[1]);
                }
            }
            dbg("[CHECK] %d of %d addresses changed", changed, g_snap_count);
        }
    } else if (strstr(line, "\"set_light_addr\"")) {
        // {"cmd":"set_light_addr","addr":"0xB2ECF8","render_addr":"0xB2ECFC"}
        const char* ad = strstr(line, "\"addr\"");
        const char* ra = strstr(line, "\"render_addr\"");
        HMODULE game = GetModuleHandle(NULL);
        uintptr_t base = (uintptr_t)game;
        if (ad) {
            ad = strchr(ad + 6, ':');
            if (ad) {
                uintptr_t rva = (uintptr_t)parse_hex_or_dec(ad + 1);
                g_light_addr = base + rva;
                // Auto-calculate render base: level at +0x08, render at +0x0C in struct
                g_light_render_base = g_light_addr + 4;
                dbg("CMD set_light_addr: level VA=0x%08X (RVA +0x%X) auto render=0x%08X",
                    (unsigned)g_light_addr, (unsigned)rva,
                    (unsigned)g_light_render_base);
            }
        }
        if (ra) {
            ra = strchr(ra + 13, ':');
            if (ra) {
                uintptr_t rva = (uintptr_t)parse_hex_or_dec(ra + 1);
                g_light_render_base = base + rva;
                dbg("CMD set_light_addr: explicit render VA=0x%08X (RVA +0x%X)",
                    (unsigned)g_light_render_base, (unsigned)rva);
            }
        }
    } else if (strstr(line, "\"probe_light\"")) {
        // {"cmd":"probe_light","addr":"0xRVA","format":"u8"}
        // Reads current value, writes max, reads back, restores original
        const char* ad = strstr(line, "\"addr\"");
        const char* fm = strstr(line, "\"format\"");
        if (ad) {
            ad = strchr(ad + 6, ':');
            if (ad) {
                HMODULE game = GetModuleHandle(NULL);
                uintptr_t rva = (uintptr_t)parse_hex_or_dec(ad + 1);
                uintptr_t va = (uintptr_t)game + rva;
                int fmt = 0;
                if (fm) {
                    fm = strchr(fm + 8, ':');
                    if (fm) {
                        const char* s = fm + 1;
                        while (*s == ' ' || *s == '"') s++;
                        if (s[0] == 'u' && s[1] == '3') fmt = 1;
                    }
                }
                int sz = fmt ? 8 : 2;
                if (safe_readable((void*)va, sz)) {
                    uint8_t before[8];
                    safe_memcpy(before, (void*)va, sz);
                    // Write max light
                    if (fmt == 0) {
                        *(uint8_t*)va = 0xFF;
                        *(uint8_t*)(va + 1) = 0xD7;
                    } else {
                        *(uint32_t*)va = 0xFF;
                        *(uint32_t*)(va + 4) = 0xD7;
                    }
                    uint8_t after[8];
                    safe_memcpy(after, (void*)va, sz);
                    dbg("CMD probe_light: RVA=0x%X VA=0x%08X fmt=%s",
                        (unsigned)rva, (unsigned)va, fmt ? "u32" : "u8");
                    dbg("  before: [%02X %02X %02X %02X %02X %02X %02X %02X]",
                        before[0], before[1], before[2], before[3],
                        before[4], before[5], before[6], before[7]);
                    dbg("  after:  [%02X %02X %02X %02X %02X %02X %02X %02X]",
                        after[0], after[1], after[2], after[3],
                        after[4], after[5], after[6], after[7]);
                    // Note: does NOT restore — caller can check if screen changed
                } else {
                    dbg("CMD probe_light: RVA=0x%X — NOT READABLE", (unsigned)rva);
                }
            }
        }
    } else if (strstr(line, "\"full_light\"")) {
        // {"cmd":"full_light","enabled":true}
        const char* en = strstr(line, "\"enabled\"");
        BOOL enable = TRUE;
        if (en) {
            en = strchr(en + 9, ':');
            if (en) {
                while (*++en == ' ');
                enable = (*en == 't' || *en == '1') ? TRUE : FALSE;
            }
        }
        g_full_light = enable;
        dbg("CMD full_light: %s (addr=0x%08X fmt=%s)",
            enable ? "ENABLED" : "DISABLED",
            (unsigned)g_light_addr,
            g_light_format ? "u32" : "u8");
    } else if (strstr(line, "\"full_snap\"")) {
        dbg("CMD full_snap");
        do_full_snap();
        // Acknowledge via pipe
        if (g_active_pipe != INVALID_HANDLE_VALUE) {
            char resp[128];
            int len = _snprintf(resp, sizeof(resp),
                "{\"full_snap\":{\"regions\":%d,\"bytes\":%zu}}\n",
                g_fsnap_nregions, g_fsnap_total);
            DWORD written = 0;
            WriteFile(g_active_pipe, resp, (DWORD)len, &written, NULL);
        }
    } else if (strstr(line, "\"full_diff\"")) {
        dbg("CMD full_diff");
        do_full_diff();
    } else if (strstr(line, "\"find_xrefs\"")) {
        // {"cmd":"find_xrefs","rva":"0xB2ECF8"}
        // Scan ALL code sections for any instruction referencing this absolute VA.
        const char* rv = strstr(line, "\"rva\"");
        if (rv) {
            rv = strchr(rv + 5, ':');
            if (rv) {
                HMODULE game = GetModuleHandle(NULL);
                uintptr_t base = (uintptr_t)game;
                uintptr_t rva = (uintptr_t)parse_hex_or_dec(rv + 1);
                uintptr_t va = base + rva;

                IMAGE_DOS_HEADER* dos = (IMAGE_DOS_HEADER*)base;
                IMAGE_NT_HEADERS* nt = (IMAGE_NT_HEADERS*)(base + dos->e_lfanew);
                IMAGE_SECTION_HEADER* sec = IMAGE_FIRST_SECTION(nt);
                int nsec = nt->FileHeader.NumberOfSections;

                dbg("[XREF] searching for VA=0x%08X (RVA +0x%X) in %d sections",
                    (unsigned)va, (unsigned)rva, nsec);

                int total_count = 0;
                // Also write results to file for easy reading
                FILE* xf = fopen("dll/xref_results.txt", "w");
                if (xf) fprintf(xf, "=== XREF scan for RVA 0x%X (VA 0x%08X) ===\n\n",
                    (unsigned)rva, (unsigned)va);

                for (int s = 0; s < nsec; s++) {
                    if (!(sec[s].Characteristics & 0x20)) continue; // skip non-code
                    uintptr_t text_start = base + sec[s].VirtualAddress;
                    uintptr_t text_end = text_start + sec[s].Misc.VirtualSize;
                    char nm[9] = {0};
                    memcpy(nm, sec[s].Name, 8);
                    dbg("[XREF] scanning code section '%s': 0x%08X - 0x%08X (%u bytes)",
                        nm, (unsigned)text_start, (unsigned)text_end,
                        (unsigned)(text_end - text_start));
                    if (xf) fprintf(xf, "Section '%s': 0x%08X - 0x%08X\n",
                        nm, (unsigned)text_start, (unsigned)text_end);

                    for (uintptr_t p = text_start; p + 3 < text_end; p++) {
                        if (*(uint32_t*)p == (uint32_t)va) {
                            uintptr_t ref_rva = p - base;
                            // Context: 10 bytes before + 4 match + 10 after
                            uintptr_t ctx_start = (p >= text_start + 10) ? p - 10 : text_start;
                            uintptr_t ctx_end = p + 14;
                            if (ctx_end > text_end) ctx_end = text_end;
                            size_t ctx_len = ctx_end - ctx_start;
                            uint8_t ctx[40];
                            memcpy(ctx, (void*)ctx_start, ctx_len);
                            int off = (int)(p - ctx_start);

                            // Detect instruction type from byte before the VA
                            const char* itype = "unknown";
                            if (p > text_start) {
                                uint8_t prev = *(uint8_t*)(p - 1);
                                uint8_t prev2 = (p > text_start + 1) ? *(uint8_t*)(p - 2) : 0;
                                if (prev == 0xA1) itype = "MOV EAX,[addr]";
                                else if (prev == 0xA3) itype = "MOV [addr],EAX";
                                else if (prev == 0x05) itype = "ADD EAX,imm (or MOV reg,[addr])";
                                else if (prev == 0x0D) itype = "OR EAX,imm";
                                else if (prev == 0x15) itype = "ADC/MOV reg,[addr]";
                                else if (prev == 0x25) itype = "AND EAX,imm";
                                else if (prev == 0x35) itype = "XOR EAX,imm";
                                else if (prev == 0x3D) itype = "CMP EAX,imm";
                                else if (prev == 0xB8 || prev == 0xB9 || prev == 0xBA ||
                                         prev == 0xBB || prev == 0xBC || prev == 0xBD ||
                                         prev == 0xBE || prev == 0xBF)
                                    itype = "MOV reg,imm32";
                                else if (prev == 0x68) itype = "PUSH imm32";
                                else if (prev2 == 0x8B) itype = "MOV reg,[addr]";
                                else if (prev2 == 0x89) itype = "MOV [addr],reg";
                                else if (prev2 == 0xC7) itype = "MOV [addr],imm";
                                else if (prev2 == 0x83) itype = "CMP/ADD/SUB [addr],imm8";
                                else if (prev2 == 0x80) itype = "CMP/ADD byte [addr],imm8";
                                else if (prev2 == 0x8A) itype = "MOV reg8,[addr]";
                                else if (prev2 == 0x88) itype = "MOV [addr],reg8";
                                else if (prev2 == 0xFE) itype = "INC/DEC byte [addr]";
                                else if (prev2 == 0xFF) itype = "INC/DEC/CALL/JMP [addr]";
                                else if (prev2 == 0x0F) itype = "0F-prefixed (MOVZX/CMOV/etc)";
                                else if (prev2 == 0xA2) itype = "MOV [addr],AL (or prev instr)";
                            }

                            char hex[300];
                            int hpos = 0;
                            for (size_t i = 0; i < ctx_len && hpos < 260; i++) {
                                if ((int)i == off) hpos += sprintf(hex + hpos, "[");
                                hpos += sprintf(hex + hpos, "%02X", ctx[i]);
                                if ((int)i == off + 3) hpos += sprintf(hex + hpos, "]");
                                if (i + 1 < ctx_len) hpos += sprintf(hex + hpos, " ");
                            }
                            dbg("[XREF] #%d RVA +0x%X (%s): %s",
                                total_count, (unsigned)ref_rva, itype, hex);
                            if (xf) fprintf(xf, "#%d RVA +0x%06X  %-30s  %s\n",
                                total_count, (unsigned)ref_rva, itype, hex);
                            total_count++;
                            if (total_count >= 100) break;
                        }
                    }
                    if (total_count >= 100) break;
                }
                dbg("[XREF] total: %d references to VA 0x%08X", total_count, (unsigned)va);
                if (xf) {
                    fprintf(xf, "\nTotal: %d references\n", total_count);
                    fclose(xf);
                }
            }
        }
    } else if (strstr(line, "\"dump_code\"")) {
        // {"cmd":"dump_code","rva":"0x16A805","before":64,"after":128}
        // Dump raw bytes around a code RVA for manual disassembly
        const char* rv = strstr(line, "\"rva\"");
        int before = 64, after = 128;
        const char* bv = strstr(line, "\"before\"");
        const char* av = strstr(line, "\"after\"");
        if (bv) { bv = strchr(bv, ':'); if (bv) before = (int)parse_hex_or_dec(bv+1); }
        if (av) { av = strchr(av, ':'); if (av) after = (int)parse_hex_or_dec(av+1); }
        if (before > 512) before = 512;
        if (after > 512) after = 512;
        if (rv) {
            rv = strchr(rv + 4, ':');
            if (rv) {
                HMODULE game = GetModuleHandle(NULL);
                uintptr_t base = (uintptr_t)game;
                uintptr_t rva = (uintptr_t)parse_hex_or_dec(rv + 1);
                uintptr_t target = base + rva;
                uintptr_t start = target - before;
                uintptr_t end = target + after;
                dbg("[DUMP] RVA +0x%X (VA 0x%08X), range -%d to +%d",
                    (unsigned)rva, (unsigned)target, before, after);

                FILE* df = fopen("dll/code_dump.txt", "a");
                if (df) fprintf(df, "\n=== Code dump RVA +0x%X (VA 0x%08X) -%d/+%d ===\n",
                    (unsigned)rva, (unsigned)target, before, after);

                // Dump in 16-byte rows
                for (uintptr_t row = start; row < end; row += 16) {
                    char hex[200];
                    int hpos = 0;
                    hpos += sprintf(hex + hpos, "+0x%06X: ", (unsigned)(row - base));
                    for (int i = 0; i < 16 && row + i < end; i++) {
                        if (row + i == target)
                            hpos += sprintf(hex + hpos, ">>%02X ", *(uint8_t*)(row + i));
                        else
                            hpos += sprintf(hex + hpos, "%02X ", *(uint8_t*)(row + i));
                    }
                    dbg("%s", hex);
                    if (df) fprintf(df, "%s\n", hex);
                }
                if (df) fclose(df);
            }
        }
    } else if (strstr(line, "\"read_mem\"")) {
        // {"cmd":"read_mem","rva":"0xB2ECF0","size":32}
        // Read N bytes at RVA and log as hex dump
        const char* rv = strstr(line, "\"rva\"");
        int size = 32;
        const char* sv = strstr(line, "\"size\"");
        if (sv) { sv = strchr(sv, ':'); if (sv) size = (int)parse_hex_or_dec(sv+1); }
        if (size > 256) size = 256;
        if (rv) {
            rv = strchr(rv + 4, ':');
            if (rv) {
                HMODULE game = GetModuleHandle(NULL);
                uintptr_t base = (uintptr_t)game;
                uintptr_t rva = (uintptr_t)parse_hex_or_dec(rv + 1);
                uintptr_t addr = base + rva;
                if (safe_readable((void*)addr, size)) {
                    char hex[600];
                    int hpos = 0;
                    hpos += sprintf(hex, "[RMEM] RVA +0x%X (%d bytes):", (unsigned)rva, size);
                    for (int i = 0; i < size && hpos < 580; i++) {
                        if (i % 16 == 0 && i > 0) {
                            dbg("%s", hex);
                            hpos = sprintf(hex, "  +%02X:", i);
                        }
                        hpos += sprintf(hex + hpos, " %02X", *(uint8_t*)(addr + i));
                    }
                    dbg("%s", hex);
                } else {
                    dbg("[RMEM] RVA +0x%X not readable", (unsigned)rva);
                }
            }
        }
    } else if (strstr(line, "\"deref\"")) {
        // {"cmd":"deref","rva":"0xB2ECE4","offset":0,"size":256}
        // Read pointer at RVA, then dump 'size' bytes from the pointed-to address + offset
        const char* rv = strstr(line, "\"rva\"");
        int offset = 0, size = 256;
        const char* ov = strstr(line, "\"offset\"");
        const char* sv = strstr(line, "\"size\"");
        if (ov) { ov = strchr(ov, ':'); if (ov) offset = (int)parse_hex_or_dec(ov+1); }
        if (sv) { sv = strchr(sv, ':'); if (sv) size = (int)parse_hex_or_dec(sv+1); }
        if (size > 1024) size = 1024;
        if (rv) {
            rv = strchr(rv + 4, ':');
            if (rv) {
                HMODULE game = GetModuleHandle(NULL);
                uintptr_t base = (uintptr_t)game;
                uintptr_t rva = (uintptr_t)parse_hex_or_dec(rv + 1);
                uintptr_t ptr_addr = base + rva;
                if (safe_readable((void*)ptr_addr, 4)) {
                    uintptr_t target = *(uintptr_t*)ptr_addr;
                    uintptr_t read_addr = target + offset;
                    dbg("[DEREF] ptr at RVA +0x%X = 0x%08X, reading %d bytes at 0x%08X (+%d)",
                        (unsigned)rva, (unsigned)target, size, (unsigned)read_addr, offset);
                    if (target && safe_readable((void*)read_addr, size)) {
                        for (int row = 0; row < size; row += 16) {
                            char hex[200];
                            int hpos = sprintf(hex, "[DEREF] +%04X:", row + offset);
                            int cols = (size - row < 16) ? (size - row) : 16;
                            for (int c = 0; c < cols; c++) {
                                uint8_t b = *(uint8_t*)(read_addr + row + c);
                                hpos += sprintf(hex + hpos, " %02X", b);
                            }
                            // Also show as floats if 4-byte aligned
                            if (cols >= 4) {
                                hpos += sprintf(hex + hpos, "  |");
                                for (int f = 0; f + 3 < cols; f += 4) {
                                    float fv;
                                    memcpy(&fv, (void*)(read_addr + row + f), 4);
                                    if (fv > -1e6 && fv < 1e6 && fv != 0.0f)
                                        hpos += sprintf(hex + hpos, " %.4f", fv);
                                    else
                                        hpos += sprintf(hex + hpos, " ---");
                                }
                            }
                            dbg("%s", hex);
                        }
                    } else {
                        dbg("[DEREF] target 0x%08X (+%d) not readable", (unsigned)target, offset);
                    }
                } else {
                    dbg("[DEREF] ptr at RVA +0x%X not readable", (unsigned)rva);
                }
            }
        }
    } else if (strstr(line, "\"write_mem\"")) {
        // {"cmd":"write_mem","rva":"0xB2ECF8","bytes":"FF D7"}
        // Write raw bytes at RVA (hex string, space-separated)
        const char* rv = strstr(line, "\"rva\"");
        const char* bv = strstr(line, "\"bytes\"");
        if (rv && bv) {
            rv = strchr(rv + 4, ':');
            bv = strchr(bv + 7, ':');
            if (rv && bv) {
                HMODULE game = GetModuleHandle(NULL);
                uintptr_t base = (uintptr_t)game;
                uintptr_t rva = (uintptr_t)parse_hex_or_dec(rv + 1);
                uintptr_t addr = base + rva;
                // Parse hex bytes from the "bytes" value
                // Find the opening quote
                const char* p = strchr(bv + 1, '"');
                if (p) {
                    p++;
                    uint8_t buf[128];
                    int nbytes = 0;
                    while (*p && *p != '"' && nbytes < 128) {
                        while (*p == ' ') p++;
                        if (*p == '"' || !*p) break;
                        unsigned val = 0;
                        for (int d = 0; d < 2 && *p && *p != '"' && *p != ' '; d++, p++) {
                            val <<= 4;
                            if (*p >= '0' && *p <= '9') val |= (*p - '0');
                            else if (*p >= 'A' && *p <= 'F') val |= (*p - 'A' + 10);
                            else if (*p >= 'a' && *p <= 'f') val |= (*p - 'a' + 10);
                        }
                        buf[nbytes++] = (uint8_t)val;
                    }
                    if (nbytes > 0 && safe_readable((void*)addr, nbytes)) {
                        // Skip write if bytes already match (avoids cache flush + race with executing code)
                        if (memcmp((void*)addr, buf, nbytes) == 0) {
                            dbg("[WMEM] skip RVA +0x%X — already patched", (unsigned)rva);
                        } else {
                            DWORD oldProt = 0;
                            if (VirtualProtect((void*)addr, nbytes, PAGE_EXECUTE_READWRITE, &oldProt)) {
                                memcpy((void*)addr, buf, nbytes);
                                VirtualProtect((void*)addr, nbytes, oldProt, &oldProt);
                                FlushInstructionCache(GetCurrentProcess(), (void*)addr, nbytes);
                                dbg("[WMEM] wrote %d bytes at RVA +0x%X (prot=%X)", nbytes, (unsigned)rva, oldProt);
                            } else {
                                dbg("[WMEM] VirtualProtect failed at RVA +0x%X err=%lu", (unsigned)rva, GetLastError());
                            }
                        }
                    } else {
                        dbg("[WMEM] cannot read %d bytes at RVA +0x%X", nbytes, (unsigned)rva);
                    }
                }
            }
        }
    } else if (strstr(line, "\"light_diag\"")) {
        // {"cmd":"light_diag"}
        // Read ALL known light-related addresses and log their current values
        HMODULE game = GetModuleHandle(NULL);
        uintptr_t base = (uintptr_t)game;
        struct { const char* name; uintptr_t rva; int size; } addrs[] = {
            {"light_struct_base", 0xB2ECE4, 4},
            {"cleared_1",        0xB2ECF0, 4},
            {"cleared_2",        0xB2ECF4, 4},
            {"world_level",      0xB2ECF8, 1},
            {"world_color",      0xB2ECF9, 1},
            {"pad_FA_FB",        0xB2ECFA, 2},
            {"render_param1",    0xB2ECFC, 4},
            {"render_param2",    0xB2ED00, 4},
            {"render_param3",    0xB2ED04, 2},
            {"pad_06_07",        0xB2ED06, 2},
            {"field_08",         0xB2ED08, 4},
            {"field_0C",         0xB2ED0C, 4},
            {"field_10",         0xB2ED10, 4},
            {"field_14",         0xB2ED14, 4},
            {"field_18",         0xB2ED18, 4},
            {"field_1C",         0xB2ED1C, 1},
        };
        dbg("[LDIAG] === Light diagnostic ===");
        for (int i = 0; i < (int)(sizeof(addrs)/sizeof(addrs[0])); i++) {
            uintptr_t a = base + addrs[i].rva;
            if (safe_readable((void*)a, addrs[i].size)) {
                if (addrs[i].size == 1)
                    dbg("[LDIAG] %s (+0x%X) = 0x%02X (%d)",
                        addrs[i].name, (unsigned)addrs[i].rva,
                        *(uint8_t*)a, *(uint8_t*)a);
                else if (addrs[i].size == 2)
                    dbg("[LDIAG] %s (+0x%X) = 0x%04X (%d)",
                        addrs[i].name, (unsigned)addrs[i].rva,
                        *(uint16_t*)a, *(uint16_t*)a);
                else
                    dbg("[LDIAG] %s (+0x%X) = 0x%08X (%d)",
                        addrs[i].name, (unsigned)addrs[i].rva,
                        *(uint32_t*)a, *(uint32_t*)a);
            } else {
                dbg("[LDIAG] %s (+0x%X) = NOT READABLE", addrs[i].name, (unsigned)addrs[i].rva);
            }
        }
        // Also read the diff candidate at 0xB2F03C
        uintptr_t dc = base + 0xB2F03C;
        if (safe_readable((void*)dc, 8)) {
            dbg("[LDIAG] diff_candidate (+0xB2F03C) = 0x%08X (%d) next=0x%08X",
                *(uint32_t*)dc, *(uint32_t*)dc, *(uint32_t*)(dc+4));
        }
        dbg("[LDIAG] g_full_light=%d g_light_addr=0x%08X g_light_render_base=0x%08X",
            (int)g_full_light, (unsigned)g_light_addr, (unsigned)g_light_render_base);
    } else if (strstr(line, "\"write_loop\"")) {
        // {"cmd":"write_loop","slots":[{"rva":"0xB2ECF8","bytes":"FF D7"},{"rva":"0xB2ECFC","bytes":"FF FF 00 00"}]}
        // Configure up to 8 address/value pairs to write continuously in the pipe loop
        // Replaces the hardcoded full_light write
        const char* sl = strstr(line, "\"slots\"");
        if (sl) {
            // Simple: parse each rva+bytes pair
            // For now, just enable g_full_light — actual multi-slot is done via write_mem + full_light
            dbg("[WLOOP] write_loop command received (use write_mem + full_light for now)");
        }
    } else if (strstr(line, "\"stop\"")) {
        dbg("CMD stop");
        g_running = FALSE;
    }
}

// ── Pipe server thread ──────────────────────────────────────────────

static DWORD WINAPI pipe_thread(LPVOID param) {
    (void)param;
    dbg_open();
    g_scan_thread_id = GetCurrentThreadId();
    dbg("pipe_thread started (tid=%lu)", g_scan_thread_id);

    while (g_running) {
        HANDLE pipe = CreateNamedPipeA(
            PIPE_NAME, PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            1, PIPE_BUF_SIZE, PIPE_BUF_SIZE, 0, NULL);

        if (pipe == INVALID_HANDLE_VALUE) {
            dbg("CreateNamedPipe err=%lu", GetLastError());
            Sleep(1000);
            continue;
        }

        dbg("Waiting for client...");
        if (!ConnectNamedPipe(pipe, NULL) && GetLastError() != ERROR_PIPE_CONNECTED) {
            dbg("ConnectNamedPipe err=%lu", GetLastError());
            CloseHandle(pipe);
            continue;
        }
        dbg("Client connected");
        g_active_pipe = pipe;

        DWORD mode = PIPE_READMODE_BYTE | PIPE_NOWAIT;
        SetNamedPipeHandleState(pipe, &mode, NULL, NULL);

        char read_buf[4096];
        char line_buf[4096];
        int line_len = 0;
        DWORD last_full_scan = 0;
        DWORD last_fast_scan = 0;
        DWORD last_map_scan = 0;
        DWORD last_send = 0;

        while (g_running) {
            DWORD nread = 0;
            BOOL ok = ReadFile(pipe, read_buf, sizeof(read_buf) - 1, &nread, NULL);
            if (ok && nread > 0) {
                for (DWORD i = 0; i < nread && line_len < (int)sizeof(line_buf) - 1; i++) {
                    if (read_buf[i] == '\n') {
                        line_buf[line_len] = '\0';
                        parse_command(line_buf);
                        line_len = 0;
                    } else {
                        line_buf[line_len++] = read_buf[i];
                    }
                }
            } else if (!ok && GetLastError() != ERROR_NO_DATA) {
                dbg("Read err=%lu, client gone", GetLastError());
                break;
            }

            {
                DWORD now = GetTickCount();

                if (g_use_map_scan && g_map_addr) {
                    // Map scan mode: walk the creature tree every 100ms
                    if (now - last_map_scan > MAP_SCAN_INTERVAL) {
                        int result = walk_creature_map();
                        last_map_scan = GetTickCount();
                        if (result < 0) {
                            // Map validation failed — auto-revert to VirtualQuery
                            dbg("[MAP] tree walk failed — reverting to VirtualQuery scan");
                            g_use_map_scan = FALSE;
                            g_map_addr = 0;
                        } else {
                            g_map_scan_count++;
                            if (g_map_scan_count <= 3 || g_map_scan_count % 100 == 0) {
                                dbg("[MAP] scan#%d: %d creatures", g_map_scan_count, result);
                            }
                        }
                    }
                } else {
                    // VirtualQuery fallback mode
                    // Full scan: expensive, finds new creatures
                    if (now - last_full_scan > FULL_SCAN_INTERVAL) {
                        full_scan();
                        last_full_scan = GetTickCount();  // use time AFTER scan completes
                        last_fast_scan = last_full_scan;
                    }
                    // Fast scan: re-read cached addresses for updated hp/position
                    else if (now - last_fast_scan > FAST_SCAN_INTERVAL) {
                        fast_scan();
                        last_fast_scan = now;
                    }
                }

                // Flush XTEA captures from ring buffer to log file
                if (g_xtea_hook_active) flush_xtea_captures();

                // Continuous full light write
                if (g_full_light) {
                    // Write to packet handler input (0xB2ECF8/F9)
                    if (g_light_addr && safe_readable((void*)g_light_addr, 2)) {
                        *(uint8_t*)g_light_addr = 0xFF;       // max level
                        *(uint8_t*)(g_light_addr + 1) = 0xD7; // white color
                    }
                    // Write to rendering output params (0xB2ECFC, 0xB2ED00, 0xB2ED04)
                    if (g_light_render_base && safe_readable((void*)g_light_render_base, 12)) {
                        *(uint32_t*)(g_light_render_base)     = 0x0000FFFF; // render param 1
                        *(uint32_t*)(g_light_render_base + 4) = 0x0000FFFF; // render param 2
                        *(uint16_t*)(g_light_render_base + 8) = 0x00FF;     // render param 3
                    }
                }

                DWORD after = GetTickCount();
                if (after - last_send > SEND_INTERVAL) {
                    char json[PIPE_BUF_SIZE];
                    int json_len = build_json(json, sizeof(json));
                    if (json_len <= 0 || json_len >= (int)sizeof(json)) continue;
                    DWORD written = 0;
                    if (!WriteFile(pipe, json, (DWORD)json_len, &written, NULL)) {
                        dbg("Write err=%lu", GetLastError());
                        break;
                    }
                    last_send = after;
                }
            }

            Sleep(4);  // ~250 Hz loop — fast enough for 60fps scan, safe for game thread
        }

        g_active_pipe = INVALID_HANDLE_VALUE;
        g_full_light = FALSE;
        DisconnectNamedPipe(pipe);
        CloseHandle(pipe);
        g_player_id = 0;
        g_scan_count = 0;
        g_addr_count = 0;
        g_map_scan_count = 0;
        g_use_map_scan = FALSE;
        // Keep g_map_addr — it's still valid if game hasn't restarted
        EnterCriticalSection(&g_cs);
        g_output_count = 0;
        LeaveCriticalSection(&g_cs);
        dbg("Session ended");
    }

    dbg("pipe_thread exit");
    if (g_dbg) { fclose(g_dbg); g_dbg = NULL; }
    return 0;
}

// ── Vectored Exception Handler — catches crashes and logs them ───────

static FILE* g_crash_log = NULL;

static void crash_log_open(const char* dir) {
    if (g_crash_log) return;
    char path[MAX_PATH];
    _snprintf(path, sizeof(path), "%s\\dbvbot_crash.txt", dir);
    g_crash_log = fopen(path, "a");
}

static LONG WINAPI crash_handler(EXCEPTION_POINTERS* ep) {
    if (!ep || !ep->ExceptionRecord || !ep->ContextRecord)
        return EXCEPTION_CONTINUE_SEARCH;

    DWORD code = ep->ExceptionRecord->ExceptionCode;

    // ── Crash recovery via longjmp ──────────────────────────────────
    // If a protected code region (tree scan) hit an AV, recover by
    // longjmp'ing back to the setjmp point instead of crashing.
    // Thread ID check prevents cross-thread longjmp.
    if (code == EXCEPTION_ACCESS_VIOLATION) {
        DWORD tid = GetCurrentThreadId();
        if (g_scan_recovery && tid == g_scan_thread_id) {
            g_scan_recovery = FALSE;
            g_last_scan_av_tick = GetTickCount();  // Fix 11: record for stability check
            dbg("[VEH] recovering scan thread from AV at EIP=0x%08X",
                (unsigned)ep->ContextRecord->Eip);
            longjmp(g_scan_jmpbuf, 1);
            // NOT REACHED
        }
        // ── Fix 10: Also recover AVs during Game::attack ──
        // Fix 7 removed AV recovery (longjmp corrupts Lua state).
        // But Game::attack Lua callbacks can ALSO crash with AV (e.g.,
        // Lua traceback code hits corrupted data → EIP="trac").
        // Without recovery → 100% game crash. With recovery → game may
        // get Lua errors later but has a chance to survive.
        if (g_attack_recovery && tid == g_attack_thread_id) {
            g_attack_recovery = FALSE;
            g_last_attack_av_tick = GetTickCount();  // Fix 11: record for stability check
            dbg("[VEH] recovering game thread from AV during Game::attack at EIP=0x%08X",
                (unsigned)ep->ContextRecord->Eip);
            longjmp(g_attack_jmpbuf, 1);
            // NOT REACHED
        }
    }

    // ── Fix 9: Catch MSVC C++ exceptions during Game::attack ─────────
    // MinGW try/catch can't catch MSVC exceptions (incompatible ABI).
    // VEH sees 0xE06D7363 before MSVC's handler, so we can longjmp.
    // Safe: Lua has already cleaned up its state before throwing.
    if (code == 0xE06D7363) {
        DWORD tid = GetCurrentThreadId();
        if (g_attack_recovery && tid == g_attack_thread_id) {
            g_attack_recovery = FALSE;
            dbg("[VEH] catching MSVC C++ exception during Game::attack at EIP=0x%08X",
                (unsigned)ep->ContextRecord->Eip);
            longjmp(g_attack_jmpbuf, 1);
            // NOT REACHED
        }
    }

    // Skip benign / OS-internal exceptions — logging during these can corrupt heap
    if (code == 0xE24C4A02 || code == 0xE0434352 || code == 0x406D1388)
        return EXCEPTION_CONTINUE_SEARCH;
    if (code == 0x80000001  // STATUS_GUARD_PAGE_VIOLATION (stack/heap growth)
        || code == 0xC0000374  // STATUS_HEAP_CORRUPTION (already too late)
        || code == 0x80000003  // STATUS_BREAKPOINT
        || code == 0x80000004  // STATUS_SINGLE_STEP
        || code == 0xE06D7363  // MSVC C++ exception (normal runtime throw/catch)
        || (code & 0xF0000000) == 0xE0000000)  // All software exceptions (Delphi, .NET, etc)
        return EXCEPTION_CONTINUE_SEARCH;
    if (g_crash_log) {
        HMODULE game = GetModuleHandle(NULL);
        uintptr_t base = (uintptr_t)game;
        uintptr_t eip = ep->ContextRecord->Eip;
        fprintf(g_crash_log, "!!! CRASH code=0x%08X addr=0x%08X (RVA +0x%X)\n",
            (unsigned)ep->ExceptionRecord->ExceptionCode,
            (unsigned)eip, (unsigned)(eip - base));
        fprintf(g_crash_log, "  EAX=%08X EBX=%08X ECX=%08X EDX=%08X\n",
            ep->ContextRecord->Eax, ep->ContextRecord->Ebx,
            ep->ContextRecord->Ecx, ep->ContextRecord->Edx);
        fprintf(g_crash_log, "  ESI=%08X EDI=%08X EBP=%08X ESP=%08X\n",
            ep->ContextRecord->Esi, ep->ContextRecord->Edi,
            ep->ContextRecord->Ebp, ep->ContextRecord->Esp);
        fprintf(g_crash_log, "  base=%08X target_updates=%d pending=%d\n",
            (unsigned)base, (int)g_target_update_calls, (int)g_pending_game_attack);
        fflush(g_crash_log);
    }
    // Also write to main debug log
    if (ep && ep->ExceptionRecord && ep->ContextRecord) {
        HMODULE game = GetModuleHandle(NULL);
        uintptr_t base = (uintptr_t)game;
        dbg("!!! VEH CRASH code=0x%08X EIP=0x%08X (RVA +0x%X) ESP=0x%08X",
            (unsigned)ep->ExceptionRecord->ExceptionCode,
            (unsigned)ep->ContextRecord->Eip,
            (unsigned)(ep->ContextRecord->Eip - base),
            (unsigned)ep->ContextRecord->Esp);
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

// ── Early debug (called from DllMain before pipe thread) ────────────

static void early_debug(const char* dir) {
    char path[MAX_PATH];
    _snprintf(path, sizeof(path), "%s\\dbvbot_debug.txt", dir);
    FILE* f = fopen(path, "a");
    if (f) {
        fprintf(f, "=== DllMain ATTACH v50 (map scan + WndProc) === base=%p\n", (void*)GetModuleHandle(NULL));
        fflush(f);
        fclose(f);
    }
}

// ── DLL entry ───────────────────────────────────────────────────────

extern "C" BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hModule);
        GetModuleFileNameA(hModule, g_dll_dir, MAX_PATH);
        char* slash = strrchr(g_dll_dir, '\\');
        if (slash) *slash = '\0';

        // Immediate debug write (before anything else)
        early_debug(g_dll_dir);

        // Install crash handler
        crash_log_open(g_dll_dir);
        AddVectoredExceptionHandler(1, crash_handler);

        InitializeCriticalSection(&g_cs);
        g_running = TRUE;
        g_thread = CreateThread(NULL, 0, pipe_thread, NULL, 0, NULL);
    } else if (reason == DLL_PROCESS_DETACH) {
        g_running = FALSE;
        if (g_thread) {
            WaitForSingleObject(g_thread, 2000);
            CloseHandle(g_thread);
        }
        DeleteCriticalSection(&g_cs);
    }
    return TRUE;
}
