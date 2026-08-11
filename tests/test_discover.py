"""Reading which rooms are open on a carrier, without joining anything."""
import json
import os
import subprocess
import tempfile
import time
import unittest

from link import discover
from tests.test_door import ROOM


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class DiscoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.remote = os.path.join(self.tmp.name, "remote.git")
        _git(self.tmp.name, "init", "--bare", "-q", self.remote)
        work = os.path.join(self.tmp.name, "work")
        _git(self.tmp.name, "init", "-q", work)
        _git(work, "config", "user.email", "t@t")
        _git(work, "config", "user.name", "t")
        _git(work, "checkout", "-q", "-b", "claude-link")
        pres = os.path.join(work, "claude-link", ROOM, "presence")
        os.makedirs(pres)
        with open(os.path.join(pres, "dev_" + "a" * 16 + ".json"), "w") as fh:
            json.dump({"device_id": "dev_" + "a" * 16, "room_id": ROOM,
                       "epoch": time.time()}, fh)
        os.makedirs(os.path.join(work, "claude-link", ROOM, "door"))
        with open(os.path.join(work, "claude-link", ROOM, "door",
                               "dev_" + "a" * 16 + ".json"), "w") as fh:
            json.dump({"device_id": "dev_" + "a" * 16}, fh)
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "seed")
        _git(work, "push", "-q", self.remote, "claude-link")

    def test_finds_the_room_with_count_and_recency(self):
        rooms = discover.discover_rooms(self.remote, "claude-link")
        self.assertEqual(len(rooms), 1)
        room = rooms[0]
        self.assertEqual(room["room_id"], ROOM)
        self.assertEqual(room["members"], 1)
        self.assertTrue(room["has_door"])
        self.assertLess(room["last_active_s"], 300)

    def test_no_branch_means_no_rooms(self):
        empty = os.path.join(self.tmp.name, "empty.git")
        _git(self.tmp.name, "init", "--bare", "-q", empty)
        self.assertEqual(discover.discover_rooms(empty, "claude-link"), [])

    def test_unreachable_remote_is_none_not_empty(self):
        self.assertIsNone(discover.discover_rooms(
            os.path.join(self.tmp.name, "missing.git"), "claude-link"))


class JoinOrCreateGateTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.home = tempfile.TemporaryDirectory()
        self._old = os.environ.get("CLAUDE_LINK_HOME")
        os.environ["CLAUDE_LINK_HOME"] = self.home.name
        from link import store, identity
        store.save_config({**store.load_config(), "display_name": "t"})
        identity.load(refresh=True)
        from link.daemon import LinkDaemon
        self.daemon = LinkDaemon(store.load_config())
        self.found = [{"room_id": ROOM, "members": 2,
                       "last_active_s": 60.0, "has_door": True}]
        self.daemon._discover = self._fake_discover      # no network in tests

    async def _fake_discover(self, _remote, _branch):
        return self.found

    async def asyncTearDown(self):
        for room in list(self.daemon.rooms.values()):
            await room.stop()
        if self._old is None:
            os.environ.pop("CLAUDE_LINK_HOME", None)
        else:
            os.environ["CLAUDE_LINK_HOME"] = self._old
        self.home.cleanup()

    async def test_creating_over_an_open_room_asks_first(self):
        resp = await self.daemon._op_join({"room": "new-room",
                                           "git_remote": "https://example.com/x.git"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp.get("needs_decision"), "join_or_create")
        self.assertEqual(resp["open_rooms"][0]["room_id"], ROOM)

    async def test_create_anyway_proceeds_and_remembers_the_decline(self):
        resp = await self.daemon._op_join({"room": "new-room",
                                           "git_remote": "https://example.com/x.git",
                                           "create_anyway": True})
        self.assertTrue(resp["ok"], resp)
        from link.store import load_state
        self.assertIn(ROOM, load_state()["rooms_declined"])
        # Asked once: the same create now sails through without create_anyway.
        resp2 = await self.daemon._op_join({"room": "second-room",
                                            "git_remote": "https://example.com/x.git"})
        self.assertTrue(resp2["ok"], resp2)

    async def test_roomless_status_carries_open_rooms(self):
        self.daemon.open_rooms = self.found
        resp = await self.daemon._op_status({})
        self.assertEqual(resp["open_rooms"][0]["room_id"], ROOM)


if __name__ == "__main__":
    unittest.main()
