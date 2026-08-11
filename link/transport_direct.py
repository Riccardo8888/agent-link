"""Direct WebSocket between members on a network that can route to itself.

The fastest transport and the narrowest: it needs one member to be able to open
a TCP connection to another, which is exactly the assumption the other two
transports exist to avoid. So it is **off unless asked for**
(`direct_enabled`), and it never carries a message on its own unless it can
reach every member of the room -- a partial send would look like a success while
leaving the people who are not on this LAN with nothing.

Addresses are advertised inside the room's own `hello`, which is end-to-end
encrypted. A relay carrying that hello cannot read where anyone lives; only
members can. Members are people you have already given a room secret to.

That last point is the trust boundary worth stating: a member can advertise any
address, and with this enabled you will connect to it. On a LAN you control
that is fine. It is the reason this is opt-in.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any, Awaitable, Callable

from cryptography.exceptions import InvalidSignature

from .crypto import (
    CryptoError,
    challenge_bytes,
    b64,
    is_device_id,
    key_matches_device,
    load_public,
    sign_challenge,
    unb64,
    verify_challenge,
)
from .wsproto import WSClosed, WSError, server_handshake, ws_connect

DIAL_TIMEOUT = 4.0
HANDSHAKE_TIMEOUT = 5.0
REDIAL_MIN_S = 2.0
REDIAL_MAX_S = 60.0
MAX_FRAME_BYTES = 8 * 1024 * 1024


class DirectServer:
    """One listener per daemon, shared by every room that wants direct links.

    A connection names its room during the handshake, so one port serves all of
    them rather than one port per room.
    """

    def __init__(
        self,
        host: str,
        port: int,
        lookup_room: Callable[[str], Any],
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.lookup_room = lookup_room
        self.log = log or (lambda _m: None)
        self.server: asyncio.Server | None = None
        self.actual_port: int | None = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._on_connection, self.host, self.port)
        self.actual_port = self.server.sockets[0].getsockname()[1]
        self.log(f"[direct] listening on {self.host}:{self.actual_port}")
        return self.actual_port

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
            self.server = None

    async def _on_connection(self, reader, writer) -> None:
        conn = None
        try:
            conn = await server_handshake(reader, writer, path_prefix="/link",
                                          max_payload=MAX_FRAME_BYTES)
            challenge = secrets.token_urlsafe(32)
            await conn.send_text(json.dumps({"type": "challenge", "challenge": challenge}))

            raw = await asyncio.wait_for(conn.recv(), timeout=HANDSHAKE_TIMEOUT)
            if raw is None:
                return
            req = json.loads(raw)
            if not isinstance(req, dict) or req.get("type") != "join":
                await conn.close(1008)
                return

            room_id = str(req.get("room_id") or "")
            device_id = str(req.get("device_id") or "")
            room = self.lookup_room(room_id)
            if room is None:
                self.log(f"[direct] rejected {device_id}: unknown room")
                await conn.close(1008)
                return

            if not is_device_id(device_id):
                self.log("[direct] rejected a join: not a device id")
                await conn.close(1008)
                return

            # Verified against the room's own auth key. Only someone holding the
            # room secret can sign this, which is the same bar the relay applies.
            # The audience is what stops the proof being forwarded: without
            # it, a signature made for one listener is valid at every other.
            audience = str(req.get("audience") or "")
            if not verify_challenge(room.keys.room_auth_pk, challenge, room_id,
                                    device_id, str(req.get("sig") or ""),
                                    audience):
                self.log(f"[direct] rejected {device_id}: bad room signature")
                await conn.close(1008)
                return

            # ...and that proves membership, not identity: every member holds
            # the room key, so any of them could sign the line above while
            # claiming somebody else's device id. `adopt` then replaces that
            # member's live connection with this one, and their direct traffic
            # arrives here instead. So the caller must also sign with the device
            # key whose hash *is* the id it is claiming.
            public_key = str(req.get("public_key") or "")
            if not key_matches_device(public_key, device_id):
                self.log(f"[direct] rejected {device_id}: key does not match the id")
                await conn.close(1008)
                return
            try:
                load_public(public_key).verify(
                    unb64(str(req.get("dev_sig") or "")),
                    challenge_bytes(challenge, room_id, device_id, audience),
                )
            except (InvalidSignature, CryptoError):
                self.log(f"[direct] rejected {device_id}: bad device signature")
                await conn.close(1008)
                return

            transport = room.transport("direct")
            if transport is None:
                await conn.close(1008)
                return

            await conn.send_text(json.dumps({
                "type": "joined", "device_id": transport.identity.device_id,
            }))
            if await transport.adopt(conn, device_id, "accepted"):
                await transport.read_loop(conn, device_id)
        except (WSError, OSError, ValueError, asyncio.TimeoutError):
            pass
        except Exception as exc:
            self.log(f"[direct] inbound failed: {type(exc).__name__}: {exc}")
        finally:
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
            try:
                writer.close()
            except OSError:
                pass


class DirectTransport:
    """The direct links for one room: who we can reach, and how."""

    def __init__(
        self,
        keys,
        identity,
        on_frame: Callable[[dict[str, Any]], Awaitable[None]],
        endpoint: Callable[[], dict[str, Any] | None] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.keys = keys
        self.identity = identity
        self.on_frame = on_frame
        self._endpoint = endpoint or (lambda: None)
        self.log = log or (lambda _m: None)

        self.conns: dict[str, Any] = {}          # device_id -> WSConnection
        self.kinds: dict[str, str] = {}          # device_id -> dialed | accepted
        self.addresses: dict[str, tuple[str, int]] = {}
        self.messages_in = 0
        self.messages_out = 0
        self.last_error: str | None = None
        self._dialers: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()

    # -- lifecycle ----------------------------------------------------------- #

    async def start(self) -> None:
        self._stop.clear()

    async def stop(self) -> None:
        self._stop.set()
        for task in list(self._dialers.values()):
            task.cancel()
        self._dialers.clear()
        for conn in list(self.conns.values()):
            try:
                await conn.close()
            except Exception:
                pass
        self.conns.clear()
        self.kinds.clear()

    @property
    def online(self) -> bool:
        return any(not c.closed for c in self.conns.values())

    def covers(self, recipients: list[str]) -> bool:
        """True when every recipient has a live direct connection."""
        live = {d for d, c in self.conns.items() if not c.closed}
        return bool(recipients) and all(d in live for d in recipients)

    def advertisement(self) -> dict[str, Any] | None:
        """What to put in `hello` so members know where to find us."""
        return self._endpoint()

    # -- learning about members ----------------------------------------------- #

    def learn(self, device_id: str, host: str, port: int) -> None:
        """Note where a member says it can be reached, and start dialling."""
        if device_id == self.identity.device_id or not host or not port:
            return
        address = (str(host), int(port))
        if self.addresses.get(device_id) == address and device_id in self._dialers:
            return
        self.addresses[device_id] = address
        if device_id in self.conns and not self.conns[device_id].closed:
            return
        self._spawn_dialer(device_id)

    def _spawn_dialer(self, device_id: str) -> None:
        existing = self._dialers.get(device_id)
        if existing is not None and not existing.done():
            return
        self._dialers[device_id] = asyncio.create_task(
            self._dial_loop(device_id), name=f"direct-dial-{device_id[:12]}"
        )

    async def _dial_loop(self, device_id: str) -> None:
        backoff = REDIAL_MIN_S
        while not self._stop.is_set():
            address = self.addresses.get(device_id)
            if address is None:
                return
            conn = self.conns.get(device_id)
            if conn is not None and not conn.closed:
                await self._sleep(backoff)
                continue
            try:
                await self._dial_once(device_id, *address)
                backoff = REDIAL_MIN_S
            except (WSError, OSError, asyncio.TimeoutError, ValueError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
            except asyncio.CancelledError:
                raise
            await self._sleep(backoff)
            backoff = min(backoff * 1.8, REDIAL_MAX_S)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _dial_once(self, device_id: str, host: str, port: int) -> None:
        conn = await ws_connect(host, port, path=f"/link/{self.keys.room_id}",
                                timeout=DIAL_TIMEOUT, max_payload=MAX_FRAME_BYTES)
        try:
            raw = await asyncio.wait_for(conn.recv(), timeout=HANDSHAKE_TIMEOUT)
            if raw is None:
                raise WSError("closed before the challenge")
            hello = json.loads(raw)
            if hello.get("type") != "challenge":
                raise WSError(f"unexpected frame {hello.get('type')!r}")

            await conn.send_text(json.dumps({
                "type": "join",
                "room_id": self.keys.room_id,
                "device_id": self.identity.device_id,
                # Proves membership of the room. Every member can produce this,
                # so on its own it says nothing about *which* member is calling.
                "sig": sign_challenge(self.keys, hello["challenge"],
                                      self.identity.device_id,
                                      f"{host}:{port}"),
                "audience": f"{host}:{port}",
                # Proves which device. `device_id` is a hash of this key, so the
                # pair together is what stops one member dialling in wearing
                # another's id and displacing their connection.
                "public_key": self.identity.public_key,
                "dev_sig": b64(self.identity.sign_key().sign(
                    challenge_bytes(hello["challenge"], self.keys.room_id,
                                    self.identity.device_id,
                                    f"{host}:{port}"))),
            }))
            raw = await asyncio.wait_for(conn.recv(), timeout=HANDSHAKE_TIMEOUT)
            if raw is None:
                raise WSError("rejected by the peer")
            ack = json.loads(raw)
            if ack.get("type") != "joined":
                raise WSError(f"expected 'joined', got {ack.get('type')!r}")

            # The id we dialled, never the one the answer claims. The listening
            # half of this same handshake refuses to take a caller's word for
            # its own id, and says why: the room key proves membership, not
            # identity, so a member could otherwise wear somebody else's device
            # id and have their traffic delivered here instead. This side had no
            # check at all -- no key, no signature, not even a shape -- so any
            # member could answer a dial with a third member's id, take that
            # member's slot in `conns`, and silently swallow everything
            # addressed to them while `covers()` still reported full coverage
            # and the room stayed on `direct`.
            answered = ack.get("device_id")
            if answered is not None and str(answered) != device_id:
                raise WSError(
                    f"dialled {device_id} and it answered as {str(answered)[:32]!r}")
            if await self.adopt(conn, device_id, "dialed"):
                self.last_error = None
                await self.read_loop(conn, device_id)
        finally:
            try:
                await conn.close()
            except Exception:
                pass

    # -- connections ----------------------------------------------------------- #

    async def adopt(self, conn, device_id: str, kind: str) -> bool:
        """Install a connection, resolving the duplicate both sides will create.

        Neither side can assume the other's port is reachable, so both dial.
        When both succeed there are two sockets, and the tie-break has to give
        the same answer on both machines or they will each drop the other's.
        Keeping the connection whose *dialer* has the smaller device id does
        that, since both know both ids.
        """
        current = self.conns.get(device_id)
        if current is not None and not current.closed and current is not conn:
            keep_dialed = self.identity.device_id < device_id
            incoming_wins = (kind == "dialed") == keep_dialed
            if not incoming_wins:
                await conn.close()
                return False
            self.log(f"[direct] replacing {self.kinds.get(device_id)} link "
                     f"with {kind} for {device_id}")
            self.conns.pop(device_id, None)
            try:
                await current.close()
            except Exception:
                pass

        self.conns[device_id] = conn
        self.kinds[device_id] = kind
        self.log(f"[direct] {kind} link up with {device_id} @ {conn.remote_ip}")
        return True

    async def read_loop(self, conn, device_id: str) -> None:
        try:
            while True:
                raw = await conn.recv()
                if raw is None:
                    return
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(msg, dict) or msg.get("type") != "frame":
                    continue
                self.messages_in += 1
                await self.on_frame({k: v for k, v in msg.items() if k != "type"})
        except (WSError, OSError, ValueError):
            return
        finally:
            if self.conns.get(device_id) is conn:
                self.conns.pop(device_id, None)
                self.kinds.pop(device_id, None)

    # -- sending ---------------------------------------------------------------- #

    async def send(self, frame: dict[str, Any], recipients: list[str]) -> bool:
        """Send to every recipient. False if any of them could not be reached.

        All-or-nothing on purpose: the caller only chooses this transport when
        it covers the whole room, and a half-delivered broadcast is the one
        outcome no transport here is allowed to produce.
        """
        payload = json.dumps({"type": "frame", **frame}, ensure_ascii=False)
        delivered = 0
        for device_id in recipients:
            conn = self.conns.get(device_id)
            if conn is None or conn.closed:
                return False
            try:
                await conn.send_text(payload)
                delivered += 1
            except (WSClosed, WSError, OSError) as exc:
                self.last_error = f"send to {device_id} failed: {exc}"
                self.conns.pop(device_id, None)
                return False
        if delivered:
            self.messages_out += 1
        return delivered == len(recipients)

    def stats(self) -> dict[str, Any]:
        return {
            "endpoint": self.advertisement(),
            "connected": sorted(d for d, c in self.conns.items() if not c.closed),
            "known_addresses": {d: f"{h}:{p}" for d, (h, p) in self.addresses.items()},
            "messages_in": self.messages_in,
            "messages_out": self.messages_out,
            "last_error": self.last_error,
        }
