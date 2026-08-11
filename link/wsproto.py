"""Minimal RFC 6455 WebSocket over asyncio streams. No third-party deps.

Supports exactly what the link needs: the HTTP upgrade handshake (both
directions), text frames, fragmentation via continuation frames, and the
ping/pong/close control frames. Binary frames are accepted and handed up as
bytes but never produced.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_PAYLOAD = 8 * 1024 * 1024  # 8 MB; envelopes are kilobytes, this is a guard
# How long one frame may sit unflushed before the peer is treated as gone. The
# relay closes a stalled member from inside another member's send loop, so an
# unbounded drain there parks the sender rather than the stalled peer.
DRAIN_TIMEOUT_S = 20.0


class WSError(Exception):
    pass


class WSClosed(WSError):
    """Raised (or returned as None from recv) when the peer closed the socket."""


def accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #


class WSConnection:
    """One WebSocket connection. `is_client` decides masking per RFC 6455."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        is_client: bool,
        peername: tuple | None = None,
        headers: dict[str, str] | None = None,
        max_payload: int = MAX_PAYLOAD,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.is_client = is_client
        self.headers = headers or {}
        self._peername = peername or _safe_peername(writer)
        self._send_lock = asyncio.Lock()
        self._closed = False
        # Every frame read, including the control frames `recv` answers
        # itself and never returns. The relay meters on this: charging
        # per message left pings, pongs and empty continuations free.
        self.frames_read = 0
        # Per-connection so a public relay can hold untrusted peers to a much
        # tighter budget than a LAN peer we chose to dial.
        self._max_payload = max(1024, int(max_payload))

    # -- identity ---------------------------------------------------------- #

    @property
    def remote_ip(self) -> str | None:
        return self._peername[0] if self._peername else None

    @property
    def remote_port(self) -> int | None:
        return self._peername[1] if self._peername else None

    @property
    def closed(self) -> bool:
        return self._closed

    # -- frame I/O --------------------------------------------------------- #

    async def _read_exactly(self, n: int) -> bytes:
        if n == 0:
            return b""
        try:
            return await self.reader.readexactly(n)
        except (asyncio.IncompleteReadError, ConnectionError) as exc:
            raise WSClosed(str(exc)) from exc

    async def _read_frame(self) -> tuple[int, bytes, bool]:
        # Counted here rather than in `recv`, because the whole point is the
        # frames `recv` never returns: a peer can send pings and empty
        # continuations for ever and the caller sees nothing to charge for.
        self.frames_read += 1
        head = await self._read_exactly(2)
        b0, b1 = head[0], head[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F

        # 5.2: reserved bits are zero unless an extension negotiated them, and
        # none is ever negotiated here. Accepting them is accepting frames whose
        # meaning we do not know.
        if b0 & 0x70:
            raise WSError("reserved bits set with no extension negotiated")
        # 5.1: a client MUST mask. This is the rule that stops a browser being
        # used to push bytes that look like a WebSocket frame through a proxy.
        if not self.is_client and not masked:
            raise WSError("client frame is not masked")

        if length == 126:
            length = struct.unpack("!H", await self._read_exactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self._read_exactly(8))[0]

        # 5.5: control frames carry at most 125 bytes and are never
        # fragmented. Without this a member can make the relay echo a 256 KiB
        # ping, unmetered, for as long as it likes.
        if opcode >= 0x8:
            if length > 125:
                raise WSError(f"control frame with a {length}-byte payload")
            if not fin:
                raise WSError("fragmented control frame")

        if length > self._max_payload:
            raise WSError(f"frame too large: {length} bytes")

        mask_key = await self._read_exactly(4) if masked else b""
        payload = await self._read_exactly(length)
        if masked:
            payload = _apply_mask(payload, mask_key)
        return opcode, payload, fin

    async def _write_frame(self, opcode: int, payload: bytes, fin: bool = True) -> None:
        if self._closed:
            raise WSClosed("connection already closed")
        b0 = (0x80 if fin else 0x00) | opcode
        length = len(payload)
        mask_bit = 0x80 if self.is_client else 0x00

        if length < 126:
            header = struct.pack("!BB", b0, mask_bit | length)
        elif length < (1 << 16):
            header = struct.pack("!BBH", b0, mask_bit | 126, length)
        else:
            header = struct.pack("!BBQ", b0, mask_bit | 127, length)

        if self.is_client:
            mask_key = os.urandom(4)
            frame = header + mask_key + _apply_mask(payload, mask_key)
        else:
            frame = header + payload

        async with self._send_lock:
            try:
                self.writer.write(frame)
                # Bounded. The relay closes a stalled member from inside the
                # *sender* loop, on a socket whose peer is by definition not
                # reading, and an unbounded drain there parks the sender.
                await asyncio.wait_for(self.writer.drain(),
                                       timeout=DRAIN_TIMEOUT_S)
            except asyncio.TimeoutError as exc:
                self._closed = True
                raise WSClosed("peer stopped reading") from exc
            except (ConnectionError, RuntimeError) as exc:
                self._closed = True
                raise WSClosed(str(exc)) from exc

    # -- public API -------------------------------------------------------- #

    async def send_text(self, text: str) -> None:
        await self._write_frame(OP_TEXT, text.encode("utf-8"))

    async def ping(self, data: bytes = b"") -> None:
        await self._write_frame(OP_PING, data)

    async def recv(self) -> str | bytes | None:
        """Return the next application message, or None once the peer closes.

        Control frames are answered transparently and never surface to callers.
        """
        buffer = bytearray()
        buffer_op: int | None = None
        while True:
            try:
                opcode, payload, fin = await self._read_frame()
            except WSClosed:
                self._closed = True
                return None

            if opcode == OP_CLOSE:
                # Echo first, then mark it shut, for the reason in `close`.
                try:
                    await self._write_frame(OP_CLOSE, payload[:2])
                except WSError:
                    pass
                self._closed = True
                return None
            if opcode == OP_PING:
                try:
                    await self._write_frame(OP_PONG, payload)
                except WSError:
                    self._closed = True
                    return None
                continue
            if opcode == OP_PONG:
                continue

            # Check before growing, not after: a peer sending a long chain of
            # continuation frames must be cut off at the limit rather than
            # allowed to allocate past it and be rejected once it already has.
            if len(buffer) + len(payload) > self._max_payload:
                raise WSError("fragmented message exceeds payload limit")

            if opcode in (OP_TEXT, OP_BINARY):
                if buffer_op is not None:
                    # 5.4: a data frame may not interrupt a fragmented message.
                    # This used to reset the buffer, so the part already
                    # received was discarded without a word and the caller was
                    # handed the interrupting frame as the whole message.
                    raise WSError("data frame arrived inside a fragmented message")
                buffer_op = opcode
                buffer = bytearray(payload)
            elif opcode == OP_CONT:
                if buffer_op is None:
                    raise WSError("continuation frame without an opening frame")
                buffer.extend(payload)
            else:
                raise WSError(f"unsupported opcode: {opcode}")
            if fin:
                data = bytes(buffer)
                op, buffer_op, buffer = buffer_op, None, bytearray()
                return data.decode("utf-8", errors="replace") if op == OP_TEXT else data

    async def close(self, code: int = 1000) -> None:
        if not self._closed:
            # Written *before* the flag is set. `_write_frame` begins with
            # `if self._closed: raise WSClosed`, and WSClosed is a WSError, so
            # setting it first meant the Close frame was never sent and the
            # except below hid that. Every 4xxx code the relay defines was
            # therefore dead: a peer saw a bare EOF for a bad signature, an
            # auth timeout, a room limit and a rate-limit cut alike, and the
            # client guessed -- wrongly for three of those four.
            try:
                await self._write_frame(OP_CLOSE, struct.pack("!H", code))
            except (WSError, OSError):
                pass
            self._closed = True
        try:
            self.writer.close()
        except (OSError, RuntimeError):
            pass


# --------------------------------------------------------------------------- #
# handshakes
# --------------------------------------------------------------------------- #


async def read_request(
    reader: asyncio.StreamReader,
    timeout: float = 10.0,
    max_head: int = 16 * 1024,
) -> tuple[str, str, dict[str, str]]:
    """Read one HTTP request head. Returns (method, path, headers).

    Split out of the handshake so a server can route on the path before
    committing to an upgrade -- a health check is a plain GET, not a WebSocket.
    """
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    except asyncio.LimitOverrunError as exc:
        raise WSError(f"request head too large: {exc}") from exc
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError) as exc:
        raise WSError(f"handshake read failed: {exc}") from exc
    if len(raw) > max_head:
        raise WSError("request head too large")

    lines = raw.decode("latin-1").split("\r\n")
    parts = (lines[0] if lines else "").split(" ")
    if len(parts) < 2:
        raise WSError(f"bad request line: {lines[0] if lines else ''!r}")

    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return parts[0].upper(), parts[1], headers


async def accept_upgrade(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    path: str,
    headers: dict[str, str],
    max_payload: int = MAX_PAYLOAD,
) -> WSConnection:
    """Finish an upgrade whose request head has already been read and routed."""
    if headers.get("upgrade", "").lower() != "websocket":
        await _http_error(writer, 400, "expected Upgrade: websocket")
        raise WSError("missing websocket upgrade")

    key = headers.get("sec-websocket-key")
    if not key:
        await _http_error(writer, 400, "missing Sec-WebSocket-Key")
        raise WSError("missing Sec-WebSocket-Key")

    writer.write(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_key(key)}\r\n"
            "\r\n"
        ).encode("ascii")
    )
    await writer.drain()
    headers = dict(headers, _path=path)
    return WSConnection(reader, writer, is_client=False, headers=headers,
                        max_payload=max_payload)


async def server_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    path_prefix: str = "/link",
    timeout: float = 10.0,
    max_payload: int = MAX_PAYLOAD,
) -> WSConnection:
    """Complete the server side of the upgrade. Raises WSError on a bad request."""
    method, path, headers = await read_request(reader, timeout)
    if method != "GET":
        await _http_error(writer, 400, "expected GET")
        raise WSError(f"bad method: {method}")
    if not path.startswith(path_prefix):
        await _http_error(writer, 404, "unknown path")
        raise WSError(f"unexpected path: {path}")
    return await accept_upgrade(reader, writer, path, headers, max_payload)


async def ws_connect(
    host: str,
    port: int,
    path: str = "/link",
    timeout: float = 5.0,
    extra_headers: dict[str, str] | None = None,
    ssl_context=None,
    server_hostname: str | None = None,
    max_payload: int = MAX_PAYLOAD,
) -> WSConnection:
    """Dial a peer and complete the client side of the upgrade.

    `ssl_context` is what makes `wss://` work: the relay is reached over TLS,
    the LAN transport is not.
    """
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(
            host, port, ssl=ssl_context,
            server_hostname=server_hostname if ssl_context else None,
        ),
        timeout,
    )
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    host_header = host if (ssl_context and port == 443) or port == 80 else f"{host}:{port}"
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host_header}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    for k, v in (extra_headers or {}).items():
        lines.append(f"{k}: {v}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    await writer.drain()

    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError,
            asyncio.LimitOverrunError) as exc:
        writer.close()
        raise WSError(f"handshake response failed: {exc}") from exc

    head = raw.decode("latin-1").split("\r\n")
    if "101" not in head[0]:
        writer.close()
        raise WSError(f"upgrade refused: {head[0]}")

    resp_headers = {}
    for line in head[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            resp_headers[k.strip().lower()] = v.strip()
    if resp_headers.get("sec-websocket-accept") != accept_key(key):
        writer.close()
        raise WSError("Sec-WebSocket-Accept mismatch")

    return WSConnection(reader, writer, is_client=True, headers=resp_headers,
                        max_payload=max_payload)


async def _http_error(writer: asyncio.StreamWriter, code: int, msg: str) -> None:
    try:
        body = msg.encode("utf-8")
        writer.write(
            f"HTTP/1.1 {code} Error\r\nContent-Length: {len(body)}\r\n"
            f"Content-Type: text/plain\r\nConnection: close\r\n\r\n".encode("ascii")
            + body
        )
        await writer.drain()
        writer.close()
    except (OSError, RuntimeError):
        pass


# A multiple of 4, so every chunk starts on the same phase of the mask.
_MASK_CHUNK = 64 * 1024


def _apply_mask(data: bytes, mask: bytes) -> bytes:
    """XOR `data` with the repeating 4-byte `mask`.

    Done as one big-int XOR rather than a per-byte loop: ~20x faster on
    kilobyte payloads, which matters because every client frame is masked.
    """
    n = len(data)
    if not mask or n == 0:
        return data
    if n <= _MASK_CHUNK:
        repeated = mask * (n // 4) + mask[: n % 4]
        return (int.from_bytes(data, "big")
                ^ int.from_bytes(repeated, "big")).to_bytes(n, "big")
    # In chunks past that. The big-int form holds the repeated mask, both source
    # integers and the result all at once: 1334 KiB peak for a 256 KiB frame,
    # which at the relay connection ceiling is ~340 MiB of transient on a box
    # sized for much less.
    out = bytearray(n)
    for start in range(0, n, _MASK_CHUNK):
        piece = data[start:start + _MASK_CHUNK]
        m = len(piece)
        repeated = mask * (m // 4) + mask[: m % 4]
        out[start:start + m] = (
            int.from_bytes(piece, "big") ^ int.from_bytes(repeated, "big")
        ).to_bytes(m, "big")
    return bytes(out)


def _safe_peername(writer: asyncio.StreamWriter) -> tuple | None:
    try:
        return writer.get_extra_info("peername")
    except (OSError, AttributeError):
        return None
