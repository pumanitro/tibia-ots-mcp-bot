"""
OT Protocol Proxy - Sits between the game client and server.

Handles TWO phases:
1. LOGIN phase (port 7171): Intercepts login, extracts XTEA keys,
   modifies character list response to redirect game server to our proxy.
2. GAME phase (port 7172): Intercepts game traffic, allows packet injection.
"""

import asyncio
import struct
import logging
import time
from crypto import (
    rsa_decrypt, rsa_encrypt, xtea_decrypt, xtea_encrypt,
    adler32_checksum, generate_proxy_rsa_keypair, get_default_rsa_key,
    DEFAULT_RSA_N, DEFAULT_RSA_E
)
from protocol import PacketReader, PacketWriter, ServerOpcode, ClientOpcode
from constants import SERVER_IP_BYTES, SERVER_IP_STR

log = logging.getLogger("proxy")


class OTProxy:
    """
    TCP proxy for Open Tibia protocol.

    Handles both login and game connections.
    """

    def __init__(self, server_host: str, server_port: int, listen_port: int,
                 is_login_proxy: bool = False, shared_rsa_key=None):
        self.server_host = server_host
        self.server_port = server_port
        self.listen_port = listen_port
        self.is_login_proxy = is_login_proxy

        # Use the default OTClient RSA key - we know the private key!
        # No need to patch the client, just decrypt with the known key.
        self.default_rsa_key = get_default_rsa_key()

        # Lazy-init proxy RSA key only if needed (keygen is expensive)
        self._shared_rsa_key = shared_rsa_key
        self._proxy_rsa_key = None
        self.server_rsa_n = DEFAULT_RSA_N
        self.server_rsa_e = DEFAULT_RSA_E

        # Session state
        self.xtea_keys = None
        self.logged_in = False

        # Connection handles
        self.client_reader = None
        self.client_writer = None
        self.server_reader = None
        self.server_writer = None

        # Callbacks for bot
        self.on_server_packet = None          # legacy — only bot.py/start.py use it
        self._client_packet_callbacks = []    # list of (opcode, reader) callables
        self.on_login_success = None
        self.on_game_disconnected = None
        self.on_raw_server_data = None  # Called with full decrypted bytes
        self._inject_queue = asyncio.Queue()

        # Stats
        self.packets_from_server = 0
        self.packets_from_client = 0

        # Track the active connection handler task for clean shutdown
        self._connection_task: asyncio.Task | None = None
        self._server: asyncio.AbstractServer | None = None

        # Timestamps for proxy sequence debugging
        self._ts_created = time.time()
        self._ts_listening = None
        self._ts_client_connected = None
        self._ts_server_connected = None
        self._ts_xtea_captured = None
        self._ts_logged_in = None


    # ── Client packet callback registry ────────────────────────────
    def register_client_packet_callback(self, callback):
        if callback not in self._client_packet_callbacks:
            self._client_packet_callbacks.append(callback)

    def unregister_client_packet_callback(self, callback):
        try:
            self._client_packet_callbacks.remove(callback)
        except ValueError:
            pass

    @property
    def on_client_packet(self):
        return self._client_packet_callbacks[0] if self._client_packet_callbacks else None

    @on_client_packet.setter
    def on_client_packet(self, callback):
        if callback is None:
            self._client_packet_callbacks.clear()
        elif self._client_packet_callbacks:
            self._client_packet_callbacks[0] = callback
        else:
            self._client_packet_callbacks.append(callback)

    @property
    def proxy_rsa_key(self):
        if self._proxy_rsa_key is None:
            self._proxy_rsa_key = self._shared_rsa_key or generate_proxy_rsa_keypair()
        return self._proxy_rsa_key

    async def start(self):
        """Start the proxy server."""
        server = await asyncio.start_server(
            self._handle_client_connection,
            '127.0.0.1',
            self.listen_port
        )
        self._server = server
        self._ts_listening = time.time()
        addr = server.sockets[0].getsockname()
        mode = "LOGIN" if self.is_login_proxy else "GAME"
        log.info(f"[{mode}] Proxy listening on {addr[0]}:{addr[1]} -> {self.server_host}:{self.server_port}")

        async with server:
            await server.serve_forever()

    def close_server(self):
        """Explicitly close the listening server socket (for clean reset)."""
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None

    async def _handle_client_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle a new client connection."""
        # Track this handler task so it can be cancelled on reset
        self._connection_task = asyncio.current_task()
        client_addr = writer.get_extra_info('peername')
        mode = "LOGIN" if self.is_login_proxy else "GAME"
        log.info(f"[{mode}] Client connected from {client_addr}")

        # H6: If there's already an active session, close the old one first
        if self.client_writer is not None:
            log.warning(f"[{mode}] New connection while session active — closing old connection")
            try:
                self.client_writer.close()
                await self.client_writer.wait_closed()
            except Exception:
                pass
            if self.server_writer is not None:
                try:
                    self.server_writer.close()
                    await self.server_writer.wait_closed()
                except Exception:
                    pass

        self.client_reader = reader
        self.client_writer = writer
        self._ts_client_connected = time.time()
        self.logged_in = False
        self.xtea_keys = None

        try:
            self.server_reader, self.server_writer = await asyncio.open_connection(
                self.server_host, self.server_port
            )
            self._ts_server_connected = time.time()
            log.info(f"[{mode}] Connected to server {self.server_host}:{self.server_port}")
        except Exception as e:
            log.error(f"[{mode}] Failed to connect to server: {e}")
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return

        try:
            if self.is_login_proxy:
                await self._handle_login_session()
            else:
                await self._run_relay_loop()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"[{mode}] Error: {e}")
        finally:
            log.info(f"[{mode}] Game connection ended, cleaning up")
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            if self.server_writer:
                self.server_writer.close()
                try:
                    await self.server_writer.wait_closed()
                except Exception:
                    pass
            self.client_writer = None
            self.server_writer = None
            if self.on_game_disconnected:
                try:
                    self.on_game_disconnected()
                except Exception as e:
                    log.error(f"[{mode}] on_game_disconnected callback error: {e}")

    async def _handle_login_session(self):
        """
        Handle a login session:
        1. Client sends login packet -> we intercept RSA, extract XTEA, re-encrypt, forward
        2. Server sends character list -> we decrypt, modify game server IP, re-encrypt, forward
        """
        # Step 1: Client login packet
        raw = await self._read_packet(self.client_reader)
        if raw is None:
            log.error("[LOGIN] No login packet received")
            return

        log.info(f"[LOGIN] Received login packet ({len(raw)} bytes)")

        processed = self._process_login_packet(raw)
        if processed is not None:
            self.server_writer.write(self._wrap_packet(processed))
            await self.server_writer.drain()
            log.info("[LOGIN] Forwarded login packet to server")
        else:
            self.server_writer.write(self._wrap_packet(raw))
            await self.server_writer.drain()
            log.warning("[LOGIN] Could not process login packet, forwarded as-is")

        # Step 2: Server response (character list)
        response = await self._read_packet(self.server_reader)
        if response is None:
            log.error("[LOGIN] No response from server")
            return

        log.info(f"[LOGIN] Received server response ({len(response)} bytes)")

        if self.xtea_keys:
            modified = self._modify_login_response(response)
            if modified:
                self.client_writer.write(self._wrap_packet(modified))
                await self.client_writer.drain()
                log.info("[LOGIN] Forwarded modified character list (game IP -> 127.0.0.1)")
                return

        # Forward as-is if we couldn't modify
        self.client_writer.write(self._wrap_packet(response))
        await self.client_writer.drain()
        log.info("[LOGIN] Forwarded response as-is")

    def _modify_login_response(self, data: bytes) -> bytes | None:
        """
        Decrypt the login response, modify game server IP to localhost,
        re-encrypt and return.
        """
        try:
            # Detect checksum
            has_checksum = False
            offset = 0
            if len(data) > 4:
                checksum = struct.unpack_from('<I', data, 0)[0]
                computed = adler32_checksum(data[4:])
                if checksum == computed:
                    has_checksum = True
                    offset = 4

            encrypted = data[offset:]
            if len(encrypted) % 8 != 0:
                log.warning("[LOGIN] Response not aligned to 8 bytes")
                return None

            decrypted = xtea_decrypt(encrypted, self.xtea_keys)
            inner_len = struct.unpack_from('<H', decrypted, 0)[0]
            payload = decrypted[2:2 + inner_len]

            log.info(f"[LOGIN] Decrypted response: {inner_len} bytes, first byte=0x{payload[0]:02X}")

            # Replace server IP in the response
            # The character list contains IP addresses as 4-byte values (packed IP)
            # and also as strings in some protocol versions
            server_ip_bytes = SERVER_IP_BYTES
            localhost_bytes = bytes([127, 0, 0, 1])

            # Also try string replacement
            server_ip_str = SERVER_IP_STR
            localhost_str = b"127.0.0.1" + b"\x00" * max(0, len(SERVER_IP_STR) - 9)

            modified_payload = bytearray(payload)
            replaced = False

            # Replace packed IP (4 bytes)
            idx = 0
            while True:
                idx = modified_payload.find(server_ip_bytes, idx)
                if idx == -1:
                    break
                modified_payload[idx:idx + 4] = localhost_bytes
                log.info(f"[LOGIN] Replaced packed IP at offset {idx}")
                replaced = True
                idx += 4

            # Replace string IP
            idx = 0
            while True:
                idx = modified_payload.find(server_ip_str, idx)
                if idx == -1:
                    break
                modified_payload[idx:idx + len(server_ip_str)] = localhost_str[:len(server_ip_str)]
                log.info(f"[LOGIN] Replaced string IP at offset {idx}")
                replaced = True
                idx += len(server_ip_str)

            if not replaced:
                log.warning("[LOGIN] No server IP found in character list to replace!")
                log.warning("[LOGIN] The response may use a different IP format.")
                # Try to log the raw payload for debugging
                log.debug(f"[LOGIN] Payload hex: {payload.hex()}")

            # Re-encrypt
            new_inner = struct.pack('<H', len(modified_payload)) + bytes(modified_payload)
            # Pad to match original
            if len(new_inner) < len(decrypted):
                new_inner = new_inner + decrypted[len(new_inner):]

            re_encrypted = xtea_encrypt(new_inner, self.xtea_keys)

            if has_checksum:
                new_checksum = adler32_checksum(re_encrypted)
                return struct.pack('<I', new_checksum) + re_encrypted
            else:
                return re_encrypted

        except Exception as e:
            log.error(f"[LOGIN] Error modifying response: {e}")
            log.debug("Login response modification traceback:", exc_info=True)
            return None

    async def _read_packet(self, reader: asyncio.StreamReader) -> bytes | None:
        """Read a full OT protocol packet (length-prefixed)."""
        try:
            header = await reader.readexactly(2)
            length = struct.unpack('<H', header)[0]
            if length == 0 or length > 65535:
                return None
            data = await reader.readexactly(length)
            return data
        except (asyncio.IncompleteReadError, ConnectionError):
            return None

    def _wrap_packet(self, data: bytes) -> bytes:
        """Wrap data with length header."""
        return struct.pack('<H', len(data)) + data

    async def _run_relay_loop(self):
        """Supervised relay loop. Creates 3 relay tasks and monitors them."""
        while True:
            client_task = asyncio.create_task(self._relay_client_to_server())
            server_task = asyncio.create_task(self._relay_server_to_client())
            inject_task = asyncio.create_task(self._process_inject_queue())
            tasks = {client_task, server_task, inject_task}

            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                for t in tasks:
                    t.cancel()
                for t in tasks:
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
                raise

            # Normal end: client or server disconnected
            for t in pending:
                t.cancel()
            for t in pending:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                        pass
                break

    async def _relay_client_to_server(self):
        """Relay packets from client to server, intercepting login."""
        while True:
            raw = await self._read_packet(self.client_reader)
            if raw is None:
                log.info("Client disconnected")
                break

            self.packets_from_client += 1

            if self.server_writer is None:
                log.warning("Server writer gone, stopping client relay")
                break

            if not self.logged_in:
                # Login phase: extract XTEA keys
                processed = self._process_login_packet(raw)
                if processed is not None:
                    self.server_writer.write(self._wrap_packet(processed))
                    await self.server_writer.drain()
                else:
                    self.server_writer.write(self._wrap_packet(raw))
                    await self.server_writer.drain()
            else:
                processed = self._process_client_game_packet(raw)
                self.server_writer.write(self._wrap_packet(processed))
                await self.server_writer.drain()

    async def _relay_server_to_client(self):
        """Relay packets from server to client."""
        while True:
            raw = await self._read_packet(self.server_reader)
            if raw is None:
                log.info("Server disconnected")
                break

            self.packets_from_server += 1

            if not self.logged_in and self.xtea_keys is not None:
                self.logged_in = True
                self._ts_logged_in = time.time()
                log.info("=== GAME SESSION ESTABLISHED ===")
                log.info(f"XTEA Keys: {' '.join(f'{k:08X}' for k in self.xtea_keys)}")
                if self.on_login_success:
                    self.on_login_success(self.xtea_keys)

            if self.logged_in and (self.on_server_packet or self.on_raw_server_data):
                try:
                    decrypted = self._decrypt_game_packet(raw)
                    if decrypted:
                        if self.on_raw_server_data:
                            try:
                                self.on_raw_server_data(decrypted)
                            except Exception:
                                pass
                        if self.on_server_packet:
                            pr = PacketReader(decrypted)
                            if pr.remaining > 0:
                                opcode = pr.read_u8()
                                try:
                                    self.on_server_packet(opcode, pr)
                                except Exception as e:
                                    log.debug(f"Server packet callback error: {e}")
                except Exception:
                    pass

            if self.client_writer is None:
                log.warning("Client writer gone, stopping server relay")
                break
            self.client_writer.write(self._wrap_packet(raw))
            await self.client_writer.drain()

    def _process_login_packet(self, data: bytes) -> bytes | None:
        """
        Process a login packet from the client.
        Extract XTEA keys from the RSA-encrypted block.

        Since the client uses the default OTClient RSA key and we know the
        private key, we just decrypt to extract XTEA keys and forward as-is.
        No re-encryption needed - the server has the same key.
        """
        try:
            has_checksum = False
            if len(data) > 4:
                checksum = struct.unpack_from('<I', data, 0)[0]
                computed = adler32_checksum(data[4:])
                if checksum == computed:
                    has_checksum = True

            offset = 4 if has_checksum else 0

            proto_byte = data[offset]
            log.info(f"Login packet: proto=0x{proto_byte:02X}, size={len(data)}, checksum={'yes' if has_checksum else 'no'}")

            # Skip small packets (handshake/challenge packets, not login)
            rsa_block_size = 128
            if len(data) < rsa_block_size + offset + 5:
                log.debug(f"Packet too small for RSA ({len(data)} bytes), forwarding as-is")
                return data  # Forward as-is

            # Try to find and decrypt the RSA block using the DEFAULT key
            # (the client encrypted with the default OTClient RSA public key)
            keys_to_try = [self.default_rsa_key, self.proxy_rsa_key]

            for key in keys_to_try:
                # Try the last 128 bytes first (most common RSA block position
                # in OT login packets), then fall back to brute-force iteration
                last_offset = len(data) - rsa_block_size
                offsets_to_try = [last_offset] if last_offset > offset else []
                offsets_to_try += [
                    o for o in range(offset + 1, len(data) - rsa_block_size + 1)
                    if o != last_offset
                ]

                for try_offset in offsets_to_try:
                    rsa_block = data[try_offset:try_offset + rsa_block_size]
                    try:
                        decrypted = rsa_decrypt(rsa_block, key)
                        if decrypted[0] == 0x00:
                            key_name = "DEFAULT" if key == self.default_rsa_key else "PROXY"
                            log.info(f"RSA block found at offset {try_offset} (using {key_name} key)")

                            xtea_data = decrypted[1:17]
                            self.xtea_keys = struct.unpack('<4I', xtea_data)
                            self._ts_xtea_captured = time.time()
                            log.info(f"XTEA keys: {' '.join(f'{k:08X}' for k in self.xtea_keys)}")

                            # Log additional info from the decrypted block
                            try:
                                pr = PacketReader(decrypted[17:])
                                gm_flag = decrypted[17] if len(decrypted) > 17 else 0
                                log.debug(f"GM flag: {gm_flag}")
                            except Exception:
                                pass

                            # Forward the original packet as-is
                            # (server can decrypt it with the same RSA private key)
                            return data
                    except Exception:
                        continue

            log.warning("Could not find RSA block in login packet!")
            log.warning(f"Packet hex (first 32 bytes): {data[:32].hex()}")
            return data  # Forward as-is anyway

        except Exception as e:
            log.error(f"Error processing login packet: {e}")
            return data

    def _decrypt_game_packet(self, data: bytes) -> bytes | None:
        """Decrypt an XTEA-encrypted game packet."""
        if self.xtea_keys is None:
            return None

        try:
            offset = 0
            if len(data) > 4:
                checksum = struct.unpack_from('<I', data, 0)[0]
                computed = adler32_checksum(data[4:])
                if checksum == computed:
                    offset = 4

            encrypted = data[offset:]
            if len(encrypted) % 8 != 0:
                return None

            decrypted = xtea_decrypt(encrypted, self.xtea_keys)
            inner_len = struct.unpack_from('<H', decrypted, 0)[0]
            if inner_len > len(decrypted) - 2:
                return None

            return decrypted[2:2 + inner_len]
        except Exception:
            return None

    def _encrypt_game_packet(self, payload: bytes) -> bytes:
        """Encrypt a game packet with XTEA for sending to server."""
        data = struct.pack('<H', len(payload)) + payload
        encrypted = xtea_encrypt(data, self.xtea_keys)
        checksum = adler32_checksum(encrypted)
        return struct.pack('<I', checksum) + encrypted

    def _process_client_game_packet(self, data: bytes) -> bytes:
        """Process a game packet from the client (inspect, forward)."""
        if self._client_packet_callbacks and self.xtea_keys:
            try:
                decrypted = self._decrypt_game_packet(data)
                if decrypted:
                    for cb in list(self._client_packet_callbacks):
                        try:
                            pr = PacketReader(decrypted)
                            if pr.remaining > 0:
                                opcode = pr.read_u8()
                                cb(opcode, pr)
                        except Exception as e:
                            log.debug(f"Client packet callback error: {e}")
            except Exception:
                pass

        return data

    async def inject_to_server(self, payload: bytes):
        """Inject a packet to the game server."""
        if not self.logged_in or self.xtea_keys is None:
            log.warning("Cannot inject: not logged in yet")
            return
        await self._inject_queue.put(('server', payload))

    async def inject_to_client(self, payload: bytes):
        """Inject a packet to the client."""
        if not self.logged_in or self.xtea_keys is None:
            log.warning("Cannot inject: not logged in yet")
            return
        await self._inject_queue.put(('client', payload))

    async def _process_inject_queue(self):
        """Process queued packet injections."""
        while True:
            target, payload = await self._inject_queue.get()

            try:
                encrypted = self._encrypt_game_packet(payload)
                packet = self._wrap_packet(encrypted)

                if target == 'server' and self.server_writer:
                    self.server_writer.write(packet)
                    await self.server_writer.drain()
                    log.info(f"Injected to server: opcode=0x{payload[0]:02X} len={len(payload)}B")
                elif target == 'client' and self.client_writer:
                    self.client_writer.write(packet)
                    await self.client_writer.drain()
                    log.info(f"Injected to client: opcode=0x{payload[0]:02X} len={len(payload)}B")
                else:
                    log.warning(f"Inject DROPPED: target={target} "
                                f"server_writer={self.server_writer is not None} "
                                f"client_writer={self.client_writer is not None}")
            except Exception as e:
                log.error(f"Injection error: {e}")

    def get_proxy_rsa_public_key(self) -> str:
        """Get the proxy's RSA public key as a decimal string."""
        return str(self.proxy_rsa_key.n)

