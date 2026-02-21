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
import logging

log = logging.getLogger("patcher")

# Win32 memory constants
MEM_COMMIT = 0x1000
PAGE_READWRITE = 0x04
PAGE_EXECUTE_READWRITE = 0x40
MAX_REGION_SIZE = 100 * 1024 * 1024  # 100 MB


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
    VirtualQueryEx = ctypes.windll.kernel32.VirtualQueryEx

    address = 0
    mbi = MEMORY_BASIC_INFORMATION()

    log.info("Scanning memory for RSA key...")

    while address < 0xFFFFFFFF:
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
            mbi.RegionSize < MAX_REGION_SIZE):

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
                        log.info(f"  Found RSA key at 0x{addr:08X}: {bytes(full_key)[:40]}...")
                        idx += 1

            except Exception:
                pass

        address = mbi.BaseAddress + mbi.RegionSize

    return results


def find_server_address_in_memory(pm: pymem.Pymem, server_ip: str = "87.98.220.215") -> list[tuple[int, bytes]]:
    """Search process memory for server IP address strings."""
    results = []
    pattern = server_ip.encode('ascii')
    VirtualQueryEx = ctypes.windll.kernel32.VirtualQueryEx

    address = 0
    mbi = MEMORY_BASIC_INFORMATION()

    log.info(f"Scanning memory for server address '{server_ip}'...")

    while address < 0xFFFFFFFF:
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
            mbi.RegionSize < MAX_REGION_SIZE):

            try:
                data = pm.read_bytes(mbi.BaseAddress, mbi.RegionSize)
                idx = 0
                while True:
                    idx = data.find(pattern, idx)
                    if idx == -1:
                        break
                    addr = mbi.BaseAddress + idx
                    results.append((addr, pattern))
                    log.info(f"  Found server IP at 0x{addr:08X}")
                    idx += 1
            except Exception:
                pass

        address = mbi.BaseAddress + mbi.RegionSize

    return results


def patch_memory(pm: pymem.Pymem, address: int, old_data: bytes, new_data: bytes) -> bool:
    """Patch a memory location, handling page protections."""
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
        log.warning(f"Could not change memory protection at 0x{address:08X}")
        # Try anyway

    try:
        # Verify current content
        current = pm.read_bytes(address, len(old_data))
        if current != old_data:
            log.warning(f"Memory content doesn't match expected value at 0x{address:08X}")
            log.warning(f"  Expected: {old_data[:30]}...")
            log.warning(f"  Found:    {current[:30]}...")

        # Write new data (pad with null if shorter)
        write_data = new_data
        if len(new_data) < len(old_data):
            write_data = new_data + b'\x00' * (len(old_data) - len(new_data))

        pm.write_bytes(address, write_data, len(write_data))

        # Verify
        verify = pm.read_bytes(address, len(new_data))
        if verify == new_data:
            log.info(f"Patched 0x{address:08X}")
            return True
        else:
            log.error(f"Verification failed at 0x{address:08X}")
            return False

    except Exception as e:
        log.error(f"Patch error: {e}")
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
        log.error("Usage: python patcher.py <proxy_rsa_key_n>")
        log.error("       python patcher.py scan  (just scan, don't patch)")
        sys.exit(1)

    mode = sys.argv[1]
    process_name = "dbvStart.exe"

    log.info(f"Attaching to {process_name}...")
    try:
        pm = pymem.Pymem(process_name)
    except pymem.exception.ProcessNotFound:
        log.error(f"{process_name} not found. Is the game running?")
        sys.exit(1)
    except Exception as e:
        log.error(f"Could not attach: {e}")
        log.error("Try running as Administrator.")
        sys.exit(1)

    log.info(f"Attached! PID: {pm.process_id}")

    # Find RSA keys
    rsa_locations = find_rsa_key_in_memory(pm)
    if not rsa_locations:
        log.warning("No RSA key found in memory!")
        log.warning("The client may use a different RSA key format or it's not loaded yet.")
        log.warning("Make sure you're at the login screen (key is loaded during init).")
    else:
        log.info(f"Found {len(rsa_locations)} RSA key location(s)")

    # Find server addresses
    ip_locations = find_server_address_in_memory(pm)
    if not ip_locations:
        log.warning("No server IP found in memory!")
    else:
        log.info(f"Found {len(ip_locations)} server IP location(s)")

    if mode == 'scan':
        log.info("Scan complete. Run with proxy RSA key to patch.")
        pm.close_process()
        return

    # Patch mode
    proxy_rsa_key = mode.encode('ascii')
    log.info(f"Proxy RSA key (first 40 chars): {proxy_rsa_key[:40]}...")

    # Patch RSA keys
    patched_rsa = 0
    for addr, old_key in rsa_locations:
        log.info(f"Patching RSA key at 0x{addr:08X}...")
        if patch_memory(pm, addr, old_key, proxy_rsa_key):
            patched_rsa += 1

    # Patch server IP to localhost
    patched_ip = 0
    localhost = b"127.0.0.1"
    for addr, old_ip in ip_locations:
        log.info(f"Patching server IP at 0x{addr:08X}...")
        if patch_memory(pm, addr, old_ip, localhost):
            patched_ip += 1

    log.info(f"=== Patching Summary ===")
    log.info(f"RSA keys patched: {patched_rsa}/{len(rsa_locations)}")
    log.info(f"Server IPs patched: {patched_ip}/{len(ip_locations)}")

    if patched_rsa > 0 and patched_ip > 0:
        log.info("SUCCESS! The client should now connect through the proxy.")
        log.info("Start the bot (python bot.py) and then login in the game client.")
    elif patched_rsa > 0:
        log.warning("RSA key patched but no server IP found to patch.")
        log.warning("You may need to manually edit hosts file or DNS to redirect.")
    else:
        log.warning("Patching may have failed. Check the output above.")

    pm.close_process()


if __name__ == "__main__":
    main()
