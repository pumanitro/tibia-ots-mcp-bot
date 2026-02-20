"""
Named pipe client for communicating with the injected dbvbot.dll.

The DLL creates \\.\pipe\dbvbot and serves creature data as newline-delimited
JSON. This module provides a clean Python interface to that pipe.
"""

import ctypes
import ctypes.wintypes
import json
import logging

log = logging.getLogger("dll_bridge")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Win32 constants
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.wintypes.HANDLE(-1).value
PIPE_READMODE_BYTE = 0x00000000

PIPE_NAME = b"\\\\.\\pipe\\dbvbot"


class DllBridge:
    """Client for the dbvbot.dll named pipe."""

    def __init__(self):
        self._handle = None
        self._buffer = ""

    @property
    def connected(self) -> bool:
        return self._handle is not None

    def connect(self) -> bool:
        """Open the named pipe. Returns True on success."""
        if self._handle is not None:
            return True

        handle = kernel32.CreateFileA(
            PIPE_NAME,
            GENERIC_READ | GENERIC_WRITE,
            0,       # no sharing
            None,    # default security
            OPEN_EXISTING,
            0,       # default attributes
            None,    # no template
        )

        if handle == INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            log.debug(f"Pipe not available (error {err})")
            return False

        # Set to byte read mode
        mode = ctypes.wintypes.DWORD(PIPE_READMODE_BYTE)
        kernel32.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None)

        self._handle = handle
        self._buffer = ""
        log.info("Connected to dbvbot pipe")
        return True

    def pipe_exists(self) -> bool:
        """Check if the named pipe exists (DLL is loaded and running)."""
        if self._handle is not None:
            return True  # already connected
        return bool(kernel32.WaitNamedPipeA(PIPE_NAME, 0))

    def send_command(self, cmd: dict) -> bool:
        """Send a JSON command to the DLL. Returns True on success."""
        if self._handle is None:
            return False

        data = (json.dumps(cmd) + "\n").encode("utf-8")
        written = ctypes.wintypes.DWORD(0)
        ok = kernel32.WriteFile(self._handle, data, len(data), ctypes.byref(written), None)
        if not ok:
            log.warning(f"WriteFile failed (error {ctypes.get_last_error()})")
            self.disconnect()
            return False
        return True

    def read_creatures(self) -> list[dict] | None:
        """Non-blocking read of creature data from the pipe.

        Returns a list of creature dicts, or None if no data available.
        """
        if self._handle is None:
            return None

        # Peek to see if data is available
        bytes_available = ctypes.wintypes.DWORD(0)
        ok = kernel32.PeekNamedPipe(
            self._handle, None, 0, None, ctypes.byref(bytes_available), None
        )
        if not ok:
            err = ctypes.get_last_error()
            log.warning(f"PeekNamedPipe failed (error {err})")
            self.disconnect()
            return None

        if bytes_available.value == 0:
            return None

        # Read available data
        buf = ctypes.create_string_buffer(bytes_available.value + 1)
        bytes_read = ctypes.wintypes.DWORD(0)
        ok = kernel32.ReadFile(self._handle, buf, bytes_available.value, ctypes.byref(bytes_read), None)
        if not ok or bytes_read.value == 0:
            self.disconnect()
            return None

        self._buffer += buf.raw[:bytes_read.value].decode("utf-8", errors="replace")

        # Parse complete lines — use the LAST complete JSON (most recent data)
        creatures = None
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if "creatures" in data:
                    creatures = data["creatures"]
            except json.JSONDecodeError:
                pass

        return creatures

    def disconnect(self):
        """Close the pipe handle."""
        if self._handle is not None:
            try:
                kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
            self._buffer = ""
            log.info("Disconnected from dbvbot pipe")
