"""Door files on a carrier directory: write, scan, GC."""
import os
import tempfile
import time
import unittest

from link import door
from tests.test_door import FakeIdentity, ROOM


class DoorFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_write_door_entry_only_when_changed(self):
        ident = FakeIdentity()
        entry = door.door_entry(ident, ROOM)
        self.assertTrue(door.write_door_entry(self.root, ROOM, entry))
        self.assertFalse(door.write_door_entry(self.root, ROOM, entry))
        self.assertEqual(door.read_door_entries(self.root, ROOM), [entry])

    def test_knock_write_scan_and_remove(self):
        member, joiner = FakeIdentity(), FakeIdentity()
        door.write_door_entry(self.root, ROOM, door.door_entry(member, ROOM))
        knock = door.build_knock(joiner, ROOM, "Sofia",
                                 door.read_door_entries(self.root, ROOM))
        door.write_knock_file(self.root, ROOM, knock)
        scanned = door.read_knock_files(self.root, ROOM)
        self.assertEqual(scanned, [knock])
        door.remove_join_files(self.root, ROOM, joiner.device_id)
        self.assertEqual(door.read_knock_files(self.root, ROOM), [])

    def test_grant_write_and_read(self):
        member, joiner = FakeIdentity(), FakeIdentity()
        grant = door.build_grant(member, ROOM, joiner.device_id,
                                 door.door_box_key_b64(joiner),
                                 room_name="t", secret="s")
        door.write_grant_file(self.root, ROOM, grant)
        self.assertEqual(door.read_grant_file(self.root, ROOM, joiner.device_id),
                         grant)
        self.assertIsNone(door.read_grant_file(self.root, ROOM, "dev_" + "x" * 16))

    def test_gc_removes_only_stale_knocks(self):
        member, joiner = FakeIdentity(), FakeIdentity()
        door.write_door_entry(self.root, ROOM, door.door_entry(member, ROOM))
        knock = door.build_knock(joiner, ROOM, "S",
                                 door.read_door_entries(self.root, ROOM))
        door.write_knock_file(self.root, ROOM, knock)
        door.gc_stale_knocks(self.root, ROOM, now=time.time())
        self.assertEqual(len(door.read_knock_files(self.root, ROOM)), 1)
        door.gc_stale_knocks(self.root, ROOM,
                             now=time.time() + door.KNOCK_TTL_S + 60)
        self.assertEqual(door.read_knock_files(self.root, ROOM), [])


class PresenceFlagTest(unittest.IsolatedAsyncioTestCase):
    async def test_knocker_transport_writes_no_heartbeat(self):
        from link.transport_file import FileTransport, presence_dir

        async def swallow(_frame):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            t = FileTransport(shared_dir=tmp, room_id=ROOM,
                              device_id="dev_" + "a" * 16, on_frame=swallow,
                              presence=False)
            await t.start()
            await t.stop()
            self.assertEqual(
                [n for n in os.listdir(presence_dir(tmp, ROOM))
                 if n.endswith(".json")],
                [])


if __name__ == "__main__":
    unittest.main()
