"""
XTEA Key Finder - Scans the DBVictory client process memory for XTEA keys.

In OTClient, the XTEA keys are 4 uint32 values stored in the Protocol object.
We can find them by scanning memory for patterns near known protocol structures.

Strategy:
1. Find the ProtocolGame object in memory
2. Extract the 4 XTEA key uint32 values
3. Validate by trying to decrypt a captured packet
"""

import pymem
import pymem.process
import struct
import sys


def find_xtea_keys_by_pattern(pm: pymem.Pymem) -> list[tuple[int, int, int, int]]:
    """
    Scan process memory for potential XTEA keys.

    XTEA keys are 4 consecutive uint32 values (16 bytes total).
    In OTClient they're stored in std::array<uint32, 4> m_xteaKey.

    We look for regions that contain plausible key values
    (non-zero, non-trivial patterns).
    """
    candidates = []

    # Get all readable memory regions
    for module in pymem.process.enum_process_module(pm.process_handle):
        print(f"Module: {module.name} at 0x{module.lpBaseOfDll:08X}, size: {module.SizeOfImage}")

    print("\nScanning heap memory for XTEA key candidates...")

    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    MEM_COMMIT = 0x1000
    PAGE_READWRITE = 0x04
    PAGE_READONLY = 0x02
    PAGE_EXECUTE_READ = 0x20
    PAGE_EXECUTE_READWRITE = 0x40

    class MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wintypes.DWORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]

    VirtualQueryEx = ctypes.windll.kernel32.VirtualQueryEx
    ReadProcessMemory = ctypes.windll.kernel32.ReadProcessMemory

    readable_protections = {PAGE_READWRITE, PAGE_READONLY, PAGE_EXECUTE_READ, PAGE_EXECUTE_READWRITE}

    address = 0
    mbi = MEMORY_BASIC_INFORMATION()
    found_keys = []
    regions_scanned = 0

    while address < 0x7FFFFFFF:  # 32-bit process
        result = VirtualQueryEx(
            pm.process_handle,
            ctypes.c_void_p(address),
            ctypes.byref(mbi),
            ctypes.sizeof(mbi)
        )

        if result == 0:
            break

        if (mbi.State == MEM_COMMIT and
            mbi.Protect in readable_protections and
            mbi.RegionSize > 0 and
            mbi.RegionSize < 100 * 1024 * 1024):  # Skip regions > 100MB

            try:
                data = pm.read_bytes(mbi.BaseAddress, mbi.RegionSize)
                regions_scanned += 1

                # Scan for potential XTEA keys
                # XTEA keys are 4 uint32 values that should be:
                # - Non-zero (at least 3 of 4)
                # - Not all the same value
                # - Not sequential/simple patterns
                for offset in range(0, len(data) - 16, 4):
                    keys = struct.unpack_from('<4I', data, offset)

                    # Filter criteria
                    non_zero = sum(1 for k in keys if k != 0)
                    if non_zero < 3:
                        continue

                    # Skip trivial patterns
                    if len(set(keys)) == 1:
                        continue

                    # Skip very small values (likely not crypto keys)
                    if all(k < 1000 for k in keys):
                        continue

                    # Skip values that look like pointers (common in heap)
                    pointer_like = sum(1 for k in keys if 0x00400000 <= k <= 0x7FFFFFFF)
                    if pointer_like >= 3:
                        continue

                    # Skip values that are all in ASCII range
                    ascii_like = sum(1 for k in keys if all(32 <= b <= 126 for b in k.to_bytes(4, 'little')))
                    if ascii_like >= 3:
                        continue

                    # Look for keys that have good entropy (spread across byte range)
                    entropy_score = 0
                    for k in keys:
                        key_bytes = k.to_bytes(4, 'little')
                        unique_bytes = len(set(key_bytes))
                        if unique_bytes >= 3:
                            entropy_score += 1

                    if entropy_score >= 2:
                        addr = mbi.BaseAddress + offset
                        found_keys.append((addr, keys))

            except Exception:
                pass

        address = mbi.BaseAddress + mbi.RegionSize

    print(f"Scanned {regions_scanned} memory regions")
    print(f"Found {len(found_keys)} potential XTEA key candidates")

    return found_keys


def find_xtea_near_protocol(pm: pymem.Pymem) -> list[tuple[int, tuple]]:
    """
    Alternative approach: search for XTEA keys near ProtocolGame vtable references.
    """
    # First find the "ProtocolGame" string in memory
    results = []

    try:
        pattern = b"ProtocolGame"
        # Search in the main module
        base = pm.base_address
        module_size = 0
        for module in pymem.process.enum_process_module(pm.process_handle):
            if "dbvStart" in module.name.lower():
                module_size = module.SizeOfImage
                break

        if module_size == 0:
            module_size = 0x2000000  # 32MB default

        print(f"\nSearching for ProtocolGame references near base 0x{base:08X}...")

        try:
            data = pm.read_bytes(base, min(module_size, 0x2000000))
            idx = 0
            pg_addrs = []
            while True:
                idx = data.find(pattern, idx)
                if idx == -1:
                    break
                pg_addrs.append(base + idx)
                print(f"  Found 'ProtocolGame' string at 0x{base + idx:08X}")
                idx += 1
        except Exception as e:
            print(f"  Error reading module: {e}")

    except Exception as e:
        print(f"Error: {e}")

    return results


def main():
    process_name = "dbvStart.exe"

    print(f"Attaching to {process_name}...")
    try:
        pm = pymem.Pymem(process_name)
    except pymem.exception.ProcessNotFound:
        print(f"ERROR: {process_name} not found. Is the game running?")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Could not attach: {e}")
        print("Try running this script as Administrator.")
        sys.exit(1)

    print(f"Attached! PID: {pm.process_id}")
    print(f"Base address: 0x{pm.base_address:08X}")

    # Method 1: Search near ProtocolGame references
    find_xtea_near_protocol(pm)

    # Method 2: Pattern-based scan
    keys = find_xtea_keys_by_pattern(pm)

    if keys:
        # Show top candidates (limit output)
        print(f"\nTop XTEA key candidates (showing first 20):")
        for i, (addr, key_vals) in enumerate(keys[:20]):
            print(f"  [{i}] 0x{addr:08X}: {' '.join(f'{k:08X}' for k in key_vals)}")
    else:
        print("\nNo XTEA key candidates found.")

    pm.close_process()


if __name__ == "__main__":
    main()
