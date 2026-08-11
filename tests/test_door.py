"""The door's crypto: derived X25519 keys and sealed boxes."""
import unittest

from link import door
from link.crypto import CryptoError


class FakeIdentity:
    """The three members of Identity that door.py touches."""

    def __init__(self):
        from link import crypto
        self._key = crypto.generate_device_key()
        self.public_key = crypto.public_bytes(self._key)
        self.device_id = crypto.device_id_for(self.public_key)
        self.label = "test@host"
        self.agent_kind = "cli"

    def sign_key(self):
        return self._key


ROOM = "room_" + "a" * 26


class DoorKeyTest(unittest.TestCase):
    def test_door_key_is_deterministic_per_identity(self):
        ident = FakeIdentity()
        a = door.door_box_key_b64(ident)
        b = door.door_box_key_b64(ident)
        self.assertEqual(a, b)

    def test_two_identities_have_different_door_keys(self):
        self.assertNotEqual(door.door_box_key_b64(FakeIdentity()),
                            door.door_box_key_b64(FakeIdentity()))


class SealedBoxTest(unittest.TestCase):
    def test_round_trip(self):
        ident = FakeIdentity()
        box = door.seal_box(door.door_box_key_b64(ident),
                            {"name": "Sofia"}, ROOM, "knock")
        self.assertEqual(door.open_box(ident, box, ROOM, "knock"),
                         {"name": "Sofia"})

    def test_wrong_recipient_fails(self):
        box = door.seal_box(door.door_box_key_b64(FakeIdentity()),
                            {"name": "Sofia"}, ROOM, "knock")
        with self.assertRaises(CryptoError):
            door.open_box(FakeIdentity(), box, ROOM, "knock")

    def test_wrong_room_or_purpose_fails(self):
        ident = FakeIdentity()
        box = door.seal_box(door.door_box_key_b64(ident),
                            {"name": "Sofia"}, ROOM, "knock")
        with self.assertRaises(CryptoError):
            door.open_box(ident, box, "room_" + "b" * 26, "knock")
        with self.assertRaises(CryptoError):
            door.open_box(ident, box, ROOM, "grant")

    def test_tampered_ciphertext_fails(self):
        ident = FakeIdentity()
        box = door.seal_box(door.door_box_key_b64(ident),
                            {"name": "Sofia"}, ROOM, "knock")
        box["ct"] = box["ct"][:-4] + ("AAAA" if box["ct"][-4:] != "AAAA" else "BBBB")
        with self.assertRaises(CryptoError):
            door.open_box(ident, box, ROOM, "knock")

    def test_garbage_box_fails_closed(self):
        with self.assertRaises(CryptoError):
            door.open_box(FakeIdentity(), {"epk": "!!", "nonce": "!!", "ct": "!!"},
                          ROOM, "knock")


class DoorEntryTest(unittest.TestCase):
    def test_entry_round_trip(self):
        ident = FakeIdentity()
        entry = door.door_entry(ident, ROOM)
        self.assertEqual(door.verify_door_entry(entry, ROOM),
                         door.door_box_key_b64(ident))

    def test_entry_is_deterministic(self):
        ident = FakeIdentity()
        self.assertEqual(door.door_entry(ident, ROOM), door.door_entry(ident, ROOM))

    def test_entry_for_other_room_rejected(self):
        entry = door.door_entry(FakeIdentity(), ROOM)
        with self.assertRaises(CryptoError):
            door.verify_door_entry(entry, "room_" + "b" * 26)

    def test_entry_with_swapped_key_rejected(self):
        a, b = FakeIdentity(), FakeIdentity()
        entry = door.door_entry(a, ROOM)
        entry["box_key"] = door.door_box_key_b64(b)   # tamper
        with self.assertRaises(CryptoError):
            door.verify_door_entry(entry, ROOM)


class DoorCodeTest(unittest.TestCase):
    def test_code_round_trip(self):
        code = door.door_code("Team X", ROOM)
        self.assertEqual(door.parse_door_code(code), ("team-x", ROOM))

    def test_ordinary_invite_is_not_a_door_code(self):
        self.assertIsNone(door.parse_door_code("team-x#K7PQ2M4XBVWZ9NRTYD3JFHCS8A"))
        self.assertIsNone(door.parse_door_code("not an invite at all"))

    def test_damaged_door_code_raises_rather_than_derives(self):
        # 'DOOR-' present but the id is mangled: this must be an error, never
        # fall through to being treated as a secret (the old typo trap).
        with self.assertRaises(CryptoError):
            door.parse_door_code("team-x#DOOR-NOT!VALID")


class KnockGrantTest(unittest.TestCase):
    def setUp(self):
        self.member = FakeIdentity()
        self.joiner = FakeIdentity()
        self.entries = [door.door_entry(self.member, ROOM)]

    def test_knock_and_read(self):
        knock = door.build_knock(self.joiner, ROOM, "Sofia", self.entries)
        info = door.read_knock(self.member, ROOM, knock)
        self.assertEqual(info["name"], "Sofia")
        self.assertEqual(info["device_id"], self.joiner.device_id)
        self.assertEqual(info["box_key"], door.door_box_key_b64(self.joiner))

    def test_knock_with_no_usable_doors_raises(self):
        with self.assertRaises(CryptoError):
            door.build_knock(self.joiner, ROOM, "Sofia", [])

    def test_tampered_knock_rejected(self):
        knock = door.build_knock(self.joiner, ROOM, "Sofia", self.entries)
        knock["box_key"] = door.door_box_key_b64(FakeIdentity())
        with self.assertRaises(CryptoError):
            door.read_knock(self.member, ROOM, knock)

    def test_grant_round_trip(self):
        knock = door.build_knock(self.joiner, ROOM, "Sofia", self.entries)
        info = door.read_knock(self.member, ROOM, knock)
        grant = door.build_grant(self.member, ROOM, info["device_id"],
                                 info["box_key"], room_name="team-x",
                                 secret="SECRETSECRET")
        opened = door.read_grant(self.joiner, ROOM, grant)
        self.assertEqual(opened, {"name": "team-x", "secret": "SECRETSECRET",
                                  "granter": self.member.device_id})

    def test_denial_round_trip(self):
        knock = door.build_knock(self.joiner, ROOM, "Sofia", self.entries)
        info = door.read_knock(self.member, ROOM, knock)
        grant = door.build_grant(self.member, ROOM, info["device_id"],
                                 info["box_key"], denied=True)
        opened = door.read_grant(self.joiner, ROOM, grant)
        self.assertTrue(opened["denied"])

    def test_grant_for_someone_else_rejected(self):
        knock = door.build_knock(self.joiner, ROOM, "Sofia", self.entries)
        info = door.read_knock(self.member, ROOM, knock)
        grant = door.build_grant(self.member, ROOM, info["device_id"],
                                 info["box_key"], room_name="t", secret="s")
        with self.assertRaises(CryptoError):
            door.read_grant(FakeIdentity(), ROOM, grant)

    def test_grant_matches_room_fails_on_wrong_secret(self):
        # The check that closes the forged-grant hole. Slow (~1s: two scrypts).
        from link.crypto import new_invite, parse_invite, derive_room
        name, secret = parse_invite(new_invite("real-room"))
        real = derive_room(name, secret)
        self.assertTrue(door.grant_matches_room(name, secret, real.room_id))
        self.assertFalse(door.grant_matches_room(name, "WRONGSECRETWRONGSECRETWRON",
                                                 real.room_id))


if __name__ == "__main__":
    unittest.main()
