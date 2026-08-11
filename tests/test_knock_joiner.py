"""The knock worker: door code in, joined room out — or a clean no."""
import asyncio
import os
import tempfile
import unittest

from link import door, store
from tests.test_door import FakeIdentity


class KnockJoinerTest(unittest.IsolatedAsyncioTestCase):
    """Drives the worker against a shared_dir carrier. The git carrier runs
    the identical file layout through GitTransport, which Task 4 pinned."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("CLAUDE_LINK_HOME")
        os.environ["CLAUDE_LINK_HOME"] = self.tmp.name
        store.save_config({**store.load_config(), "display_name": "Sofia"})
        from link import identity
        identity.load(refresh=True)
        from link.daemon import LinkDaemon
        self.daemon = LinkDaemon(store.load_config())
        self.carrier = tempfile.TemporaryDirectory()
        # The room being knocked at, owned by a different identity.
        self.host = FakeIdentity()
        from link.crypto import new_invite, parse_invite, derive_room
        self.room_name, self.secret = parse_invite(new_invite("team-x"))
        self.keys = derive_room(self.room_name, self.secret)
        door.write_door_entry(self.carrier.name, self.keys.room_id,
                              door.door_entry(self.host, self.keys.room_id))

    async def asyncTearDown(self):
        for room in list(self.daemon.rooms.values()):
            await room.stop()
        for task in list(self.daemon._knock_tasks.values()):
            task.cancel()
        self.carrier.cleanup()
        if self._old is None:
            os.environ.pop("CLAUDE_LINK_HOME", None)
        else:
            os.environ["CLAUDE_LINK_HOME"] = self._old
        self.tmp.cleanup()

    def _door_code(self):
        return door.door_code(self.room_name, self.keys.room_id)

    async def _wait(self, predicate, timeout=20.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            self.assertLess(asyncio.get_running_loop().time(), deadline,
                            "timed out waiting")
            await asyncio.sleep(0.1)

    async def test_knock_grant_join(self):
        resp = await self.daemon._op_join({
            "invite": self._door_code(), "shared_dir": self.carrier.name})
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(resp.get("knocked"))
        # The worker writes the knock...
        await self._wait(lambda: door.read_knock_files(
            self.carrier.name, self.keys.room_id))
        knock = door.read_knock_files(self.carrier.name, self.keys.room_id)[0]
        info = door.read_knock(self.host, self.keys.room_id, knock)
        self.assertEqual(info["name"], "Sofia")
        # ...the host answers...
        door.write_grant_file(self.carrier.name, self.keys.room_id,
                              door.build_grant(self.host, self.keys.room_id,
                                               info["device_id"], info["box_key"],
                                               room_name=self.room_name,
                                               secret=self.secret))
        # ...and the pending knock becomes a joined room.
        await self._wait(lambda: self.keys.room_id in self.daemon.rooms)
        self.assertNotIn(self.keys.room_id, self.daemon.pending_knocks)
        # The joiner cleaned up after itself.
        await self._wait(lambda: not door.read_knock_files(
            self.carrier.name, self.keys.room_id))
        self.assertIsNone(door.read_grant_file(
            self.carrier.name, self.keys.room_id,
            self.daemon.identity.device_id))

    async def test_denial_ends_the_knock_with_a_notice(self):
        resp = await self.daemon._op_join({
            "invite": self._door_code(), "shared_dir": self.carrier.name})
        self.assertTrue(resp["ok"], resp)
        await self._wait(lambda: door.read_knock_files(
            self.carrier.name, self.keys.room_id))
        knock = door.read_knock_files(self.carrier.name, self.keys.room_id)[0]
        info = door.read_knock(self.host, self.keys.room_id, knock)
        door.write_grant_file(self.carrier.name, self.keys.room_id,
                              door.build_grant(self.host, self.keys.room_id,
                                               info["device_id"], info["box_key"],
                                               denied=True))
        await self._wait(lambda: self.keys.room_id not in self.daemon.pending_knocks)
        self.assertNotIn(self.keys.room_id, self.daemon.rooms)
        texts = [(r["env"].get("body") or {}).get("text") or ""
                 for r in self.daemon.inbox]
        self.assertTrue(any("declined" in t for t in texts), texts)

    async def test_forged_grant_is_ignored(self):
        resp = await self.daemon._op_join({
            "invite": self._door_code(), "shared_dir": self.carrier.name})
        self.assertTrue(resp["ok"], resp)
        await self._wait(lambda: door.read_knock_files(
            self.carrier.name, self.keys.room_id))
        knock = door.read_knock_files(self.carrier.name, self.keys.room_id)[0]
        info = door.read_knock(self.host, self.keys.room_id, knock)
        # A "grant" whose secret derives to a different room entirely.
        door.write_grant_file(self.carrier.name, self.keys.room_id,
                              door.build_grant(self.host, self.keys.room_id,
                                               info["device_id"], info["box_key"],
                                               room_name="another-room",
                                               secret="X" * 26))
        await asyncio.sleep(6.0)
        self.assertNotIn(self.keys.room_id, self.daemon.rooms)
        self.assertIn(self.keys.room_id, self.daemon.pending_knocks)


if __name__ == "__main__":
    unittest.main()
