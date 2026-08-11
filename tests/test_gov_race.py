"""Concurrent removals converge to one winner on every machine."""
import itertools
import unittest

from link import door, gov
from link.crypto import new_invite, parse_invite
from tests.test_door import FakeIdentity, ROOM


class RaceTest(unittest.TestCase):
    def test_every_shuffle_of_arrival_order_agrees(self):
        a, b = FakeIdentity(), FakeIdentity()
        genesis = gov.build_genesis(a, ROOM)
        grant = gov.build_role(a, ROOM, gov.record_hash(genesis),
                               b.device_id, "admin")
        h = gov.record_hash(grant)
        keys = {a.device_id: door.door_box_key_b64(a),
                b.device_id: door.door_box_key_b64(b)}
        n1, s1 = parse_invite(new_invite("team-x"))
        n2, s2 = parse_invite(new_invite("team-x"))
        r1 = gov.build_removal(a, ROOM, h, b.device_id, n1, s1,
                               [a.device_id], keys, "A")
        r2 = gov.build_removal(b, ROOM, h, a.device_id, n2, s2,
                               [b.device_id], keys, "B")
        records = [genesis, grant, r1, r2]
        winners = set()
        for perm in itertools.permutations(records):
            state = gov.evaluate(list(perm), ROOM)
            self.assertIsNotNone(state.removal)
            winners.add(gov.record_hash(state.removal))
        self.assertEqual(len(winners), 1)

    def test_partial_sync_still_agrees_once_the_gap_fills(self):
        """A member that saw only one removal migrates to it; when the sibling
        with the lower hash arrives, evaluation converges to the same winner
        everywhere. The transient divergence is documented; the terminal state
        must not be."""
        a, b = FakeIdentity(), FakeIdentity()
        genesis = gov.build_genesis(a, ROOM)
        grant = gov.build_role(a, ROOM, gov.record_hash(genesis),
                               b.device_id, "admin")
        h = gov.record_hash(grant)
        keys = {a.device_id: door.door_box_key_b64(a),
                b.device_id: door.door_box_key_b64(b)}
        n1, s1 = parse_invite(new_invite("team-x"))
        n2, s2 = parse_invite(new_invite("team-x"))
        r1 = gov.build_removal(a, ROOM, h, b.device_id, n1, s1,
                               [a.device_id], keys, "A")
        r2 = gov.build_removal(b, ROOM, h, a.device_id, n2, s2,
                               [b.device_id], keys, "B")
        winner = min([r1, r2], key=gov.record_hash)
        partial = gov.evaluate([genesis, grant, max([r1, r2],
                                                    key=gov.record_hash)], ROOM)
        self.assertIsNotNone(partial.removal)   # acted on what it could see
        full = gov.evaluate([genesis, grant, r1, r2], ROOM)
        self.assertEqual(gov.record_hash(full.removal),
                         gov.record_hash(winner))


if __name__ == "__main__":
    unittest.main()
