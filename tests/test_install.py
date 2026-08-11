"""The installer, tested rather than grepped.

There used to be three installers -- bash for Claude Code, bash for Codex,
PowerShell for Windows -- with the same logic written out three times, and the
only thing checking any of it was a CI job that parsed two of them and ran one.
They drifted, exactly where you would expect: `install.ps1` wired one of the two
hook events and left `--home` off the command, so a Windows machine running both
agents had Codex's hook draining Claude Code's inbox.

These are the assertions that would have caught that. Two of them are the same
claims the CI installers job makes, moved here so they hold on every platform
and cost a second instead of a job.

Nothing here touches the real home directory: HOME and USERPROFILE are pointed
at a temp directory and the module is reloaded so it recomputes its paths.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from link import install as _install  # noqa: E402
from link import util as _util        # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:                      # pragma: no cover - 3.10 only
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

PYTHON = "/opt/python/bin/python3" if os.name != "nt" else r"C:\Python312\python.exe"


class InstallerCase(unittest.TestCase):
    """A fresh fake home per test, with the module's paths recomputed for it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self._saved = {k: os.environ.get(k)
                       for k in ("HOME", "USERPROFILE", "CODEX_HOME", "CLAUDE_LINK_HOME")}
        os.environ["HOME"] = self.home
        os.environ["USERPROFILE"] = self.home
        os.environ.pop("CODEX_HOME", None)
        os.environ.pop("CLAUDE_LINK_HOME", None)
        self.install = importlib.reload(_install)
        self.out = self.install.Out(quiet=True)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(_install)
        self._tmp.cleanup()

    # -- helpers ----------------------------------------------------------- #

    def claude_json(self) -> dict:
        with open(self.install.CLAUDE_JSON, encoding="utf-8") as fh:
            return json.load(fh)

    def claude_settings(self) -> dict:
        with open(self.install.CLAUDE_SETTINGS, encoding="utf-8") as fh:
            return json.load(fh)

    def codex_config(self) -> dict:
        with open(self.install.CODEX_CONFIG, "rb") as fh:
            return tomllib.load(fh)

    def read(self, path: str) -> str:
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def hook_commands(self, event: str) -> list[str]:
        blocks = self.claude_settings().get("hooks", {}).get(event, [])
        return [h["command"] for b in blocks for h in b.get("hooks", [])]


class TestClaudeCode(InstallerCase):
    def test_registers_the_mcp_server(self) -> None:
        self.install.install_claude(PYTHON, skip_hook=False, out=self.out)

        entry = self.claude_json()["mcpServers"]["agent-link"]
        self.assertEqual(entry["command"], PYTHON)
        # As a module, not as a path into the checkout. A path would make the
        # checkout load-bearing forever: move or delete it after installing and
        # the config still looks perfect while no server ever starts.
        self.assertEqual(entry["args"], ["-X", "utf8", "-m", "link.mcp_server"])
        # Declared, never sniffed: the same binary runs under Codex, which
        # spawns it with a cleared environment.
        self.assertEqual(entry["env"]["CLAUDE_LINK_AGENT_KIND"], "claude-code")

    def test_falls_back_to_a_path_when_the_package_did_not_install(self) -> None:
        """`-m` is only honest if there is an installed package behind it.

        `install_package` has a path that installs the dependency alone when the
        package itself will not build. Registering a module after that produces
        a configuration that looks right and starts nothing, so the absolute
        path is still the answer for exactly that case.
        """
        self.install.install_claude(PYTHON, skip_hook=False, out=self.out,
                                    as_module=False)

        entry = self.claude_json()["mcpServers"]["agent-link"]
        self.assertEqual(entry["args"][:2], ["-X", "utf8"])
        self.assertTrue(entry["args"][2].endswith("mcp_server.py"))
        for event in ("UserPromptSubmit", "PostToolUse"):
            self.assertIn("hook_notify.py", self.hook_commands(event)[0], event)

    def test_the_hook_runs_as_a_module_too(self) -> None:
        self.install.install_claude(PYTHON, skip_hook=False, out=self.out)
        for event in ("UserPromptSubmit", "PostToolUse"):
            command = self.hook_commands(event)[0]
            self.assertIn("-m link.hook_notify", command, event)
            self.assertNotIn("hook_notify.py", command, event)

    def test_installs_the_skill_with_its_frontmatter(self) -> None:
        self.install.install_claude(PYTHON, skip_hook=False, out=self.out)
        path = os.path.join(self.home, ".claude", "skills", "agent-link", "SKILL.md")
        self.assertTrue(os.path.exists(path))
        self.assertTrue(self.read(path).startswith("---"))

    def test_wires_both_hook_events(self) -> None:
        """UserPromptSubmit alone cannot deliver anything mid-task.

        It fires when a human types. PostToolUse fires between the agent's own
        steps, and is the one that lets two agents coordinate with nobody
        relaying for them. install.ps1 shipped with only the first.
        """
        self.install.install_claude(PYTHON, skip_hook=False, out=self.out)
        for event in ("UserPromptSubmit", "PostToolUse"):
            self.assertEqual(len(self.hook_commands(event)), 1, event)

    def test_the_hook_names_its_own_home(self) -> None:
        """Hooks are spawned with a cleaned environment.

        A hook without --home resolves to whichever install owns the default
        home. On a machine running two agents that is the other agent's inbox,
        and it does not fail: it drains their mail, marks it read, and prints it
        into the wrong context.
        """
        self.install.install_claude(PYTHON, skip_hook=False, out=self.out)
        for event in ("UserPromptSubmit", "PostToolUse"):
            command = self.hook_commands(event)[0]
            self.assertIn("--home", command)
            self.assertIn("claude-link", command)

    def test_the_server_and_the_hook_agree_on_which_home_they_serve(self) -> None:
        """The failure this closes comes from the opposite end of the usual one.

        The hook's --home used to be read from the installing shell, while the
        MCP entry declared no home at all and fell back to the default. So
        running the installer with CLAUDE_LINK_HOME set -- which is exactly what
        this project's own notes tell people to do on a two-agent machine --
        pointed Claude Code's server at one home and its hook at another.
        """
        os.environ["CLAUDE_LINK_HOME"] = os.path.join(self.home, "somewhere-else")
        try:
            self.install.install_claude(PYTHON, skip_hook=False, out=self.out)
        finally:
            os.environ.pop("CLAUDE_LINK_HOME", None)

        env = self.claude_json()["mcpServers"]["agent-link"]["env"]
        self.assertEqual(env["CLAUDE_LINK_HOME"], self.install.CLAUDE_LINK_HOME)
        self.assertEqual(env["CLAUDE_LINK_CTRL_PORT"], str(self.install.CLAUDE_CTRL_PORT))
        for event in ("UserPromptSubmit", "PostToolUse"):
            self.assertIn(env["CLAUDE_LINK_HOME"], self.hook_commands(event)[0], event)
            self.assertNotIn("somewhere-else", self.hook_commands(event)[0], event)

    def test_an_incomplete_checkout_is_refused_before_anything_is_written(self) -> None:
        """A config that names files which are not there looks perfect and does
        nothing, and the symptom is "no link_* tools" with no cause on screen."""
        saved = self.install.MCP_SERVER
        self.install.MCP_SERVER = os.path.join(self.home, "not-here", "mcp_server.py")
        try:
            self.assertFalse(self.install.check_layout(self.out))
        finally:
            self.install.MCP_SERVER = saved
        self.assertTrue(self.out.problems)

    def test_rerunning_does_not_stack_hooks(self) -> None:
        for _ in range(3):
            self.install.install_claude(PYTHON, skip_hook=False, out=self.out)
        for event in ("UserPromptSubmit", "PostToolUse"):
            self.assertEqual(len(self.hook_commands(event)), 1, event)

    def test_skip_hook_writes_no_hook(self) -> None:
        self.install.install_claude(PYTHON, skip_hook=True, out=self.out)
        self.assertFalse(os.path.exists(self.install.CLAUDE_SETTINGS))

    def test_leaves_other_configuration_alone(self) -> None:
        os.makedirs(os.path.dirname(self.install.CLAUDE_SETTINGS), exist_ok=True)
        with open(self.install.CLAUDE_JSON, "w", encoding="utf-8") as fh:
            json.dump({"projects": {"/work": {"allowedTools": ["Bash"]}},
                       "mcpServers": {"other": {"command": "node"}}}, fh)
        with open(self.install.CLAUDE_SETTINGS, "w", encoding="utf-8") as fh:
            json.dump({"model": "opus",
                       "hooks": {"UserPromptSubmit": [
                           {"hooks": [{"type": "command", "command": "mine.sh"}]}]}}, fh)

        self.install.install_claude(PYTHON, skip_hook=False, out=self.out)

        config = self.claude_json()
        self.assertEqual(config["projects"]["/work"]["allowedTools"], ["Bash"])
        self.assertEqual(config["mcpServers"]["other"]["command"], "node")
        settings = self.claude_settings()
        self.assertEqual(settings["model"], "opus")
        self.assertIn("mine.sh", self.hook_commands("UserPromptSubmit"))
        self.assertEqual(len(self.hook_commands("UserPromptSubmit")), 2)

    def test_refuses_to_overwrite_a_config_it_cannot_parse(self) -> None:
        """Overwriting a file we do not understand is how an installer eats
        somebody's settings. It has to stop, and it has to say so."""
        with open(self.install.CLAUDE_JSON, "w", encoding="utf-8") as fh:
            fh.write("{ not json at all")
        self.install.install_claude(PYTHON, skip_hook=True, out=self.out)
        self.assertEqual(self.read(self.install.CLAUDE_JSON), "{ not json at all")
        self.assertTrue(self.out.problems)

    def test_the_first_backup_is_the_one_that_is_kept(self) -> None:
        """A second run must not back up the config the first run wrote.

        The backup worth having is the file as it was before claude-link ever
        touched it; overwriting it on every run loses exactly that.
        """
        with open(self.install.CLAUDE_JSON, "w", encoding="utf-8") as fh:
            json.dump({"pristine": True}, fh)

        self.install.install_claude(PYTHON, skip_hook=True, out=self.out)
        self.install.install_claude(PYTHON, skip_hook=True, out=self.out)

        backup = self.install.CLAUDE_JSON + self.install.BACKUP_SUFFIX
        with open(backup, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"pristine": True})


@unittest.skipIf(tomllib is None, "no TOML reader on this interpreter")
class TestCodex(InstallerCase):
    def test_registers_a_server_codex_can_load(self) -> None:
        self.install.install_codex(PYTHON, skip_hook=False, out=self.out)
        server = self.codex_config()["mcp_servers"]["agent-link"]
        self.assertEqual(server["command"], PYTHON)
        # 660 must stay above link_wait's own ceiling, or Codex aborts the call
        # before our deadline fires and the user sees a transport error instead
        # of an empty result.
        self.assertGreater(server["tool_timeout_sec"], 605)
        self.assertEqual(server["env"]["CLAUDE_LINK_AGENT_KIND"], "codex")

    def test_codex_never_shares_claude_codes_identity(self) -> None:
        """The most expensive defect this project has had.

        Without its own home Codex resolves to Claude Code's, and the two become
        one device and one room member. A send goes out to the room and is never
        echoed to the local inbox, so neither agent ever sees the other while
        link_status reports a healthy room.
        """
        self.install.install_claude(PYTHON, skip_hook=False, out=self.out)
        self.install.install_codex(PYTHON, skip_hook=False, out=self.out)

        env = self.codex_config()["mcp_servers"]["agent-link"]["env"]
        self.assertTrue(env.get("CLAUDE_LINK_HOME"))
        self.assertNotEqual(env["CLAUDE_LINK_HOME"], self.install.CLAUDE_LINK_HOME)
        self.assertTrue(env.get("CLAUDE_LINK_CTRL_PORT"))
        self.assertNotEqual(int(env["CLAUDE_LINK_CTRL_PORT"]), 45814)

        # And the hooks must point at their own homes, not at each other's.
        codex_hook = [h["command"]
                      for b in self.codex_config()["hooks"]["PostToolUse"]
                      for h in b["hooks"]][0]
        self.assertIn(env["CLAUDE_LINK_HOME"], codex_hook)
        claude_hook = self.hook_commands("PostToolUse")[0]
        self.assertNotIn(env["CLAUDE_LINK_HOME"], claude_hook)

    def test_both_hook_events_once_each_after_three_runs(self) -> None:
        for _ in range(3):
            self.install.install_codex(PYTHON, skip_hook=False, out=self.out)
        config = self.codex_config()
        for event in ("UserPromptSubmit", "PostToolUse"):
            commands = [h for b in config["hooks"][event] for h in b["hooks"]]
            self.assertEqual(len(commands), 1, f"{event} stacked: {commands}")
            self.assertIn("--home", commands[0]["command"])

    def test_keeps_the_users_own_config_and_comments(self) -> None:
        os.makedirs(self.install.CODEX_HOME, exist_ok=True)
        original = (
            "# my notes, which a TOML round-trip would delete\n"
            'model = "gpt-5-codex"\n'
            "\n"
            "[mcp_servers.something-else]\n"
            'command = "node"\n'
        )
        with open(self.install.CODEX_CONFIG, "w", encoding="utf-8") as fh:
            fh.write(original)

        self.install.install_codex(PYTHON, skip_hook=False, out=self.out)

        text = self.read(self.install.CODEX_CONFIG)
        self.assertIn("# my notes", text)
        config = self.codex_config()
        self.assertEqual(config["model"], "gpt-5-codex")
        self.assertEqual(config["mcp_servers"]["something-else"]["command"], "node")

    def test_refuses_a_config_it_cannot_parse(self) -> None:
        os.makedirs(self.install.CODEX_HOME, exist_ok=True)
        with open(self.install.CODEX_CONFIG, "w", encoding="utf-8") as fh:
            fh.write("[[[ not toml")
        self.install.install_codex(PYTHON, skip_hook=False, out=self.out)
        self.assertEqual(self.read(self.install.CODEX_CONFIG), "[[[ not toml")
        self.assertTrue(self.out.problems)

    def test_skip_hook_writes_the_server_and_no_hook(self) -> None:
        self.install.install_codex(PYTHON, skip_hook=True, out=self.out)
        config = self.codex_config()
        self.assertIn("agent-link", config["mcp_servers"])
        self.assertEqual(config.get("hooks", {}), {})


class TestHookQuoting(unittest.TestCase):
    """The hook command is a string a shell will parse before Python sees it.

    A home directory with a space in it is the common case on Windows and not
    rare on macOS; one with a `$` in it is rare and would silently expand.
    """

    def test_a_path_with_a_space_survives(self) -> None:
        command = _install.hook_command(
            "/usr/bin/python3" if os.name != "nt" else r"C:\Program Files\Python\python.exe",
            os.path.join("/home", "First Last", ".claude", "claude-link"),
        )
        self.assertIn("--home", command)
        # Whatever the quoting style, the raw space must not be left bare.
        home_part = command.split("--home", 1)[1].strip()
        self.assertTrue(home_part.startswith(("'", '"')), home_part)

    @unittest.skipIf(os.name == "nt", "POSIX quoting only")
    def test_a_dollar_sign_is_not_left_to_expand(self) -> None:
        command = _install.hook_command("/usr/bin/python3", "/home/$USER/.claude/claude-link")
        self.assertNotIn('"$USER', command)


class TestTheCommandItPrints(unittest.TestCase):
    """The next-steps block has to name a command that exists on this machine.

    It did not, on Windows, which is where `pip install --user` puts the console
    script somewhere nothing adds to PATH. The installer detected that correctly
    and warned about it, then printed `agent-link config --set ...` four lines
    lower as step one -- the line somebody copies. Two real installs ran into it
    before anybody noticed the two halves disagreed.
    """

    def test_the_module_form_is_printed_when_the_script_is_not_on_path(self) -> None:
        with unittest.mock.patch.object(_util.shutil, "which", return_value=None):
            command = _install.cli_command(PYTHON)
        self.assertIn("-m link.cli", command)
        self.assertNotEqual(command.strip(), "agent-link")

    def test_it_names_the_interpreter_it_is_installing_for(self) -> None:
        """The installer's own case, and the reason `cli_command` takes an
        argument at all: the wrappers find a Python and hand over, so the
        interpreter being installed for is not always the one running this.
        Printing `sys.executable` here would name the wrong one.

        The rest of the behaviour is `util.cli_invocation`, tested in
        tests/test_cli.py rather than twice.
        """
        with unittest.mock.patch.object(_util.shutil, "which", return_value=None):
            self.assertIn(PYTHON, _install.cli_command(PYTHON))


class TestNothingHasToStayOnDisk(unittest.TestCase):
    """The claim that makes this a skill rather than a checkout.

    Registration used to bake absolute paths into two config files, so the
    directory the installer ran from could never be moved again. `check_layout`
    caught that at install time and nothing caught it afterwards: the symptom of
    a moved clone was "no link_* tools", with nothing on screen pointing at the
    cause. These are the assertions that keep the paths out.
    """

    def test_the_skill_ships_inside_the_package(self) -> None:
        """Otherwise an install from a URL has no SKILL.md to copy."""
        self.assertTrue(os.path.isfile(_install.SKILL_SRC), _install.SKILL_SRC)
        self.assertEqual(os.path.dirname(_install.SKILL_SRC), _install.PACKAGE_ROOT)

    def test_the_mcp_entry_names_no_directory(self) -> None:
        args = _install.mcp_entry(PYTHON)["args"]
        self.assertEqual(args, ["-X", "utf8", "-m", "link.mcp_server"])
        self.assertNotIn(_install.LINK_ROOT, " ".join(args))

    def test_the_hook_command_names_no_directory(self) -> None:
        command = _install.hook_command(PYTHON, "/somewhere/claude-link")
        self.assertIn("-m link.hook_notify", command)
        self.assertNotIn(_install.LINK_ROOT, command)

    def test_the_codex_block_uses_the_module_too(self) -> None:
        block = _install.render_codex_block(PYTHON, want_hook=True)
        self.assertIn("link.mcp_server", block)
        self.assertIn("link.hook_notify", block)
        self.assertNotIn("mcp_server.py", block)

    def test_the_fallback_still_names_the_files(self) -> None:
        args = _install.mcp_entry(PYTHON, as_module=False)["args"]
        self.assertTrue(args[2].endswith("mcp_server.py"))
        self.assertIn("hook_notify.py",
                      _install.hook_command(PYTHON, "/x", as_module=False))

    def test_a_checkout_is_recognised_as_one(self) -> None:
        """The suite runs from the checkout, so this has to be true here, and
        false from site-packages: pip must never be pointed at its own output."""
        self.assertTrue(_install.is_source_checkout())
        self.assertTrue(os.path.isfile(
            os.path.join(_install.LINK_ROOT, "pyproject.toml")))


class TestAgentDetection(InstallerCase):
    def test_finds_claude_code_by_its_home(self) -> None:
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        self.assertIn("claude", self.install.detect_agents())

    def test_finds_codex_by_its_home(self) -> None:
        os.makedirs(os.path.join(self.home, ".codex"), exist_ok=True)
        self.assertIn("codex", self.install.detect_agents())


class TestAnInstalledSkillGoesStale(InstallerCase):
    """`SKILL.md` is copied into each agent's skills directory at install time,
    so from that moment it is a snapshot. Upgrading the package replaces the one
    inside it and does not touch the copies, and nothing ever looked.

    That is not hypothetical. The copy on the machine this was written on still
    told the agent to offer a synced folder a day after the folder stopped being
    a supported carrier: the agent read it, believed it, and recommended a
    transport this project had removed. Nobody would notice, because a stale
    copy is a perfectly good file that nothing was comparing against anything.

    It matters more the moment strangers install this, because their copy starts
    ageing on the day they install it and they have no way of knowing.
    """

    def skills_root(self, agent: str = ".claude") -> str:
        return os.path.join(self.home, agent, "skills")

    def installed(self, agent: str = ".claude") -> str:
        return os.path.join(self.skills_root(agent), "agent-link", "SKILL.md")

    def test_what_was_just_installed_is_not_stale(self) -> None:
        self.install.install_skill(self.skills_root(), self.out)
        self.assertEqual(self.install.stale_skills(), [])

    def test_a_copy_that_no_longer_matches_the_package_is_stale(self) -> None:
        self.install.install_skill(self.skills_root(), self.out)
        with open(self.installed(), "a", encoding="utf-8") as fh:
            fh.write("\nOffer the user a synced folder such as OneDrive.\n")
        self.assertEqual(self.install.stale_skills(), [self.installed()])

    def test_an_agent_that_never_had_a_copy_is_not_out_of_date(self) -> None:
        """Never installed for is a different thing from stale, and saying
        otherwise would put a permanent warning in front of everyone with one
        agent, which is most people."""
        self.install.install_skill(self.skills_root(".claude"), self.out)
        self.assertEqual(self.install.stale_skills(), [])
        self.assertFalse(os.path.exists(self.installed(".codex")))

    def test_both_agents_are_checked(self) -> None:
        for agent in (".claude", ".codex"):
            self.install.install_skill(self.skills_root(agent), self.out)
            with open(self.installed(agent), "w", encoding="utf-8") as fh:
                fh.write("stale")
        self.assertEqual(sorted(self.install.stale_skills()),
                         sorted([self.installed(".claude"), self.installed(".codex")]))

    def test_a_copy_that_only_differs_by_line_endings_is_current(self) -> None:
        """Found on a real machine: of the two copies here, one was byte-wise
        different and word-for-word identical, because something along the way
        had written it CRLF. A copy that only differs that way is not out of
        date, it is on Windows -- and a warning that cannot be acted on is one
        people learn to scroll past, which would cost the one that can."""
        self.install.install_skill(self.skills_root(), self.out)
        with open(self.install.SKILL_SRC, "rb") as fh:
            unix = fh.read().replace(b"\r\n", b"\n")
        with open(self.installed(), "wb") as fh:
            fh.write(unix.replace(b"\n", b"\r\n"))

        self.assertEqual(self.install.stale_skills(), [])

    def test_refreshing_puts_the_packaged_one_back(self) -> None:
        self.install.install_skill(self.skills_root(), self.out)
        with open(self.installed(), "w", encoding="utf-8") as fh:
            fh.write("stale")

        refreshed = self.install.refresh_skills()

        self.assertEqual(refreshed, [self.installed()])
        self.assertEqual(self.install.stale_skills(), [])
        with open(self.installed(), "rb") as fh, open(self.install.SKILL_SRC, "rb") as src:
            self.assertEqual(fh.read(), src.read())

    def test_refreshing_does_not_wire_up_an_agent_that_was_never_set_up(self) -> None:
        """`update` refreshes; `install` installs. Conflating them would put
        claude-link into an agent the user never asked to have it in."""
        self.assertEqual(self.install.refresh_skills(), [])
        self.assertFalse(os.path.exists(self.installed(".claude")))
        self.assertFalse(os.path.exists(self.installed(".codex")))


if __name__ == "__main__":
    unittest.main()


class TestNoticingTheAgentCannotSeeThisAtAll(InstallerCase):
    """The next silent failure, and the one nothing was looking for.

    `doctor` checked the dependency, the identity, the skill, the daemon, git,
    the relay and the rooms, and never opened `~/.claude.json` or
    `~/.codex/config.toml`. So a machine where the MCP server is no longer
    registered reports a healthy daemon, a green git channel, joined rooms and
    exit 0, while the agent has no `link_*` tools whatsoever.

    Nobody has to do anything wrong to get there: Claude Code rewriting its
    config from an in-memory copy while the installer ran, a `claude mcp
    remove`, `--agent auto` guessing the wrong agent, or a hand edit.
    """

    def skill_for(self, agent: str) -> None:
        self.install.install_skill(
            os.path.join(self.home, agent, "skills"), self.out)

    def test_an_agent_that_was_never_set_up_is_not_a_problem(self):
        self.assertEqual(self.install.registration_problems(), [])

    def test_a_skill_with_no_mcp_entry_is_a_problem(self):
        self.skill_for(".claude")
        problems = self.install.registration_problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("link_* tools", problems[0])

    def test_a_complete_install_reports_nothing(self):
        self.install.install_claude(PYTHON, skip_hook=True, out=self.out)
        self.skill_for(".claude")
        self.assertEqual(self.install.registration_problems(), [])

    def test_codex_is_checked_too(self):
        self.skill_for(".codex")
        problems = self.install.registration_problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("Codex", problems[0])

    def test_an_unreadable_config_counts_as_missing(self):
        """A config we cannot parse is one we cannot vouch for, and saying
        nothing there is how this whole class of failure survives."""
        self.skill_for(".claude")
        with open(self.install.CLAUDE_JSON, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(len(self.install.registration_problems()), 1)


class TestTheInstallerKeepsOtherPeoplesHooks(InstallerCase):
    """`HOOK_MARKER` was matched against a whole hook *block*.

    Claude Code groups several commands under one `matcher`, so one match
    deleted every command in that group. Somebody with their own
    `my_hook_notify.py` next to a linter lost the linter as well, with nothing
    said about it -- and the guard whose failure message is literally "the edit
    would have changed config that is not ours" applied the identical filter to
    both sides of its comparison, so the victim was removed from `before` too
    and the check passed.

    The README says every file it edits is backed up first, and it is, so this
    was recoverable. Not being told is the part that makes it a safety defect.
    """

    def settings_with(self, *commands: str) -> None:
        os.makedirs(os.path.dirname(self.install.CLAUDE_SETTINGS), exist_ok=True)
        with open(self.install.CLAUDE_SETTINGS, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"UserPromptSubmit": [
                {"matcher": "Edit",
                 "hooks": [{"type": "command", "command": c} for c in commands]},
            ]}}, fh)

    def commands(self, event: str = "UserPromptSubmit") -> list[str]:
        return self.hook_commands(event)

    def test_a_neighbour_in_the_same_group_survives(self):
        self.settings_with("python C:/me/my_hook_notify.py",
                           "python C:/me/run_linter.py")

        self.install.install_claude(PYTHON, skip_hook=False, out=self.out)

        joined = " ".join(self.commands())
        self.assertIn("run_linter.py", joined,
                      "an unrelated hook was deleted along with ours")

    def test_our_own_entry_is_still_replaced_rather_than_stacked(self):
        self.install.install_claude(PYTHON, skip_hook=False, out=self.out)
        self.install.install_claude(PYTHON, skip_hook=False, out=self.out)

        for event in self.install.HOOK_EVENTS:
            ours = [c for c in self.commands(event) if "hook_notify" in c]
            self.assertEqual(len(ours), 1, f"{event}: {ours}")

    def test_a_group_of_only_ours_leaves_no_empty_shell(self):
        self.settings_with("python C:/me/my_hook_notify.py")

        self.install.install_claude(PYTHON, skip_hook=False, out=self.out)

        blocks = self.claude_settings()["hooks"]["UserPromptSubmit"]
        self.assertTrue(all(b.get("hooks") for b in blocks), blocks)

    def test_an_unrelated_group_is_untouched(self):
        os.makedirs(os.path.dirname(self.install.CLAUDE_SETTINGS), exist_ok=True)
        with open(self.install.CLAUDE_SETTINGS, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"UserPromptSubmit": [
                {"matcher": "Bash",
                 "hooks": [{"type": "command", "command": "python audit.py"}]},
            ]}}, fh)

        self.install.install_claude(PYTHON, skip_hook=False, out=self.out)

        self.assertIn("audit.py", " ".join(self.commands()))


class TestUpgradingFromTheOldName(InstallerCase):
    """The product was `claude-link` until 2026-08-09 and is `agent-link` now.

    A rename that only adds the new name leaves the old one in place: two
    entries in `mcpServers`, so the agent starts two servers against one home
    and both write the same inbox; two skills saying the same things under
    names only one of which exists; and, for Codex, a second managed block
    whose markers the splicer no longer recognises, so every later install
    appends another.

    Nobody would see any of that until it misbehaved, which is why it is worth
    a test for a migration that matters exactly once.
    """

    def stage_an_old_install(self) -> None:
        old_skill = os.path.join(self.home, ".claude", "skills", "claude-link")
        os.makedirs(old_skill, exist_ok=True)
        with open(os.path.join(old_skill, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("what the old name said")
        with open(self.install.CLAUDE_JSON, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {"claude-link": {"command": "python"}},
                       "theirs": "untouched"}, fh)
        os.makedirs(self.install.CODEX_HOME, exist_ok=True)
        with open(self.install.CODEX_CONFIG, "w", encoding="utf-8") as fh:
            fh.write('model = "gpt-5"\n\n'
                     "# >>> claude-link (managed block; regenerated by the installer) >>>\n"
                     "[mcp_servers.claude-link]\ncommand = \"python\"\n"
                     "# <<< claude-link (managed block) <<<\n")

    def test_the_old_skill_directory_does_not_survive(self):
        self.stage_an_old_install()
        self.install.install_skill(
            os.path.join(self.home, ".claude", "skills"), self.out)

        skills = os.listdir(os.path.join(self.home, ".claude", "skills"))
        self.assertEqual(skills, ["agent-link"], skills)

    def test_the_old_mcp_entry_does_not_survive(self):
        self.stage_an_old_install()
        self.install.install_claude(PYTHON, skip_hook=True, out=self.out)

        servers = self.claude_json()["mcpServers"]
        self.assertEqual(sorted(servers), ["agent-link"])

    def test_their_own_settings_are_still_there(self):
        self.stage_an_old_install()
        self.install.install_claude(PYTHON, skip_hook=True, out=self.out)

        self.assertEqual(self.claude_json()["theirs"], "untouched")

    def test_the_old_codex_block_is_replaced_rather_than_joined(self):
        self.stage_an_old_install()
        self.install.install_codex(PYTHON, skip_hook=True, out=self.out)

        body = self.read(self.install.CODEX_CONFIG)
        self.assertEqual(body.count(">>> claude-link"), 0, "the old block survived")
        self.assertEqual(body.count(">>> agent-link"), 1, "more than one block")
        self.assertIn('model = "gpt-5"', body, "their own config was dropped")


class TestTheAdviceNamesTheRealBranch(InstallerCase):
    """The installer tells people to add `branches-ignore: [<branch>]` before
    pointing this at a repository that builds.

    A rename sweep turned that into `[agent-link]` -- the product name, not the
    branch -- and it went out in a real install before anybody read the output.
    Advice to ignore a branch that does not exist is worse than none, because
    it looks actionable and the heartbeat still triggers CI 1900 times a day.
    """

    def test_the_installer_and_the_transport_agree_on_the_branch(self):
        from link.transport_git import DEFAULT_BRANCH as USED

        self.assertEqual(self.install.DEFAULT_BRANCH, USED)

    def test_the_next_steps_are_three_runnable_lines(self):
        # The branches-ignore advice moved to `config --set git_remote=...`
        # itself (shared_repo_warning), which fires at the moment of the
        # decision; next_steps is now a lead sentence plus exactly three
        # commands, each one runnable on the machine that printed it.
        import contextlib
        import io as _io

        buf = _io.StringIO()
        out = self.install.Out(quiet=False)
        with contextlib.redirect_stdout(buf):
            self.install.next_steps(PYTHON, ["claude"], out)
        text = buf.getvalue()

        self.assertIn('config --set git_remote=', text)
        self.assertIn(" name <your-name>", text)
        self.assertIn(" doctor", text)
        commands = [line for line in text.splitlines()
                    if line.startswith("    ")]
        self.assertEqual(len(commands), 3, text)
