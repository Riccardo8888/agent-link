"""Human names: on the origin block, in the hello, into the roster."""
import unittest

from link.envelope import make_origin
from link.room import Member, Room
from link import crypto
from tests.test_door import FakeIdentity


class OriginNameTest(unittest.TestCase):
    def test_name_rides_the_origin_when_given(self):
        ident = FakeIdentity()
        self.assertEqual(make_origin(ident, name="Sofia")["name"], "Sofia")
        self.assertNotIn("name", make_origin(ident))


class RosterNameTest(unittest.TestCase):
    def _room(self, ident):
        keys = crypto.derive_room("t", "S" * 26)

        async def deliver(_env, _t):
            pass

        return Room(keys=keys, identity=ident, record={"secret": "S" * 26},
                    deliver=deliver, save_state=lambda: None)

    def test_member_learns_name_from_origin(self):
        room = self._room(FakeIdentity())
        member = Member("dev_" + "b" * 16)
        room._update_member(member, {"origin": {"name": "Sofia", "label": "s@x"}})
        self.assertEqual(member.name, "Sofia")
        self.assertEqual(member.status()["name"], "Sofia")

    def test_build_stamps_the_rooms_display_name(self):
        room = self._room(FakeIdentity())
        room.display_name = "Riccardo"
        env = room.build("msg", {"text": "hi"})
        self.assertEqual(env["origin"]["name"], "Riccardo")


class DisplayNameSetTest(unittest.TestCase):
    def test_display_name_set_reads_the_file_not_the_default(self):
        import os, tempfile
        from link import store
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("CLAUDE_LINK_HOME")
            os.environ["CLAUDE_LINK_HOME"] = tmp
            try:
                self.assertFalse(store.display_name_set())
                store.save_config({**store.load_config(), "display_name": "Sofia"})
                self.assertTrue(store.display_name_set())
            finally:
                if old is None:
                    os.environ.pop("CLAUDE_LINK_HOME", None)
                else:
                    os.environ["CLAUDE_LINK_HOME"] = old


if __name__ == "__main__":
    unittest.main()
