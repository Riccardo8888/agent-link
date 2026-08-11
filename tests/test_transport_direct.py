"""Direct links between members, the opt-in transport.

Two properties matter here and neither is about speed. First, both members dial
each other -- neither can assume the other's port is reachable -- so the
duplicate connection has to be resolved the same way on both machines. Second,
direct must never carry a broadcast it cannot complete: a room where only some
members are on this LAN has to fall back, not half-deliver.
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from link import crypto                                          # noqa: E402
from link.envelope import make_envelope, make_origin, open_and_verify, seal_frame  # noqa: E402
from link.identity import Identity                               # noqa: E402
from link.transport_direct import DirectServer, DirectTransport   # noqa: E402


def a_device(label):
    key = crypto.generate_device_key()
    public = crypto.public_bytes(key)
    return Identity(crypto.device_id_for(public), public, label, "cli", key)


class FakeRoom:
    """The two attributes DirectServer needs from a room, and nothing else."""

    def __init__(self, keys, transport):
        self.keys = keys
        self._transport = transport

    def transport(self, name):
        return self._transport if name == "direct" else None


class DirectCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.keys = crypto.derive_room("direct-room", "a-secret-for-direct")
        self.nodes = []

    async def asyncTearDown(self):
        for _identity, transport, server in self.nodes:
            await transport.stop()
            await server.stop()

    async def node(self, label):
        """One daemon's worth of direct plumbing: a listener and a transport."""
        identity = a_device(label)
        received = []

        async def on_frame(frame):
            received.append(frame)

        rooms = {}
        server = DirectServer("127.0.0.1", 0, rooms.get, log=lambda _m: None)
        port = await server.start()
        transport = DirectTransport(
            keys=self.keys, identity=identity, on_frame=on_frame,
            endpoint=lambda p=port: {"host": "127.0.0.1", "port": p},
            log=lambda _m: None,
        )
        await transport.start()
        rooms[self.keys.room_id] = FakeRoom(self.keys, transport)
        transport.identity = identity
        transport.received = received
        self.nodes.append((identity, transport, server))
        return transport

    async def until(self, predicate, what, timeout=15.0):
        for _ in range(int(timeout / 0.05)):
            if predicate():
                return True
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")

    def a_frame(self, identity, text, seq=1):
        envelope = make_envelope("msg", self.keys.room_id, identity.device_id, seq,
                                 make_origin(identity), body={"text": text})
        return seal_frame(self.keys, identity, envelope)


class TestDirectLinks(DirectCase):
    async def test_two_members_connect_and_exchange(self):
        a = await self.node("a")
        b = await self.node("b")
        a.learn(b.identity.device_id, *b.advertisement().values())
        await self.until(lambda: a.online and b.online, "the link to come up")

        frame = self.a_frame(a.identity, "straight across the LAN")
        self.assertTrue(await a.send(frame, [b.identity.device_id]))
        await self.until(lambda: b.received, "delivery")
        opened = open_and_verify(self.keys, b.received[0])
        self.assertEqual(opened["body"]["text"], "straight across the LAN")

    async def test_mutual_dialling_leaves_exactly_one_connection(self):
        """Both sides dial, so both must drop the same duplicate."""
        a = await self.node("a")
        b = await self.node("b")
        a.learn(b.identity.device_id, *b.advertisement().values())
        b.learn(a.identity.device_id, *a.advertisement().values())
        await self.until(lambda: a.online and b.online, "both links")
        await asyncio.sleep(1.5)                      # let any duplicate settle

        self.assertEqual(len(a.conns), 1)
        self.assertEqual(len(b.conns), 1)
        kinds = {a.kinds[b.identity.device_id], b.kinds[a.identity.device_id]}
        self.assertEqual(kinds, {"dialed", "accepted"},
                         "both sides must keep the same physical socket")

    async def test_traffic_flows_both_ways(self):
        a = await self.node("a")
        b = await self.node("b")
        a.learn(b.identity.device_id, *b.advertisement().values())
        await self.until(lambda: a.online and b.online, "the link")

        await a.send(self.a_frame(a.identity, "there"), [b.identity.device_id])
        await self.until(lambda: b.received, "a to b")
        await b.send(self.a_frame(b.identity, "back"), [a.identity.device_id])
        await self.until(lambda: a.received, "b to a")
        self.assertEqual(open_and_verify(self.keys, a.received[0])["body"]["text"], "back")

    async def test_a_wrong_room_secret_is_refused(self):
        a = await self.node("a")
        b = await self.node("b")

        impostor = crypto.derive_room("direct-room", "not-the-secret")
        forged = crypto.RoomKeys(self.keys.room_name, self.keys.room_id,
                                 impostor.room_auth_sk, impostor.room_key)
        rogue = DirectTransport(keys=forged, identity=a_device("rogue"),
                                on_frame=lambda f: asyncio.sleep(0),
                                log=lambda _m: None)
        await rogue.start()
        rogue.learn(b.identity.device_id, *b.advertisement().values())
        await asyncio.sleep(2.0)
        self.assertFalse(rogue.online, "a bad room signature must not get a link")
        await rogue.stop()


class TestCoverage(DirectCase):
    """The rule that stops direct half-delivering a room broadcast."""

    async def test_covers_is_false_when_a_member_is_not_connected(self):
        a = await self.node("a")
        b = await self.node("b")
        c = await self.node("c")
        a.learn(b.identity.device_id, *b.advertisement().values())
        await self.until(lambda: a.online, "the link to b")

        both = [b.identity.device_id, c.identity.device_id]
        self.assertTrue(a.covers([b.identity.device_id]))
        self.assertFalse(a.covers(both), "c is not reachable directly")

    async def test_covers_is_false_for_an_empty_room(self):
        a = await self.node("a")
        self.assertFalse(a.covers([]))

    async def test_send_refuses_rather_than_half_delivering(self):
        a = await self.node("a")
        b = await self.node("b")
        c = await self.node("c")
        a.learn(b.identity.device_id, *b.advertisement().values())
        await self.until(lambda: a.online, "the link to b")

        frame = self.a_frame(a.identity, "everyone or nobody")
        sent = await a.send(frame, [b.identity.device_id, c.identity.device_id])
        self.assertFalse(sent, "an unreachable recipient must fail the whole send")

    async def test_the_room_falls_back_when_direct_cannot_cover_everyone(self):
        """The integration that matters: Room picks a transport that reaches all."""
        from link.room import Room

        a = await self.node("a")
        b = await self.node("b")
        a.learn(b.identity.device_id, *b.advertisement().values())
        await self.until(lambda: a.online, "the direct link")

        delivered = []

        class Fallback:
            online = True

            async def send(self, frame):
                delivered.append(frame)
                return True

            async def stop(self):
                pass

        room = Room(self.keys, a.identity, {"name": self.keys.room_name, "secret": "x"},
                    deliver=lambda env, t: asyncio.sleep(0), save_state=lambda: None)
        room.attach("direct", a)
        room.attach("relay", Fallback())
        room.members[b.identity.device_id] = type(
            "M", (), {"device_id": b.identity.device_id})()
        absent = "dev_notconnectedaaaa"
        room.members[absent] = type("M", (), {"device_id": absent})()

        transport = await room.send(room.build("msg", {"text": "to the whole room"}))
        self.assertEqual(transport, "relay",
                         "with one member off-LAN the whole room must go via relay")
        self.assertEqual(len(delivered), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTheDialerBelievesOnlyWhoItDialled(unittest.IsolatedAsyncioTestCase):
    """The listening half of this handshake refuses to take a caller's word for
    its own device id, and the comment there says exactly why: the room key
    proves membership, not identity, so any member could sign the join line
    while claiming somebody else's id, and `adopt` would then replace that
    member's live connection with theirs.

    The dialling half had no check at all -- no key, no signature, not even a
    shape -- and simply believed `{"type":"joined","device_id":...}`. So a
    member could answer a dial with a third member's id, take that member's
    slot in `conns`, and swallow everything addressed to them, while `covers()`
    still reported full coverage and the room stayed on `direct`.

    It is the one place claim (g) -- a device id is a hash of its own key, so
    nothing is trusted without checking -- did not hold.
    """

    async def drive(self, answered_id: str):
        """Run the real `_dial_once` against a connection we script."""
        import json as _json

        from link import transport_direct as td

        me = a_device("me")
        keys = crypto.derive_room("dial-room", "a-secret-for-dialling")
        transport = td.DirectTransport.__new__(td.DirectTransport)
        transport.keys = keys
        transport.identity = me
        transport.conns = {}
        transport.kinds = {}
        transport.last_error = None
        transport.log = lambda _m: None

        sent = []
        closed = []

        class Scripted:
            def __init__(self):
                self._to_send = [
                    _json.dumps({"type": "challenge", "challenge": "c" * 32}),
                    _json.dumps({"type": "joined", "device_id": answered_id}),
                ]

            async def recv(self):
                return self._to_send.pop(0) if self._to_send else None

            async def send_text(self, text):
                sent.append(text)

            async def close(self, *_a):
                closed.append(True)

        adopted = []

        async def adopt(_conn, device_id, kind):
            adopted.append(device_id)
            return False        # do not enter read_loop

        transport.adopt = adopt
        with unittest.mock.patch.object(
                td, "ws_connect", unittest.mock.AsyncMock(return_value=Scripted())):
            try:
                await transport._dial_once(self.wanted, "127.0.0.1", 45813)
            except td.WSError as exc:
                return adopted, str(exc)
        return adopted, None

    wanted = "dev_" + "d" * 16

    async def test_an_ack_naming_someone_else_is_refused(self):
        adopted, error = await self.drive("dev_" + "v" * 16)

        self.assertEqual(adopted, [], "it adopted the id the peer chose")
        self.assertIsNotNone(error, "it accepted the substitution silently")
        self.assertIn("answered as", error)

    async def test_the_id_we_dialled_is_the_one_adopted(self):
        adopted, error = await self.drive(self.wanted)

        self.assertIsNone(error)
        self.assertEqual(adopted, [self.wanted])

    async def test_an_ack_that_names_nobody_is_still_the_id_we_dialled(self):
        """An older peer, or one that simply does not echo it back."""
        adopted, error = await self.drive(None)

        self.assertIsNone(error)
        self.assertEqual(adopted, [self.wanted])
