"""SKILL.md ships the scripts. If these lines drift, every agent drifts."""
import os
import unittest

SKILL = os.path.join(os.path.dirname(__file__), "..", "link", "SKILL.md")


class SkillContractTest(unittest.TestCase):
    def setUp(self):
        with open(SKILL, "r", encoding="utf-8") as fh:
            self.text = fh.read()

    def test_the_scripts_are_present_verbatim(self):
        for line in (
            'Your name?',
            'Knock sent, waiting for someone to let you in.',
            'wants to join',
            'Join it? I\'ll need the door code. Or make a new one?',
            'one line, no preamble',
        ):
            self.assertIn(line, self.text, line)

    def test_door_codes_are_explained(self):
        self.assertIn("#DOOR-", self.text)
        self.assertIn("link_grant", self.text)


if __name__ == "__main__":
    unittest.main()
