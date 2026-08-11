"""A member sees a knock, and a grant answers it."""
import asyncio
import os
import tempfile
import unittest

from link import door, store
from tests.test_door import FakeIdentity


class MemberKnockTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("CLAUDE_LINK_HOME")
        os.environ["CLAUDE_LINK_HOME"] = self.tmp.name
        store.save_config({**store.load_config(), "display_name": "Riccardo"})
        from link import identity
        identity.load(refresh=True)
        from link.daemon import LinkDaemon
        self.daemon = LinkDaemon(store.load_config())
        # A real room over a shared_dir carrier (local temp dir): the cheapest
        # transport that exercises the same file layout the git channel syncs.
        self.carrier = tempfile.TemporaryDirectory()
        resp = await self.daemon._op_join({
            "room": "team-x", "shared_dir": self.carrier.name})
        assert resp["ok"], resp
        self.room = next(iter(self.daemon.rooms.values()))

    async def asyncTearDown(self):
        await self.room.stop()
        self.carrier.cleanup()
        if self._old is None:
            os.environ.pop("CLAUDE_LINK_HOME", None)
        else:
            os.environ["CLAUDE_LINK_HOME"] = self._old
        self.tmp.cleanup()

    async def _tick(self):
        # One iteration of the knock scan, without waiting out the 2 s loop.
        await self.daemon._scan_knocks(self.room)

    async def test_door_entry_is_published_and_knock_becomes_an_event(self):
        await self._tick()
        entries = door.read_door_entries(self.carrier.name, self.room.room_id)
        self.assertEqual(len(entries), 1)

        joiner = FakeIdentity()
        knock = door.build_knock(joiner, self.room.room_id, "Sofia", entries)
        door.write_knock_file(self.carrier.name, self.room.room_id, knock)
        await self._tick()

        self.assertIn(joiner.device_id, self.room.pending_knocks)
        kinds = [r["env"]["kind"] for r in self.daemon.inbox]
        self.assertIn("knock", kinds)
        # The default inbox fetch (include_system False) must surface it.
        resp = await self.daemon._op_inbox({})
        self.assertTrue(any(m["kind"] == "knock" for m in resp["messages"]))
        # Seen once: a second scan must not re-notify.
        before = len(self.daemon.inbox)
        await self._tick()
        self.assertEqual(len(self.daemon.inbox), before)

    async def test_grant_op_writes_a_grant_the_joiner_can_open(self):
        await self._tick()
        joiner = FakeIdentity()
        knock = door.build_knock(
            joiner, self.room.room_id, "Sofia",
            door.read_door_entries(self.carrier.name, self.room.room_id))
        door.write_knock_file(self.carrier.name, self.room.room_id, knock)
        await self._tick()

        resp = await self.daemon._op_grant({"device": joiner.device_id,
                                            "allow": True})
        self.assertTrue(resp["ok"], resp)
        raw = door.read_grant_file(self.carrier.name, self.room.room_id,
                                   joiner.device_id)
        opened = door.read_grant(joiner, self.room.room_id, raw)
        self.assertEqual(opened["secret"], self.room.record["secret"])
        self.assertNotIn(joiner.device_id, self.room.pending_knocks)

    async def test_deny_writes_a_denial(self):
        await self._tick()
        joiner = FakeIdentity()
        knock = door.build_knock(
            joiner, self.room.room_id, "Sofia",
            door.read_door_entries(self.carrier.name, self.room.room_id))
        door.write_knock_file(self.carrier.name, self.room.room_id, knock)
        await self._tick()
        resp = await self.daemon._op_grant({"device": "Sofia", "allow": False})
        self.assertTrue(resp["ok"], resp)
        raw = door.read_grant_file(self.carrier.name, self.room.room_id,
                                   joiner.device_id)
        self.assertTrue(door.read_grant(joiner, self.room.room_id, raw)["denied"])


if __name__ == "__main__":
    unittest.main()
