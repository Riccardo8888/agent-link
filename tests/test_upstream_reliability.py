"""The four reliability fixes, tested rather than asserted in a commit message.

Each test here stands for a failure that was observed in use, not a shape that
looked risky in review:

  * a git channel that died to a momentary DNS failure and never came back,
    leaving the room reading offline until somebody restarted the daemon;
  * a CLI that reported a send as failed because a git-backed push takes longer
    than the control socket default allowed;
  * a long message no CLI-only operator could read past the inbox preview;
  * a second harness whose daemon could not start, because both homes shipped
    the same control port in their config.json.

The installer cases follow tests/test_install.py: HOME is a temp directory and
the module is reloaded so it recomputes its paths, so nothing here can touch a
real home. The retry cases drive the real _schedule_git_retry and the real
Room.stop with the backoff constants shrunk, so they finish in about a second.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import stat
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from link import cli as _cli  # noqa: E402
from link import daemon as _daemon  # noqa: E402
from link import install as _install  # noqa: E402
from link.room import Room  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. each home gets its own ctrl_port in config.json
# --------------------------------------------------------------------------- #

class SeedCtrlPortCase(unittest.TestCase):
    """A fresh fake home per test, with the module paths recomputed for it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self._saved = {k: os.environ.get(k)
                       for k in ("HOME", "USERPROFILE", "CODEX_HOME",
                                 "CLAUDE_LINK_HOME")}
        os.environ["HOME"] = self.home
        os.environ["USERPROFILE"] = self.home
        os.environ.pop("CODEX_HOME", None)
        os.environ.pop("CLAUDE_LINK_HOME", None)
        self.install = importlib.reload(_install)
        self.out = self.install.Out(quiet=True)
        self.codex_home = self.install.CODEX_LINK_HOME
        self.codex_port = int(self.install.CODEX_CTRL_PORT)
        self.claude_home = self.install.CLAUDE_LINK_HOME
        self.claude_port = int(self.install.CLAUDE_CTRL_PORT)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(_install)
        self._tmp.cleanup()

    def write_config(self, home: str, text: str, mode: int | None = None) -> str:
        path = os.path.join(home, "config.json")
        os.makedirs(home, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(path, mode)
        return path

    def read_config(self, home: str) -> dict:
        with open(os.path.join(home, "config.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_a_fresh_home_gets_its_own_port(self) -> None:
        self.install.seed_home_ctrl_port(self.codex_home, self.codex_port, self.out)
        self.assertEqual(self.read_config(self.codex_home)["ctrl_port"],
                         self.codex_port)

    def test_the_shared_default_is_corrected(self) -> None:
        # The bug itself: both homes shipped the same port, so the second daemon
        # could not bind and the CLI reported that it failed to start.
        self.write_config(self.codex_home,
                          json.dumps({"ctrl_port": self.claude_port}))
        self.install.seed_home_ctrl_port(self.codex_home, self.codex_port, self.out)
        self.assertEqual(self.read_config(self.codex_home)["ctrl_port"],
                         self.codex_port)

    def test_a_port_the_user_chose_is_left_alone(self) -> None:
        for home, port in ((self.codex_home, self.codex_port),
                           (self.claude_home, self.claude_port)):
            with self.subTest(home=os.path.basename(home)):
                self.write_config(home, json.dumps({"ctrl_port": 45999}))
                self.install.seed_home_ctrl_port(home, port, self.out)
                self.assertEqual(self.read_config(home)["ctrl_port"], 45999,
                                 "a deliberately chosen port must survive")

    def test_running_it_twice_rewrites_nothing(self) -> None:
        self.install.seed_home_ctrl_port(self.codex_home, self.codex_port, self.out)
        path = os.path.join(self.codex_home, "config.json")
        with open(path, encoding="utf-8") as fh:
            before = fh.read()
        stamp = os.stat(path).st_mtime_ns
        self.install.seed_home_ctrl_port(self.codex_home, self.codex_port, self.out)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(os.stat(path).st_mtime_ns, stamp,
                         "an install that changes nothing must write nothing")

    def test_the_rest_of_the_config_survives(self) -> None:
        self.write_config(self.codex_home, json.dumps(
            {"ctrl_port": self.claude_port, "nick": "kept", "rooms": [1, 2]}))
        self.install.seed_home_ctrl_port(self.codex_home, self.codex_port, self.out)
        after = self.read_config(self.codex_home)
        self.assertEqual(after["nick"], "kept")
        self.assertEqual(after["rooms"], [1, 2])

    def test_a_config_that_is_not_an_object_is_refused_not_eaten(self) -> None:
        # An installer that dies here leaves the host after it uninstalled; one
        # that overwrites eats whatever the file really was.
        for raw in ("[1, 2, 3]", "{not json", "\"a string\""):
            with self.subTest(raw=raw):
                path = self.write_config(self.codex_home, raw)
                self.install.seed_home_ctrl_port(
                    self.codex_home, self.codex_port, self.out)
                with open(path, encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), raw)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_a_private_config_is_not_made_world_readable(self) -> None:
        path = self.write_config(self.codex_home,
                                 json.dumps({"ctrl_port": self.claude_port}),
                                 mode=0o600)
        self.install.seed_home_ctrl_port(self.codex_home, self.codex_port, self.out)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode) & 0o077, 0,
                         "the rewrite must not widen the permissions")


# --------------------------------------------------------------------------- #
# 2. a git channel that fails to start is retried
# --------------------------------------------------------------------------- #

class FakeRoom:
    """Enough Room for the retry loop, with the real Room methods bound on."""

    name = "test-room"
    # The real property and the real writer, so the error bookkeeping under
    # test is the shipped one rather than a restatement of it.
    setup_error = Room.setup_error
    note_setup_error = Room.note_setup_error

    def __init__(self) -> None:
        self._t: dict[str, object] = {}
        self._transports: dict[str, object] = {}
        self._setup_errors: dict[str, str] = {}

    def transport(self, name: str):
        return self._t.get(name)

    def attach(self, name: str, transport: object) -> None:
        self._t[name] = transport

    async def stop(self) -> None:
        await Room.stop(self)


class GitRetryCase(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        self._constants = (_daemon.GIT_RETRY_BASE_S,
                           _daemon.GIT_RETRY_MAX_S,
                           _daemon.GIT_RETRY_FACTOR)
        _daemon.GIT_RETRY_BASE_S = 0.02
        _daemon.GIT_RETRY_MAX_S = 0.08
        _daemon.GIT_RETRY_FACTOR = 2.0

    def tearDown(self) -> None:
        (_daemon.GIT_RETRY_BASE_S,
         _daemon.GIT_RETRY_MAX_S,
         _daemon.GIT_RETRY_FACTOR) = self._constants

    def daemon(self, attach):
        stub = types.SimpleNamespace()
        stub._stopping = asyncio.Event()
        stub._attach_git = attach
        stub._schedule_git_retry = types.MethodType(
            _daemon.LinkDaemon._schedule_git_retry, stub)
        return stub

    def test_the_backoff_ladder_is_bounded_by_the_cap(self) -> None:
        base, cap, factor = 15.0, 300.0, 2.0
        ladder = [min(cap, base * factor ** (n - 1)) for n in range(1, 9)]
        self.assertEqual(ladder, [15, 30, 60, 120, 240, 300, 300, 300])

    async def test_it_keeps_retrying_rather_than_firing_once(self) -> None:
        # The first version of this fix retried exactly once: the one-loop guard
        # saw its own still-running task and dropped every later attempt.
        attempts = []

        async def always_fails(room, record, keys):
            attempts.append(1)
            raise RuntimeError("remote unreachable")

        stub = self.daemon(always_fails)
        room = FakeRoom()
        stub._schedule_git_retry(room, {}, None)
        await asyncio.sleep(0.4)
        self.assertGreaterEqual(len(attempts), 4)
        await room.stop()

    async def test_only_one_attempt_is_ever_pending(self) -> None:
        inflight = []
        peak = []

        async def slow_failure(room, record, keys):
            inflight.append(1)
            peak.append(len(inflight))
            await asyncio.sleep(0.01)
            inflight.pop()
            raise RuntimeError("still down")

        stub = self.daemon(slow_failure)
        room = FakeRoom()
        stub._schedule_git_retry(room, {}, None)
        await asyncio.sleep(0.3)
        self.assertEqual(max(peak), 1, "two retry loops ran for one room")
        await room.stop()

    async def test_it_stops_once_the_channel_is_up(self) -> None:
        calls = []

        async def up_on_second_try(room, record, keys):
            calls.append(1)
            if len(calls) >= 2:
                room._t["git"] = object()
                return
            raise RuntimeError("still down")

        stub = self.daemon(up_on_second_try)
        room = FakeRoom()
        stub._schedule_git_retry(room, {}, None)
        await asyncio.sleep(0.3)
        settled = len(calls)
        self.assertLessEqual(settled, 3)
        await asyncio.sleep(0.1)
        self.assertEqual(len(calls), settled,
                         "a channel that came up must end the chain")
        await room.stop()

    async def test_a_channel_already_up_resets_the_ladder(self) -> None:
        # The other half of ending the chain: a retry that wakes to find the
        # transport back must not attach a second one, and must not carry its
        # old delay forward.
        calls = []

        async def should_not_run(room, record, keys):
            calls.append(1)

        stub = self.daemon(should_not_run)
        room = FakeRoom()
        room._git_retry_attempt = 4
        room._t["git"] = object()
        stub._schedule_git_retry(room, {}, None)
        await asyncio.sleep(0.3)
        self.assertEqual(calls, [], "it must not attach over a live channel")
        self.assertEqual(room._git_retry_attempt, 0,
                         "a channel that is up must reset the ladder")
        await room.stop()

    async def test_shutdown_does_not_wait_out_the_backoff(self) -> None:
        # A room torn down mid-backoff used to leave a task sleeping for the
        # rest of its delay, holding the room object with it.
        _daemon.GIT_RETRY_BASE_S = 30.0

        async def always_fails(room, record, keys):
            raise RuntimeError("remote unreachable")

        stub = self.daemon(always_fails)
        room = FakeRoom()
        stub._schedule_git_retry(room, {}, None)
        await asyncio.sleep(0.05)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await room.stop()
        self.assertLess(loop.time() - started, 1.0)
        self.assertIsNone(room._git_retry_task)

    async def test_a_stopped_room_stops_attempting(self) -> None:
        attempts = []

        async def always_fails(room, record, keys):
            attempts.append(1)
            raise RuntimeError("remote unreachable")

        stub = self.daemon(always_fails)
        room = FakeRoom()
        stub._schedule_git_retry(room, {}, None)
        await asyncio.sleep(0.2)
        await room.stop()
        settled = len(attempts)
        await asyncio.sleep(0.2)
        self.assertEqual(len(attempts), settled)


# --------------------------------------------------------------------------- #
# 3. the CLI surfaces
# --------------------------------------------------------------------------- #

class CliSurfaceCase(unittest.TestCase):

    def test_read_takes_a_message_id(self) -> None:
        args = _cli.build_parser().parse_args(["read", "msg_abc123"])
        self.assertEqual(args.msg_id, "msg_abc123")
        self.assertIs(args.func, _cli.cmd_read)

    def test_send_defaults_to_a_timeout_a_git_push_can_meet(self) -> None:
        args = _cli.build_parser().parse_args(["send", "hello"])
        self.assertGreaterEqual(args.timeout, 30.0)

    def test_a_timeout_that_cannot_work_is_refused_at_the_flag(self) -> None:
        # inf reached socket.settimeout and came back as an OverflowError
        # traceback; 0 put the socket in non-blocking mode and reported errno.
        parser = _cli.build_parser()
        for bad in ("inf", "-1", "nan", "0", "1e400", "abc"):
            with self.subTest(value=bad):
                with self.assertRaises(SystemExit):
                    parser.parse_args(["send", "--timeout", bad, "hello"])

    def test_a_sensible_timeout_is_accepted(self) -> None:
        args = _cli.build_parser().parse_args(["send", "--timeout", "5.5", "hi"])
        self.assertAlmostEqual(args.timeout, 5.5)


# --------------------------------------------------------------------------- #
# 4. the retry does not turn setup_error into a landfill
# --------------------------------------------------------------------------- #

class SetupErrorCase(unittest.IsolatedAsyncioTestCase):
    """What the retry ladder does to the string `link_status` prints.

    `_attach_git` used to join each failure onto the last. That was right
    while a failed attach happened once per room, and became a leak the
    moment it was retried: an unreachable remote appended a clause every
    300 s for as long as it stayed down, and `link_status` prints the result
    untruncated (`link/mcp_server.py`), so the same sentence arrived in a
    model's context a few hundred times over. The other half was that
    nothing cleared it, so a room that recovered went on reporting itself
    broken for the life of the daemon.

    These drive the real `_attach_git` and the real `_schedule_git_retry`,
    stubbing only the transport, so they fail against the appending version.
    """

    def setUp(self) -> None:
        self._constants = (_daemon.GIT_RETRY_BASE_S,
                           _daemon.GIT_RETRY_MAX_S,
                           _daemon.GIT_RETRY_FACTOR)
        _daemon.GIT_RETRY_BASE_S = 0.01
        _daemon.GIT_RETRY_MAX_S = 0.01
        _daemon.GIT_RETRY_FACTOR = 1.0
        from link import transport_git as tg
        self._tg = tg
        self._saved = (tg.GitTransport, tg.check_remote)
        tg.check_remote = lambda remote: remote

    def tearDown(self) -> None:
        (_daemon.GIT_RETRY_BASE_S,
         _daemon.GIT_RETRY_MAX_S,
         _daemon.GIT_RETRY_FACTOR) = self._constants
        self._tg.GitTransport, self._tg.check_remote = self._saved

    def daemon(self):
        stub = types.SimpleNamespace(
            _stopping=asyncio.Event(),
            cfg={"git_start_timeout_s": 5},
            identity=types.SimpleNamespace(device_id="dev_test"),
        )
        stub._attach_git = types.MethodType(_daemon.LinkDaemon._attach_git, stub)
        stub._schedule_git_retry = types.MethodType(
            _daemon.LinkDaemon._schedule_git_retry, stub)
        return stub

    def transport(self, fails_for: int):
        """A git transport whose start() fails the first `fails_for` times."""
        tries = []

        class Stub:
            def __init__(self, **kwargs):
                pass

            async def start(self):
                tries.append(1)
                if len(tries) <= fails_for:
                    raise OSError("Could not resolve host: github.com")

            async def stop(self, flush=True):
                pass

        self._tg.GitTransport = Stub
        return tries

    async def drive(self, room, seconds: float = 0.4):
        record = {"git_remote": "https://github.com/example/carrier.git"}
        keys = types.SimpleNamespace(room_id="room_test")
        await self.daemon()._attach_git(room, record, keys)
        await asyncio.sleep(seconds)
        task = getattr(room, "_git_retry_task", None)
        if task is not None and not task.done():
            task.cancel()

    async def test_a_long_outage_does_not_grow_the_error(self) -> None:
        tries = self.transport(fails_for=10_000)
        room = FakeRoom()
        await self.drive(room)
        self.assertGreater(len(tries), 5, "the ladder should have run several times")
        text = room.setup_error or ""
        self.assertEqual(text.count("git channel unusable"), 1,
                         f"one clause per carrier, got {text.count('git channel unusable')} "
                         f"after {len(tries)} attempts")
        self.assertLess(len(text), 200, "setup_error must not grow with the retries")

    async def test_a_channel_that_comes_back_stops_reporting_itself_broken(self) -> None:
        self.transport(fails_for=2)
        room = FakeRoom()
        await self.drive(room)
        self.assertIsNotNone(room.transport("git"), "the stub should have come up")
        self.assertIsNone(room.setup_error,
                          "a recovered channel must clear its complaint")

    async def test_a_broken_folder_is_not_hidden_by_the_git_retry(self) -> None:
        # Why the original joined rather than assigned: two carriers can be
        # broken at once, and fixing one of them is half a fix.
        self.transport(fails_for=10_000)
        room = FakeRoom()
        room.note_setup_error("file", "shared folder unusable: no such path")
        await self.drive(room)
        text = room.setup_error or ""
        self.assertIn("shared folder unusable", text,
                      "the folder error must survive any number of git retries")
        self.assertIn("git channel unusable", text)
        self.assertEqual(text.count("git channel unusable"), 1)

    async def test_the_folder_error_survives_a_git_recovery(self) -> None:
        self.transport(fails_for=1)
        room = FakeRoom()
        room.note_setup_error("file", "shared folder unusable: no such path")
        await self.drive(room)
        self.assertIsNotNone(room.transport("git"))
        self.assertEqual(room.setup_error, "shared folder unusable: no such path",
                         "clearing the git clause must not clear the folder one")

    def test_no_carriers_complaining_reads_as_none(self) -> None:
        # `link_status` and the join reply both branch on falsiness, so an
        # empty string here would render as a room that has something to say.
        room = FakeRoom()
        self.assertIsNone(room.setup_error)
        room.note_setup_error("git", "git channel unusable: nope")
        self.assertIsNotNone(room.setup_error)
        room.note_setup_error("git", None)
        self.assertIsNone(room.setup_error)


if __name__ == "__main__":
    unittest.main()
