"""Governance records: genesis, role, removal. Pure crypto, no daemon."""
import os
import tempfile
import unittest

from link import door, gov
from link.crypto import CryptoError, new_invite, parse_invite
from tests.test_door import FakeIdentity, ROOM


class RecordBuildTest(unittest.TestCase):
    def test_genesis_roundtrip(self):
        creator = FakeIdentity()
        rec = gov.build_genesis(creator, ROOM)
        self.assertEqual(rec["kind"], "genesis")
        self.assertEqual(rec["prev"], "")
        self.assertEqual(gov.verify_record(rec, ROOM), rec["device_id"])

    def test_role_roundtrip(self):
        admin, member = FakeIdentity(), FakeIdentity()
        genesis = gov.build_genesis(admin, ROOM)
        rec = gov.build_role(admin, ROOM, prev=gov.record_hash(genesis),
                             target=member.device_id, role="admin")
        self.assertEqual(gov.verify_record(rec, ROOM), admin.device_id)
        self.assertEqual(rec["target"], member.device_id)

    def test_tampered_record_is_refused(self):
        creator = FakeIdentity()
        rec = gov.build_genesis(creator, ROOM)
        rec["ts"] = rec["ts"] + 1
        with self.assertRaises(CryptoError):
            gov.verify_record(rec, ROOM)

    def test_wrong_room_is_refused(self):
        creator = FakeIdentity()
        rec = gov.build_genesis(creator, ROOM)
        with self.assertRaises(CryptoError):
            gov.verify_record(rec, "room_" + "b" * 26)

    def test_record_hash_is_stable_and_distinct(self):
        creator = FakeIdentity()
        a = gov.build_genesis(creator, ROOM)
        self.assertEqual(gov.record_hash(a), gov.record_hash(dict(a)))
        b = gov.build_role(creator, ROOM, prev=gov.record_hash(a),
                           target=FakeIdentity().device_id, role="admin")
        self.assertNotEqual(gov.record_hash(a), gov.record_hash(b))


class EvaluationTest(unittest.TestCase):
    """Total order is (chain position, record hash); validity is admin-at-that-point."""

    def setUp(self):
        self.creator = FakeIdentity()
        self.other = FakeIdentity()
        self.genesis = gov.build_genesis(self.creator, ROOM)
        self.g = gov.record_hash(self.genesis)

    def test_genesis_seeds_the_admin_set(self):
        state = gov.evaluate([self.genesis], ROOM)
        self.assertEqual(state.admins, {self.creator.device_id})
        self.assertEqual(state.removed, set())
        self.assertEqual(state.head, self.g)

    def test_role_grant_by_admin_applies(self):
        rec = gov.build_role(self.creator, ROOM, self.g,
                             self.other.device_id, "admin")
        state = gov.evaluate([self.genesis, rec], ROOM)
        self.assertIn(self.other.device_id, state.admins)

    def test_role_grant_by_non_admin_is_void(self):
        rec = gov.build_role(self.other, ROOM, self.g,
                             self.other.device_id, "admin")
        state = gov.evaluate([self.genesis, rec], ROOM)
        self.assertEqual(state.admins, {self.creator.device_id})
        self.assertEqual(state.void, [gov.record_hash(rec)])

    def test_evaluation_is_order_independent(self):
        rec = gov.build_role(self.creator, ROOM, self.g,
                             self.other.device_id, "admin")
        a = gov.evaluate([self.genesis, rec], ROOM)
        b = gov.evaluate([rec, self.genesis], ROOM)
        self.assertEqual(a.admins, b.admins)
        self.assertEqual(a.order, b.order)

    def test_sibling_records_order_by_hash(self):
        third = FakeIdentity()
        r1 = gov.build_role(self.creator, ROOM, self.g,
                            self.other.device_id, "admin")
        r2 = gov.build_role(self.creator, ROOM, self.g,
                            third.device_id, "admin")
        state = gov.evaluate([self.genesis, r1, r2], ROOM)
        expected = sorted([gov.record_hash(r1), gov.record_hash(r2)])
        self.assertEqual(state.order[1:], expected)

    def test_revoked_admin_loses_authority_downstream(self):
        grant = gov.build_role(self.creator, ROOM, self.g,
                               self.other.device_id, "admin")
        revoke = gov.build_role(self.creator, ROOM, gov.record_hash(grant),
                                self.other.device_id, "member")
        late = gov.build_role(self.other, ROOM, gov.record_hash(revoke),
                              FakeIdentity().device_id, "admin")
        state = gov.evaluate([self.genesis, grant, revoke, late], ROOM)
        self.assertNotIn(self.other.device_id, state.admins)
        self.assertIn(gov.record_hash(late), state.void)


class GovFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.creator = FakeIdentity()

    def test_write_then_read_roundtrip(self):
        rec = gov.build_genesis(self.creator, ROOM)
        gov.write_gov_record(self.root, ROOM, rec)
        self.assertEqual(gov.read_gov_records(self.root, ROOM), [rec])

    def test_write_is_idempotent_and_multi_record(self):
        a = gov.build_genesis(self.creator, ROOM)
        b = gov.build_role(self.creator, ROOM, gov.record_hash(a),
                           FakeIdentity().device_id, "admin")
        gov.write_gov_record(self.root, ROOM, a)
        gov.write_gov_record(self.root, ROOM, a)   # rewrite: no error, no dup
        gov.write_gov_record(self.root, ROOM, b)
        got = gov.read_gov_records(self.root, ROOM)
        self.assertEqual({gov.record_hash(r) for r in got},
                         {gov.record_hash(a), gov.record_hash(b)})

    def test_write_survives_the_scanner_holding_the_fresh_file(self):
        """Windows: antivirus briefly holds a just-created file, and
        os.replace answers PermissionError. Seen for real, one run in eight,
        as `gov scan failed on file: PermissionError ... .tmp-42912-gov`."""
        import unittest.mock
        rec = gov.build_genesis(self.creator, ROOM)
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(13, "Permission denied", src)
            return real_replace(src, dst)

        with unittest.mock.patch.object(gov.os, "replace", flaky_replace):
            self.assertTrue(gov.write_gov_record(self.root, ROOM, rec))
        self.assertEqual(gov.read_gov_records(self.root, ROOM), [rec])
        self.assertEqual(calls["n"], 3)

    def test_garbage_files_are_skipped(self):
        rec = gov.build_genesis(self.creator, ROOM)
        gov.write_gov_record(self.root, ROOM, rec)
        d = os.path.join(self.root, "claude-link", ROOM, "gov")
        with open(os.path.join(d, "not-a-hash.json"), "w") as f:
            f.write("{broken")
        with open(os.path.join(d, ("f" * 32) + ".json"), "w") as f:
            f.write('{"v":1}')
        self.assertEqual(gov.read_gov_records(self.root, ROOM), [rec])


class RemovalRecordTest(unittest.TestCase):
    def setUp(self):
        self.admin = FakeIdentity()
        self.stays = FakeIdentity()
        self.goes = FakeIdentity()
        self.genesis = gov.build_genesis(self.admin, ROOM)
        self.g = gov.record_hash(self.genesis)
        name, secret = parse_invite(new_invite("team-x"))
        self.succ_name, self.succ_secret = name, secret
        self.entries = {
            self.admin.device_id: door.door_box_key_b64(self.admin),
            self.stays.device_id: door.door_box_key_b64(self.stays),
            self.goes.device_id: door.door_box_key_b64(self.goes),
        }

    def _removal(self):
        return gov.build_removal(
            self.admin, ROOM, prev=self.g, target=self.goes.device_id,
            successor_name=self.succ_name, successor_secret=self.succ_secret,
            admins=[self.admin.device_id],
            box_keys=self.entries, removed_by_name="Riccardo")

    def test_remaining_members_can_open_their_box(self):
        rec = self._removal()
        for who in (self.admin, self.stays):
            opened = gov.open_rekey_box(who, rec)
            self.assertEqual(opened["secret"], self.succ_secret)
            self.assertEqual(opened["name"], self.succ_name)

    def test_removed_member_has_no_rekey_box(self):
        rec = self._removal()
        self.assertNotIn(self.goes.device_id, rec["successor"]["boxes"])
        with self.assertRaises(CryptoError):
            gov.open_rekey_box(self.goes, rec)

    def test_removed_member_can_open_the_notice(self):
        rec = self._removal()
        notice = gov.open_notice_box(self.goes, rec)
        self.assertIn("Riccardo", notice["text"])
        self.assertIn("removed", notice["text"])

    def test_successor_embeds_the_admin_set(self):
        rec = self._removal()
        self.assertEqual(rec["successor"]["admins"], [self.admin.device_id])

    def test_removal_by_non_admin_is_void_in_evaluation(self):
        rec = gov.build_removal(
            self.stays, ROOM, prev=self.g, target=self.admin.device_id,
            successor_name=self.succ_name, successor_secret=self.succ_secret,
            admins=[self.stays.device_id], box_keys=self.entries,
            removed_by_name="Mallory")
        state = gov.evaluate([self.genesis, rec], ROOM)
        self.assertIsNone(state.removal)
        self.assertIn(gov.record_hash(rec), state.void)

    def test_concurrent_removals_resolve_by_hash(self):
        grant = gov.build_role(self.admin, ROOM, self.g,
                               self.stays.device_id, "admin")
        h = gov.record_hash(grant)
        r1 = gov.build_removal(self.admin, ROOM, prev=h,
                               target=self.stays.device_id,
                               successor_name=self.succ_name,
                               successor_secret=self.succ_secret,
                               admins=[self.admin.device_id],
                               box_keys=self.entries, removed_by_name="A")
        r2 = gov.build_removal(self.stays, ROOM, prev=h,
                               target=self.admin.device_id,
                               successor_name=self.succ_name,
                               successor_secret=self.succ_secret,
                               admins=[self.stays.device_id],
                               box_keys=self.entries, removed_by_name="B")
        state = gov.evaluate([self.genesis, grant, r1, r2], ROOM)
        winner = min([r1, r2], key=gov.record_hash)
        self.assertEqual(gov.record_hash(state.removal), gov.record_hash(winner))
        loser = max([r1, r2], key=gov.record_hash)
        self.assertIn(gov.record_hash(loser), state.void)


if __name__ == "__main__":
    unittest.main()
