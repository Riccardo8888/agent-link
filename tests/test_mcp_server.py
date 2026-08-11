"""Drive the MCP server the way Claude Code and Codex do: JSON-RPC over stdio.

Two budgets are guarded here, both for the same reason: everything this server
prints is paid for in a model's context, and paid again on every later turn.
Latency is guarded too, because these tools are meant to be called between steps
of the caller's own work.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Mirrors tests/test_link_e2e.py: the relay server ships separately, and the
# two tests here that boot one do not apply to a tree without relay/.
RELAY_AVAILABLE = os.path.exists(os.path.join(ROOT, "relay", "server.py"))
RELAY_SKIP_REASON = "the relay server is not in this tree; it ships separately"

from link.util import free_port  # noqa: E402
from tests.timing import budget  # noqa: E402

TOOLS_LIST_BUDGET = 6 * 1024      # schemas sit in context for the whole session
INBOX_RENDER_BUDGET = 1024        # one long message must not flood a turn


class MCPSession:
    """A live `link.mcp_server` subprocess, spoken to in JSON-RPC lines."""

    def __init__(self, home: str, agent_kind: str = "claude-code"):
        self.env = {
            **os.environ,
            "CLAUDE_LINK_HOME": home,
            "CLAUDE_LINK_CTRL_PORT": str(free_port()),
            "CLAUDE_LINK_AGENT_KIND": agent_kind,
            "CLAUDE_LINK_RELAY_URL": f"ws://127.0.0.1:{free_port()}/relay",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        self.proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "link.mcp_server"],
            cwd=ROOT, env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
        self._id = 0

    def request(self, method, params=None, timeout=40.0):
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

        deadline = time.monotonic() + budget(timeout)
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f"server exited: {self.proc.stderr.read()}")
            line = line.strip()
            if not line:
                continue
            resp = json.loads(line)
            if resp.get("id") == self._id:
                return resp
        raise TimeoutError(f"no response to {method}")

    def notify(self, method):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def call_tool(self, name, args=None, timeout=40.0):
        return self.request("tools/call", {"name": name, "arguments": args or {}},
                            timeout=timeout)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


class TestMCPServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="claude-link-mcp-")
        cls.s = MCPSession(cls.home)
        cls.init = cls.s.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        })
        cls.s.notify("notifications/initialized")
        start = time.monotonic()
        cls.s.call_tool("link_status")
        cls.cold_start_s = time.monotonic() - start

    @classmethod
    def tearDownClass(cls):
        try:
            from link.client import ControlClient
            ControlClient(port=int(cls.s.env["CLAUDE_LINK_CTRL_PORT"]),
                          home=cls.s.env["CLAUDE_LINK_HOME"]).call(
                "shutdown", timeout=2)
        except Exception:
            pass
        cls.s.close()
        time.sleep(0.5)
        shutil.rmtree(cls.home, ignore_errors=True)

    def _text(self, resp):
        return resp["result"]["content"][0]["text"]

    # -- protocol -------------------------------------------------------------- #

    def test_initialize_advertises_the_server(self):
        result = self.init["result"]
        self.assertEqual(result["serverInfo"]["name"], "agent-link")
        self.assertIn("tools", result["capabilities"])
        self.assertIn("non-blocking", result["instructions"])

    def test_the_client_protocol_version_is_echoed_when_supported(self):
        """Claude Code and Codex do not always ask for the same version, and a
        server that answers with its own regardless fails on one of them."""
        home = tempfile.mkdtemp(prefix="claude-link-proto-")
        try:
            session = MCPSession(home)
            resp = session.request("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "codex", "version": "1"},
            })
            self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")
            session.close()
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_an_unknown_protocol_version_falls_back_to_ours(self):
        home = tempfile.mkdtemp(prefix="claude-link-proto2-")
        try:
            session = MCPSession(home)
            resp = session.request("initialize", {
                "protocolVersion": "1999-01-01", "capabilities": {},
                "clientInfo": {"name": "x", "version": "1"},
            })
            self.assertEqual(resp["result"]["protocolVersion"], "2025-06-18")
            session.close()
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_tools_list_is_complete_and_well_formed(self):
        tools = self.s.request("tools/list")["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {
            "link_status", "link_join", "link_grant", "link_send", "link_inbox",
            "link_read", "link_wait", "link_channel", "link_history",
            "link_leave",
        })
        for tool in tools:
            self.assertTrue(tool["description"].strip(), tool["name"])
            self.assertEqual(tool["inputSchema"]["type"], "object")
            for required in tool["inputSchema"].get("required", []):
                self.assertIn(required, tool["inputSchema"]["properties"], tool["name"])

    # -- token budgets ----------------------------------------------------------- #

    def test_the_tool_schemas_fit_the_context_budget(self):
        """These bytes are in context for the entire session, every turn."""
        tools = self.s.request("tools/list")["result"]["tools"]
        size = len(json.dumps(tools, ensure_ascii=False))
        self.assertLess(size, TOOLS_LIST_BUDGET,
                        f"tools/list is {size} bytes, budget {TOOLS_LIST_BUDGET}")

    def test_status_renders_as_a_summary_not_a_state_dump(self):
        text = self._text(self.s.call_tool("link_status"))
        self.assertLess(len(text), 400, f"link_status rendered {len(text)} bytes")
        self.assertNotIn('"ok": true', text, "the default rendering must not be JSON")

    def test_verbose_is_available_when_the_detail_is_actually_wanted(self):
        text = self._text(self.s.call_tool("link_status", {"verbose": True}))
        self.assertIn('"ok": true', text)

    def test_status_tells_an_unjoined_user_what_to_do(self):
        text = self._text(self.s.call_tool("link_status"))
        self.assertIn("link_join", text)

    def test_send_without_a_room_fails_clearly(self):
        resp = self.s.call_tool("link_send", {"text": "nobody there"})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("link_join", self._text(resp))

    def test_an_unknown_channel_action_is_refused(self):
        resp = self.s.call_tool("link_channel", {"action": "wibble"})
        self.assertTrue(resp["result"]["isError"])

    # -- latency ------------------------------------------------------------------ #

    def test_cold_start_cost_is_bounded(self):
        self.assertLess(self.cold_start_s, budget(12.0),
                        f"cold start took {self.cold_start_s:.2f}s")

    def test_inbox_is_fast_and_empty(self):
        start = time.monotonic()
        resp = self.s.call_tool("link_inbox")
        self.assertLess(time.monotonic() - start, budget(1.0))
        self.assertIn("no new messages", self._text(resp))

    def test_repeated_calls_stay_in_the_millisecond_range(self):
        self.s.call_tool("link_inbox")            # warm the daemon connection
        start = time.monotonic()
        for _ in range(10):
            self.s.call_tool("link_inbox")
        per_call = (time.monotonic() - start) / 10
        self.assertLess(per_call, budget(0.15), f"{per_call * 1000:.1f} ms per call")

    def test_wait_respects_its_timeout(self):
        start = time.monotonic()
        text = self._text(self.s.call_tool("link_wait", {"timeout_ms": 700}, timeout=20))
        elapsed = time.monotonic() - start
        self.assertIn("timed out", text)
        self.assertGreaterEqual(elapsed, 0.6)
        self.assertLess(elapsed, budget(5.0))

    # -- robustness ----------------------------------------------------------------- #

    def test_unknown_tool_is_reported_not_crashed(self):
        resp = self.s.call_tool("link_nonexistent")
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("unknown tool", self._text(resp))

    def test_unknown_method_returns_a_jsonrpc_error(self):
        resp = self.s.request("does/not/exist")
        self.assertEqual(resp["error"]["code"], -32601)

    def test_the_server_survives_a_bad_request(self):
        self.s.proc.stdin.write("this is not json\n")
        self.s.proc.stdin.flush()
        self.assertIn("link_join", self._text(self.s.call_tool("link_status")))


class TestInboxRendering(unittest.TestCase):
    """The truncation half of the token budget, exercised against a real room."""

    @unittest.skipUnless(RELAY_AVAILABLE, RELAY_SKIP_REASON)
    def test_a_ten_kilobyte_message_renders_under_a_kilobyte(self):
        from tests.test_link_e2e import Peer, Relay, wait_for

        base = tempfile.mkdtemp(prefix="claude-link-render-")
        relay = Relay(base)
        try:
            a = Peer("sender", base, relay.url)
            b = Peer("reader", base, relay.url)
            created = a.call("join", room="render-room", timeout=40)
            b.call("join", invite=created["invite"], timeout=40)
            wait_for(lambda: a.call("status")["rooms"][0]["online"] >= 1,
                     what="the pair to connect")

            a.call("send", text="y" * 10240)
            wait_for(lambda: b.call("inbox", peek=True)["count"] > 0, what="the message")

            from link.mcp_server import render

            result = b.call("inbox")
            text = render("link_inbox", result)
            self.assertLess(len(text), INBOX_RENDER_BUDGET,
                            f"rendered {len(text)} bytes, budget {INBOX_RENDER_BUDGET}")
            self.assertIn("link_read", text, "the way to get the rest must be offered")
            a.stop()
            b.stop()
        finally:
            relay.stop()
            shutil.rmtree(base, ignore_errors=True)


class TestControlLatency(unittest.TestCase):
    """Guards the reused-socket optimisation. A fresh TCP connection per call
    costs ~13 ms on Windows loopback; reusing one costs ~0.1 ms. If this test
    starts failing, connection reuse has regressed."""

    @classmethod
    def setUpClass(cls):
        from link.client import ControlClient

        cls.home = tempfile.mkdtemp(prefix="claude-link-perf-")
        os.environ["CLAUDE_LINK_HOME"] = cls.home
        os.environ["CLAUDE_LINK_CTRL_PORT"] = str(free_port())
        cls.client = ControlClient()
        cls.client.ensure_daemon()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.client.call("shutdown", timeout=2)
        except Exception:
            pass
        for key in ("CLAUDE_LINK_HOME", "CLAUDE_LINK_CTRL_PORT"):
            os.environ.pop(key, None)
        time.sleep(0.4)
        shutil.rmtree(cls.home, ignore_errors=True)

    def test_inbox_round_trip_is_sub_millisecond_ish(self):
        for _ in range(20):
            self.client.call("inbox")
        start = time.perf_counter()
        for _ in range(200):
            self.client.call("inbox")
        per_call_ms = (time.perf_counter() - start) / 200 * 1000
        self.assertLess(per_call_ms, budget(5.0), f"{per_call_ms:.3f} ms per control call")

    def test_client_recovers_if_the_socket_is_dropped(self):
        self.client.call("status")
        self.client.close()          # simulate a daemon restart killing the connection
        self.assertTrue(self.client.call("status")["ok"])


class TestNotifyHook(unittest.TestCase):
    """The hook runs on every prompt: it must be silent, safe and quick."""

    def test_hook_is_silent_and_fast_without_a_daemon(self):
        home = tempfile.mkdtemp(prefix="claude-link-hook-")
        try:
            env = {
                **os.environ,
                "CLAUDE_LINK_HOME": home,
                "CLAUDE_LINK_CTRL_PORT": str(free_port()),   # nothing listening
                "PYTHONUTF8": "1",
            }
            start = time.monotonic()
            proc = subprocess.run(
                [sys.executable, "-X", "utf8", os.path.join(ROOT, "link", "hook_notify.py")],
                cwd=ROOT, env=env, input="{}", capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")
            self.assertLess(time.monotonic() - start, budget(3.0))
        finally:
            shutil.rmtree(home, ignore_errors=True)

    @unittest.skipUnless(RELAY_AVAILABLE, RELAY_SKIP_REASON)
    def test_the_hook_never_consumes_more_than_it_prints(self):
        """Draining twenty and printing three would mark seventeen messages read
        that nobody ever saw -- and being read, link_inbox would never show them."""
        from tests.test_link_e2e import Peer, Relay, wait_for

        base = tempfile.mkdtemp(prefix="claude-link-hookdrain-")
        relay = Relay(base)
        try:
            a = Peer("talker", base, relay.url)
            b = Peer("listener", base, relay.url)
            created = a.call("join", room="hook-room", timeout=40)
            b.call("join", invite=created["invite"], timeout=40)
            wait_for(lambda: a.call("status")["rooms"][0]["online"] >= 1,
                     what="the pair to connect")

            b.drain()
            for i in range(10):
                a.call("send", text=f"message {i}")
            wait_for(lambda: b.call("inbox", peek=True)["count"] >= 10,
                     what="all ten to arrive")

            proc = subprocess.run(
                [sys.executable, "-X", "utf8", os.path.join(ROOT, "link", "hook_notify.py")],
                cwd=ROOT, env={**os.environ, "CLAUDE_LINK_HOME": b.home,
                               "CLAUDE_LINK_CTRL_PORT": str(b.ctrl_port),
                               "PYTHONUTF8": "1"},
                input="{}", capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(proc.returncode, 0)
            shown = [line for line in proc.stdout.splitlines() if "message " in line]
            self.assertEqual(len(shown), 3, proc.stdout)

            left = b.call("inbox", limit=50)["count"]
            self.assertEqual(left, 7, "the seven the hook did not print must survive")
            a.stop()
            b.stop()
        finally:
            relay.stop()
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
