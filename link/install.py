"""One installer, for every host application and every platform.

    python3 -m link.install                 # install for whatever is on this machine
    python3 -m link.install --agent codex   # just Codex
    python3 -m link.install --skip-hook     # no notification hook

`install.sh` and `install.ps1` are three-line wrappers around this: they find a
Python 3.10+ interpreter and hand over. Everything that decides *what gets
written where* lives here, in one implementation, with tests behind it.

There used to be three scripts -- bash for Claude Code, bash for Codex,
PowerShell for Windows -- and the same logic in each. They drifted, which is a
thing duplicated installers reliably do: `install.ps1` was still wiring one hook
event out of two and omitting `--home`, so a Windows machine running both agents
had Codex's hook draining Claude Code's inbox. That class of bug is the reason
this file exists.

What it does, in order:

  1. check this interpreter is 3.10+
  2. install the package so `agent-link` works from any directory, and with it
     the one real dependency, cryptography. Not editable: the MCP server and the
     hook are registered as `-m link.mcp_server` and `-m link.hook_notify`, so
     no directory has to survive the install. `--dev` is the editable path, for
     working on this rather than using it.
  3. prove the crypto path actually runs on this machine
  4. copy SKILL.md where the host application looks for skills
  5. register the MCP server
  6. wire the notification hook
  7. run the diagnostics and print what to do next

Every file it edits is backed up before it is touched, and re-running is safe.

Two agents on one machine must never share a link home: they would load the
same identity, become one room member, and see a healthy room and total silence
from each other. Codex is therefore given its own home and control port here,
and the hook is told which home it belongs to by argument -- hooks are spawned
with a cleaned environment, so a variable would not survive the trip.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable

# `util` has no third-party imports and no side effects, which is what makes it
# safe here: this module runs before the rest of the package has been proven to
# work, and `shell_quote` is needed by both halves of that -- the hook command
# the installer writes, and the advice `doctor` prints afterwards.
from link.util import cli_invocation, shell_quote

# The channel branch, named here so the advice this prints can never drift
# from the branch the transport actually uses.
DEFAULT_BRANCH = "claude-link"

LINK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_ROOT = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")

MIN_PYTHON = (3, 10)

CLAUDE_HOME = os.path.join(HOME, ".claude")
CLAUDE_JSON = os.path.join(HOME, ".claude.json")
CLAUDE_SETTINGS = os.path.join(CLAUDE_HOME, "settings.json")
CLAUDE_LINK_HOME = os.path.join(CLAUDE_HOME, "claude-link")
CLAUDE_CTRL_PORT = 45814

# Codex honours CODEX_HOME when it is set; assuming the default silently
# installs past a relocated one.
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
CODEX_CONFIG = os.path.join(CODEX_HOME, "config.toml")
# Deliberately under ~/.claude/: it is this project's home, not Claude Code's,
# and moving it now would orphan the identity of every existing Codex install.
CODEX_LINK_HOME = os.path.join(CLAUDE_HOME, "claude-link-codex")
CODEX_CTRL_PORT = "45815"

HOOK_EVENTS = ("UserPromptSubmit", "PostToolUse")
BACKUP_SUFFIX = ".claude-link-backup"

# What identifies an existing hook entry as one of ours, so that re-running
# replaces it instead of adding a second. It has to match both registration
# forms: `-m link.hook_notify` and the fallback `.../link/hook_notify.py`.
# Matching the filename alone is not enough, and the failure is silent -- every
# re-install appends another hook, and the agent gets each message twice, then
# three times.
HOOK_MARKER = "hook_notify"

# Fallback registration only, for the case where the package install failed and
# the one thing still able to start a server is an absolute path into this tree.
# Normally both are registered as `-m` modules; see `mcp_entry`.
MCP_SERVER = os.path.join(PACKAGE_ROOT, "mcp_server.py")
HOOK_SCRIPT = os.path.join(PACKAGE_ROOT, "hook_notify.py")
# Inside the package, so that an install from a URL carries it and there is no
# clone for it to be missing from.
SKILL_SRC = os.path.join(PACKAGE_ROOT, "SKILL.md")


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #


class Out:
    """Progress reporting. Colour only when a terminal is going to read it."""

    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self.problems: list[str] = []
        colour = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        self.c = (
            {"step": "\033[36m", "ok": "\033[32m", "warn": "\033[33m",
             "fail": "\033[31m", "off": "\033[0m"}
            if colour else dict.fromkeys(("step", "ok", "warn", "fail", "off"), "")
        )

    def _say(self, text: str) -> None:
        if not self.quiet:
            print(text, flush=True)

    def step(self, msg: str) -> None:
        self._say(f"{self.c['step']}==> {msg}{self.c['off']}")

    def ok(self, msg: str) -> None:
        self._say(f"    {self.c['ok']}OK  {msg}{self.c['off']}")

    def warn(self, msg: str) -> None:
        self._say(f"    {self.c['warn']}!   {msg}{self.c['off']}")

    def fail(self, msg: str) -> None:
        self.problems.append(msg)
        # Failures go out even under --quiet: an installer that says nothing and
        # leaves a half-configured machine is worse than a noisy one.
        print(f"    {self.c['fail']}X   {msg}{self.c['off']}", flush=True)

    def plain(self, msg: str = "") -> None:
        self._say(msg)


# --------------------------------------------------------------------------- #
# quoting
# --------------------------------------------------------------------------- #


def cli_command(python: str) -> str:
    """The invocation to print, which has to be one that exists on this machine.

    The installer's own case for `util.cli_invocation`, which is where the
    reasoning lives. It is passed the interpreter it is installing *for*, which
    is not always the one running this: the wrappers find a Python and hand
    over, and `--user` installs put the console script somewhere PATH may not
    reach. Found on two real Windows installs before it was noticed.
    """
    return cli_invocation(python)


def hook_command(python: str, home: str, as_module: bool = True) -> str:
    """The command string a host application runs when a message may be waiting.

    `--home`, not an environment variable. Hooks are spawned with a cleaned
    environment, so a hook without it resolves to whichever install owns the
    default home -- and on a machine running two agents that is the other
    agent's inbox. It does not fail loudly: it drains someone else's mail, marks
    it read, and prints it into the wrong context.

    `-m link.hook_notify` rather than a path into this directory. A path makes
    the checkout load-bearing forever: move it, rename it, or delete it after
    installing and the configuration still looks perfect while nothing runs.
    The module form resolves through the installed package instead, so there is
    nothing left on disk that has to stay where it was.
    """
    target = ("-m", "link.hook_notify") if as_module else (HOOK_SCRIPT,)
    return " ".join(shell_quote(part) for part in (
        python, "-X", "utf8", *target, "--home", home,
    ))


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #


def backup_once(path: str, out: Out) -> str | None:
    """Copy `path` aside, but never over an existing backup.

    The backup is "this file as it was before agent-link first touched it", and
    that is the only version worth keeping: a second run would otherwise back up
    the config the first run wrote, and the original -- the one someone would
    actually want to restore -- would be gone. Delete the backup to take a fresh
    baseline.
    """
    if not os.path.exists(path):
        return None
    target = path + BACKUP_SUFFIX
    if os.path.exists(target):
        return target
    shutil.copyfile(path, target)
    # `~/.claude.json` holds an oauth account, project history, and whatever
    # keys somebody put in an mcpServers env block. `copyfile` does not carry
    # permissions, so the copy landed at 0644 while `write_text` was tightening
    # the original to 0600: the installer was loosening a full copy of the file
    # it was busy protecting.
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def write_text(path: str, text: str) -> None:
    """Atomic write: temp file beside the target, then replace.

    ~/.claude.json is Claude Code's live configuration and may be several
    hundred kilobytes. A plain open(w) truncates it first, so an installer
    interrupted at the wrong moment leaves an empty file where the user's
    projects, history and MCP servers used to be.
    """
    from .util import atomic_write_text

    atomic_write_text(path, text)


def edit_json(path: str, mutate: Callable[[dict], None], out: Out) -> bool:
    """Read a JSON config, apply `mutate`, write it back atomically.

    Refuses to touch a file it cannot parse: overwriting a config we do not
    understand is how an installer eats somebody's settings.
    """
    data: dict[str, Any] = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (OSError, ValueError) as exc:
            out.fail(f"{path} is not valid JSON; leaving it alone. ({exc})")
            return False
        if not isinstance(loaded, dict):
            out.fail(f"{path} is not a JSON object; leaving it alone.")
            return False
        data = loaded

    backup = backup_once(path, out)
    mutate(data)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    out.ok(f"{path}{' (backup: ' + BACKUP_SUFFIX + ')' if backup else ''}")
    return True


def seed_home_ctrl_port(home: str, port: int, out: Out) -> None:
    """Write a link home's own control port into its config.json.

    Each harness gets its own CLAUDE_LINK_HOME and its own control port, and the
    MCP registration carries that port as an env var. The CLI does not read that
    env: it goes through `load_config`, which reads the home's config.json, and
    every home starts on the same shipped default. So without this,
    `CLAUDE_LINK_HOME=<the codex home> agent-link ...` tries to bind the daemon
    port that Claude Code's home already owns, and dies with "daemon failed to
    start within 12.0s: no response".

    The decision is made before anything is written, so an install that would
    change nothing touches nothing -- no rewrite, no backup churn. A port the
    user set to something of their own is left alone; only a missing value or
    the shipped default is filled in.
    """
    cfg_path = os.path.join(home, "config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                current = json.load(fh)
        except (OSError, ValueError, RecursionError):
            # Unreadable or unparseable: edit_json refuses it and says so, which
            # is what the user needs to hear -- that home cannot start either way.
            current = None
        if isinstance(current, dict):
            have = current.get("ctrl_port")
            if have == port:
                return
            if have not in (None, CLAUDE_CTRL_PORT):
                return

    def _set(cfg: dict) -> None:
        cfg["ctrl_port"] = port

    edit_json(cfg_path, _set, out)


# --------------------------------------------------------------------------- #
# 1. interpreter
# --------------------------------------------------------------------------- #


def check_layout(out: Out) -> bool:
    """The files this is about to install must exist.

    A partial checkout, or a copy of `install.sh` saved on its own, produces a
    config that looks perfect and a server that never starts -- and the symptom
    is "no link_* tools", with nothing anywhere pointing at the cause.

    Registration no longer bakes these paths into anybody's config, so a
    directory renamed *after* a successful install is no longer a way to reach
    that symptom. It still is under `--dev`, and under the fallback taken when
    the package itself would not install.
    """
    missing = [p for p in (MCP_SERVER, HOOK_SCRIPT, SKILL_SRC) if not os.path.isfile(p)]
    if not missing:
        return True
    out.fail(f"this does not look like a complete agent-link checkout ({LINK_ROOT}).")
    for path in missing:
        out.plain(f"      missing: {path}")
    return False


def check_python(out: Out) -> str:
    """This interpreter, if it is new enough. The wrappers do the searching."""
    out.step("Checking Python")
    version = sys.version_info[:2]
    if version < MIN_PYTHON:
        out.fail(f"Python {version[0]}.{version[1]} is too old; 3.10+ is required.")
        return ""
    python = sys.executable
    if not python:
        out.fail("cannot determine this interpreter's path (sys.executable is empty).")
        return ""
    out.ok(f"{python} (Python {version[0]}.{version[1]})")
    return python


# --------------------------------------------------------------------------- #
# 2. dependency
# --------------------------------------------------------------------------- #


def _pip(python: str, args: list[str], out: Out) -> bool:
    proc = subprocess.run([python, "-m", "pip", *args],
                          capture_output=True, text=True)
    if proc.returncode == 0:
        return True
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    for line in tail[-4:]:
        out.warn(line)
    return False


def _importable(python: str) -> bool:
    """Ask a *fresh* interpreter, not this one.

    This process may have imported cryptography from a directory that is about
    to stop being on the path, and the question that matters is whether the
    interpreter registered in the MCP config can import it.
    """
    return subprocess.run(
        [python, "-c", "from cryptography.hazmat.primitives.ciphers.aead import AESGCM"],
        capture_output=True,
    ).returncode == 0


def is_source_checkout() -> bool:
    """Whether this module is running out of a clone rather than an install.

    `python -m link.install` is a legitimate entry point from both. From a
    clone there is a package to build; from site-packages the package is
    already there and pip must not be pointed at its own directory.
    """
    return os.path.isfile(os.path.join(LINK_ROOT, "pyproject.toml"))


def module_importable(python: str) -> bool:
    """Whether `python -m link.mcp_server` will resolve for this interpreter.

    Asked from a directory that is deliberately not the checkout. Run from the
    checkout, `import link` succeeds through the current directory whether or
    not anything is installed, and a false yes here registers an MCP server
    that works until the first time somebody starts it from somewhere else.
    """
    where = tempfile.mkdtemp(prefix="claude-link-probe-")
    try:
        return subprocess.run(
            [python, "-c", "import link.mcp_server, link.hook_notify"],
            capture_output=True, cwd=where,
        ).returncode == 0
    finally:
        shutil.rmtree(where, ignore_errors=True)


def install_package(python: str, out: Out, dev: bool = False) -> bool:
    """Install this repository, with its dependencies.

    A normal install by default, not an editable one. Editable used to be
    required because the MCP server and the hook were registered by absolute
    path into this working tree; they are registered as `-m` modules now, so the
    copy under site-packages is the install and the clone is free to be deleted.
    That is the difference between a skill and a checkout somebody has to keep.

    `--dev` restores the editable install, which is what you want when you are
    changing this code and not what you want when you are using it.

    The console script is the part worth the trouble either way. Without it
    every instruction has to read `python3 -m link.cli ...`, which only works
    from a clone of this repository -- and nobody is standing in this repository
    when they need `agent-link doctor`.
    """
    if not is_source_checkout():
        # Already installed: `python -m link.install` from site-packages. There
        # is nothing to build, and pointing pip at this directory would be
        # asking it to install its own output.
        out.step("Checking the dependency")
        if not _importable(python):
            for args in (["install", "cryptography>=42"],
                         ["install", "--break-system-packages", "cryptography>=42"]):
                if _pip(python, args, out):
                    break
        if not _importable(python):
            out.fail("cryptography is not importable; agent-link cannot encrypt without it.")
            out.plain(f"      {shell_quote(python)} -m pip install 'cryptography>=42'")
            return False
        out.ok("cryptography is importable, and agent-link is already installed")
        return True

    out.step("Installing agent-link and its dependency"
             + (" (editable: --dev)" if dev else ""))

    # A virtualenv has no user site directory, so --user is an error there
    # rather than a safety net; the active environment is the right target.
    in_venv = sys.prefix != sys.base_prefix
    scope: list[str] = [] if in_venv else ["--user"]
    what = ["-e", LINK_ROOT] if dev else [LINK_ROOT]

    attempts = [
        ["install", *scope, *what],
        # PEP 668: a distro-managed interpreter refuses writes without an
        # explicit override. Take it rather than leave the link unable to
        # encrypt, but say so, because it does touch an environment the distro
        # owns.
        ["install", *scope, "--break-system-packages", *what],
    ]
    installed = False
    for i, args in enumerate(attempts):
        if i:
            out.warn("pip refused the install (externally-managed environment?).")
            out.warn("Retrying with --break-system-packages.")
        if _pip(python, args, out):
            installed = True
            break

    if not installed:
        # The editable install is the convenience; the dependency is the
        # requirement. Fall back to just the dependency so the link works even
        # when packaging does not.
        out.warn("Editable install failed; installing the dependency on its own.")
        for args in (["install", *scope, "cryptography>=42"],
                     ["install", *scope, "--break-system-packages", "cryptography>=42"]):
            if _pip(python, args, out):
                break

    if not _importable(python):
        out.fail("cryptography is not importable; agent-link cannot encrypt without it.")
        out.plain("    Install it by hand, then re-run:")
        out.plain(f"      {shell_quote(python)} -m pip install 'cryptography>=42'")
        out.plain("    If your distro refuses to be written to, use a virtual environment:")
        out.plain("      python3 -m venv ~/.venvs/agent-link")
        out.plain("      . ~/.venvs/claude-link/bin/activate   # Windows: Scripts\\Activate.ps1")
        out.plain(f"      pip install -e {LINK_ROOT}")
        return False

    version = subprocess.run(
        [python, "-c", "import cryptography; print(cryptography.__version__)"],
        capture_output=True, text=True,
    ).stdout.strip()
    out.ok(f"cryptography {version}")

    if installed:
        script = shutil.which("agent-link")
        if script:
            out.ok(f"agent-link -> {script}")
        else:
            out.warn("`agent-link` is installed but not on PATH. Either add the")
            out.warn("scripts directory below to PATH, or use the long form:")
            out.warn(f"  {shell_quote(python)} -m link.cli doctor")
            out.warn(f"  scripts directory: {_scripts_dir(python)}")
    return True


def _scripts_dir(python: str) -> str:
    """Where pip just put the console script, so the message can name it."""
    code = (
        "import sysconfig, site, sys;"
        "print(sysconfig.get_path('scripts', 'nt_user' if sys.platform=='win32'"
        " else 'posix_user') if site.ENABLE_USER_SITE else"
        " sysconfig.get_path('scripts'))"
    )
    proc = subprocess.run([python, "-c", code], capture_output=True, text=True)
    return proc.stdout.strip() or "(unknown)"


# --------------------------------------------------------------------------- #
# 3. self-test
# --------------------------------------------------------------------------- #


def self_test(python: str, level: str, out: Out) -> None:
    """Prove the sealing path runs *here*, before anything is registered.

    The default is a smoke test rather than the suite. The suite takes a minute
    and a half and mostly revalidates code that CI already ran on six platforms;
    what it cannot tell you is whether *this machine* can do the one thing the
    link needs, which is derive a room key and seal a frame. scrypt at 128 MiB
    is the part that fails on small or locked-down boxes, so it is exercised on
    purpose rather than skipped for speed.
    """
    if level == "none":
        out.step("Skipping the self-test (--self-test none)")
        return

    if level == "smoke":
        out.step("Checking the crypto path (about 1 s)")
        code = (
            "import sys; sys.path.insert(0, %r)\n" % LINK_ROOT +
            "from link import crypto, identity\n"
            "from link.envelope import make_envelope, make_origin, open_and_verify, seal_frame\n"
            "keys = crypto.derive_room_from_invite(crypto.new_invite('install-check'))\n"
            "k = crypto.generate_device_key(); pub = crypto.public_bytes(k)\n"
            "me = identity.Identity(crypto.device_id_for(pub), pub, 'installer', 'cli', k)\n"
            "env = make_envelope('msg', keys.room_id, me.device_id, 1,\n"
            "                    make_origin(me), body={'text': 'ok'})\n"
            "assert open_and_verify(keys, seal_frame(keys, me, env))['body']['text'] == 'ok'\n"
            "print('sealed, signed, verified')\n"
        )
        proc = subprocess.run([python, "-X", "utf8", "-c", code],
                              capture_output=True, text=True, cwd=LINK_ROOT)
        if proc.returncode == 0:
            out.ok("a frame sealed, signed and verified on this machine")
        else:
            out.fail("the crypto path does not run here; the link will not send.")
            for line in (proc.stderr or "").strip().splitlines()[-6:]:
                out.plain(f"      {line}")
        return

    out.step("Running the test suite (about 90 s)")
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if level == "suite":
        # The git-channel cases spawn several hundred git processes. Left out
        # unless asked for, and said out loud rather than silently.
        env["CLAUDE_LINK_SKIP_GIT_TESTS"] = "1"
    proc = subprocess.run(
        [python, "-X", "utf8", "-W", "ignore::ResourceWarning",
         "-m", "unittest", "discover", "-s", "tests"],
        cwd=LINK_ROOT, env=env,
    )
    if proc.returncode == 0:
        out.ok("all tests passed")
    else:
        out.warn("tests failed (see above); installing anyway.")
    if level == "suite":
        out.warn("The git-channel tests were skipped. To run them:")
        # Two lines, not `&&`: Windows PowerShell 5.1 answers "The token '&&'
        # is not a valid statement separator in this version." Same family as
        # the bash line continuation `doctor` used to print.
        out.warn(f"  cd {shell_quote(LINK_ROOT)}")
        out.warn(f"  {shell_quote(python)} -m unittest tests.test_transport_git")


# --------------------------------------------------------------------------- #
# 4. skill
# --------------------------------------------------------------------------- #


# What the agent sees the skill called, and the directory it lives in.
# Renamed with the product; `install_skill` removes the old one so a
# machine that had the previous name does not end up offering both.
SKILL_DIR_NAME = "agent-link"
FORMER_SKILL_DIR_NAME = "claude-link"


def install_skill(skills_root: str, out: Out) -> None:
    # The directory the previous name used, if this machine has one. Left
    # behind it would sit in the agent's skill list beside the new one, saying
    # the same things under a name that no longer exists.
    former = os.path.join(skills_root, FORMER_SKILL_DIR_NAME)
    if os.path.isdir(former) and FORMER_SKILL_DIR_NAME != SKILL_DIR_NAME:
        shutil.rmtree(former, ignore_errors=True)
        out.ok(f"removed the old {FORMER_SKILL_DIR_NAME} skill")

    target = os.path.join(skills_root, SKILL_DIR_NAME)
    os.makedirs(target, exist_ok=True)
    landing = os.path.join(target, "SKILL.md")
    # Backed up like everything else this touches. The README says every file it
    # edits is backed up first, and `stale_skills` exists partly to notice a
    # hand-edited copy -- so the project expects people to edit these and was
    # overwriting the edit without keeping one.
    backup_once(landing, out)
    shutil.copyfile(SKILL_SRC, landing)
    out.ok(target)


def registration_problems() -> list[str]:
    """Agents that have agent-link's skill installed but no MCP server wired.

    Nothing ever checked this, and it is the next silent failure: the daemon is
    healthy, the git channel is green, the rooms are joined, `doctor` exits 0,
    and the agent has no `link_*` tools at all. Reachable without anybody doing
    anything wrong -- Claude Code rewriting `~/.claude.json` from its in-memory
    copy while the installer was running, a `claude mcp remove`, `--agent auto`
    guessing the wrong agent, or a hand-edited config.

    Keyed on the skill copy, because that is the durable evidence that somebody
    installed for this agent on purpose. An agent that was never set up is not
    broken.
    """
    problems: list[str] = []

    claude_skill = os.path.join(CLAUDE_HOME, "skills", SKILL_DIR_NAME, "SKILL.md")
    if os.path.isfile(claude_skill):
        entry = None
        try:
            with open(CLAUDE_JSON, encoding="utf-8") as fh:
                entry = (json.load(fh).get("mcpServers") or {}).get("agent-link")
        except (OSError, ValueError):
            entry = None
        if not entry:
            problems.append(
                "Claude Code has the agent-link skill installed but no MCP "
                f"server registered in {CLAUDE_JSON}, so it has no link_* tools")

    codex_skill = os.path.join(CODEX_HOME, "skills", SKILL_DIR_NAME, "SKILL.md")
    if os.path.isfile(codex_skill):
        try:
            with open(CODEX_CONFIG, encoding="utf-8") as fh:
                body = fh.read()
        except OSError:
            body = ""
        if "mcp_servers.agent-link" not in body:
            problems.append(
                "Codex has the agent-link skill installed but no MCP server "
                f"registered in {CODEX_CONFIG}, so it has no link_* tools")

    return problems


def skill_copies() -> list[str]:
    """Every installed `SKILL.md` this installer would have written.

    Only the ones that exist. An agent that never had a copy has not gone out
    of date, and saying otherwise would put a permanent warning in front of
    everybody running one agent, which is most people.
    """
    found: list[str] = []
    for home in (CLAUDE_HOME, CODEX_HOME):
        path = os.path.join(home, "skills", SKILL_DIR_NAME, "SKILL.md")
        if path not in found and os.path.isfile(path):
            found.append(path)
    return found


def _same_text(a: bytes, b: bytes) -> bool:
    """Content comparison with line endings kept out of it.

    Found on a real machine: of the two copies installed there, one was
    byte-wise different and word-for-word identical, because something on the
    way had written it CRLF. That is not out of date, it is Windows, and a
    warning that cannot be acted on is one people learn to scroll past.
    """
    return a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n")


def _packaged_skill() -> bytes | None:
    try:
        with open(SKILL_SRC, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def stale_skills() -> list[str]:
    """Installed copies that no longer match the one inside this package.

    Compared by content rather than by a stamped version, because the failure
    is that the file says something untrue, and a version number can only catch
    that if somebody remembered to bump it. Content also catches a hand-edited
    copy, which is the same problem wearing a different hat: the agent is
    reading something this package did not write.
    """
    current = _packaged_skill()
    if current is None:
        return []
    stale = []
    for path in skill_copies():
        try:
            with open(path, "rb") as fh:
                if not _same_text(fh.read(), current):
                    stale.append(path)
        except OSError:
            continue
    return stale


def refresh_skills() -> list[str]:
    """Re-copy this package's SKILL.md over every copy that already exists.

    Refreshes; does not install. Wiring up an agent the user never asked for is
    `link.install`'s job and needs the rest of what it does.
    """
    current = _packaged_skill()
    if current is None:
        return []
    done = []
    for path in skill_copies():
        try:
            shutil.copyfile(SKILL_SRC, path)
        except OSError:
            continue
        done.append(path)
    return done


# --------------------------------------------------------------------------- #
# 5 + 6. Claude Code
# --------------------------------------------------------------------------- #


def mcp_entry(python: str, as_module: bool = True) -> dict[str, Any]:
    return {
        "command": python,
        # `-m link.mcp_server` for the same reason the hook uses it: an absolute
        # path into the checkout means the checkout can never move again.
        "args": ["-X", "utf8"] + (["-m", "link.mcp_server"] if as_module
                                  else [MCP_SERVER]),
        # CLAUDE_LINK_AGENT_KIND is declared, never sniffed: the same server
        # binary also runs under Codex, which spawns it with a cleared
        # environment, so the installer that wired it up is the only honest
        # source of this answer.
        "env": {
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "CLAUDE_LINK_AGENT_KIND": "claude-code",
            # Pinned, for the same reason Codex's are. The hook's --home used to
            # come from the installing shell's environment while the server got
            # nothing and fell back to the default -- so running
            # `CLAUDE_LINK_HOME=...claude-link-codex ./install.sh`, which is the
            # incantation this project's own notes tell people to use, wired
            # Claude Code's server to one home and its hook to another. Both
            # sides now read the same constant.
            "CLAUDE_LINK_HOME": CLAUDE_LINK_HOME,
            "CLAUDE_LINK_CTRL_PORT": str(CLAUDE_CTRL_PORT),
        },
    }


def install_claude(python: str, skip_hook: bool, out: Out,
                   as_module: bool = True) -> None:
    out.step("Claude Code: installing the skill")
    install_skill(os.path.join(CLAUDE_HOME, "skills"), out)

    out.step("Claude Code: registering the MCP server (user scope)")

    def add_server(config: dict) -> None:
        servers = config.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcpServers is not an object")
        # The old key goes when the new one is written, or a machine that
        # had the previous name keeps two servers and the agent starts two
        # daemons against one home.
        servers.pop("agent-link", None)
        servers.pop("claude-link", None)
        servers["agent-link"] = mcp_entry(python, as_module)

    edit_json(CLAUDE_JSON, add_server, out)

    if skip_hook:
        out.step("Claude Code: skipping the notification hook (--skip-hook)")
        return

    out.step("Claude Code: wiring the notification hook")
    # The same constant the MCP entry above declares, not the ambient
    # environment. A hook and a server that disagree about which home they
    # belong to is the two-agents-one-inbox failure, arrived at from the other
    # end: the hook drains a different daemon's mail, marks it read, and prints
    # it into the wrong context.
    command = hook_command(python, CLAUDE_LINK_HOME, as_module)

    def add_hooks(settings: dict) -> None:
        hooks = settings.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("hooks is not an object")
        # UserPromptSubmit only fires when a person types, so on its own it
        # cannot deliver anything while the agent is mid-task -- which is the
        # case that matters when two agents work in parallel. PostToolUse fires
        # between the agent's own steps and is what closes that loop.
        for event in HOOK_EVENTS:
            hooks[event] = _without_our_hooks(hooks.get(event) or [], out) + [
                {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
            ]

    edit_json(CLAUDE_SETTINGS, add_hooks, out)


# --------------------------------------------------------------------------- #
# 5 + 6. Codex
# --------------------------------------------------------------------------- #
# Both the server and the hook live in config.toml, so they are one edit: one
# backup, one splice, one verification.
#
# The block is generated textually and spliced between markers rather than
# round-tripped through a parser: a parser would reformat the user's file and
# drop every comment in it, which is a rude thing to do to a config someone
# hand-wrote.

BEGIN = "# >>> agent-link (managed block; regenerated by the installer) >>>"
END = "# <<< agent-link (managed block) <<<"
SERVER_PATH = ("mcp_servers", "agent-link")
HOOK_PATHS = {("hooks", event) for event in HOOK_EVENTS}
_HEADER = re.compile(r"^(\[\[?)([^\]]*)\]\]?")


def _load_toml():
    try:
        import tomllib
        return tomllib
    except ModuleNotFoundError:            # 3.10 shipped without a TOML reader
        try:
            import tomli
            return tomli
        except ModuleNotFoundError:
            return None                    # verification is skipped, never faked


def _basic(text: str) -> str:
    """A TOML basic string. Backslashes matter: this also renders paths."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _literal(text: str) -> str:
    """A literal string keeps the command readable, but cannot hold a quote."""
    return "'" + text + "'" if "'" not in text else _basic(text)


def _key_path(raw: str) -> tuple[str, ...]:
    """Split a dotted TOML key into its parts, honouring quoted segments."""
    parts, current, quote = [], "", ""
    for ch in raw:
        if quote:
            if ch == quote:
                quote = ""
            else:
                current += ch
        elif ch in "\"'":
            quote = ch
        elif ch == ".":
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    parts.append(current.strip())
    return tuple(parts)


def _multiline_state(line: str, state: str) -> str:
    """Which multi-line delimiter, if any, is still open at the end of `line`.

    Tracked so that a line beginning with '[' inside a multi-line string is
    never mistaken for a table header and cut in half.
    """
    i = 0
    while i < len(line):
        if state:
            if line.startswith(state, i):
                state, i = "", i + 3
                continue
        elif line.startswith('"""', i) or line.startswith("'''", i):
            state, i = line[i:i + 3], i + 3
            continue
        i += 1
    return state


def _sections(text: str) -> list[tuple[tuple[str, ...] | None, bool, str]]:
    """[(key path or None, is_array_of_tables, chunk)] in file order.

    The first entry is whatever precedes the first table header.
    """
    rows: list[tuple[tuple[str, ...] | None, bool, str]] = []
    current: tuple[str, ...] | None = None
    is_array, buf, state = False, [], ""
    for line in text.splitlines(keepends=True):
        head = None if state else _HEADER.match(line)
        state = _multiline_state(line, state)
        if head is None:
            buf.append(line)
            continue
        rows.append((current, is_array, "".join(buf)))
        current, is_array, buf = _key_path(head.group(2)), head.group(1) == "[[", [line]
    rows.append((current, is_array, "".join(buf)))
    return rows


# What a managed block written under the previous name looks like. Recognised
# so that re-installing over one replaces it instead of appending a second
# server beside it, which would start two daemons against one home.
FORMER_BEGIN = "# >>> claude-link (managed block"
FORMER_END = "# <<< claude-link (managed block"


def _strip_markers(text: str) -> str:
    """Remove a previous run's managed block, markers included."""
    kept, skipping = [], False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not skipping and (stripped.startswith("# >>> agent-link")
                             or stripped.startswith(FORMER_BEGIN)):
            skipping = True
            continue
        if skipping:
            skipping = not (stripped.startswith("# <<< agent-link")
                            or stripped.startswith(FORMER_END))
            continue
        kept.append(line)
    return "".join(kept)


def _strip_ours(rows) -> str:
    """Drop hand-written or pre-marker agent-link entries, keep the rest.

    Table sections are dropped individually; a hook entry is dropped together
    with its nested hook children, because the marker that identifies it
    as ours lives in the child.
    """
    kept, i = [], 0
    while i < len(rows):
        key, is_array, chunk = rows[i]
        if key is not None and key[:2] == SERVER_PATH:
            i += 1
            continue
        if key in HOOK_PATHS and is_array:
            end, group = i + 1, chunk
            while end < len(rows):
                child = rows[end][0]
                if child is None or len(child) <= 2 or child[:2] != key:
                    break
                group += rows[end][2]
                end += 1
            if HOOK_MARKER in group:
                i = end
                continue
        kept.append(chunk)
        i += 1
    return "".join(kept)



def _without_our_hooks(blocks: list, out: Out) -> list:
    """Drop our own hook entries, and nothing that merely sits beside one.

    Claude Code groups several commands under one `matcher`, and this used to
    test the whole *block* for the marker, so one match took every command in
    that block with it. A user with their own `my_hook_notify.py` next to a
    linter lost the linter too, silently -- and the guard meant to catch that
    applied the same filter to both sides of its comparison, so it saw nothing
    and passed.

    Anything of somebody else's that is removed is now reported rather than
    quietly restored, because a block we cannot understand is not one to
    rewrite on their behalf.
    """
    kept = []
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("hooks"), list):
            # Not a shape we understand: leave it exactly as it is.
            if HOOK_MARKER in json.dumps(block):
                continue
            kept.append(block)
            continue
        inner = [h for h in block["hooks"]
                 if HOOK_MARKER not in json.dumps(h)]
        if len(inner) == len(block["hooks"]):
            kept.append(block)
            continue
        if inner:
            # Ours went; the rest of the group stays where it was.
            kept.append({**block, "hooks": inner})
        elif HOOK_MARKER not in json.dumps(block):
            kept.append(block)
    return kept


def render_codex_block(python: str, want_hook: bool,
                       link_home: str = CODEX_LINK_HOME,
                       ctrl_port: str = CODEX_CTRL_PORT,
                       as_module: bool = True) -> str:
    server_args = ["-X", "utf8"] + (["-m", "link.mcp_server"] if as_module
                                    else [MCP_SERVER])
    lines = [
        BEGIN,
        "[mcp_servers.agent-link]",
        "command = " + _basic(python),
        "args = [" + ", ".join(_basic(a) for a in server_args) + "]",
        "# Codex applies a per-tool deadline -- 60 s as configured by default --",
        "# which would abort link_wait mid-call. 660 s sits above the server's own",
        "# 605 s worst case, so our deadline always fires first and Codex sees an",
        "# ordinary empty result instead of a tool failure.",
        "tool_timeout_sec = 660",
        "",
        "[mcp_servers.agent-link.env]",
        'PYTHONUTF8 = "1"',
        'PYTHONIOENCODING = "utf-8"',
        "# Codex spawns stdio MCP servers with a cleared environment, so this",
        "# cannot be inherited from the shell -- declaring it here is the only",
        "# way the server learns which agent is driving it.",
        'CLAUDE_LINK_AGENT_KIND = "codex"',
        "# Its own home and port. Sharing Claude Code's would make the two agents",
        "# one device and one room member: every send would leave the machine and",
        "# never be echoed locally, so both would report a healthy room and hear",
        "# nothing from each other.",
        "CLAUDE_LINK_HOME = " + _basic(link_home),
        "CLAUDE_LINK_CTRL_PORT = " + _basic(ctrl_port),
    ]
    if want_hook:
        # Top level, not under mcp_servers: the hook is Codex's, not the
        # server's, and nesting it would make Codex ignore it silently.
        command = hook_command(python, link_home, as_module)
        for event in HOOK_EVENTS:
            lines += [
                "",
                "[[hooks.%s]]" % event,
                "[[hooks.%s.hooks]]" % event,
                'type = "command"',
                "command = " + _literal(command),
                "timeout = 5",
            ]
    return "\n".join(lines + [END]) + "\n"


def _without_ours(doc: dict) -> dict:
    """The document minus everything this installer owns."""
    doc = copy.deepcopy(doc)
    servers = doc.get("mcp_servers")
    if isinstance(servers, dict):
        servers.pop("agent-link", None)
        servers.pop("claude-link", None)
        if not servers:
            doc.pop("mcp_servers", None)
    hooks = doc.get("hooks")
    if isinstance(hooks, dict):
        for event in HOOK_EVENTS:
            if not isinstance(hooks.get(event), list):
                continue
            others = [e for e in hooks[event] if HOOK_MARKER not in repr(e)]
            if others:
                hooks[event] = others
            else:
                hooks.pop(event)
        if not hooks:
            doc.pop("hooks", None)
    return doc


def splice_codex_config(original: str, python: str, want_hook: bool,
                        link_home: str = CODEX_LINK_HOME,
                        ctrl_port: str = CODEX_CTRL_PORT,
                        as_module: bool = True) -> str:
    """The new config.toml text: everything that is not ours, then our block."""
    body = re.sub(r"\n{3,}", "\n\n",
                  _strip_ours(_sections(_strip_markers(original)))).strip()
    block = render_codex_block(python, want_hook, link_home, ctrl_port, as_module)
    return (body + "\n\n" if body else "") + block


def install_codex(python: str, skip_hook: bool, out: Out,
                  as_module: bool = True) -> None:
    out.step("Codex: installing the skill")
    # Codex loads skills on demand, exactly as Claude Code does, so the same
    # SKILL.md serves both. AGENTS.md is deliberately left alone: it is Codex's
    # always-on context, and nothing that only matters once a room exists
    # belongs in every prompt.
    install_skill(os.path.join(CODEX_HOME, "skills"), out)

    out.step("Codex: registering the MCP server"
             + ("" if not skip_hook else " (no notification hook: --skip-hook)"))

    want_hook = not skip_hook
    tomllib = _load_toml()

    original, before = "", {}
    if os.path.exists(CODEX_CONFIG):
        with open(CODEX_CONFIG, encoding="utf-8") as fh:
            original = fh.read()
        if tomllib is not None:
            try:
                before = tomllib.loads(original)
            except Exception as exc:
                out.fail(f"config.toml is not valid TOML; leaving it alone. ({exc})")
                return

    new_text = splice_codex_config(original, python, want_hook,
                                   as_module=as_module)

    # Refuse to write anything that does not parse, does not carry the
    # load-bearing settings, or would disturb configuration that is not ours.
    if tomllib is None:
        out.warn("No TOML reader on this interpreter (Python 3.10); written unverified.")
    else:
        try:
            after = tomllib.loads(new_text)
        except Exception as exc:
            out.fail(f"the edit would not parse as TOML; leaving config.toml alone. ({exc})")
            out.plain("        Remove any hand-written agent-link entry and re-run.")
            return
        entry = after.get("mcp_servers", {}).get("agent-link", {})
        hook_counts = {
            event: len([e for e in after.get("hooks", {}).get(event, [])
                        if HOOK_MARKER in repr(e)])
            for event in HOOK_EVENTS
        }
        if (entry.get("command") != python
                or entry.get("tool_timeout_sec") != 660
                or entry.get("env", {}).get("CLAUDE_LINK_AGENT_KIND") != "codex"
                or entry.get("env", {}).get("CLAUDE_LINK_HOME") != CODEX_LINK_HOME
                or entry.get("env", {}).get("CLAUDE_LINK_CTRL_PORT") != CODEX_CTRL_PORT
                or any(count != (1 if want_hook else 0)
                       for count in hook_counts.values())):
            out.fail("the generated block came out wrong; leaving config.toml alone.")
            return
        if _without_ours(before) != _without_ours(after):
            out.fail("the edit would have changed config that is not ours; stopping.")
            out.plain(f"        Move the agent-link entries out of {CODEX_CONFIG} and re-run.")
            return

    os.makedirs(os.path.dirname(CODEX_CONFIG) or ".", exist_ok=True)
    backup = backup_once(CODEX_CONFIG, out)
    write_text(CODEX_CONFIG, new_text)
    out.ok(f"{CODEX_CONFIG}{' (backup: ' + BACKUP_SUFFIX + ')' if backup else ''}")


# --------------------------------------------------------------------------- #
# 7. diagnostics
# --------------------------------------------------------------------------- #


def diagnostics(python: str, agents: list[str], out: Out) -> bool:
    """Run `doctor` for each agent. False if any of them is unhappy.

    The return value is the point. Every installer here used to discard it and
    print "Installed. Next steps:" underneath, which on a fresh machine sits
    directly below `doctor` saying "nothing is configured to carry messages, so
    no room can reach anyone" -- and the next steps did not mention configuring
    one. It also buried a real `daemon: FAILED` under the same cheerful banner.
    """
    out.step("Diagnostics")
    healthy = True
    for agent in agents:
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        env["CLAUDE_LINK_HOME"] = (CODEX_LINK_HOME if agent == "codex"
                                   else CLAUDE_LINK_HOME)
        env["CLAUDE_LINK_CTRL_PORT"] = (CODEX_CTRL_PORT if agent == "codex"
                                        else str(CLAUDE_CTRL_PORT))
        if len(agents) > 1:
            out.plain(f"  --- {agent} ---")
        proc = subprocess.run([python, "-X", "utf8", "-m", "link.cli", "doctor"],
                              cwd=LINK_ROOT, env=env)
        healthy = healthy and proc.returncode == 0
    return healthy


# --------------------------------------------------------------------------- #
# agent detection
# --------------------------------------------------------------------------- #


def detect_agents() -> list[str]:
    """Which host applications look installed on this machine."""
    found = []
    if os.path.isdir(CLAUDE_HOME) or os.path.exists(CLAUDE_JSON):
        found.append("claude")
    if os.path.isdir(CODEX_HOME) or shutil.which("codex"):
        found.append("codex")
    return found


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        # The same fix `cli.py` got: `prog` is in every usage line and every
        # error, and after a `pipx install` there is no importable `link` for
        # `python3 -m link.install` to find.
        prog=f"{cli_invocation()} install",
        description="Install agent-link for Claude Code and/or Codex.",
    )
    p.add_argument(
        "--agent", choices=("auto", "claude", "codex", "both"), default="auto",
        help="which host application to install for (default: whatever is on this machine)",
    )
    p.add_argument(
        "--skip-hook", action="store_true",
        help="do not wire the notification hook; incoming messages then reach the "
             "agent only when it calls link_inbox itself",
    )
    p.add_argument(
        "--self-test", choices=("smoke", "suite", "all", "none"), default="smoke",
        help="smoke: seal and verify one frame (default). suite: the test suite "
             "without the slow git cases. all: everything. none: skip it.",
    )
    p.add_argument(
        "--no-diagnostics", action="store_true",
        help="do not run `doctor` at the end; it starts a daemon, which an "
             "unattended install may not want",
    )
    p.add_argument(
        "--dev", action="store_true",
        help="install editable, so edits in this checkout take effect without "
             "reinstalling. For working on agent-link, not for using it: it "
             "makes this directory load-bearing.",
    )
    p.add_argument("--quiet", action="store_true", help="only print problems")
    return p


def next_steps(python: str, agents: list[str], out: Out,
               healthy: bool = True) -> None:
    """What to do once the install is written.

    Its own function because it is the most-read output this program has and
    every line of it is about to be pasted, so it needs to be reachable from a
    test. The branch advice in particular: a rename sweep once turned
    `branches-ignore: [claude-link]` into the *product* name, and it went out
    in a real install before anybody read the output. Advice to ignore a branch
    that does not exist is worse than none, because it looks actionable while
    the heartbeat keeps triggering CI about 1900 times a day.
    """
    cli = cli_command(python)
    restart = " and ".join(
        {"claude": "your editor", "codex": "Codex"}[a] for a in agents
    )
    out.plain(f"{out.c['step']}Next:{out.c['off']}")
    # doctor is unhappy on every fresh machine, for exactly one reason: nothing
    # is configured to carry a message yet. Saying "Installed" over the top of
    # that, with next steps that never mention it, is how somebody ends up in a
    # room that cannot reach anybody.
    #
    # Three lines, every one a command that runs on this machine, and nothing
    # else. The CI warning that used to live here now fires from
    # `config --set git_remote=...` itself, at the moment of the decision;
    # `config --set` also says when a room is already open on that repo, and
    # rooms themselves ask for a name if these lines are run out of order.
    lead = ("Nothing can carry a message yet: see the diagnostics above."
            if not healthy else
            f"Point it at the repo you already share, then restart {restart}:")
    out.plain(f"  {lead}")
    out.plain(f'    {cli} config --set git_remote="<the repo you already share>"')
    out.plain(f"    {cli} name <your-name>")
    out.plain(f"    {cli} doctor")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = Out(args.quiet)

    if args.agent == "auto":
        agents = detect_agents()
        if not agents:
            out.warn("Neither ~/.claude nor ~/.codex found; installing for Claude Code.")
            out.warn("Pass --agent codex if that is the wrong guess.")
            agents = ["claude"]
    elif args.agent == "both":
        agents = ["claude", "codex"]
    else:
        agents = [args.agent]

    out.plain(f"agent-link installer  ({LINK_ROOT})")
    out.plain(f"installing for: {', '.join(agents)}")
    out.plain()

    if not check_layout(out):
        return 1
    python = check_python(out)
    if not python:
        return 1
    if not install_package(python, out, dev=args.dev):
        return 1
    self_test(python, args.self_test, out)

    # Which form the MCP server and the hook get registered in. The module form
    # is the point of the whole exercise -- nothing on disk has to survive -- but
    # it is only honest if the package really did install, and `install_package`
    # has a fallback path that installs the dependency alone. Registering `-m`
    # after that would produce a configuration that looks right and starts
    # nothing, so the absolute path stays as the answer for exactly that case.
    as_module = module_importable(python)
    if not as_module:
        out.warn("The package did not install, so the MCP server and hook are")
        out.warn(f"registered by path into {LINK_ROOT}. Do not move or delete it.")

    if "claude" in agents:
        # The MCP env carries this home's control port, but the CLI reads
        # config.json. Seed it so both agree.
        seed_home_ctrl_port(CLAUDE_LINK_HOME, CLAUDE_CTRL_PORT, out)
        install_claude(python, args.skip_hook, out, as_module)
    if "codex" in agents:
        seed_home_ctrl_port(CODEX_LINK_HOME, int(CODEX_CTRL_PORT), out)
        install_codex(python, args.skip_hook, out, as_module)

    healthy = True
    if not args.no_diagnostics:
        healthy = diagnostics(python, agents, out)

    out.plain()
    if out.problems:
        out.plain(f"{out.c['fail']}Finished with {len(out.problems)} problem(s):{out.c['off']}")
        for problem in out.problems:
            out.plain(f"  - {problem}")
        out.plain()

    next_steps(python, agents, out, healthy=healthy)
    if as_module and is_source_checkout() and not args.dev:
        out.plain()
        out.plain("Nothing points back at this directory any more, so this checkout is")
        out.plain("no longer needed and can be deleted. Re-install with --dev if you")
        out.plain("mean to work on agent-link itself.")
    return 1 if out.problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
