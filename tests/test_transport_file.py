"""The shared-folder fallback, which has to fan out without a server to help.

The v1 transport was 1:1 and destructive-read: it deleted each envelope as it
consumed it. Dropped into a three-member room, whichever member polled first
would have eaten the message and the third would never have seen it. These tests
exist to keep that from coming back.
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from link import crypto                                          # noqa: E402
from link.envelope import make_envelope, make_origin, open_and_verify, seal_frame  # noqa: E402
from link.identity import Identity                               # noqa: E402
from link.transport_file import FileTransport, probe_shared_dir   # noqa: E402


def a_device(label):
    key = crypto.generate_device_key()
    public = crypto.public_bytes(key)
    return Identity(crypto.device_id_for(public), public, label, "cli", key)


class FileTransportCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base = tempfile.mkdtemp(prefix="claude-link-share-")
        self.share = os.path.join(self.base, "share")
        os.makedirs(self.share, exist_ok=True)
        self.keys = crypto.derive_room("share-room", "a-secret-for-the-share")
        self.transports = []

    async def asyncTearDown(self):
        for transport in self.transports:
            await transport.stop()
        shutil.rmtree(self.base, ignore_errors=True)

    async def member(self, identity):
        received = []

        async def on_frame(frame):
            received.append(frame)

        transport = FileTransport(
            shared_dir=self.share,
            room_id=self.keys.room_id,
            device_id=identity.device_id,
            on_frame=on_frame,
            poll_ms=50,
            log=lambda _m: None,
        )
        transport.identity = identity
        transport.received = received
        await transport.start()
        self.transports.append(transport)
        return transport

    def a_frame(self, identity, text, seq=1):
        envelope = make_envelope("msg", self.keys.room_id, identity.device_id, seq,
                                 make_origin(identity), body={"text": text})
        return seal_frame(self.keys, identity, envelope), envelope

    def pending_for(self, sender_id, recipient_id):
        """Frames still sitting on the share, waiting to be collected."""
        from link.transport_file import out_dir

        where = out_dir(self.share, self.keys.room_id, sender_id, recipient_id)
        if not os.path.isdir(where):
            return 0
        return len([f for f in os.listdir(where) if f.endswith(".json")])

    async def until(self, predicate, what, timeout=15.0):
        for _ in range(int(timeout / 0.05)):
            if predicate():
                return True
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")


class TestFanOut(FileTransportCase):
    async def test_all_three_members_receive_one_message(self):
        a = await self.member(a_device("a"))
        b = await self.member(a_device("b"))
        c = await self.member(a_device("c"))
        await self.until(lambda: len(a.roster()) >= 2, "the roster to fill in")

        frame, envelope = self.a_frame(a.identity, "everyone should see this")
        self.assertTrue(await a.send(frame, [b.identity.device_id, c.identity.device_id],
                                     envelope["msg_id"]))
        await self.until(lambda: b.received and c.received, "both peers to receive")
        self.assertEqual(len(a.received), 0, "the sender must not read its own copy")

        for peer in (b, c):
            opened = open_and_verify(self.keys, peer.received[0])
            self.assertEqual(opened["body"]["text"], "everyone should see this")

    async def test_one_member_reading_does_not_consume_anothers_copy(self):
        """The exact v1 bug: a destructive read that stole a third party's mail."""
        a = await self.member(a_device("a"))
        b = await self.member(a_device("b"))
        await self.until(lambda: len(a.roster()) >= 1, "b to appear")

        c_identity = a_device("c")
        frame, envelope = self.a_frame(a.identity, "for b and c")
        await a.send(frame, [b.identity.device_id, c_identity.device_id],
                     envelope["msg_id"])
        await self.until(lambda: b.received, "b to consume its copy")

        # c has not started yet; its copy must still be sitting on the share.
        c = await self.member(c_identity)
        await self.until(lambda: c.received, "c to find the copy b did not take")

    async def test_a_message_is_delivered_once_per_member(self):
        a = await self.member(a_device("a"))
        b = await self.member(a_device("b"))
        await self.until(lambda: len(a.roster()) >= 1, "b to appear")

        frame, envelope = self.a_frame(a.identity, "exactly once")
        await a.send(frame, [b.identity.device_id], envelope["msg_id"])
        await self.until(lambda: b.received, "delivery")
        await asyncio.sleep(0.6)                    # several more poll cycles
        self.assertEqual(len(b.received), 1)

    async def test_order_is_preserved_per_sender(self):
        a = await self.member(a_device("a"))
        b = await self.member(a_device("b"))
        await self.until(lambda: len(a.roster()) >= 1, "b to appear")

        for i in range(8):
            frame, envelope = self.a_frame(a.identity, f"ordered {i}", seq=i + 1)
            await a.send(frame, [b.identity.device_id], envelope["msg_id"])
        await self.until(lambda: len(b.received) >= 8, "all eight")
        texts = [open_and_verify(self.keys, f)["body"]["text"] for f in b.received]
        self.assertEqual(texts, [f"ordered {i}" for i in range(8)])


class TestPresence(FileTransportCase):
    async def test_members_discover_each_other_through_the_share(self):
        a = await self.member(a_device("a"))
        b = await self.member(a_device("b"))
        await self.until(lambda: b.identity.device_id in a.roster(),
                         "b's heartbeat to appear")
        self.assertNotIn(a.identity.device_id, a.roster(), "self is not a peer")

    async def test_a_stopped_member_still_gets_its_copies_queued(self):
        """Offline is not gone: their copy waits in their own directory."""
        a = await self.member(a_device("a"))
        b = await self.member(a_device("b"))
        await self.until(lambda: len(a.roster()) >= 1, "b to appear")
        await b.stop()
        self.transports.remove(b)

        frame, envelope = self.a_frame(a.identity, "waiting for you")
        self.assertTrue(await a.send(frame, [b.identity.device_id], envelope["msg_id"]))

        again = await self.member(b.identity)
        await self.until(lambda: again.received, "the queued copy on restart")


class TestProbe(unittest.TestCase):
    def test_a_writable_share_probes_clean(self):
        base = tempfile.mkdtemp(prefix="claude-link-probe-")
        try:
            ok, why = probe_shared_dir(base)
            self.assertTrue(ok, why)
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_a_missing_share_is_reported_not_raised(self):
        base = tempfile.mkdtemp(prefix="claude-link-missing-")
        try:
            missing = os.path.join(base, "OneDrive-typo", "claude-link")
            ok, why = probe_shared_dir(missing)
            self.assertFalse(ok, "a path that is not there must not read as healthy")
            self.assertIn("does not exist", why)
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_a_typo_is_never_created_as_a_local_directory(self):
        """The failure this guards is silent and total.

        A synced folder is made by the sync client, so a path that is missing is
        a typo. Creating it would produce a perfectly working local directory
        that syncs to nobody -- and from the inside that is indistinguishable
        from a colleague who never joined.
        """
        base = tempfile.mkdtemp(prefix="claude-link-typo-")
        try:
            typo = os.path.join(base, "Dropbx", "shared")
            probe_shared_dir(typo)
            self.assertFalse(os.path.exists(typo),
                             "the probe created the typo instead of reporting it")
            self.assertEqual(os.listdir(base), [])
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_a_file_where_a_folder_should_be_is_reported(self):
        base = tempfile.mkdtemp(prefix="claude-link-notdir-")
        try:
            path = os.path.join(base, "not-a-folder")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x")
            ok, why = probe_shared_dir(path)
            self.assertFalse(ok)
            self.assertIn("not a directory", why)
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_no_share_configured_is_not_an_error_state(self):
        ok, why = probe_shared_dir("")
        self.assertFalse(ok)
        self.assertIn("no shared_dir", why)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestStoppingDoesNotEatTheBatchInFlight(FileTransportCase):
    """`_drain` unlinks every frame it collects before delivering any of them.

    The loop already knows this is dangerous: the comment above the per-frame
    try/except says a raising frame "used to take the untouched remainder of
    the batch with it -- other senders' messages, deleted from the share and
    never delivered". That was fixed for exceptions. `asyncio.CancelledError`
    is re-raised on the next line, and `stop()` cancelled the task outright, so
    the identical loss happened on the ordinary shutdown path instead.

    Not a crash path. `daemon._shutdown` stops every room, and so does
    `link_leave`, and so does re-joining a room you are already in.

    `GitTransport.stop()` waits its *sync* loop out rather than cancelling, and
    says why in a comment. The poll loop it inherits never got the same
    treatment.
    """

    async def test_every_frame_already_unlinked_is_still_delivered(self):
        sender = a_device("sender")
        reader = a_device("reader")

        # Every frame written before the reader's loop exists, so `_drain`
        # collects all of them in one pass and one `stop()` decides the fate of
        # the whole batch. Starting the reader first makes the split between
        # batches depend on machine load, which is how the first version of this
        # test passed on its own and failed under `discover`.
        writer = FileTransport(
            shared_dir=self.share, room_id=self.keys.room_id,
            device_id=sender.device_id, on_frame=lambda _f: asyncio.sleep(0),
            poll_ms=50, log=lambda _m: None)
        sent = 6
        for n in range(sent):
            frame, _env = self.a_frame(sender, f"MSG-{n}", seq=n + 1)
            await writer.send(frame, [reader.device_id])

        first = asyncio.Event()
        release = asyncio.Event()
        delivered = []

        async def slow_on_frame(frame):
            delivered.append(frame)
            if len(delivered) == 1:
                first.set()
                await release.wait()

        b = FileTransport(
            shared_dir=self.share, room_id=self.keys.room_id,
            device_id=reader.device_id, on_frame=slow_on_frame,
            poll_ms=50, log=lambda _m: None)
        await b.start()
        self.transports.append(b)

        # Hold the first delivery open, so `stop()` lands with the rest of the
        # batch already gone from disk and not yet handed to anybody.
        await asyncio.wait_for(first.wait(), timeout=15.0)
        stopping = asyncio.create_task(b.stop())
        await asyncio.sleep(0.2)
        release.set()
        await asyncio.wait_for(stopping, timeout=15.0)
        self.transports.remove(b)

        # What may never happen is a frame existing nowhere: not delivered, and
        # no longer on the share for the next run to find.
        left = self.pending_for(sender.device_id, reader.device_id)
        self.assertEqual(len(delivered) + left, sent,
                         f"{sent - len(delivered) - left} frame(s) were unlinked "
                         f"from the share and never delivered: only "
                         f"{len(delivered)} arrived and {left} are still on disk")

    async def test_stopping_still_finishes_promptly(self):
        """The wait has to be bounded, or a wedged `on_frame` turns shutdown
        into a hang, which is worse than the loss it is preventing."""
        b = await self.member(a_device("reader"))

        async def never(_frame):
            await asyncio.Event().wait()

        b.on_frame = never
        start = asyncio.get_running_loop().time()
        await asyncio.wait_for(b.stop(), timeout=30.0)
        self.transports.remove(b)
        self.assertLess(asyncio.get_running_loop().time() - start, 20.0)
