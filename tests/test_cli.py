"""The command an agent leaves running so a message can wake it.

Hooks only fire while an agent is already doing something. Between turns
nothing of the agent's runs, so a colleague's reply -- which arrives exactly
when you are idle waiting for it -- sits unread until a human types. What
closes that gap is a process the agent starts in the background and the harness
watches: when it exits, the agent is re-invoked.

So the contract here is narrow and unusual for a CLI. `wake` must *exit* on the
first message rather than tail forever like `watch`, and it must not consume
what it saw: the notification hook fetches the same inbox a moment later, and a
message marked read is a message no hook and no `link_inbox` will ever show
again. Waking someone by eating the letter is worse than not waking them.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from link import cli                                            # noqa: E402
from link import store                                          # noqa: E402
from link import util                                           # noqa: E402

A_MESSAGE = {
    "room": "ccc",
    "from": "riccardo@laptop",
    "from_agent_kind": "codex",
    "text": "installer done, 152 green",
    "received_at": "2026-07-26T11:22:56.174684Z",
}


class TestAllowPublicCarrier(unittest.TestCase):
    """A public carrier repo is an informed choice, not a scolding."""

    def test_the_key_exists_off_by_default_and_validates(self):
        prior = os.environ.get("CLAUDE_LINK_HOME")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["CLAUDE_LINK_HOME"] = tmp.name
        try:
            cfg = store.load_config()
            self.assertIs(cfg["allow_public_carrier"], False)
            self.assertTrue(store.config_value_ok("allow_public_carrier", True))
            self.assertFalse(store.config_value_ok("allow_public_carrier", "yes"))
        finally:
            if prior is None:
                os.environ.pop("CLAUDE_LINK_HOME", None)
            else:
                os.environ["CLAUDE_LINK_HOME"] = prior

    def test_doctor_accepts_a_public_repo_when_chosen(self):
        ok, lines = cli.visibility_verdict(
            "PUBLIC", {"allow_public_carrier": True})
        self.assertTrue(ok)
        self.assertIn("by choice", "\n".join(lines))

    def test_doctor_still_fails_a_public_repo_by_default(self):
        ok, lines = cli.visibility_verdict("PUBLIC", {})
        self.assertFalse(ok)
        self.assertIn("Make it private", "\n".join(lines))


class TestWakeExitCode(unittest.TestCase):
    """The exit code is the signal; the text is for whoever reads the log."""

    def test_a_message_exits_zero_so_the_wake_reads_as_success(self):
        _text, code = cli.render_wake({"ok": True, "messages": [A_MESSAGE]})
        self.assertEqual(code, 0)

    def test_the_message_is_printed_so_the_agent_wakes_knowing_what_arrived(self):
        text, _code = cli.render_wake({"ok": True, "messages": [A_MESSAGE]})
        self.assertIn("installer done", text)
        self.assertIn("codex", text)

    def test_an_expired_window_is_distinguishable_from_a_message(self):
        """Both re-invoke the agent. Only one of them means "go look"."""
        _text, code = cli.render_wake({"ok": True, "messages": [], "timed_out": True})
        self.assertEqual(code, 1)

    def test_an_expired_window_says_so_rather_than_printing_nothing(self):
        text, _code = cli.render_wake({"ok": True, "messages": [], "timed_out": True})
        self.assertTrue(text.strip(), "a silent exit is indistinguishable from a crash")


class TestWhichInstallTheCliTalksTo(unittest.TestCase):
    """Two agents on one machine, one CLI, and a shell that carries no home.

    This is the collision of 2026-07-26 in its command-line form: an agent that
    reaches the link through the CLI inherits whichever identity the default
    home holds, which is the *other* agent's. A flag works where an exported
    variable does not, because the environment is exactly what goes missing.
    """

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="claude-link-cli-")
        self.before = os.environ.get("CLAUDE_LINK_HOME")

    def tearDown(self):
        if self.before is None:
            os.environ.pop("CLAUDE_LINK_HOME", None)
        else:
            os.environ["CLAUDE_LINK_HOME"] = self.before
        shutil.rmtree(self.base, ignore_errors=True)

    def test_the_home_flag_redirects_where_the_cli_looks(self):
        cli.apply_home(self.base)
        from link import store
        self.assertEqual(store.root_dir(), self.base)

    def test_no_flag_leaves_the_environment_alone(self):
        os.environ["CLAUDE_LINK_HOME"] = "/somewhere/chosen/earlier"
        cli.apply_home(None)
        self.assertEqual(os.environ["CLAUDE_LINK_HOME"], "/somewhere/chosen/earlier")


class TestSayingTheDaemonIsOutOfDate(unittest.TestCase):
    """The daemon builds its transports once, at startup.

    So `config --set git_remote=...` against one that is already running writes
    the file and changes nothing else, and the room then comes up offline with
    nothing anywhere saying why. Two real machines on 2026-08-08 spent twenty
    minutes on exactly that. The daemon reports it now; these are the two places
    a person actually looks.
    """

    TRANSPORT = {
        "problem": "config.json has changed since this daemon started",
        "changed": ["git_remote"],
        "affects_transport": ["git_remote"],
        "fix": "restart the daemon: `agent-link restart`",
    }
    COSMETIC = {
        "problem": "config.json has changed since this daemon started",
        "changed": ["display_name"],
        "affects_transport": [],
        "fix": "restart the daemon: `agent-link restart`",
    }

    def test_doctor_says_nothing_when_the_daemon_is_current(self):
        """The common case. A line printed every time is a line nobody reads."""
        self.assertEqual(cli.stale_lines(None), [])

    def test_doctor_names_the_setting_and_the_fix(self):
        text = "\n".join(cli.stale_lines(self.TRANSPORT))
        self.assertIn("git_remote", text)
        self.assertIn("restart", text)

    def test_doctor_still_reports_a_change_that_needs_no_restart(self):
        text = "\n".join(cli.stale_lines(self.COSMETIC))
        self.assertTrue(text.strip())
        self.assertIn("display_name", text)

    def test_config_says_plainly_that_nothing_has_changed_yet(self):
        """The old text was printed unconditionally and read as boilerplate."""
        message = cli.config_saved_message(self.TRANSPORT)
        self.assertIn("restart", message)
        self.assertIn("git_remote", message)

    def test_config_does_not_demand_a_restart_when_none_is_running(self):
        """`drift` is None when no daemon answered: there is nothing to
        restart, and telling someone to restart nothing wastes the one warning
        they will actually read."""
        self.assertNotIn("restart", cli.config_saved_message(None).lower())


class TestTheCommandsDoctorPrints(unittest.TestCase):
    """Doctor is what somebody runs when something is already wrong.

    Every command it offers is about to be pasted, so a command that cannot run
    on the machine that printed it is a second dead end at the worst moment.
    Both faults below were found by installing into a throwaway home on
    2026-08-09 and reading what came out, not by a test.
    """

    def git_advice(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._doctor_git({})          # nothing configured: the advice branch
        return out.getvalue()

    def test_no_line_ends_in_a_bash_continuation(self):
        """The `config --set` line was wrapped with a trailing backslash. In
        PowerShell and cmd, which is where the Windows half of the users are,
        that runs `config --set` with no value and then a stray second
        command."""
        for line in self.git_advice().splitlines():
            self.assertFalse(line.rstrip().endswith("\\"),
                             f"bash line continuation printed on every platform: {line!r}")

    def test_the_whole_command_is_on_one_line(self):
        lines = [ln for ln in self.git_advice().splitlines() if "config --set" in ln]
        self.assertTrue(lines, "doctor stopped offering the setting to change")
        self.assertTrue(any("git_remote=" in ln for ln in lines),
                        "the setting and its value were split across lines again")

    def test_it_names_an_invocation_rather_than_assuming_one(self):
        """With nothing on PATH, the advice has to be the module form."""
        with unittest.mock.patch.object(util.shutil, "which", return_value=None):
            text = self.git_advice()
        self.assertIn("-m link.cli", text)


class TestTheSkillTheAgentActuallyReads(unittest.TestCase):
    """The package's SKILL.md is not the one any agent reads.

    The installer copies it into `~/.claude/skills/claude-link/` and
    `~/.codex/skills/claude-link/`, and those copies are snapshots from the
    moment of the install. Upgrading the package rewrites the original and
    leaves them exactly where they were, saying whatever they said.

    Nothing checked, and nothing suggested re-running anything, so the only
    signal was an agent confidently recommending something that had been
    removed. This is `doctor`'s job: it is the command people run when the
    thing is behaving oddly, and an agent reading last month's instructions is
    behaving oddly in the hardest way to trace.
    """

    def report(self, stale):
        with unittest.mock.patch.object(cli.install, "stale_skills",
                                        return_value=stale):
            return cli._doctor_skill()

    def test_a_current_copy_reports_no_fault(self):
        lines, ok = self.report([])
        self.assertTrue(ok)

    def test_a_stale_copy_is_a_fault(self):
        """`doctor` exits non-zero on it, exactly as it does for a stale config.
        Both are a live install acting on something that is no longer true."""
        _lines, ok = self.report(["/home/x/.claude/skills/claude-link/SKILL.md"])
        self.assertFalse(ok)

    def test_it_says_which_copy_and_what_to_run(self):
        path = "/home/x/.claude/skills/claude-link/SKILL.md"
        text = "\n".join(self.report([path])[0])
        self.assertIn(path, text)
        self.assertIn("update", text)

    def test_the_fix_it_prints_is_runnable_on_this_machine(self):
        """The same defect as the `config --set` line: advice printed at the
        moment something is already wrong must not name a command that is not
        there."""
        with unittest.mock.patch.object(util.shutil, "which", return_value=None):
            text = "\n".join(self.report(["/x/SKILL.md"])[0])
        self.assertIn("-m link.cli", text)

    def test_no_line_ends_in_a_backslash(self):
        text = "\n".join(self.report(["/x/SKILL.md"])[0])
        for line in text.splitlines():
            self.assertFalse(line.rstrip().endswith("\\"),
                             f"bash line continuation printed on every platform: {line!r}")


class TestDoctorOnADaemonStillWakingUp(unittest.TestCase):
    """`doctor` starts a daemon when none is running, which is the ordinary
    case: you run `doctor` because something already seems wrong, so there is
    often nothing up. For about two seconds after the control port opens the
    rooms are not loaded and their transports are not attached, and `status`
    says exactly that with `loading`.

    `doctor` did not look. Reproduced against a real daemon on 2026-08-09: at
    2.01 s it reported `our-work transport=offline`, at 3.91 s the same room
    reported `transport=git`, and nothing had changed except that a fetch had
    finished. Land the status call slightly earlier and the room is not in the
    list at all, and then `doctor` prints `rooms: none, run join --room <name>`
    to somebody who is already in one.

    Both of those are the cold-start defect that was found and fixed in
    `mcp_server.render`, in the one command whose entire job is to be trusted
    about this. Fixing it in one renderer and not the other is how it survived.
    """

    ROOM = {"room": "our-work", "transport": "offline",
            "online": 0, "members": 1, "queued": 0}

    # -- what it prints ----------------------------------------------------- #

    def test_a_loaded_daemon_with_no_rooms_still_says_to_join_one(self):
        text = "\n".join(cli.room_lines({"rooms": []}, loaded=True))
        self.assertIn("join --room", text)

    def test_a_loading_daemon_never_says_to_join_a_room_you_are_in(self):
        text = "\n".join(cli.room_lines({"rooms": []}, loaded=False))
        self.assertNotIn("join --room", text)
        self.assertIn("loading", text.lower())

    def test_a_transport_still_attaching_is_not_reported_as_fact(self):
        text = "\n".join(cli.room_lines({"rooms": [self.ROOM]}, loaded=False))
        self.assertIn("our-work", text)
        self.assertIn("loading", text.lower())

    def test_a_loaded_room_is_reported_plainly(self):
        text = "\n".join(cli.room_lines(
            {"rooms": [{**self.ROOM, "transport": "git", "online": 1}]},
            loaded=True))
        self.assertIn("transport=git", text)
        self.assertNotIn("loading", text.lower())

    # -- and why it usually does not have to ---------------------------------- #

    def test_it_waits_for_the_rooms_to_finish_loading(self):
        """Cheaper than explaining a provisional answer: `doctor` is not on any
        latency budget, and a correct line beats a fast one."""
        replies = [{"ok": True, "loading": True, "rooms": []},
                   {"ok": True, "loading": True, "rooms": []},
                   {"ok": True, "loading": False, "rooms": [self.ROOM]}]
        with unittest.mock.patch.object(cli.client, "call",
                                        unittest.mock.Mock(side_effect=replies)):
            status, loaded = cli.status_when_loaded(timeout=5.0, poll=0.0)

        self.assertTrue(loaded)
        self.assertEqual(len(status["rooms"]), 1)

    def test_it_gives_up_rather_than_waiting_forever(self):
        """A wedged transport must not be able to make the diagnostic hang.
        That is the same reason `_op_status`'s own gate is bounded, and a
        `doctor` that never returns is worse than one that answers `loading`."""
        stuck = unittest.mock.Mock(
            return_value={"ok": True, "loading": True, "rooms": []})
        with unittest.mock.patch.object(cli.client, "call", stuck):
            _status, loaded = cli.status_when_loaded(timeout=0.05, poll=0.0)

        self.assertFalse(loaded)


class TestInstallingWithNothingCloned(unittest.TestCase):
    """The README's headline install is two lines and the second one has never
    worked:

        pipx install git+https://github.com/.../agent-link.git
        python3 -m link.install

    `pipx` puts the package in an isolated virtualenv on purpose, so no system
    interpreter can import `link`. Run on 2026-08-09 with a real pipx and a
    throwaway `PIPX_HOME`: the install succeeds, `agent-link.exe` appears in
    the bin directory, and the very next line dies on
    `ModuleNotFoundError: No module named 'link'`.

    The console script is the only thing pipx exposes, so that is where the
    installer has to be reachable from.
    """

    def test_install_is_a_subcommand(self):
        args = cli.parse_args(["install"])
        self.assertIs(args.func, cli.cmd_install)

    def test_every_other_subcommand_is_as_strict_as_it_was(self):
        """Forwarding must not turn into swallowing. A typo anywhere else has
        to keep being an error rather than being quietly handed somewhere."""
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.parse_args(["status", "--typo"])

    def test_the_installers_own_flags_pass_straight_through(self):
        """`--agent`, `--dev`, `--self-test` belong to the installer's parser.
        This one must not need to know what they are, or the two drift and the
        wrapper starts rejecting flags the thing it wraps accepts."""
        args = cli.parse_args(
            ["install", "--agent", "codex", "--dev"])
        self.assertEqual(list(args.installer_args), ["--agent", "codex", "--dev"])

    def test_it_runs_the_installer_and_returns_its_code(self):
        with unittest.mock.patch.object(cli.install, "main",
                                        return_value=3) as installer:
            code = cli.cmd_install(
                unittest.mock.Mock(installer_args=["--quiet"]))
        self.assertEqual(code, 3)
        installer.assert_called_once_with(["--quiet"])


class TestTheNameThisProgramPrintsForItself(unittest.TestCase):
    """argparse puts `prog` in every usage line and every error it prints.

    It was the literal string `link`, which is not a command on any machine
    this has ever run on. Same defect as the installer's next-steps block and
    `doctor`'s `config --set` line, in the place a person looks after typing
    something wrong.
    """

    def usage(self, which):
        with unittest.mock.patch.object(util.shutil, "which", return_value=which):
            return cli.build_parser().format_usage()

    def test_it_never_calls_itself_link(self):
        self.assertNotIn("usage: link ", self.usage(None))

    def test_with_nothing_on_path_it_is_the_module_form(self):
        self.assertIn("-m link.cli", self.usage(None))

    def test_with_the_script_on_path_it_is_the_short_name(self):
        usage = self.usage("/usr/local/bin/agent-link")
        self.assertIn("agent-link", usage)
        self.assertNotIn("-m link.cli", usage)


class TestReportingASharedIdentity(unittest.TestCase):
    """What `doctor` prints when two agent paths have signed as one device."""

    SHARED = {
        "kinds": ["claude-code", "cli"],
        "problem": "messages have been sent as this one device (dev_abc) by "
                   "more than one agent path: claude-code, cli",
        "fix": "Each agent needs its own CLAUDE_LINK_HOME and "
               "CLAUDE_LINK_CTRL_PORT; re-running the installer sets both.",
    }

    def test_the_ordinary_case_prints_nothing(self):
        """`doctor` is read when something is wrong, and a line printed every
        run is a line nobody reads by the third one."""
        self.assertEqual(cli.identity_lines(None), [])

    def test_it_names_both_paths_and_the_fix(self):
        text = "\n".join(cli.identity_lines(self.SHARED))
        self.assertIn("claude-code", text)
        self.assertIn("cli", text)
        self.assertIn("CLAUDE_LINK_HOME", text)

    def test_it_does_not_call_it_a_fault(self):
        """One person with one agent who also types `agent-link send` produces
        exactly this. So does the hour of silence in postmortems.md. Only the
        reader can tell them apart, so the reader gets the fact."""
        text = "\n".join(cli.identity_lines(self.SHARED)).lower()
        for word in ("error", "failed", "broken"):
            self.assertNotIn(word, text)

    def test_it_stays_inside_the_column_the_rest_of_doctor_uses(self):
        for line in "\n".join(cli.identity_lines(self.SHARED)).splitlines():
            self.assertLessEqual(len(line), 80, line)


class TestRefreshingAnInstall(unittest.TestCase):
    """`agent-link update`. Two things go out of date and only one is ours."""

    def run_update(self, refreshed, stale=()):
        buf = io.StringIO()
        with unittest.mock.patch.object(cli.install, "refresh_skills",
                                        return_value=list(refreshed)), \
                unittest.mock.patch.object(cli.install, "stale_skills",
                                           return_value=list(stale)), \
                contextlib.redirect_stdout(buf):
            code = cli.cmd_update(unittest.mock.Mock())
        return buf.getvalue(), code

    def test_it_names_each_copy_it_refreshed(self):
        path = "/home/x/.codex/skills/claude-link/SKILL.md"
        text, code = self.run_update([path])
        self.assertEqual(code, 0)
        self.assertIn(path, text)

    def test_nothing_installed_says_so_rather_than_reporting_success(self):
        """Silence here reads as "up to date", which is the opposite of true:
        it means no agent on this machine has claude-link wired up at all."""
        text, code = self.run_update([])
        self.assertIn("install", text)
        self.assertNotEqual(code, 0)

    def test_it_says_what_it_cannot_update(self):
        """The package is pip's or pipx's, and this command does not run either
        of them. Leaving that unsaid would make `update` look like it had done
        the whole job when it had done half."""
        text, _code = self.run_update(["/x/SKILL.md"])
        self.assertIn("pipx", text.lower())


class TestNamingThisProgram(unittest.TestCase):
    """`agent-link` is not on PATH as often as it is."""

    def test_the_bare_name_when_it_resolves(self):
        """`which` searching PATH is exactly the question, so an answer means
        the short form works, and it is the form every document uses."""
        with unittest.mock.patch.object(util.shutil, "which",
                                        return_value="/usr/local/bin/agent-link"):
            self.assertEqual(util.cli_invocation(), "agent-link")

    def test_the_module_form_when_it_does_not(self):
        with unittest.mock.patch.object(util.shutil, "which", return_value=None):
            command = util.cli_invocation("/usr/bin/python3")
        self.assertEqual(command, "/usr/bin/python3 -m link.cli")

    def test_an_interpreter_path_with_a_space_is_quoted(self):
        python = (r"C:\Program Files\Python\python.exe" if os.name == "nt"
                  else "/opt/My Python/bin/python3")
        with unittest.mock.patch.object(util.shutil, "which", return_value=None):
            command = util.cli_invocation(python)
        self.assertFalse(command.startswith(python), f"left bare: {command}")
        self.assertIn("-m link.cli", command)

    def test_it_falls_back_to_the_running_interpreter(self):
        with unittest.mock.patch.object(util.shutil, "which", return_value=None):
            self.assertIn(os.path.basename(sys.executable), util.cli_invocation())


if __name__ == "__main__":
    unittest.main(verbosity=2)
