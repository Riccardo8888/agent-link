"""Surviving a daemon restart, which a long-lived client could not do.

The MCP server is a long-lived `ControlClient` and it is every agent's only
route to the link. A daemon restart broke it until its editor was restarted,
and `agent-link restart` is what `doctor`, `config --set` and `link_status`
all tell people to do, so the advice broke the agent that read it.

The cause was one budget shared by two different retries. A restart kills the
pooled socket *and* invalidates the token, always in that order:

  1. the pooled socket is dead, `sendall` raises, the handler drops it and
     retries -- spending the only attempt, and never rebuilding the request,
     so the stale token is still inside it
  2. the fresh connection is refused as unauthorised, and the token-refresh
     branch was guarded on being the first attempt, so it never ran

Each failure needs its own allowance, and the request has to be rebuilt from
the current token every time round.
"""

import json
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from link.client import ControlClient, LinkClientError  # noqa: E402


class FakeSocket:
    """A socket that answers from a script, and can be made to be already dead.

    Records the token of every request it is handed, which is the assertion
    that matters: a retry that resends the token just refused is not a retry.
    """

    def __init__(self, replies, dead=False):
        self.replies = list(replies)
        self.dead = dead
        self.tokens = []
        self.closed = False
        self._pending = b""

    def settimeout(self, _t):
        pass

    def setsockopt(self, *_a):
        pass

    def sendall(self, wire):
        if self.dead:
            raise OSError("an existing connection was forcibly closed")
        self.tokens.append(json.loads(wire.decode("utf-8")).get("token"))
        reply = self.replies.pop(0) if self.replies else {"ok": True}
        self._pending = (json.dumps(reply) + "\n").encode("utf-8")

    def recv(self, _n):
        out, self._pending = self._pending, b""
        return out

    def close(self):
        self.closed = True


class ScriptedClientCase(unittest.TestCase):
    """A client whose connections come from a script instead of the network."""

    def setUp(self):
        self.tokens = ["stale-token", "fresh-token"]
        self.client = ControlClient(port=45999)
        self.client._token = self.tokens[0]
        self.made = []

    def token_now(self):
        """What a re-read of daemon.json would return: the daemon has restarted."""
        return self.tokens[-1]

    def give(self, *sockets):
        """Hand these out in order as `_connect` is called."""
        queue = list(sockets)

        def connect():
            sock = queue.pop(0)
            self.made.append(sock)
            self.client._sock = sock
            return sock

        return patch.object(self.client, "_connect", side_effect=connect)

    def reading_a_fresh_token(self):
        return patch.object(
            type(self.client), "token",
            property(lambda c: c._token or self.token_now()),
        )


UNAUTHORISED = {"ok": False, "error": "unauthorised: no valid control token"}


class TestARestartIsSurvived(ScriptedClientCase):
    def test_a_dead_socket_then_a_stale_token_still_gets_through(self):
        """The reported failure, in the order a restart actually produces it."""
        self.client._sock = FakeSocket([], dead=True)
        second = FakeSocket([UNAUTHORISED])
        third = FakeSocket([{"ok": True, "pong": True}])

        with self.give(second, third), self.reading_a_fresh_token():
            resp = self.client.call("ping", timeout=1.0)

        self.assertTrue(resp.get("ok"), resp)
        self.assertEqual(second.tokens, ["stale-token"])
        self.assertEqual(third.tokens, ["fresh-token"],
                         "the retry resent the token that had just been refused")

    def test_a_stale_token_alone_is_still_refreshed(self):
        """The case that already worked, which must keep working."""
        first = FakeSocket([UNAUTHORISED])
        second = FakeSocket([{"ok": True}])

        with self.give(first, second), self.reading_a_fresh_token():
            self.assertTrue(self.client.call("ping", timeout=1.0).get("ok"))
        self.assertEqual(second.tokens, ["fresh-token"])

    def test_a_dead_socket_alone_is_still_retried(self):
        self.client._sock = FakeSocket([], dead=True)
        fresh = FakeSocket([{"ok": True}])

        with self.give(fresh), self.reading_a_fresh_token():
            self.assertTrue(self.client.call("ping", timeout=1.0).get("ok"))


class TestItStillGivesUp(ScriptedClientCase):
    """A retry that never stops is worse than the bug it fixes."""

    def test_a_token_that_is_simply_wrong_is_reported_not_retried_forever(self):
        sockets = [FakeSocket([UNAUTHORISED]) for _ in range(4)]
        with self.give(*sockets), self.reading_a_fresh_token():
            resp = self.client.call("ping", timeout=1.0)

        self.assertFalse(resp.get("ok"))
        self.assertIn("unauthorised", resp.get("error", ""))
        self.assertLessEqual(len(self.made), 3, "the client kept reconnecting")

    def test_a_daemon_that_is_gone_raises_rather_than_spinning(self):
        self.client._sock = FakeSocket([], dead=True)

        def refuse():
            raise LinkClientError("daemon not reachable on 127.0.0.1:45999")

        with patch.object(self.client, "_connect", side_effect=refuse):
            with self.assertRaises(LinkClientError):
                self.client.call("ping", timeout=1.0)


class TestAgainstARealDaemon(unittest.TestCase):
    """The same thing without a single stand-in, because this is the one that
    was actually broken and no scripted socket proved it at the time."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = {k: os.environ.get(k)
                       for k in ("CLAUDE_LINK_HOME", "CLAUDE_LINK_CTRL_PORT")}
        os.environ["CLAUDE_LINK_HOME"] = self._tmp.name
        os.environ["CLAUDE_LINK_CTRL_PORT"] = str(self._free_port())

    def tearDown(self):
        try:
            ControlClient().call("shutdown", timeout=3.0)
        except LinkClientError:
            pass
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            self._tmp.cleanup()
        except OSError:
            pass                      # the daemon's log handle may still be open

    @staticmethod
    def _free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def test_a_long_lived_client_keeps_working_across_a_restart(self):
        agent = ControlClient()            # stands in for the MCP server
        agent.ensure_daemon()
        self.assertTrue(agent.call("ping", timeout=5.0).get("ok"))
        before = agent.token

        ControlClient().call("shutdown", timeout=3.0)
        time.sleep(1.5)
        ControlClient().ensure_daemon()
        time.sleep(0.5)

        resp = agent.call("ping", timeout=5.0)
        self.assertTrue(resp.get("ok"),
                        f"the agent was locked out by a restart: {resp}")
        self.assertNotEqual(agent.token, before, "the token was never re-read")


if __name__ == "__main__":
    unittest.main(verbosity=2)
