"""What `gaps` is allowed to mean.

`gaps` exists so that a lost message is visible, and the biggest untested risk
in this project is a shared folder silently dropping one. It was measuring
something else entirely.

Sequence numbers come from a reserved block, `SEQ_BLOCK = 1000`, and on startup
a room resumes at the persisted *ceiling* rather than the last number it used,
so that a crash loses the rest of the block instead of reusing it. That is
correct. The receiver was never told, and counted the resulting jump as loss --
up to a thousand phantom gaps per restart of the sender.

Found on 2026-08-08 by the second real machine, which reported `gaps=999`
against `highest_seq=4006` while having received four messages, and reasonably
concluded the counter was noise. A counter nobody believes is worse than no
counter, because the one time it is right nobody will act on it.

Real frames throughout: real keys, real signatures, a real second identity.
"""

import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from link import crypto                                          # noqa: E402
from link.envelope import make_envelope, make_origin, seal_frame  # noqa: E402
from link.identity import Identity                               # noqa: E402
from link.room import Room                                       # noqa: E402


def a_device(label):
    key = crypto.generate_device_key()
    public = crypto.public_bytes(key)
    return Identity(crypto.device_id_for(public), public, label, "cli", key)


class SequencingCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.keys = crypto.derive_room("seq-room", "a-secret-for-sequencing")
        self.me = a_device("me")
        self.peer = a_device("peer")
        self.delivered = []

        async def deliver(envelope, transport):
            self.delivered.append(envelope)

        self.room = Room(
            keys=self.keys,
            identity=self.me,
            record={"name": "seq-room", "secret": "a-secret-for-sequencing"},
            deliver=deliver,
            save_state=lambda: None,
        )

    async def hear(self, seq, epoch="run_one"):
        """One real sealed frame from the peer, at `seq`, from run `epoch`."""
        origin = make_origin(self.peer, epoch=epoch)
        envelope = make_envelope("msg", self.keys.room_id, self.peer.device_id,
                                 seq, origin, body={"text": f"m{seq}"})
        await self.room.on_frame(seal_frame(self.keys, self.peer, envelope), "file")

    def member(self):
        return self.room.members[self.peer.device_id]


class TestGapsMeanLostMessages(SequencingCase):
    async def test_a_missing_message_inside_one_run_is_a_gap(self):
        """The case the counter exists for, and it must keep working."""
        await self.hear(1)
        await self.hear(4)
        self.assertEqual(self.member().gaps, 2)

    async def test_consecutive_messages_are_no_gap(self):
        for seq in (1, 2, 3):
            await self.hear(seq)
        self.assertEqual(self.member().gaps, 0)

    async def test_the_first_message_from_anyone_is_never_a_gap(self):
        """We have no idea what they sent before we were listening."""
        await self.hear(4006)
        self.assertEqual(self.member().gaps, 0)


class TestARestartIsNotLoss(SequencingCase):
    async def test_a_resumed_block_does_not_count_as_lost_messages(self):
        """The reported bug, in one test.

        The peer restarts, resumes at its reserved ceiling, and its next
        sequence number is a thousand higher. Nothing was lost.
        """
        await self.hear(1)
        await self.hear(1001, epoch="run_two")
        self.assertEqual(self.member().gaps, 0)

    async def test_the_restart_is_still_reported_rather_than_hidden(self):
        """Quietly swallowing the jump would trade one lie for another."""
        await self.hear(1)
        await self.hear(1001, epoch="run_two")
        self.assertEqual(self.member().resumes, 1)

    async def test_seeing_a_member_for_the_first_time_is_not_a_restart(self):
        await self.hear(1)
        self.assertEqual(self.member().resumes, 0)

    async def test_loss_after_a_restart_is_still_counted(self):
        """The rebase must not blind the counter for the rest of the run."""
        await self.hear(1)
        await self.hear(1001, epoch="run_two")
        await self.hear(1004, epoch="run_two")
        self.assertEqual(self.member().gaps, 2)


class TestAPeerOnOlderCode(SequencingCase):
    """`epoch` is a new field in an existing block, and no version was bumped.

    Bumping `PROTOCOL_VERSION` would make every older peer's frames fail
    `validate_envelope` outright, which is a far worse outcome than a stale
    counter. A peer that sends no epoch has to keep working, and keep counting
    gaps exactly as it did before.
    """

    async def test_a_peer_with_no_epoch_still_has_its_gaps_counted(self):
        await self.hear(1, epoch=None)
        await self.hear(4, epoch=None)
        self.assertEqual(self.member().gaps, 2)

    async def test_a_peer_with_no_epoch_is_never_reported_as_resuming(self):
        for seq in (1, 2, 3):
            await self.hear(seq, epoch=None)
        self.assertEqual(self.member().resumes, 0)


class TestWhatStatusShows(SequencingCase):
    async def test_resumes_is_visible_where_gaps_is(self):
        """Otherwise the restart is invisible and somebody re-finds this bug."""
        await self.hear(1)
        await self.hear(1001, epoch="run_two")
        status = self.member().status()
        self.assertEqual(status["gaps"], 0)
        self.assertEqual(status["resumes"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTheOutboxDisplacesRatherThanExplodes(unittest.IsolatedAsyncioTestCase):
    """`_push`'s overflow branch increments `self.dropped_overflow`, and
    `Room.__init__` never created it.

    So the 501st queued message raised `AttributeError` on the line before
    `self.outbox.append(frame)`: the frame was destroyed instead of displacing
    the oldest, and the exception escaped `send` and `flush` into whatever
    called them. The comment three lines above that branch says the whole point
    is that dropping silently "is how a message goes missing with every status
    line still green" -- it lost a different message and crashed as well.
    """

    def a_room(self):
        async def deliver(_envelope, _transport):
            pass

        return Room(keys=crypto.derive_room("overflow", "a-secret-for-overflow"),
                    identity=a_device("me"),
                    record={"name": "overflow", "secret": "a-secret-for-overflow"},
                    deliver=deliver, save_state=lambda: None)

    async def test_a_fresh_room_has_the_counter_the_overflow_path_needs(self):
        self.assertEqual(self.a_room().dropped_overflow, 0)

    async def test_filling_the_outbox_displaces_the_oldest_and_counts_it(self):
        room = self.a_room()
        for n in range(room.outbox.maxlen):
            room.outbox.append({"n": n})

        # The real path, with no transports attached, so every send queues.
        result = await room._push({"msg_id": "msg_" + "z" * 16})

        self.assertEqual(result, "queued")
        self.assertEqual(room.dropped_overflow, 1)
        self.assertEqual(room.outbox[-1]["msg_id"], "msg_" + "z" * 16)
