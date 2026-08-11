"""End-to-end: a real relay and three real daemons, each in its own process.

Every daemon gets its own CLAUDE_LINK_HOME, so config, identity, state and
`.conv/` logs stay as separate as they would be on three different laptops.
Nothing here reaches inside the implementation: it drives the control socket,
which is exactly what the MCP server does.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The relay server ships separately from the client package, so a checkout
# without relay/ is a real configuration, not a broken one. The tests that
# boot the server as a subprocess do not apply there and say so.
RELAY_AVAILABLE = os.path.exists(os.path.join(ROOT, "relay", "server.py"))
RELAY_SKIP_REASON = "the relay server is not in this tree; it ships separately"
sys.path.insert(0, ROOT)

from link.client import ControlClient, LinkClientError  # noqa: E402
from link.util import free_port                         # noqa: E402
from tests.timing import budget                          # noqa: E402


def wait_for(predicate, timeout=20.0, interval=0.2, what="condition"):
    """Poll until the predicate is truthy.

    A predicate that raises counts as "not yet", not as a failure: a daemon that
    has not finished rejoining its rooms answers `status` with an empty list,
    and indexing that is exactly how a caller finds out it is too early.
    """
    deadline = time.monotonic() + budget(timeout)
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
        except (LinkClientError, IndexError, KeyError, TypeError) as exc:
            last = f"({type(exc).__name__}: {exc})"
        else:
            if last:
                return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}; last value: {last!r}")


class Relay:
    """`python -m relay.server` in a subprocess, as it runs in production."""

    def __init__(self, base):
        self.port = free_port()
        self.base = base
        self.log = open(os.path.join(base, "relay.log"), "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "relay.server"],
            cwd=ROOT, stdout=self.log, stderr=self.log, stdin=subprocess.DEVNULL,
            env={
                **os.environ,
                "HOST": "127.0.0.1",
                "PORT": str(self.port),
                "CLAUDE_LINK_RELAY_DB": os.path.join(base, "relay.sqlite3"),
                "PYTHONUTF8": "1",
            },
        )
        wait_for(self.healthy, what="relay to answer /healthz")

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/relay"

    def healthy(self):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/healthz", timeout=1
            ) as resp:
                return json.loads(resp.read())["ok"]
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


class Peer:
    """One daemon in its own home directory, driven through the control port."""

    def __init__(self, name, base, relay_url=None, shared_dir=None, direct=False,
                 git_remote=None):
        self.name = name
        self.home = os.path.join(base, name)
        os.makedirs(self.home, exist_ok=True)
        # Joining a room now requires a chosen display name; seed one the way
        # a human would have, before the daemon first reads its config.
        with open(os.path.join(self.home, "config.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"display_name": name}, fh)
        self.ctrl_port = free_port()
        self.relay_url = relay_url
        self.shared_dir = shared_dir
        self.git_remote = git_remote
        # Two daemons on one machine cannot share the default listen port, so
        # each gets its own. On two real machines this would be 45813 for both.
        self.ws_port = free_port() if direct else None
        self.client = ControlClient(port=self.ctrl_port, home=self.home)
        self.proc = None
        self.log = None
        self.start()

    def env(self):
        env = {
            **os.environ,
            "CLAUDE_LINK_HOME": self.home,
            "CLAUDE_LINK_CTRL_PORT": str(self.ctrl_port),
            "CLAUDE_LINK_AGENT_KIND": "claude-code" if self.name != "carol" else "codex",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        env.pop("CLAUDE_LINK_RELAY_URL", None)
        env.pop("CLAUDE_LINK_SHARED_DIR", None)
        env.pop("CLAUDE_LINK_DIRECT", None)
        env.pop("CLAUDE_LINK_GIT_REMOTE", None)
        if self.relay_url:
            env["CLAUDE_LINK_RELAY_URL"] = self.relay_url
        if self.shared_dir:
            env["CLAUDE_LINK_SHARED_DIR"] = self.shared_dir
        if self.git_remote:
            env["CLAUDE_LINK_GIT_REMOTE"] = self.git_remote
            env["CLAUDE_LINK_GIT_SYNC_MS"] = "300"
            # The heartbeat is a commit on this transport, so the default is 45
            # seconds. A test that waits for a roster cannot wait that long.
            env["CLAUDE_LINK_GIT_PRESENCE_S"] = "5"
        if self.ws_port:
            env["CLAUDE_LINK_DIRECT"] = "true"
            env["CLAUDE_LINK_WS_PORT"] = str(self.ws_port)
        return env

    def start(self, timeout=25.0):
        self.log = open(os.path.join(self.home, "stderr.log"), "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "link.daemon"],
            cwd=ROOT, env=self.env(), stdout=self.log, stderr=self.log,
            stdin=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + budget(timeout)
        while time.monotonic() < deadline:
            try:
                if self.client.call("ping", timeout=1.0).get("ok"):
                    return
            except LinkClientError:
                time.sleep(0.15)
        # Distinguish the two ways this fails. A daemon that died leaves an exit
        # code and usually a traceback; one that is merely wedged is still alive
        # with an empty log, which is the signature of a blocking call made
        # after the control port started listening.
        code = self.proc.poll()
        how = f"exited with code {code}" if code is not None else "is still running (wedged)"
        raise RuntimeError(
            f"{self.name} daemon did not come up: process {how}\n{self.stderr()}"
        )

    def kill(self):
        """Hard stop, no shutdown op: the crash case that loses unflushed state.

        `Popen.kill` is SIGKILL on POSIX and TerminateProcess on Windows, which
        is the same thing for our purposes: no handler runs, so nothing gets
        flushed on the way out. `signal.SIGKILL` itself does not exist on
        Windows and would fail at import-time on the attribute.
        """
        self.client.close()
        if self.proc:
            self.proc.kill()
            self.proc.wait(timeout=10)

    def stop(self):
        try:
            self.client.call("shutdown", timeout=2.0)
        except LinkClientError:
            pass
        if self.proc:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.log:
            self.log.close()

    def stderr(self):
        try:
            with open(os.path.join(self.home, "stderr.log"), "r",
                      encoding="utf-8", errors="replace") as fh:
                return fh.read()[-4000:]
        except OSError:
            return "(no log)"

    def call(self, op, **kw):
        return self.client.call(op, **kw)

    def drain(self):
        self.call("inbox", limit=200, include_system=True)

    def settle(self, quiet_s: float = 0.6, timeout: float = 10.0):
        """Drain until nothing new arrives for `quiet_s`.

        One drain is not enough for a member that is only a *recipient*. A
        broadcast an earlier test sent may still be crossing the folder when
        drain() runs, and it then lands in the middle of whatever this test is
        timing. That is not flakiness in the code under test; it is a test that
        started before the previous one had finished being delivered.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.drain()
            time.sleep(quiet_s)
            if not self.call("inbox", limit=200, include_system=True,
                             peek=True).get("messages"):
                return
        raise AssertionError(f"{self.name}'s inbox never went quiet")

    def texts(self, **kw):
        kw.setdefault("limit", 200)
        return [m["text"] for m in self.call("inbox", **kw).get("messages", [])]

    def conv_dir(self, room_id):
        return os.path.join(self.home, ".conv", room_id)


@unittest.skipUnless(RELAY_AVAILABLE, RELAY_SKIP_REASON)
class ThreeAgents(unittest.TestCase):
    """Alice and Bob on Claude Code, Carol on Codex, all in one room."""

    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="claude-link-e2e-")
        cls.relay = Relay(cls.base)
        cls.a = Peer("alice", cls.base, cls.relay.url)
        cls.b = Peer("bob", cls.base, cls.relay.url)
        cls.c = Peer("carol", cls.base, cls.relay.url)

        created = cls.a.call("join", room="e2e-room", timeout=40)
        assert created["ok"], created
        cls.invite = created["invite"]
        cls.room_id = created["room_id"]
        for peer in (cls.b, cls.c):
            joined = peer.call("join", invite=cls.invite, timeout=40)
            assert joined["ok"], joined

        wait_for(lambda: all(
            p.call("status")["rooms"][0]["online"] >= 2 for p in (cls.a, cls.b, cls.c)
        ), what="all three to see each other")

    @classmethod
    def tearDownClass(cls):
        for peer in (cls.a, cls.b, cls.c):
            peer.stop()
        cls.relay.stop()
        shutil.rmtree(cls.base, ignore_errors=True)

    # -- joining ------------------------------------------------------------- #

    def test_all_three_derive_the_same_room(self):
        ids = {p.call("status")["rooms"][0]["room_id"] for p in (self.a, self.b, self.c)}
        self.assertEqual(ids, {self.room_id})

    def test_a_wrong_invite_lands_you_in_an_empty_room(self):
        """The failure mode a typo actually produces: not an error, a room of one."""
        resp = self.b.call("join", invite="e2e-room#WRONGSECRETWRONGSECRETXX", timeout=40)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["members_online"], 0)
        self.assertNotEqual(resp["room_id"], self.room_id)
        self.b.call("leave", room=resp["room_id"])

    def test_members_learn_each_others_names_and_agent_kind(self):
        detail = wait_for(
            lambda: [m for m in self.a.call("status", verbose=True)["rooms"][0]
                     ["member_detail"] if m["agent_kind"] == "codex"],
            what="carol to be recognised as a codex agent")
        self.assertEqual(len(detail), 1)
        self.assertIsNotNone(detail[0]["fingerprint"])

    # -- messaging ------------------------------------------------------------ #

    def test_a_message_reaches_every_other_member(self):
        for peer in (self.b, self.c):
            peer.drain()
        sent = self.a.call("send", text="ciao a tutti")
        self.assertTrue(sent["ok"])
        self.assertEqual(sent["transport"], "relay")
        for peer in (self.b, self.c):
            got = wait_for(lambda p=peer: [m for m in p.call("wait", timeout_ms=8000,
                                                             timeout=12)["messages"]
                                           if m["kind"] == "msg"],
                           what=f"{peer.name} to receive")
            self.assertEqual(got[0]["text"], "ciao a tutti")
            self.assertEqual(got[0]["from_agent_kind"], "claude-code")

    def test_the_sender_does_not_receive_its_own_message(self):
        self.a.drain()
        self.a.call("send", text="not for me")
        time.sleep(1.0)
        self.assertNotIn("not for me", self.a.texts(include_system=True))

    def test_a_message_is_delivered_once_even_over_two_transports(self):
        self.b.drain()
        self.a.call("send", text="exactly once")
        wait_for(lambda: "exactly once" in self.b.texts(peek=True), what="delivery")
        self.b.drain()
        time.sleep(1.0)
        self.assertNotIn("exactly once", self.b.texts(include_system=True))

    def test_replies_are_threaded(self):
        self.b.drain()
        first = self.a.call("send", text="domanda")
        wait_for(lambda: self.b.call("inbox", peek=True)["count"] > 0, what="question")
        self.b.drain()
        self.a.drain()
        self.b.call("send", text="risposta", reply_to=first["msg_id"])
        got = wait_for(lambda: [m for m in self.a.call("inbox")["messages"]
                                if m["kind"] == "msg"], what="answer")
        self.assertEqual(got[0]["reply_to"], first["msg_id"])

    def test_a_long_message_is_truncated_and_link_read_returns_the_rest(self):
        self.b.drain()
        body = "x" * 5000
        sent = self.a.call("send", text=body)
        got = wait_for(lambda: [m for m in self.b.call("inbox")["messages"]
                                if m["kind"] == "msg"], what="the long message")
        self.assertTrue(got[0]["truncated"])
        self.assertLess(len(got[0]["text"]), 500)
        full = self.b.call("read", msg_id=sent["msg_id"])
        self.assertTrue(full["ok"])
        self.assertEqual(full["message"]["text"], body)

    def test_send_is_non_blocking(self):
        self.b.drain()
        start = time.monotonic()
        for i in range(20):
            self.a.call("send", text=f"burst {i}")
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, budget(3.0), f"20 sends took {elapsed:.2f}s")

        received = []
        deadline = time.monotonic() + 20
        while len(received) < 20 and time.monotonic() < deadline:
            resp = self.b.call("wait", timeout_ms=3000, timeout=8)
            received += [m["text"] for m in resp["messages"] if m["kind"] == "msg"]
        self.assertEqual(received, [f"burst {i}" for i in range(20)])

    def test_inbox_is_fast_and_empty_when_there_is_nothing(self):
        self.c.drain()
        start = time.monotonic()
        resp = self.c.call("inbox")
        self.assertLess(time.monotonic() - start, budget(0.5))
        self.assertEqual(resp["messages"], [])

    def test_wait_returns_on_timeout_without_hanging(self):
        self.c.drain()
        start = time.monotonic()
        resp = self.c.call("wait", timeout_ms=800, timeout=6)
        self.assertTrue(resp["timed_out"])
        self.assertGreaterEqual(time.monotonic() - start, 0.7)

    def test_system_notices_are_hidden_unless_asked_for(self):
        self.c.drain()
        self.a.call("send", text="a real message")
        wait_for(lambda: self.c.call("inbox", peek=True)["count"] > 0, what="message")
        quiet = self.c.call("inbox", peek=True, include_system=False)
        self.assertTrue(all(m["kind"] == "msg" for m in quiet["messages"]))

    def test_unread_remaining_counts_only_what_the_caller_can_see(self):
        """A count that included filtered records would send the caller round
        the loop for messages it can never be shown."""
        self.c.drain()
        self.a.call("send", text="visible one")
        wait_for(lambda: self.c.call("inbox", peek=True)["count"] > 0, what="message")
        resp = self.c.call("inbox", include_system=False, limit=1)
        self.assertEqual(resp["unread_remaining"], 0)

    # -- side channels ---------------------------------------------------------- #

    def test_a_subagent_can_open_a_side_channel_and_talk_on_it(self):
        self.b.drain()
        opened = self.a.call("channel_open", topic="auth review",
                             role="subagent", agent="explore-auth")
        self.assertTrue(opened["ok"])
        channel = opened["channel_id"]

        wait_for(lambda: any(c["channel_id"] == channel
                             for c in self.b.call("channel_list")["channels"]),
                 what="the peer to register the channel")

        self.b.drain()
        self.a.call("send", room=channel, text="subagent talk",
                    role="subagent", agent="explore-auth")
        msgs = wait_for(lambda: [m for m in self.b.call("inbox", room=channel)["messages"]
                                 if m["kind"] == "msg"], what="channel message")
        self.assertEqual(msgs[0]["channel"], channel)
        self.assertEqual(msgs[0]["from_role"], "subagent")
        self.assertEqual(msgs[0]["from_agent"], "explore-auth")

        self.assertTrue(self.a.call("channel_close", channel_id=channel)["ok"])
        wait_for(lambda: not any(c["channel_id"] == channel
                                 for c in self.b.call("channel_list")["channels"]),
                 what="the peer to close the channel")

    # -- transcripts -------------------------------------------------------------- #

    def test_every_machine_keeps_its_own_transcript(self):
        self.b.drain()
        self.a.call("send", text="tracked everywhere", agent="main")
        wait_for(lambda: "tracked everywhere" in self.b.texts(peek=True),
                 what="the tracked message")
        time.sleep(0.5)

        for peer, direction in ((self.a, "out"), (self.b, "in")):
            conv = peer.conv_dir(self.room_id)
            self.assertTrue(os.path.isdir(conv), f"missing {conv}")
            records = []
            for name in os.listdir(conv):
                if name.endswith(".jsonl"):
                    with open(os.path.join(conv, name), encoding="utf-8") as fh:
                        records += [json.loads(line) for line in fh if line.strip()]
            match = [r for r in records
                     if r["dir"] == direction
                     and (r.get("body") or {}).get("text") == "tracked everywhere"]
            self.assertTrue(match, f"{peer.name} has no {direction} record")
            self.assertEqual(match[0]["transport"], "relay")
            self.assertTrue(os.path.exists(os.path.join(conv, "transcript.md")))
            self.assertTrue(os.path.exists(os.path.join(conv, "meta.json")))

    def test_an_inbound_record_is_marked_verified(self):
        conv = self.b.conv_dir(self.room_id)
        records = []
        for name in os.listdir(conv):
            if name.endswith(".jsonl"):
                with open(os.path.join(conv, name), encoding="utf-8") as fh:
                    records += [json.loads(line) for line in fh if line.strip()]
        inbound = [r for r in records if r["dir"] == "in" and r["kind"] == "msg"]
        self.assertTrue(inbound)
        self.assertTrue(all(r.get("verified") for r in inbound))

    def test_history_replays_from_disk(self):
        resp = self.a.call("history", limit=500)
        self.assertTrue(resp["ok"])
        texts = [(r.get("body") or {}).get("text") for r in resp["records"]]
        self.assertIn("tracked everywhere", texts)


@unittest.skipUnless(RELAY_AVAILABLE, RELAY_SKIP_REASON)
class TestRestartAndQueueing(unittest.TestCase):
    """What happens when a daemon dies, and when nobody is listening."""

    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="claude-link-restart-")
        cls.relay = Relay(cls.base)
        cls.a = Peer("ann", cls.base, cls.relay.url)
        cls.b = Peer("ben", cls.base, cls.relay.url)
        created = cls.a.call("join", room="restart-room", timeout=40)
        cls.invite = created["invite"]
        cls.b.call("join", invite=cls.invite, timeout=40)
        wait_for(lambda: cls.a.call("status")["rooms"][0]["online"] >= 1,
                 what="the pair to connect")

    @classmethod
    def tearDownClass(cls):
        for peer in (cls.a, cls.b):
            peer.stop()
        cls.relay.stop()
        shutil.rmtree(cls.base, ignore_errors=True)

    def test_a_killed_daemon_resumes_delivery_after_a_restart(self):
        """A crash must not mute a device. The old sequence high-water rule
        would have done exactly that once a reserved block was lost."""
        self.b.drain()
        self.a.call("send", text="before the crash")
        wait_for(lambda: "before the crash" in self.b.texts(peek=True), what="first")

        self.a.kill()
        self.a.start()
        wait_for(lambda: self.a.call("status")["rooms"][0]["transport"] == "relay",
                 what="the restarted daemon to reconnect")

        self.b.drain()
        self.a.call("send", text="after the crash")
        wait_for(lambda: "after the crash" in self.b.texts(peek=True),
                 what="delivery to resume after a restart")

    def test_a_message_sent_while_the_peer_is_down_arrives_later(self):
        self.b.drain()
        self.b.kill()
        time.sleep(0.5)
        sent = self.a.call("send", text="while you were out")
        self.assertTrue(sent["ok"])
        self.assertEqual(sent["transport"], "relay")

        self.b.start()
        wait_for(lambda: "while you were out" in self.b.texts(peek=True),
                 timeout=30, what="store-and-forward delivery")

    def test_a_send_with_no_relay_queues_and_flushes_on_reconnect(self):
        self.b.drain()
        self.relay.stop()
        wait_for(lambda: self.a.call("status")["rooms"][0]["transport"] == "offline",
                 timeout=30, what="the relay to be seen as down")

        queued = self.a.call("send", text="queued while offline")
        self.assertEqual(queued["transport"], "queued")
        self.assertTrue(queued["ok"], "a send must never fail just because nobody is up")

        type(self).relay = Relay(self.base)
        # The daemons were started with the old URL, so point them at the new one
        # by restarting them; a real relay keeps its address across a redeploy.
        for peer in (self.a, self.b):
            peer.relay_url = self.relay.url
            peer.kill()
            peer.start()
        wait_for(lambda: self.a.call("status")["rooms"][0]["transport"] == "relay",
                 timeout=30, what="reconnection to the new relay")


class TestColdStartStatus(unittest.TestCase):
    """A short room list at cold start must never read as a complete one."""

    def test_a_partial_room_list_at_cold_start_says_it_is_still_loading(self):
        base = tempfile.mkdtemp(prefix="claude-link-cold-status-")
        home = os.path.join(base, "cold")
        os.makedirs(home)
        records = {
            name: {
                "name": name,
                "secret": f"secret-{name}",
                "joined_at": "2026-01-01T00:00:00Z",
                "seq_reserved_to": 0,
                "seen": [],
            }
            for name in ("one", "two", "three")
        }
        with open(os.path.join(home, "state.json"), "w", encoding="utf-8") as fh:
            json.dump({"rooms": records, "channels": {}}, fh)

        def rooms_in(reply):
            return {room["room"] for room in reply["rooms"]}

        peer = None
        try:
            peer = Peer("cold", base)
            first = peer.call("status")

            # The gate in `_op_status` is bounded on purpose
            # (STATUS_READY_TIMEOUT_S, below the control client's deadline):
            # status is the diagnostic call, and a wedged transport must not be
            # able to make it hang. Room key derivation is deliberately slow, so
            # a loaded machine is allowed to answer before every room is up.
            #
            # What it may never do is answer short and quiet. `loading` is the
            # one thing between a half-loaded daemon and a caller concluding it
            # is not in a room it is in, and `mcp_server` labels the reply from
            # it. Asserting instead that the gate always wins is what made this
            # flaky: it lost on `windows-latest / py3.12` in run 31267434784,
            # one room short, with the daemon behaving exactly as designed.
            if rooms_in(first) != {"one", "two", "three"}:
                self.assertTrue(
                    first.get("loading"),
                    f"status returned {sorted(rooms_in(first))} and did not "
                    f"say it was still loading",
                )

            wait_for(
                lambda: rooms_in(peer.call("status")) == {"one", "two", "three"},
                timeout=30, what="all three persisted rooms to load",
            )
            self.assertFalse(peer.call("status")["loading"],
                             "loading must clear once every room is up")
        finally:
            if peer is not None:
                peer.stop()
                peer.client.close()
            shutil.rmtree(base, ignore_errors=True)


class TestSharedFolderOnly(unittest.TestCase):
    """The default setup: a folder both machines sync, and nothing else.

    No relay is started anywhere in this class. This is the path someone gets
    when they install the skill and point it at their OneDrive, which is the
    only setup step the skill asks for.
    """

    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="claude-link-folder-")
        cls.share = os.path.join(cls.base, "synced-folder")
        os.makedirs(cls.share, exist_ok=True)
        cls.a = Peer("dana", cls.base, shared_dir=cls.share)
        cls.b = Peer("eli", cls.base, shared_dir=cls.share)
        cls.c = Peer("fay", cls.base, shared_dir=cls.share)

        created = cls.a.call("join", room="folder-room", timeout=40)
        assert created["ok"], created
        cls.room_id = created["room_id"]
        for peer in (cls.b, cls.c):
            assert peer.call("join", invite=created["invite"], timeout=40)["ok"]

        # Discovery here is heartbeat files rather than a relay roster, so it
        # takes a poll or two. Sending before everyone is visible would test the
        # race, not the transport -- the relay class waits for the same reason.
        wait_for(lambda: all(
            p.call("status")["rooms"][0]["online"] >= 2 for p in (cls.a, cls.b, cls.c)
        ), timeout=40, what="all three to find each other on the share")

    @classmethod
    def tearDownClass(cls):
        for peer in (cls.a, cls.b, cls.c):
            peer.stop()
        shutil.rmtree(cls.base, ignore_errors=True)

    def test_no_relay_is_involved(self):
        room = self.a.call("status")["rooms"][0]
        self.assertEqual(room["transport"], "file")
        detail = self.a.call("status", verbose=True)["rooms"][0]
        self.assertIsNone(detail["relay"])

    def test_a_message_reaches_both_other_members(self):
        for peer in (self.b, self.c):
            peer.drain()
        sent = self.a.call("send", text="over the folder")
        self.assertEqual(sent["transport"], "file")
        for peer in (self.b, self.c):
            wait_for(lambda p=peer: "over the folder" in p.texts(peek=True),
                     timeout=30, what=f"{peer.name} to receive over the folder")

    def test_waking_on_a_message_does_not_consume_it(self):
        """What `link wake` runs, and the one way it could do harm.

        An agent leaves that command running so the harness re-invokes it when
        a colleague writes. The notification hook then fetches the same inbox a
        moment later, and fetching marks messages read -- so a wait that
        consumed would wake the agent to an inbox that no longer holds the
        message that woke it. Nothing would hand it back: not the hook, not
        link_inbox. Waking someone by eating the letter is worse than not
        waking them.
        """
        self.b.drain()
        self.a.call("send", text="wake up, do not eat this")

        woken = self.b.call("wait", timeout_ms=30000, peek=True, timeout=40)
        self.assertIn("wake up, do not eat this",
                      [m["text"] for m in woken["messages"]])

        self.assertIn("wake up, do not eat this", self.b.texts(peek=True),
                      "the wait consumed the message it was only meant to notice")

    def test_an_expired_wait_reports_itself_rather_than_looking_empty(self):
        """Exit 1 has to mean "nothing came", not "something went wrong".

        `include_system=False` because that is what `cli.cmd_wake` passes, and
        the point of this test is the contract `wake` rests on. Without it the
        call is a different one: presence notices from the other two peers in
        this class keep arriving, so the wait returns messages, `timed_out` is
        False, and the test fails for a reason that has nothing to do with
        expiry -- which it did, intermittently and depending on test order.
        """
        self.c.settle()
        expired = self.c.call("wait", timeout_ms=1200, peek=True,
                              include_system=False, timeout=30)
        self.assertTrue(expired["ok"])
        self.assertTrue(expired["timed_out"])

    def test_members_find_each_other_through_the_folder(self):
        wait_for(lambda: self.a.call("status")["rooms"][0]["online"] >= 2,
                 timeout=30, what="the roster to fill from heartbeats")

    def test_a_reply_comes_back(self):
        self.a.drain()
        self.b.drain()
        first = self.a.call("send", text="folder question")
        wait_for(lambda: "folder question" in self.b.texts(peek=True),
                 timeout=30, what="the question")
        self.b.drain()
        self.b.call("send", text="folder answer", reply_to=first["msg_id"])
        got = wait_for(lambda: [m for m in self.a.call("inbox")["messages"]
                                if m["kind"] == "msg"],
                       timeout=30, what="the answer")
        self.assertEqual(got[0]["reply_to"], first["msg_id"])

    def test_transcripts_are_written_without_a_relay(self):
        wait_for(lambda: os.path.isdir(self.b.conv_dir(self.room_id)),
                 timeout=30, what="the transcript directory")
        self.assertTrue(os.path.exists(
            os.path.join(self.b.conv_dir(self.room_id), "transcript.md")))


def _skip_git_reason():
    if shutil.which("git") is None:
        return "git is not installed"
    if os.environ.get("CLAUDE_LINK_SKIP_GIT_TESTS") == "1":
        return "CLAUDE_LINK_SKIP_GIT_TESTS=1"
    return ""


@unittest.skipIf(_skip_git_reason(), _skip_git_reason())
class TestGitRepoOnly(unittest.TestCase):
    """The other setup with nothing to deploy: a repo both machines push to.

    `tests/test_transport_git.py` drives the transport directly. This drives the
    daemon, which is where the wiring lives -- whether the room brings the
    transport up, whether a send picks it, whether the roster fills from
    heartbeats that arrive as commits, and whether `status` says any of it out
    loud. Every one of those is a separate place the transport could work while
    the product does not.
    """

    @classmethod
    def setUpClass(cls):
        from link.transport_git import run_git

        cls.base = tempfile.mkdtemp(prefix="claude-link-gitrepo-")
        origin = os.path.join(cls.base, "channel.git")
        run_git(["init", "--bare", "--quiet", origin])
        remote = origin.replace("\\", "/")

        cls.a = Peer("gina", cls.base, git_remote=remote)
        cls.b = Peer("hugo", cls.base, git_remote=remote)

        created = cls.a.call("join", room="git-room", timeout=60)
        assert created["ok"], created
        cls.room_id = created["room_id"]
        assert cls.b.call("join", invite=created["invite"], timeout=60)["ok"]

        wait_for(lambda: all(p.call("status")["rooms"][0]["online"] >= 1
                             for p in (cls.a, cls.b)),
                 timeout=60, what="both to find each other through the repo")

    @classmethod
    def tearDownClass(cls):
        for peer in (cls.a, cls.b):
            peer.stop()
        shutil.rmtree(cls.base, ignore_errors=True)

    def test_the_room_reports_the_repo_as_its_transport(self):
        # `transport` and `sync_error` are waited for rather than sampled. A git
        # channel drops out of `live_transports` the moment a round fails and
        # comes back when the next one succeeds -- that is the design, and on a
        # loaded machine a round really does lose the occasional push race. What
        # the test is entitled to assert is that the channel reports itself
        # healthy, not that it does so at one arbitrary microsecond.
        wait_for(lambda: self.a.call("status")["rooms"][0]["transport"] == "git",
                 timeout=60, what="the room to name the repo as its transport")
        room = self.a.call("status")["rooms"][0]
        self.assertIsNone(room["setup_error"])

        detail = self.a.call("status", verbose=True)["rooms"][0]
        self.assertIsNone(detail["relay"])
        self.assertIsNone(detail["file"], "no folder is configured in this class")
        self.assertIsNotNone(detail["git"], "the git transport reported no stats")
        # Monotonic, so safe to sample: it has pushed at least the branch itself.
        self.assertGreater(detail["git"]["pushes"], 0)

    def test_a_message_reaches_the_other_member(self):
        self.b.drain()
        sent = self.a.call("send", text="over the repo")
        self.assertEqual(sent["transport"], "git")
        wait_for(lambda: "over the repo" in self.b.texts(peek=True),
                 timeout=60, what="hugo to receive over the repo")

    def test_a_reply_comes_back(self):
        self.a.drain()
        self.b.drain()
        first = self.a.call("send", text="repo question")
        wait_for(lambda: "repo question" in self.b.texts(peek=True),
                 timeout=60, what="the question")
        self.b.drain()
        self.b.call("send", text="repo answer", reply_to=first["msg_id"])
        got = wait_for(lambda: [m for m in self.a.call("inbox")["messages"]
                                if m["kind"] == "msg"],
                       timeout=60, what="the answer")
        self.assertEqual(got[0]["reply_to"], first["msg_id"])

    def test_the_transcript_names_git_as_the_carrier(self):
        wait_for(lambda: os.path.isdir(self.b.conv_dir(self.room_id)),
                 timeout=60, what="the transcript directory")
        path = os.path.join(self.b.conv_dir(self.room_id), "transcript.md")
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as fh:
            self.assertIn("via git", fh.read())

    def test_no_setup_hint_is_offered_when_a_repo_is_configured(self):
        """The hint exists to say "nothing can carry a message". Something can."""
        self.assertIsNone(self.a.call("status")["setup_needed"])

    def test_the_configured_remote_is_reported_without_its_credentials(self):
        status = self.a.call("status")
        self.assertTrue(status["git_remote"])
        self.assertNotIn("@", status["git_remote"].split("://")[-1])


@unittest.skipIf(_skip_git_reason(), _skip_git_reason())
class TestABrokenRepoDoesNotTakeTheRoomDown(unittest.TestCase):
    """A repo that cannot be reached must cost you the repo, not the room.

    The tempting implementation raises out of `_start_room`, and then a typo in
    one config key leaves someone with no room at all while their perfectly good
    shared folder sits there working. The failure has to be reported and
    survived, not propagated.
    """

    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="claude-link-badrepo-")
        cls.share = os.path.join(cls.base, "synced-folder")
        os.makedirs(cls.share, exist_ok=True)
        cls.a = Peer("iris", cls.base, shared_dir=cls.share,
                     git_remote=os.path.join(cls.base, "nowhere.git").replace("\\", "/"))
        cls.joined = cls.a.call("join", room="half-broken", timeout=60)
        assert cls.joined["ok"], cls.joined

    @classmethod
    def tearDownClass(cls):
        cls.a.stop()
        shutil.rmtree(cls.base, ignore_errors=True)

    def test_the_room_still_comes_up_on_the_folder(self):
        room = self.a.call("status")["rooms"][0]
        self.assertEqual(room["transport"], "file")

    def test_the_broken_repo_is_named_rather_than_swallowed(self):
        room = self.a.call("status")["rooms"][0]
        self.assertIsNotNone(room["setup_error"],
                             "a repo that cannot be reached was reported as fine")
        self.assertIn("git channel unusable", room["setup_error"])

    def test_no_git_transport_is_attached(self):
        detail = self.a.call("status", verbose=True)["rooms"][0]
        self.assertIsNone(detail["git"], "a transport that failed to start was kept")
        self.assertIsNotNone(detail["file"])


class TestDirectEnabled(unittest.TestCase):
    """The opt-in LAN transport, wired through real daemons.

    A folder carries the introductions; the addresses inside those sealed
    hellos are what let the two find each other and upgrade to a direct link.
    """

    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="claude-link-direct-")
        cls.share = os.path.join(cls.base, "share")
        os.makedirs(cls.share, exist_ok=True)
        cls.a = Peer("gus", cls.base, shared_dir=cls.share, direct=True)
        cls.b = Peer("hana", cls.base, shared_dir=cls.share, direct=True)

        created = cls.a.call("join", room="direct-room", timeout=40)
        assert created["ok"], created
        assert cls.b.call("join", invite=created["invite"], timeout=40)["ok"]
        wait_for(lambda: all(p.call("status")["rooms"][0]["online"] >= 1
                             for p in (cls.a, cls.b)),
                 timeout=40, what="the pair to find each other")

    @classmethod
    def tearDownClass(cls):
        for peer in (cls.a, cls.b):
            peer.stop()
        shutil.rmtree(cls.base, ignore_errors=True)

    def test_the_endpoint_is_advertised_and_dialled(self):
        stats = wait_for(
            lambda: (self.a.call("status", verbose=True)["rooms"][0]["direct"] or {})
                    .get("connected"),
            timeout=40, what="a direct link to come up")
        self.assertEqual(len(stats), 1)

    def test_messages_take_the_direct_path_once_it_covers_the_room(self):
        wait_for(lambda: (self.a.call("status", verbose=True)["rooms"][0]["direct"]
                          or {}).get("connected"),
                 timeout=40, what="the direct link")
        self.b.drain()
        sent = wait_for(lambda: (lambda r: r if r["transport"] == "direct" else None)(
            self.a.call("send", text="over the LAN")),
            timeout=20, what="a send to choose the direct path")
        self.assertEqual(sent["transport"], "direct")
        wait_for(lambda: "over the LAN" in self.b.texts(peek=True),
                 timeout=20, what="delivery over the direct link")

    def test_the_address_never_leaves_the_ciphertext(self):
        """The folder carries the hello; it must not reveal where anyone lives."""
        # Force fresh hellos onto the share and catch them before the peer's
        # poll consumes them, so there is definitely something to inspect.
        self.a.call("send", text="make the share busy")
        self.b.call("send", text="and from this side too")

        inspected, leaked = 0, []
        deadline = time.monotonic() + 10
        while inspected == 0 and time.monotonic() < deadline:
            for root, _dirs, files in os.walk(os.path.join(self.share, "claude-link")):
                for name in files:
                    path = os.path.join(root, name)
                    try:
                        with open(path, "rb") as fh:
                            blob = fh.read()
                    except OSError:
                        continue
                    inspected += 1
                    if b'"host"' in blob or str(self.a.ws_port).encode() in blob:
                        leaked.append(path)

        self.assertGreater(inspected, 0, "nothing on the share to check")
        self.assertEqual(leaked, [], "an address appeared in the clear on the share")


class TestNothingConfigured(unittest.TestCase):
    """With no repo and no relay there is no channel, and the skill says so."""

    def test_status_explains_what_is_missing(self):
        base = tempfile.mkdtemp(prefix="claude-link-bare-")
        try:
            peer = Peer("solo", base)
            status = peer.call("status")
            hint = status.get("setup_needed")
            self.assertIsNotNone(hint, "an unusable install must say why")
            # One carrier is offered, and it is not a synced folder.
            self.assertIn("git", hint["problem"])
            self.assertIn("git_remote", hint["command"])
            self.assertNotIn("shared_dir", json.dumps(hint))

            from link.mcp_server import render

            text = render("link_status", status)
            self.assertIn("Not connected to anything yet", text)
            peer.stop()
        finally:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
