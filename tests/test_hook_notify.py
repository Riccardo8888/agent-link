"""The hook that puts a peer's message in front of the agent unprompted.

Wired to UserPromptSubmit, the hook's stdout is handed to the model as part of
the turn, so printing a few lines is enough. Wired to PostToolUse -- which is
what makes the link work with no human in the loop, since it fires between the
agent's own steps -- plain stdout goes to the transcript the human reads and
never reaches the model.

That difference is not cosmetic, because fetching is destructive: the hook
marks what it fetched as read precisely so the same message is not shown twice.
A hook that fetches and then prints into a void does not merely fail to notify
-- it consumes the message, and link_inbox will never return it either. Silence
looks identical to nobody having written.
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from link.hook_notify import home_from_argv, render_notice      # noqa: E402

A_MESSAGE = {
    "room": "ccc",
    "from": "riccardo@laptop",
    "from_agent": "main",
    "text": "ho finito auth, passo a rate-limit",
    "msg_id": "msg_abc",
}


class TestPostToolUse(unittest.TestCase):
    """Between the agent's own steps, where there is no human turn to ride on."""

    def test_the_message_reaches_the_model_not_just_the_transcript(self):
        out = render_notice([A_MESSAGE], 0, "PostToolUse")
        payload = json.loads(out)
        self.assertIn(
            "rate-limit",
            payload["hookSpecificOutput"]["additionalContext"],
            "the peer's words must be in the field the model actually reads",
        )

    def test_the_output_names_the_event_it_belongs_to(self):
        """hookSpecificOutput without a matching hookEventName is discarded."""
        payload = json.loads(render_notice([A_MESSAGE], 0, "PostToolUse"))
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PostToolUse")

    def test_the_human_is_told_something_arrived(self):
        """The agent gets the text; the person watching gets a one-liner."""
        payload = json.loads(render_notice([A_MESSAGE], 0, "PostToolUse"))
        self.assertTrue(payload.get("systemMessage"),
                        "nothing tells the human a message came in")

    def test_the_count_of_what_is_still_waiting_is_carried_over(self):
        payload = json.loads(render_notice([A_MESSAGE], 4, "PostToolUse"))
        self.assertIn("4", payload["hookSpecificOutput"]["additionalContext"])


class TestUserPromptSubmit(unittest.TestCase):
    """The wiring that already worked, and must keep working."""

    def test_plain_text_is_still_plain_text(self):
        out = render_notice([A_MESSAGE], 0, "UserPromptSubmit")
        self.assertIn("rate-limit", out)
        with self.assertRaises(json.JSONDecodeError,
                               msg="this event takes stdout as-is, not JSON"):
            json.loads(out)

    def test_an_unknown_event_is_treated_as_plain_stdout(self):
        """Unrecognised is not a reason to invent a protocol."""
        out = render_notice([A_MESSAGE], 0, "SomeFutureEvent")
        self.assertIn("rate-limit", out)


class TestWhichInstallTheHookBelongsTo(unittest.TestCase):
    """Two agents on one machine, and a hook that must not read the other's mail.

    The hook finds its daemon through `CLAUDE_LINK_HOME`, and a harness that
    runs hooks with a clean environment gives it none -- so it resolves to the
    default home, which belongs to whichever agent installed there first. The
    result is not a hook that fails: it is a hook that quietly drains the *other*
    agent's inbox and prints that agent's messages into this one's context.
    Both agents then see silence, and the messages are marked read, so nothing
    will hand them back.

    An argument survives an empty environment, which is the whole point of
    passing the home this way rather than exporting it.
    """

    def test_the_home_can_be_passed_as_an_argument(self):
        self.assertEqual(
            home_from_argv(["hook_notify.py", "--home", "/homes/codex"]),
            "/homes/codex",
        )

    def test_no_argument_means_do_not_override_the_environment(self):
        self.assertIsNone(home_from_argv(["hook_notify.py"]))

    def test_a_dangling_flag_is_ignored_rather_than_crashing(self):
        """Exit 0 no matter what: a broken hook must not break the turn."""
        self.assertIsNone(home_from_argv(["hook_notify.py", "--home"]))

    def test_the_equals_form_works_too(self):
        self.assertEqual(
            home_from_argv(["hook_notify.py", "--home=/homes/codex"]),
            "/homes/codex",
        )


class TestSilence(unittest.TestCase):
    """Running on every tool call, saying nothing has to cost nothing."""

    def test_no_messages_prints_nothing_at_all(self):
        for event in ("PostToolUse", "UserPromptSubmit", None):
            with self.subTest(event=event):
                self.assertEqual(render_notice([], 0, event), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
