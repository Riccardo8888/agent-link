"""Who the daemon believes is there, learned from the shared folder alone.

Without a relay there is no server to announce arrivals and departures, so the
only evidence of another agent is the heartbeat file it refreshes in the folder.
Reading that evidence too generously is worse than having none: a colleague's
agent that died half an hour ago reported as "online, last seen 0.2s ago" sends
you looking for a fault in your own room, in your own invite string, in
everything except the one thing that is actually wrong.

The distinction these tests defend is between two windows that look alike and
mean opposite things. Retention -- days -- is "still owed a copy of what we
send", and is why a sleeping member keeps its queue. Liveness -- seconds -- is
"answering right now". A member is entitled to the first long after it has lost
the second.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from link import crypto                                            # noqa: E402
from link.daemon import LinkDaemon                                  # noqa: E402
from link.envelope import K_PRESENCE                                # noqa: E402
from link.room import MEMBER_STALE_S, Room                          # noqa: E402
from link.transport_file import (                                   # noqa: E402
    PRESENCE_STALE_S,
    FileTransport,
    presence_dir,
)
from link.util import atomic_write_text                             # noqa: E402

A_PEER = "dev_apeernothereaaaa"


class FolderPresenceCase(unittest.IsolatedAsyncioTestCase):
    """One daemon, one room on a folder, and peers that exist only as files.

    The peers are never real processes: a heartbeat file with a chosen age is
    the whole of what the shared folder can ever tell us about another member,
    so writing one by hand tests exactly what the daemon has to reason from.
    """

    @classmethod
    def setUpClass(cls):
        # Deliberately slow (~0.5s), so it is derived once for the whole class.
        cls.keys = crypto.derive_room("presence-room", "a-secret-for-presence")

    async def asyncSetUp(self):
        self.base = tempfile.mkdtemp(prefix="claude-link-presence-")
        self.share = os.path.join(self.base, "share")
        home = os.path.join(self.base, "home")
        os.makedirs(self.share, exist_ok=True)
        os.makedirs(home, exist_ok=True)

        self._home_before = os.environ.get("CLAUDE_LINK_HOME")
        os.environ["CLAUDE_LINK_HOME"] = home

        self.daemon = LinkDaemon({
            "shared_dir": self.share,
            "file_poll_ms": 50,
            "inbox_max": 100,
            "log_sinks": [],
        })
        self.room = Room(
            keys=self.keys,
            identity=self.daemon.identity,
            record={"name": "presence-room", "secret": "a-secret-for-presence"},
            deliver=self.daemon._deliver_envelope,
            save_state=lambda: None,
            log=lambda _m: None,
        )
        self.daemon.rooms[self.keys.room_id] = self.room

        self.transport = FileTransport(
            shared_dir=self.share,
            room_id=self.keys.room_id,
            device_id=self.daemon.identity.device_id,
            on_frame=lambda frame: self.room.on_frame(frame, "file"),
            poll_ms=50,
            log=lambda _m: None,
        )
        await self.transport.start()
        self.room.attach("file", self.transport)
        self._tracker = None

    async def asyncTearDown(self):
        if self._tracker is not None:
            self._tracker.cancel()
            try:
                await self._tracker
            except asyncio.CancelledError:
                pass
        self.daemon.rooms.clear()          # the tracker loop exits on this
        await self.transport.stop()
        if self._home_before is None:
            os.environ.pop("CLAUDE_LINK_HOME", None)
        else:
            os.environ["CLAUDE_LINK_HOME"] = self._home_before
        shutil.rmtree(self.base, ignore_errors=True)

    # -- helpers ------------------------------------------------------------- #

    def heartbeat(self, device_id: str, age_s: float) -> float:
        """Write a peer's heartbeat file, dated `age_s` seconds ago.

        Atomically, because a live poll loop is reading this directory while we
        write. A plain write is visible half-finished, and a half-finished
        heartbeat does not parse -- at which point `_scan_presence` falls back
        to the file's mtime, which is *now*, and this fixture hands the test a
        peer that looks like it wrote a moment ago. The suite then fails
        claiming a dead member read as online, which is true, and blames the
        code under test, which is not.

        Rare enough to pass for months and reliable under load: it turned up
        twice in six runs with the machine busy. Production heartbeats have
        always gone through `atomic_write_text`; only the fixture did not.
        """
        epoch = time.time() - age_s
        directory = presence_dir(self.share, self.keys.room_id)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{device_id}.json")
        atomic_write_text(path, json.dumps({
            "device_id": device_id,
            "room_id": self.keys.room_id,
            "epoch": epoch,
        }))
        # mtime is the fallback the transport reads when the JSON will not
        # parse; keeping it consistent stops it from masking a wrong epoch.
        os.utime(path, (epoch, epoch))
        return epoch

    async def until(self, predicate, what, timeout=10.0):
        for _ in range(int(timeout / 0.05)):
            if predicate():
                return True
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")

    async def track(self, device_id: str):
        """Run the daemon's folder tracker until it has considered `device_id`.

        The tracker's first pass runs the moment it is awaited, so waiting on
        the transport's own cache first is what makes this deterministic: by
        the time the tracker looks, the heartbeat is already there to be seen.
        """
        await self.until(lambda: device_id in self.transport.roster(),
                         f"{device_id}'s heartbeat to reach the roster cache")
        self._tracker = asyncio.create_task(
            self.daemon._track_folder_roster(self.room)
        )
        await asyncio.sleep(0.2)           # one pass; the loop then sleeps 2s

    def presence_events(self) -> list[dict]:
        return [item["env"] for item in self.daemon.inbox
                if item["env"].get("kind") == K_PRESENCE]


class TestAStaleHeartbeat(FolderPresenceCase):
    """A peer whose daemon has stopped. Its file remains; its process does not."""

    async def test_a_stale_heartbeat_does_not_read_as_online(self):
        self.heartbeat(A_PEER, age_s=MEMBER_STALE_S * 3)
        await self.track(A_PEER)

        member = self.room.members.get(A_PEER)
        self.assertIsNotNone(member, "a member on the share must stay listed")
        self.assertFalse(
            member.online,
            "a heartbeat three times past the staleness window read as online",
        )

    async def test_last_seen_reports_the_heartbeat_age_not_the_moment_we_looked(self):
        """The number that made the false 'online' believable.

        Reported as 0.2s, it reads as a member answering continuously. The
        honest figure is the age of the last heartbeat they actually wrote.
        """
        age = MEMBER_STALE_S * 3
        self.heartbeat(A_PEER, age_s=age)
        await self.track(A_PEER)

        last_seen = self.room.members[A_PEER].status()["last_seen_s"]
        self.assertGreaterEqual(
            last_seen, age - 5.0,
            f"last_seen_s of {last_seen} describes our poll, not their heartbeat",
        )

    async def test_a_peer_that_was_already_gone_is_not_announced_as_present(self):
        """The message that starts the wild goose chase.

        "X is on the shared folder" arriving for someone who stopped an hour
        ago is the same lie as the online flag, in the form the human reads.
        """
        self.heartbeat(A_PEER, age_s=PRESENCE_STALE_S * 10)
        await self.track(A_PEER)

        self.assertEqual(
            [e["body"]["text"] for e in self.presence_events()], [],
            "announced a peer that had not been heard from in ten windows",
        )

    async def test_a_room_holding_only_stale_members_reports_nobody_online(self):
        """What link_status shows, which is where the wrong answer surfaced."""
        self.heartbeat(A_PEER, age_s=MEMBER_STALE_S * 3)
        await self.track(A_PEER)

        status = self.room.status()
        self.assertEqual(status["members"], 1, "the member is still in the room")
        self.assertEqual(status["online"], 0, "nobody is answering")


class TestALiveHeartbeat(FolderPresenceCase):
    """The other half: a fix that stops trusting the folder entirely is no fix."""

    async def test_a_fresh_heartbeat_reads_as_online(self):
        self.heartbeat(A_PEER, age_s=1.0)
        await self.track(A_PEER)

        member = self.room.members.get(A_PEER)
        self.assertIsNotNone(member, "a live peer must be discovered")
        self.assertTrue(member.online, "a one-second-old heartbeat is not stale")
        self.assertLess(member.status()["last_seen_s"], PRESENCE_STALE_S)

    async def test_a_fresh_peer_is_announced_once(self):
        self.heartbeat(A_PEER, age_s=1.0)
        await self.track(A_PEER)
        await asyncio.sleep(2.2)           # a second pass through the loop

        events = self.presence_events()
        self.assertEqual(len(events), 1, "the arrival is news exactly once")
        self.assertIn(A_PEER, events[0]["body"]["text"] + events[0]["body"]["device_id"])

    async def test_a_peer_that_comes_back_is_announced_when_it_does(self):
        """Stale at first sight must not mean written off for good."""
        self.heartbeat(A_PEER, age_s=PRESENCE_STALE_S * 10)
        await self.track(A_PEER)
        self.assertEqual(self.presence_events(), [], "not present yet")

        self.heartbeat(A_PEER, age_s=0.0)
        await self.until(lambda: self.presence_events(),
                         "the peer to be announced once it is live", timeout=8.0)
        self.assertTrue(self.room.members[A_PEER].online)


class TestAPeerWhoseClockDisagrees(FolderPresenceCase):
    """The folder is shared; the clock that dates the heartbeats is not.

    Every other transport reports liveness over a live connection, where "when
    we heard it" and "when they said it" are the same instant. A folder is the
    one place they come apart: the peer stamps the file from its own clock, and
    two laptops that have never spoken to a time server can disagree by
    minutes. Read literally, a colleague whose clock runs slow is written off
    as gone while they are sitting there typing.
    """

    async def test_a_lagging_clock_does_not_make_a_writing_peer_look_gone(self):
        lag = MEMBER_STALE_S * 3
        self.heartbeat(A_PEER, age_s=lag)
        await self.track(A_PEER)

        # They are alive and beating; their clock simply disagrees with ours.
        self.heartbeat(A_PEER, age_s=lag - 2)
        await self.until(
            lambda: self.room.members[A_PEER].online,
            "a peer that keeps writing to read as online despite its clock",
        )

    async def test_a_clock_running_ahead_does_not_report_a_negative_age(self):
        """A heartbeat dated in the future is still evidence, not a paradox."""
        self.heartbeat(A_PEER, age_s=-600.0)
        await self.track(A_PEER)

        last_seen = self.room.members[A_PEER].status()["last_seen_s"]
        self.assertGreaterEqual(
            last_seen, 0.0, f"last_seen_s of {last_seen} puts them in the future",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
