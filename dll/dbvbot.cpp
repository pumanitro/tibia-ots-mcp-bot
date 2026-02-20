/*
 * dbvbot.dll — In-process creature scanner for DBVictory
 *
 * v10: Two-tier scanning, no proximity filter (done in Python).
 *   - Fast scan (~200ms): re-reads cached memory addresses (instant)
 *   - Full scan (~5s):    VirtualQuery to discover new creatures
 *
 * Creature struct layout (confirmed from memory analysis):
 *   +0:   u32 id          (0x10000000..0x80000000)
 *   +4:   MSVC string     (24 bytes: SSO or heap ptr + size + cap)
 *   +28:  u32 health      (0-100)
 *   +576: u32 x, y, z    (current position)
 *
 * Build (MinGW 32-bit):
 *   g++ -shared -o dbvbot.dll dbvbot.cpp -lkernel32 -static -s -O2 -std=c++17
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <cstdarg>
#include <cstdint>

// ── Constants ───────────────────────────────────────────────────────
#define MIN_CREATURE_ID 0x10000000u
#define MAX_CREATURE_ID 0x80000000u
#define PIPE_NAME       "\\\\.\\pipe\\dbvbot"
#define PIPE_BUF_SIZE   65536
#define MAX_CREATURES   200
#define MAX_NAME_LEN    63
#define FULL_SCAN_INTERVAL 5000  // ms between full VirtualQuery scans
#define FAST_SCAN_INTERVAL 200   // ms between fast re-reads of cached addrs
#define SEND_INTERVAL      200   // ms between JSON sends
#define POS_OFFSET         576   // offset from creature ID to position (NPCs)
#define PLAYER_POS_OFFSET  -40   // offset from creature ID to position (player)


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

// ── Debug log ───────────────────────────────────────────────────────
static FILE* g_dbg = NULL;

static void dbg_open(void) {
    if (g_dbg) return;
    char path[MAX_PATH];
    _snprintf(path, sizeof(path), "%s\\dbvbot_debug.txt", g_dll_dir);
    g_dbg = fopen(path, "w");
    if (g_dbg) {
        fprintf(g_dbg, "=== dbvbot.dll v12 (player pos fix) ===\n");
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
        if (IsBadReadPtr((void*)ptr, str_size)) return FALSE;
        data = (const char*)ptr;
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
    if (IsBadReadPtr(pos_ptr, 12)) return FALSE;

    memcpy(x, pos_ptr, 4);
    memcpy(y, pos_ptr + 4, 4);
    memcpy(z, pos_ptr + 8, 4);

    if (*x > 65535 || *y > 65535 || *z > 15) return FALSE;
    return TRUE;
}

static BOOL read_position(const uint8_t* id_ptr, uint32_t id, uint32_t* x, uint32_t* y, uint32_t* z) {
    // Player creature stores position at a different offset
    if (g_player_id != 0 && id == g_player_id) {
        return read_position_at(id_ptr, PLAYER_POS_OFFSET, x, y, z);
    }
    return read_position_at(id_ptr, POS_OFFSET, x, y, z);
}

// ── Try to read a creature at a known address ───────────────────────
// Returns TRUE if the address still holds a valid creature with the expected ID.

static BOOL reread_creature(CachedCreature* cc) {
    if (IsBadReadPtr(cc->addr, 32)) return FALSE;

    // Verify the ID is still the same
    uint32_t id;
    memcpy(&id, cc->addr, 4);
    if (id != cc->id) return FALSE;

    // Re-read health
    uint32_t hp_word;
    memcpy(&hp_word, cc->addr + 28, 4);
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
                if (IsBadReadPtr((void*)page, 4)) { pages_bad++; continue; }
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

static int build_json(char* buf, size_t buf_sz) {
    EnterCriticalSection(&g_cs);
    int pos = _snprintf(buf, buf_sz, "{\"creatures\":[");
    for (int i = 0; i < g_output_count && pos + 120 < (int)buf_sz; i++) {
        if (i > 0) buf[pos++] = ',';
        pos += _snprintf(buf + pos, buf_sz - pos,
            "{\"id\":%u,\"name\":\"%s\",\"hp\":%d,\"x\":%u,\"y\":%u,\"z\":%u}",
            g_output[i].id, g_output[i].name, g_output[i].health,
            g_output[i].x, g_output[i].y, g_output[i].z);
    }
    pos += _snprintf(buf + pos, buf_sz - pos, "]}\n");
    LeaveCriticalSection(&g_cs);
    return pos;
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
    } else if (strstr(line, "\"stop\"")) {
        dbg("CMD stop");
        g_running = FALSE;
    }
}

// ── Pipe server thread ──────────────────────────────────────────────

static DWORD WINAPI pipe_thread(LPVOID param) {
    (void)param;
    dbg_open();
    dbg("pipe_thread started");

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

        DWORD mode = PIPE_READMODE_BYTE | PIPE_NOWAIT;
        SetNamedPipeHandleState(pipe, &mode, NULL, NULL);

        char read_buf[4096];
        char line_buf[4096];
        int line_len = 0;
        DWORD last_full_scan = 0;
        DWORD last_fast_scan = 0;
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

                // Full scan: expensive, finds new creatures
                if (now - last_full_scan > FULL_SCAN_INTERVAL) {
                    full_scan();
                    last_full_scan = now;
                    last_fast_scan = now;  // no need for fast scan right after full
                }
                // Fast scan: re-read cached addresses for updated hp/position
                else if (now - last_fast_scan > FAST_SCAN_INTERVAL) {
                    fast_scan();
                    last_fast_scan = now;
                }

                if (now - last_send > SEND_INTERVAL) {
                    char json[PIPE_BUF_SIZE];
                    int json_len = build_json(json, sizeof(json));
                    DWORD written = 0;
                    if (!WriteFile(pipe, json, (DWORD)json_len, &written, NULL)) {
                        dbg("Write err=%lu", GetLastError());
                        break;
                    }
                    last_send = now;
                }
            }

            Sleep(50);
        }

        DisconnectNamedPipe(pipe);
        CloseHandle(pipe);
        g_player_id = 0;
        g_scan_count = 0;
        g_addr_count = 0;
        EnterCriticalSection(&g_cs);
        g_output_count = 0;
        LeaveCriticalSection(&g_cs);
        dbg("Session ended");
    }

    dbg("pipe_thread exit");
    if (g_dbg) { fclose(g_dbg); g_dbg = NULL; }
    return 0;
}

// ── DLL entry ───────────────────────────────────────────────────────

extern "C" BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hModule);
        GetModuleFileNameA(hModule, g_dll_dir, MAX_PATH);
        char* slash = strrchr(g_dll_dir, '\\');
        if (slash) *slash = '\0';

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
