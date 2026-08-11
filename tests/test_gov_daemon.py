"""Governance through the daemon: genesis, roles, removal, migration."""
import os
import shutil
import tempfile
import unittest

from link import door, gov, identity, store
from link.daemon import LinkDaemon
from tests.test_door import FakeIdentity


class GovDaemonCase(unittest.IsolatedAsyncioTestCase):
    """Shared harness: a daemon in a throwaway home, a shared-dir carrier."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_LINK_HOME"] = self.tmp.name
        store.save_config({**store.load_config(), "display_name": "Riccardo"})
        identity.load(refresh=True)
        self.daemon = LinkDaemon(store.load_config())
        self.carrier = tempfile.TemporaryDirectory()
        self.addCleanup(self.carrier.cleanup)
        self.addCleanup(self.tmp.cleanup)

    async def asyncTearDown(self):
        for room in list(self.daemon.rooms.values()):
            await room.stop()

    async def _carrier_ready(self, room):
        """Transports attach asynchronously; a scan before the carrier is up
        sees no source and reads as a room with no governance."""
        import asyncio
        for _ in range(200):
            if room.transport("file") or room.transport("git"):
                return
            await asyncio.sleep(0.05)
        self.fail("carrier transport never attached")


class GenesisTest(GovDaemonCase):
    async def test_creating_a_room_writes_genesis(self):
        resp = await self.daemon._op_join(
            {"room": "team-x", "shared_dir": self.carrier.name})
        self.assertTrue(resp["ok"], resp)
        room = next(iter(self.daemon.rooms.values()))
        await self._carrier_ready(room)
        await self.daemon._scan_knocks(room)   # the loop that publishes
        recs = gov.read_gov_records(self.carrier.name, room.room_id)
        self.assertEqual([r["kind"] for r in recs], ["genesis"])
        self.assertEqual(recs[0]["device_id"], self.daemon.identity.device_id)
        state = gov.evaluate(recs, room.room_id)
        self.assertEqual(state.admins, {self.daemon.identity.device_id})

    async def test_status_shows_roles(self):
        await self.daemon._op_join(
            {"room": "team-x", "shared_dir": self.carrier.name})
        room = next(iter(self.daemon.rooms.values()))
        await self._carrier_ready(room)
        await self.daemon._scan_knocks(room)
        st = room.status()
        self.assertEqual(st.get("admins"), [self.daemon.identity.device_id])

    async def test_a_joining_member_never_publishes_genesis(self):
        """Only the creator seeds the chain; a joiner racing a second genesis
        would make the winner depend on hash order."""
        from link.crypto import derive_room, new_invite, parse_invite
        creator = FakeIdentity()
        name, secret = parse_invite(new_invite("team-x"))
        keys = derive_room(name, secret)
        genesis = gov.build_genesis(creator, keys.room_id)
        gov.write_gov_record(self.carrier.name, keys.room_id, genesis)
        resp = await self.daemon._op_join(
            {"invite": f"{name}#{secret}", "shared_dir": self.carrier.name})
        self.assertTrue(resp["ok"], resp)
        room = self.daemon.rooms[keys.room_id]
        await self._carrier_ready(room)
        await self.daemon._scan_knocks(room)
        recs = gov.read_gov_records(self.carrier.name, keys.room_id)
        self.assertEqual([r["kind"] for r in recs], ["genesis"])
        self.assertEqual(recs[0]["device_id"], creator.device_id)
        # But governance is discovered from the carrier all the same.
        self.assertEqual(room.gov_state.admins, {creator.device_id})

    async def test_legacy_room_refuses_roles(self):
        await self.daemon._op_join(
            {"room": "old-room", "shared_dir": self.carrier.name})
        room = next(iter(self.daemon.rooms.values()))
        room.record.pop("gov_creator", None)   # simulate a pre-2.3 room
        shutil.rmtree(os.path.join(self.carrier.name, "claude-link",
                                   room.room_id, "gov"), ignore_errors=True)
        room.gov_state = gov.GovState()
        await self.daemon._scan_knocks(room)
        resp = await self.daemon._op_role(
            {"device": "dev_" + "a" * 16, "role": "admin"})
        self.assertFalse(resp["ok"])
        self.assertIn("predates roles", resp["error"])


class RoleAndRemoveOpsTest(GovDaemonCase):
    async def _make_room_with_member(self):
        await self.daemon._op_join(
            {"room": "team-x", "shared_dir": self.carrier.name})
        self.room = next(iter(self.daemon.rooms.values()))
        self.member = FakeIdentity()
        door.write_door_entry(self.carrier.name, self.room.room_id,
                              door.door_entry(self.member, self.room.room_id))
        await self._carrier_ready(self.room)
        await self.daemon._scan_knocks(self.room)

    async def test_role_grant_writes_a_valid_record(self):
        await self._make_room_with_member()
        resp = await self.daemon._op_role(
            {"device": self.member.device_id, "role": "admin"})
        self.assertTrue(resp["ok"], resp)
        await self.daemon._scan_knocks(self.room)
        self.assertIn(self.member.device_id, self.room.gov_state.admins)

    async def test_remove_writes_record_and_migrates_the_remover(self):
        await self._make_room_with_member()
        old_id = self.room.room_id
        resp = await self.daemon._op_remove({"device": self.member.device_id})
        self.assertTrue(resp["ok"], resp)
        recs = gov.read_gov_records(self.carrier.name, old_id)
        state = gov.evaluate(recs, old_id)
        self.assertIsNotNone(state.removal)
        self.assertEqual(state.removal["target"], self.member.device_id)
        # The remover migrated: old room superseded, successor live.
        self.assertNotIn(old_id, self.daemon.rooms)
        self.assertEqual(len(self.daemon.rooms), 1)
        succ = next(iter(self.daemon.rooms.values()))
        self.assertEqual(succ.name, "team-x")
        self.assertNotEqual(succ.room_id, old_id)
        # The successor chain was seeded by the remover.
        succ_recs = gov.read_gov_records(self.carrier.name, succ.room_id)
        self.assertEqual([r["kind"] for r in succ_recs], ["genesis"])
        self.assertEqual(succ.gov_state.admins,
                         {self.daemon.identity.device_id})

    async def test_remove_by_non_admin_is_refused_locally(self):
        await self._make_room_with_member()
        self.room.gov_state.admins = {self.member.device_id}
        resp = await self.daemon._op_remove({"device": self.member.device_id})
        self.assertFalse(resp["ok"])
        self.assertIn("admin", resp["error"])

    async def test_remove_in_legacy_room_is_refused(self):
        await self._make_room_with_member()
        self.room.record.pop("gov_creator", None)
        self.room.gov_state = gov.GovState()
        resp = await self.daemon._op_remove({"device": self.member.device_id})
        self.assertFalse(resp["ok"])
        self.assertIn("predates roles", resp["error"])

    async def test_removing_yourself_is_refused(self):
        await self._make_room_with_member()
        resp = await self.daemon._op_remove(
            {"device": self.daemon.identity.device_id})
        self.assertFalse(resp["ok"])
        self.assertIn("leave", resp["error"])


class MemberMigrationTest(GovDaemonCase):
    async def _room_created_by(self, creator):
        """A room whose genesis belongs to `creator` (a FakeIdentity); the
        daemon joins it as an ordinary member via the invite path."""
        from link.crypto import derive_room, new_invite, parse_invite
        name, secret = parse_invite(new_invite("team-x"))
        keys = derive_room(name, secret)
        genesis = gov.build_genesis(creator, keys.room_id)
        gov.write_gov_record(self.carrier.name, keys.room_id, genesis)
        door.write_door_entry(self.carrier.name, keys.room_id,
                              door.door_entry(creator, keys.room_id))
        resp = await self.daemon._op_join(
            {"invite": f"{name}#{secret}", "shared_dir": self.carrier.name})
        self.assertTrue(resp["ok"], resp)
        room = self.daemon.rooms[keys.room_id]
        await self._carrier_ready(room)
        await self.daemon._scan_knocks(room)   # publishes our door entry
        return room, keys, genesis

    def _removal_against(self, admin, keys, genesis, target_device):
        from link.crypto import new_invite, parse_invite
        entries = door.read_door_entries(self.carrier.name, keys.room_id)
        box_keys = {e["device_id"]: door.verify_door_entry(e, keys.room_id)
                    for e in entries}
        succ_name, succ_secret = parse_invite(new_invite("team-x"))
        return gov.build_removal(admin, keys.room_id,
                                 prev=gov.record_hash(genesis),
                                 target=target_device,
                                 successor_name=succ_name,
                                 successor_secret=succ_secret,
                                 admins=[admin.device_id], box_keys=box_keys,
                                 removed_by_name="Admin")

    async def test_survivor_migrates_on_seeing_a_removal(self):
        admin = FakeIdentity()
        goner = FakeIdentity()
        room, keys, genesis = await self._room_created_by(admin)
        door.write_door_entry(self.carrier.name, keys.room_id,
                              door.door_entry(goner, keys.room_id))
        rec = self._removal_against(admin, keys, genesis, goner.device_id)
        gov.write_gov_record(self.carrier.name, keys.room_id, rec)
        await self.daemon._scan_knocks(room)
        self.assertNotIn(keys.room_id, self.daemon.rooms)
        succ = next(r for r in self.daemon.rooms.values() if r.name == "team-x")
        self.assertNotEqual(succ.room_id, keys.room_id)
        self.assertEqual(succ.gov_state.admins, {admin.device_id})
        texts = [(r["env"].get("body") or {}).get("text") or ""
                 for r in self.daemon.inbox]
        self.assertTrue(any("removed" in t for t in texts), texts)

    async def test_removed_member_gets_the_notice_and_the_room_ends(self):
        admin = FakeIdentity()
        room, keys, genesis = await self._room_created_by(admin)
        rec = self._removal_against(admin, keys, genesis,
                                    self.daemon.identity.device_id)
        gov.write_gov_record(self.carrier.name, keys.room_id, rec)
        await self.daemon._scan_knocks(room)
        self.assertNotIn(keys.room_id, self.daemon.rooms)
        self.assertFalse(any(r.name == "team-x"
                             for r in self.daemon.rooms.values()))
        texts = [(r["env"].get("body") or {}).get("text") or ""
                 for r in self.daemon.inbox]
        self.assertTrue(any("You were removed" in t for t in texts), texts)


if __name__ == "__main__":
    unittest.main()
