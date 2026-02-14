"""
DBVictory Client Patcher - Patches the running client's memory to:
1. Replace the RSA public key with our proxy's key
2. Redirect server connections to localhost (our proxy)

This allows the proxy to intercept and decrypt all traffic.

Usage:
    python patcher.py <proxy_rsa_key_n>

Run this BEFORE logging in (after the client is open but before you press "Login").
"""

import pymem
import pymem.process
import struct
import sys
import ctypes
from ctypes import wintypes


# Known OTClient RSA key fragments to search for in memory
# These are decimal string representations that OTClient stores
KNOWN_RSA_KEYS = [
    # Default OTClient RSA key
    b"109120132967399429278860960508995541528237502902798129123468757937266291492576446330739696001110603907230888610072655818825358503429057592827629436413108566029093628212635953836686562675849720620786279431090218017681061521755056710823876476444260558147179707119674283982419152118103759076030616683978566631413",
    # First 20 chars of the key (partial match)
    b"1091201329673994292",
]


def find_rsa_key_in_memory(pm: pymem.Pymem) -> list[tuple[int, bytes]]:
    """Search process memory for RSA key strings."""
    results = []

    MEM_COMMIT = 0x1000
    PAGE_READWRITE = 0x04
    PAGE_EXECUTE_READWRITE = 0x40

    class MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_size_t),
            ("AllocationBase", ctypes.c_size_t),
            ("AllocationProtect", wintypes.DWORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]

    VirtualQueryEx = ctypes.windll.kernel32.VirtualQueryEx

    address = 0
    mbi = MEMORY_BASIC_INFORMATION()

    print("Scanning memory for RSA key...")

    while address < 0x7FFFFFFF:
        result = VirtualQueryEx(
            pm.process_handle,
            ctypes.c_size_t(address),
            ctypes.byref(mbi),
            ctypes.sizeof(mbi)
        )
        if result == 0:
            break

        if (mbi.State == MEM_COMMIT and
            mbi.RegionSize > 0 and
            mbi.RegionSize < 100 * 1024 * 1024):

            try:
                data = pm.read_bytes(mbi.BaseAddress, mbi.RegionSize)

                for key_pattern in KNOWN_RSA_KEYS:
                    idx = 0
                    while True:
                        idx = data.find(key_pattern, idx)
                        if idx == -1:
                            break
                        addr = mbi.BaseAddress + idx
                        # Read the full key string (look for end - null terminator or non-digit)
                        full_key = bytearray()
                        for j in range(idx, min(idx + 500, len(data))):
                            if data[j] == 0 or not (48 <= data[j] <= 57):  # null or non-digit
                                break
                            full_key.append(data[j])

                        results.append((addr, bytes(full_key)))
                        print(f"  Found RSA key at 0x{addr:08X}: {bytes(full_key)[:40]}...")
                        idx += 1

            except Exception:
                pass

        address = mbi.BaseAddress + mbi.RegionSize

    return results


def find_server_address_in_memory(pm: pymem.Pymem, server_ip: str = "87.98.220.215") -> list[tuple[int, bytes]]:
    """Search process memory for server IP address strings."""
    results = []
    pattern = server_ip.encode('ascii')

    MEM_COMMIT = 0x1000

    class MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_size_t),
            ("AllocationBase", ctypes.c_size_t),
            ("AllocationProtect", wintypes.DWORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]

    VirtualQueryEx = ctypes.windll.kernel32.VirtualQueryEx

    address = 0
    mbi = MEMORY_BASIC_INFORMATION()

    print(f"\nScanning memory for server address '{server_ip}'...")

    while address < 0x7FFFFFFF:
        result = VirtualQueryEx(
            pm.process_handle,
            ctypes.c_size_t(address),
            ctypes.byref(mbi),
            ctypes.sizeof(mbi)
        )
        if result == 0:
            break

        if (mbi.State == MEM_COMMIT and
            mbi.RegionSize > 0 and
            mbi.RegionSize < 100 * 1024 * 1024):

            try:
                data = pm.read_bytes(mbi.BaseAddress, mbi.RegionSize)
                idx = 0
                while True:
                    idx = data.find(pattern, idx)
                    if idx == -1:
                        break
                    addr = mbi.BaseAddress + idx
                    results.append((addr, pattern))
                    print(f"  Found server IP at 0x{addr:08X}")
                    idx += 1
            except Exception:
                pass

        address = mbi.BaseAddress + mbi.RegionSize

    return results


def patch_memory(pm: pymem.Pymem, address: int, old_data: bytes, new_data: bytes) -> bool:
    """Patch a memory location, handling page protections."""
    PAGE_EXECUTE_READWRITE = 0x40

    VirtualProtectEx = ctypes.windll.kernel32.VirtualProtectEx
    old_protect = wintypes.DWORD()

    # Make memory writable
    if not VirtualProtectEx(
        pm.process_handle,
        ctypes.c_size_t(address),
        len(new_data),
        PAGE_EXECUTE_READWRITE,
        ctypes.byref(old_protect)
    ):
        print(f"  WARNING: Could not change memory protection at 0x{address:08X}")
        # Try anyway

    try:
        # Verify current content
        current = pm.read_bytes(address, len(old_data))
        if current != old_data:
            print(f"  WARNING: Memory content doesn't match expected value at 0x{address:08X}")
            print(f"    Expected: {old_data[:30]}...")
            print(f"    Found:    {current[:30]}...")

        # Write new data (pad with null if shorter)
        write_data = new_data
        if len(new_data) < len(old_data):
            write_data = new_data + b'\x00' * (len(old_data) - len(new_data))

        pm.write_bytes(address, write_data, len(write_data))

        # Verify
        verify = pm.read_bytes(address, len(new_data))
        if verify == new_data:
            print(f"  OK: Patched 0x{address:08X}")
            return True
        else:
            print(f"  FAILED: Verification failed at 0x{address:08X}")
            return False

    except Exception as e:
        print(f"  ERROR: {e}")
        return False

    finally:
        # Restore protection
        VirtualProtectEx(
            pm.process_handle,
            ctypes.c_size_t(address),
            len(new_data),
            old_protect.value,
            ctypes.byref(old_protect)
        )


def main():
    if len(sys.argv) < 2:
        print("Usage: python patcher.py <proxy_rsa_key_n>")
        print("       python patcher.py scan  (just scan, don't patch)")
        sys.exit(1)

    mode = sys.argv[1]
    process_name = "dbvStart.exe"

    print(f"Attaching to {process_name}...")
    try:
        pm = pymem.Pymem(process_name)
    except pymem.exception.ProcessNotFound:
        print(f"ERROR: {process_name} not found. Is the game running?")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Could not attach: {e}")
        print("Try running as Administrator.")
        sys.exit(1)

    print(f"Attached! PID: {pm.process_id}")

    # Find RSA keys
    rsa_locations = find_rsa_key_in_memory(pm)
    if not rsa_locations:
        print("\nNo RSA key found in memory!")
        print("The client may use a different RSA key format or it's not loaded yet.")
        print("Make sure you're at the login screen (key is loaded during init).")
    else:
        print(f"\nFound {len(rsa_locations)} RSA key location(s)")

    # Find server addresses
    ip_locations = find_server_address_in_memory(pm)
    if not ip_locations:
        print("\nNo server IP found in memory!")
    else:
        print(f"\nFound {len(ip_locations)} server IP location(s)")

    if mode == 'scan':
        print("\nScan complete. Run with proxy RSA key to patch.")
        pm.close_process()
        return

    # Patch mode
    proxy_rsa_key = mode.encode('ascii')
    print(f"\nProxy RSA key (first 40 chars): {proxy_rsa_key[:40]}...")

    # Patch RSA keys
    patched_rsa = 0
    for addr, old_key in rsa_locations:
        print(f"\nPatching RSA key at 0x{addr:08X}...")
        if patch_memory(pm, addr, old_key, proxy_rsa_key):
            patched_rsa += 1

    # Patch server IP to localhost
    patched_ip = 0
    localhost = b"127.0.0.1"
    for addr, old_ip in ip_locations:
        print(f"\nPatching server IP at 0x{addr:08X}...")
        if patch_memory(pm, addr, old_ip, localhost):
            patched_ip += 1

    print(f"\n=== Patching Summary ===")
    print(f"RSA keys patched: {patched_rsa}/{len(rsa_locations)}")
    print(f"Server IPs patched: {patched_ip}/{len(ip_locations)}")

    if patched_rsa > 0 and patched_ip > 0:
        print("\nSUCCESS! The client should now connect through the proxy.")
        print("Start the bot (python bot.py) and then login in the game client.")
    elif patched_rsa > 0:
        print("\nRSA key patched but no server IP found to patch.")
        print("You may need to manually edit hosts file or DNS to redirect.")
    else:
        print("\nWARNING: Patching may have failed. Check the output above.")

    pm.close_process()


if __name__ == "__main__":
    main()
