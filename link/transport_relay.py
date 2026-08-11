"""Outbound connection to the relay: the transport that works from anywhere.

One connection per room, because the relay's join handshake binds a socket to
exactly one `(room_id, device_id)` pair. Rooms are few and connections are
cheap; multiplexing would buy nothing and cost a framing layer.

The daemon never waits on this. `send()` either hands a frame to the socket's
writer or reports that it could not, and the room queues it for the next
reconnect -- which is also when the relay replays anything that arrived while
this device was away.
"""

from __future__ import annotations

import asyncio
import json
import random
import ssl
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from .crypto import is_device_id, sign_challenge
from .wsproto import WSClosed, WSError, ws_connect


def _clean_roster(raw: Any, me: str) -> list[str]:
    """Device ids from a relay message, minus anything that is not one.

    The relay is assumed hostile, and a roster is the one thing it says that
    nobody signs. Entries land in the room's member map and from there in the
    fan-out set, which is a list of directory names.
    """
    if not isinstance(raw, list):
        return []
    return [d for d in raw if is_device_id(d) and d != me]

DEFAULT_RELAY_URL = "wss://localhost:8765/relay"
MAX_FRAME_BYTES = 256 * 1024


def relay_audience(url: str) -> str:
    """The name a client and a relay both derive for the same endpoint.

    Host and port only: the path and the scheme's default port are normalised
    away so that `wss://r.example/relay` and `wss://r.example:443/relay` agree,
    while two different hosts never do. This is what the join signature is bound
    to, so both sides have to compute it the same way from what they each know
    -- the client from the URL it dialled, the relay from its configured public
    origin.
    """
    host, port, _path, tls = parse_relay_url(url)
    return f"{host.strip().lower()}:{port}" if not tls or port != 443 else \
        f"{host.strip().lower()}:443"


def parse_relay_url(url: str) -> tuple[str, int, str, bool]:
    """(host, port, path, tls) from a ws:// or wss:// URL."""
    parsed = urlparse((url or "").strip())
    scheme = (parsed.scheme or "wss").lower()
    if scheme not in ("ws", "wss"):
        raise ValueError(f"relay url must be ws:// or wss://, got {scheme!r}")
    if not parsed.hostname:
        raise ValueError(f"relay url has no host: {url!r}")
    tls = scheme == "wss"
    return (
        parsed.hostname,
        parsed.port or (443 if tls else 80),
        parsed.path or "/relay",
        tls,
    )


class RelayTransport:
    """Keeps one room's connection to the relay alive."""

    def __init__(
        self,
        relay_url: str,
        keys,
        identity,
        on_frame: Callable[[dict[str, Any]], Awaitable[None]],
        on_presence: Callable[[str, bool, list[str]], Awaitable[None]] | None = None,
        on_missed: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        log: Callable[[str], None] | None = None,
        insecure: bool = False,
        reconnect_min_s: float = 1.0,
        reconnect_max_s: float = 30.0,
    ) -> None:
        self.relay_url = relay_url
        self.keys = keys
        self.identity = identity
        self.on_frame = on_frame
        self.on_presence = on_presence
        self.on_missed = on_missed
        self.log = log or (lambda _m: None)
        self.insecure = insecure
        self.reconnect_min_s = reconnect_min_s
        self.reconnect_max_s = reconnect_max_s

        self.conn = None
        self.roster: list[str] = []
        self.connected_at: float | None = None
        self.last_error: str | None = None
        self.last_room_seq: int = -1
        self.messages_in = 0
        self.messages_out = 0
        self.reconnects = 0

        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------- #

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(
            self._connect_loop(), name=f"relay-{self.keys.room_id[:12]}"
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self.conn is not None:
            try:
                await self.conn.close()
            except Exception:
                pass
            self.conn = None

    @property
    def online(self) -> bool:
        return self.conn is not None and not self.conn.closed

    # -- connection --------------------------------------------------------- #

    def _ssl_context(self, tls: bool):
        if not tls:
            return None
        context = ssl.create_default_context()
        if self.insecure:
            # Only ever set deliberately, for a self-signed relay on a LAN or in
            # a test. Certificate verification is what stops the relay being
            # impersonated wholesale.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    async def _connect_loop(self) -> None:
        backoff = self.reconnect_min_s
        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff = self.reconnect_min_s
            except (WSError, OSError, ssl.SSLError, asyncio.TimeoutError, ValueError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.log(f"[relay] {self.keys.room_name}: {self.last_error}")
            except asyncio.CancelledError:
                raise

            if self._stop.is_set():
                return
            # Jitter so a relay restart does not bring every client back at once.
            await self._sleep(backoff * (0.7 + 0.6 * random.random()))
            backoff = min(backoff * 1.7, self.reconnect_max_s)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _connect_once(self) -> None:
        host, port, path, tls = parse_relay_url(self.relay_url)
        conn = await ws_connect(
            host, port, path=path, timeout=10.0,
            ssl_context=self._ssl_context(tls),
            server_hostname=host if tls else None,
            max_payload=MAX_FRAME_BYTES,
        )
        try:
            if not await self._handshake(conn):
                return
            self.conn = conn
            self.connected_at = time.time()
            self.reconnects += 1
            self.last_error = None
            await self._read_loop(conn)
        finally:
            if self.conn is conn:
                self.conn = None
                self.roster = []
            try:
                await conn.close()
            except Exception:
                pass

    async def _handshake(self, conn) -> bool:
        raw = await asyncio.wait_for(conn.recv(), timeout=15.0)
        if raw is None:
            raise WSError("relay closed before sending a challenge")
        hello = json.loads(raw)
        if hello.get("type") != "challenge" or not hello.get("challenge"):
            raise WSError(f"unexpected first frame from relay: {hello.get('type')!r}")

        await conn.send_text(json.dumps({
            "type": "join",
            "room_id": self.keys.room_id,
            "device_id": self.identity.device_id,
            "room_auth_pk": self.keys.room_auth_pk,
            # Signed *for this relay*. Without naming it, the proof is a
            # bearer token any relay we were ever pointed at could forward to
            # an honest one and take our seat with. See `challenge_bytes`.
            "sig": sign_challenge(self.keys, hello["challenge"],
                                  self.identity.device_id,
                                  relay_audience(self.relay_url)),
            "audience": relay_audience(self.relay_url),
            "last_room_seq": self.last_room_seq if self.last_room_seq >= 0 else None,
        }, ensure_ascii=False))

        # The relay guarantees `joined` is the first frame, but a stray presence
        # notice is not worth dropping the connection over. Anything that is not
        # the acknowledgement is replayed after it -- a data frame reaching us
        # this early is still a real message, so it is buffered rather than lost.
        early: list[dict[str, Any]] = []
        ack = None
        deadline = time.monotonic() + 15.0
        while ack is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WSError("relay never acknowledged the join")
            raw = await asyncio.wait_for(conn.recv(), timeout=remaining)
            if raw is None:
                # The relay closes without a reply when it rejects a join; the
                # close code carries the reason, and 4403 is the one to name.
                raise WSError(
                    "relay rejected the join — the room may be claimed by a "
                    "different secret on this relay"
                )
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "joined":
                ack = msg
            elif msg.get("type") == "frame":
                early.append(msg)

        # The relay hands this over in the clear and nothing signs it. Every
        # entry becomes a directory name on the folder and git transports, so
        # a shape check here is what keeps a hostile relay from choosing where
        # this machine writes. See link/crypto.py.
        self.roster = _clean_roster(ack.get("roster"), self.identity.device_id)
        head = ack.get("room_seq")
        if isinstance(head, int):
            self.last_room_seq = max(self.last_room_seq, ack.get("resume_from") or -1)
        self.log(
            f"[relay] {self.keys.room_name}: joined as {self.identity.device_id} "
            f"({len(self.roster)} other member(s), {ack.get('backlog', 0)} queued)"
        )
        if ack.get("missed") and self.on_missed:
            await self.on_missed(ack["missed"])
        if self.on_presence:
            await self.on_presence("", True, self.roster)
        for msg in early:
            await self._handle_frame(msg)
        return True

    async def _handle_frame(self, msg: dict[str, Any]) -> None:
        seq = msg.pop("_room_seq", None)
        if isinstance(seq, int):
            self.last_room_seq = max(self.last_room_seq, seq)
        self.messages_in += 1
        await self.on_frame({k: v for k, v in msg.items() if k != "type"})
        if isinstance(seq, int):
            await self._ack(seq)

    async def _read_loop(self, conn) -> None:
        while True:
            raw = await conn.recv()
            if raw is None:
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue

            kind = msg.get("type")
            if kind == "frame":
                await self._handle_frame(msg)
            elif kind == "presence":
                # The relay broadcasts the whole roster; everyone else's view of
                # the room should not include the viewer.
                self.roster = _clean_roster(
                    msg.get("roster"), self.identity.device_id
                )
                if self.on_presence:
                    await self.on_presence(
                        msg.get("device_id") or "", bool(msg.get("online")), self.roster
                    )
            elif kind == "pong":
                continue

    async def _ack(self, room_seq: int) -> None:
        """Tell the relay how far we have consumed, so it can advance our cursor."""
        if self.online:
            try:
                await self.conn.send_text(json.dumps({"type": "ack", "room_seq": room_seq}))
            except (WSClosed, WSError, OSError):
                pass

    # -- sending ------------------------------------------------------------- #

    async def send(self, frame: dict[str, Any]) -> bool:
        """Push one sealed frame. False means the caller should queue it."""
        conn = self.conn
        if conn is None or conn.closed:
            return False
        try:
            await conn.send_text(json.dumps({"type": "frame", **frame}, ensure_ascii=False))
            self.messages_out += 1
            return True
        except (WSClosed, WSError, OSError) as exc:
            self.last_error = f"send failed: {exc}"
            if self.conn is conn:
                self.conn = None
            return False

    def stats(self) -> dict[str, Any]:
        return {
            "url": self.relay_url,
            "online": self.online,
            "roster": list(self.roster),
            "messages_in": self.messages_in,
            "messages_out": self.messages_out,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
        }
