"""The renders the agent actually reads: terse, and word-for-word stable."""
import unittest

from link.mcp_server import TOOLS, render


class RenderTest(unittest.TestCase):
    def test_need_name_renders_the_script(self):
        text = render("link_join", {"ok": False, "need_name": True,
                                    "error": "no display name set"})
        self.assertIn('Ask the user: "Your name?"', text)
        self.assertIn("name=", text)

    def test_knock_sent(self):
        text = render("link_join", {"ok": True, "knocked": True,
                                    "room": "team-x", "room_id": "room_x"})
        self.assertEqual(text, "Knock sent, waiting for someone to let you in. "
                               "You'll be notified when a member answers.")

    def test_join_or_create_question(self):
        text = render("link_join", {
            "ok": False, "needs_decision": "join_or_create",
            "open_rooms": [{"room_id": "room_x", "members": 3,
                            "last_active_s": 300.0, "has_door": True}],
            "error": "open room"})
        self.assertIn("already has an open room (3 people, active 5 min ago)",
                      text)
        self.assertIn("Join it? I'll need the door code. Or make a new one?",
                      text)
        self.assertIn("create_anyway", text)

    def test_grant_renders_one_line(self):
        self.assertEqual(
            render("link_grant", {"ok": True, "granted": True, "name": "Sofia",
                                  "device": "dev_x", "room": "team-x"}),
            "Sofia is in.")
        self.assertEqual(
            render("link_grant", {"ok": True, "denied": True, "name": "Sofia",
                                  "device": "dev_x", "room": "team-x"}),
            "Told Sofia no.")

    def test_status_shows_knocks_and_names(self):
        text = render("link_status", {
            "ok": True, "unread": 0, "label": "x", "device_id": "dev_x",
            "rooms": [{"room": "team-x", "room_id": "room_x", "online": 1,
                       "members": 2, "transport": "git", "queued": 0,
                       "quiet_for_s": None, "setup_error": None,
                       "knocks": [{"device_id": "dev_k", "name": "Sofia",
                                   "ts": "t"}]}]})
        self.assertIn('Sofia [dev_k] wants to join team-x', text)
        self.assertIn("link_grant", text)


class ToolSurfaceTest(unittest.TestCase):
    def test_role_and_remove_tools_exist(self):
        from link.mcp_server import _TOOL_OPS
        names = {t["name"] for t in TOOLS}
        self.assertIn("link_role", names)
        self.assertIn("link_remove", names)
        self.assertEqual(_TOOL_OPS["link_role"], "role")
        self.assertEqual(_TOOL_OPS["link_remove"], "remove")

    def test_remove_renders_the_note(self):
        text = render("link_remove", {"ok": True, "removed": "dev_x",
                                      "note": "Door codes are void."})
        self.assertIn("Door codes are void.", text)

    def test_link_grant_exists_and_join_takes_a_name(self):
        names = {t["name"] for t in TOOLS}
        self.assertIn("link_grant", names)
        join = next(t for t in TOOLS if t["name"] == "link_join")
        props = join["inputSchema"]["properties"]
        self.assertIn("name", props)
        self.assertIn("create_anyway", props)


if __name__ == "__main__":
    unittest.main()
