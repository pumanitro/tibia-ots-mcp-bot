"""
DLL Injector — injects dll/dbvbot.dll into dbvStart.exe using LoadLibraryA.

Handles 64-bit Python injecting into 32-bit target (WoW64) by resolving
the 32-bit kernel32.dll LoadLibraryA address in the target process.

Usage:
    import inject
    inject.inject()  # finds dbvStart.exe, injects the DLL
"""

import ctypes
import ctypes.wintypes
import os
import struct
import sys
import logging

log = logging.getLogger("inject")

# Win32 constants
PROCESS_ALL_ACCESS = 0x1F0FFF
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
INFINITE = 0xFFFFFFFF
TH32CS_SNAPMODULE = 0x08
TH32CS_SNAPMODULE32 = 0x10
TH32CS_SNAPPROCESS = 0x02

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _get_pid(process_name: str = "dbvStart.exe") -> int:
    """Get PID of target process using CreateToolhelp32Snapshot."""

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.wintypes.DWORD),
            ("cntUsage", ctypes.wintypes.DWORD),
            ("th32ProcessID", ctypes.wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", ctypes.wintypes.DWORD),
            ("cntThreads", ctypes.wintypes.DWORD),
            ("th32ParentProcessID", ctypes.wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(pe)

    try:
        if kernel32.Process32First(snapshot, ctypes.byref(pe)):
            while True:
                name = pe.szExeFile.decode("utf-8", errors="ignore")
                if name.lower() == process_name.lower():
                    return pe.th32ProcessID
                if not kernel32.Process32Next(snapshot, ctypes.byref(pe)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)

    raise RuntimeError(f"Process '{process_name}' not found")


def _is_target_32bit(h_process) -> bool:
    """Check if target process is 32-bit (WoW64)."""
    is_wow64 = ctypes.wintypes.BOOL(False)
    kernel32.IsWow64Process(h_process, ctypes.byref(is_wow64))
    return bool(is_wow64.value)


def _get_module_base_32(pid: int, module_name: str) -> int:
    """Get the base address of a 32-bit module in the target process.

    Uses TH32CS_SNAPMODULE32 to enumerate 32-bit modules even from
    a 64-bit process.
    """

    class MODULEENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.wintypes.DWORD),
            ("th32ModuleID", ctypes.wintypes.DWORD),
            ("th32ProcessID", ctypes.wintypes.DWORD),
            ("GlblcntUsage", ctypes.wintypes.DWORD),
            ("ProccntUsage", ctypes.wintypes.DWORD),
            ("modBaseAddr", ctypes.POINTER(ctypes.wintypes.BYTE)),
            ("modBaseSize", ctypes.wintypes.DWORD),
            ("hModule", ctypes.wintypes.HMODULE),
            ("szModule", ctypes.c_char * 256),
            ("szExePath", ctypes.c_char * 260),
        ]

    # TH32CS_SNAPMODULE32 lets 64-bit process enumerate 32-bit modules
    flags = TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32
    snapshot = kernel32.CreateToolhelp32Snapshot(flags, pid)
    if snapshot == ctypes.wintypes.HANDLE(-1).value:
        raise RuntimeError(f"CreateToolhelp32Snapshot(modules) failed: {ctypes.get_last_error()}")

    me = MODULEENTRY32()
    me.dwSize = ctypes.sizeof(me)

    try:
        if kernel32.Module32First(snapshot, ctypes.byref(me)):
            while True:
                name = me.szModule.decode("utf-8", errors="ignore").lower()
                if name == module_name.lower():
                    base = ctypes.cast(me.modBaseAddr, ctypes.c_void_p).value
                    return base
                if not kernel32.Module32Next(snapshot, ctypes.byref(me)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)

    raise RuntimeError(f"Module '{module_name}' not found in process {pid}")


def _get_export_rva(dll_path: str, export_name: str) -> int:
    """Parse a PE file on disk to find the RVA of an exported function.

    This lets us calculate the function address in a remote 32-bit process
    by adding the RVA to the module's base address.
    """
    with open(dll_path, "rb") as f:
        # DOS header
        f.seek(0x3C)
        pe_offset = struct.unpack("<I", f.read(4))[0]

        # PE signature + COFF header
        f.seek(pe_offset)
        sig = f.read(4)
        if sig != b"PE\x00\x00":
            raise RuntimeError("Not a valid PE file")

        # COFF header
        machine = struct.unpack("<H", f.read(2))[0]
        num_sections = struct.unpack("<H", f.read(2))[0]
        f.read(12)  # skip timestamp, symbol table, num symbols
        optional_hdr_size = struct.unpack("<H", f.read(2))[0]
        f.read(2)  # characteristics

        # Optional header
        opt_start = f.tell()
        magic = struct.unpack("<H", f.read(2))[0]

        if magic == 0x10B:  # PE32
            # Skip to data directories (offset 96 from optional header start)
            f.seek(opt_start + 96)
        elif magic == 0x20B:  # PE32+
            f.seek(opt_start + 112)
        else:
            raise RuntimeError(f"Unknown PE magic: 0x{magic:04X}")

        # Export directory RVA and size (first data directory entry)
        export_rva = struct.unpack("<I", f.read(4))[0]
        export_size = struct.unpack("<I", f.read(4))[0]

        if export_rva == 0:
            raise RuntimeError("No export directory")

        # Read section headers to find file offset of export directory
        f.seek(opt_start + optional_hdr_size)
        sections = []
        for _ in range(num_sections):
            sec_name = f.read(8)
            virtual_size = struct.unpack("<I", f.read(4))[0]
            virtual_addr = struct.unpack("<I", f.read(4))[0]
            raw_size = struct.unpack("<I", f.read(4))[0]
            raw_offset = struct.unpack("<I", f.read(4))[0]
            f.read(16)  # skip rest
            sections.append((virtual_addr, virtual_size, raw_offset, raw_size))

        def rva_to_file(rva):
            for va, vs, ro, rs in sections:
                if va <= rva < va + rs:
                    return ro + (rva - va)
            raise RuntimeError(f"Cannot resolve RVA 0x{rva:08X}")

        # Parse export directory
        f.seek(rva_to_file(export_rva))
        f.read(12)  # skip characteristics, timestamp, version
        f.read(4)   # name RVA
        f.read(4)   # ordinal base
        num_functions = struct.unpack("<I", f.read(4))[0]
        num_names = struct.unpack("<I", f.read(4))[0]
        addr_table_rva = struct.unpack("<I", f.read(4))[0]
        name_table_rva = struct.unpack("<I", f.read(4))[0]
        ordinal_table_rva = struct.unpack("<I", f.read(4))[0]

        # Read name pointer table
        f.seek(rva_to_file(name_table_rva))
        name_rvas = [struct.unpack("<I", f.read(4))[0] for _ in range(num_names)]

        # Read ordinal table
        f.seek(rva_to_file(ordinal_table_rva))
        ordinals = [struct.unpack("<H", f.read(2))[0] for _ in range(num_names)]

        # Read address table
        f.seek(rva_to_file(addr_table_rva))
        addresses = [struct.unpack("<I", f.read(4))[0] for _ in range(num_functions)]

        # Find our export
        target = export_name.encode("ascii")
        for i, name_rva in enumerate(name_rvas):
            f.seek(rva_to_file(name_rva))
            name = b""
            while True:
                c = f.read(1)
                if c == b"\x00" or not c:
                    break
                name += c
            if name == target:
                return addresses[ordinals[i]]

        raise RuntimeError(f"Export '{export_name}' not found")


def inject(dll_path: str = None) -> bool:
    """Inject the DLL into dbvStart.exe.

    Handles cross-architecture injection (64-bit Python → 32-bit target)
    by resolving LoadLibraryA from the target's own kernel32.dll.
    """
    if dll_path is None:
        dll_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dll", "dbvbot.dll")

    if not os.path.exists(dll_path):
        raise RuntimeError(f"DLL not found: {dll_path}")

    dll_path = os.path.abspath(dll_path)
    dll_bytes = dll_path.encode("utf-8") + b"\x00"

    pid = _get_pid()
    log.info(f"Target PID: {pid}")

    # 1. Open the target process
    h_process = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h_process:
        raise RuntimeError(f"OpenProcess failed (error {ctypes.get_last_error()}). Run as Administrator?")

    try:
        is_32bit_target = _is_target_32bit(h_process)
        is_64bit_python = (struct.calcsize("P") == 8)
        cross_arch = is_64bit_python and is_32bit_target
        log.info(f"Target is WoW64 (32-bit): {is_32bit_target}, cross-arch: {cross_arch}")

        if cross_arch:
            # Find the 32-bit kernel32.dll in the target process
            k32_base = _get_module_base_32(pid, "kernel32.dll")
            log.info(f"Target kernel32.dll base: 0x{k32_base:08X}")

            # Find the 32-bit kernel32.dll file on disk
            k32_path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                                     "SysWOW64", "kernel32.dll")
            if not os.path.exists(k32_path):
                raise RuntimeError(f"32-bit kernel32.dll not found at {k32_path}")

            # Parse the PE to get LoadLibraryA's RVA
            lla_rva = _get_export_rva(k32_path, "LoadLibraryA")
            load_library_addr = k32_base + lla_rva
            log.info(f"LoadLibraryA in target: 0x{load_library_addr:08X} (base+0x{lla_rva:X})")
        else:
            # Same architecture — our kernel32 address works
            h_kernel32 = kernel32.GetModuleHandleA(b"kernel32.dll")
            if not h_kernel32:
                raise RuntimeError("GetModuleHandleA(kernel32.dll) failed")
            kernel32.GetProcAddress.restype = ctypes.c_void_p
            load_library_addr = kernel32.GetProcAddress(h_kernel32, b"LoadLibraryA")
            if not load_library_addr:
                raise RuntimeError("GetProcAddress(LoadLibraryA) failed")

        # 2. Allocate memory in target for DLL path string
        kernel32.VirtualAllocEx.restype = ctypes.c_void_p
        remote_mem = kernel32.VirtualAllocEx(
            h_process, None, len(dll_bytes), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE
        )
        if not remote_mem:
            raise RuntimeError(f"VirtualAllocEx failed (error {ctypes.get_last_error()})")

        # 3. Write DLL path into target process
        written = ctypes.c_size_t(0)
        if not kernel32.WriteProcessMemory(h_process, remote_mem, dll_bytes,
                                            len(dll_bytes), ctypes.byref(written)):
            raise RuntimeError(f"WriteProcessMemory failed (error {ctypes.get_last_error()})")

        # 4. Create remote thread calling LoadLibraryA(dll_path)
        thread_id = ctypes.wintypes.DWORD(0)
        kernel32.CreateRemoteThread.restype = ctypes.wintypes.HANDLE
        h_thread = kernel32.CreateRemoteThread(
            h_process, None, 0,
            ctypes.c_void_p(load_library_addr),
            ctypes.c_void_p(remote_mem),
            0, ctypes.byref(thread_id)
        )
        if not h_thread:
            raise RuntimeError(f"CreateRemoteThread failed (error {ctypes.get_last_error()})")

        # 5. Wait for LoadLibraryA to finish
        kernel32.WaitForSingleObject(h_thread, INFINITE)

        # 6. Check return value (module handle; 0 = failure)
        exit_code = ctypes.wintypes.DWORD(0)
        kernel32.GetExitCodeThread(h_thread, ctypes.byref(exit_code))
        kernel32.CloseHandle(h_thread)

        # 7. Free the remote string memory
        kernel32.VirtualFreeEx(h_process, ctypes.c_void_p(remote_mem), 0, MEM_RELEASE)

        if exit_code.value == 0:
            raise RuntimeError("LoadLibraryA returned NULL — DLL failed to load in target process")

        log.info(f"DLL injected successfully (module handle: 0x{exit_code.value:08X})")
        return True

    finally:
        kernel32.CloseHandle(h_process)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        inject()
        print("Injection successful.")
    except Exception as e:
        print(f"Injection failed: {e}")
        sys.exit(1)
