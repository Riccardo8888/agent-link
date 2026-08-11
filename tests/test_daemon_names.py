"""The daemon refuses to join nameless, and saves a name passed to join."""
import os
import tempfile
import unittest

from link import store


class DaemonNameGateTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("CLAUDE_LINK_HOME")
        os.environ["CLAUDE_LINK_HOME"] = self.tmp.name
        from link import identity
        identity.load(refresh=True)
        from link.daemon import LinkDaemon
        self.daemon = LinkDaemon(store.load_config())

    async def asyncTearDown(self):
        if self._old is None:
            os.environ.pop("CLAUDE_LINK_HOME", None)
        else:
            os.environ["CLAUDE_LINK_HOME"] = self._old
        self.tmp.cleanup()

    async def test_join_without_a_name_is_refused_with_need_name(self):
        resp = await self.daemon._op_join({"room": "team-x"})
        self.assertFalse(resp["ok"])
        self.assertTrue(resp.get("need_name"))

    async def test_join_with_name_saves_it_and_proceeds(self):
        resp = await self.daemon._op_join({"room": "team-x", "name": "Sofia"})
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(store.display_name_set())
        self.assertEqual(store.load_config()["display_name"], "Sofia")
        room = next(iter(self.daemon.rooms.values()))
        self.assertEqual(room.display_name, "Sofia")
        await room.stop()


if __name__ == "__main__":
    unittest.main()
