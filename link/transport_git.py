"""Transport over a git repository -- in practice, a GitHub repo.

The same problem the shared folder solves, for people who do not share a folder.
Two machines on different networks, neither with an open inbound port, both able
to reach github.com: the repo is the thing in the middle, and there is still
nothing to deploy or pay for.

**This is `transport_file.py` with a sync client bolted on, deliberately.** A
`GitTransport` *is* a `FileTransport` pointed at a local clone instead of at
OneDrive, plus a loop that commits, fetches, rebases and pushes. Everything the
folder transport got right is inherited rather than rewritten: one copy per
recipient under `out/<sender>/<recipient>/`, exactly one writer and one
destructive reader per directory, heartbeat files for presence, sender-side
garbage collection. Reimplementing that against git would have meant getting the
three-member fan-out invariant right a second time, and the first time took a
rewrite.

Inside the repo, on the configured branch:

    claude-link/<room_id>/out/<sender>/<recipient>/<ts_ms>-<msg_id>.json
    claude-link/<room_id>/presence/<device_id>.json

The branch defaults to `agent-link` and is created as a **root** commit with no
parent, so pointing this at a repository that also holds code adds an orphan
branch beside it and touches nothing else. A repository that exists only to be a
channel works exactly the same way.

## Why conflicts are rare, and what happens when they are not anyway

Every device writes only paths it exclusively owns -- its own `out/<me>/**` and
its own `presence/<me>.json` -- and deletes only frames addressed to it, which
nobody else will ever delete first. Two devices therefore change disjoint paths,
and a rebase is a fast-forward in all but name.

That is an argument, not a guarantee, and the folder transport's own history is
the reason not to trust one. So a conflict has a defined outcome instead of an
exception: copy our recent outbound frames aside, `reset --hard` to the remote
tip, put them back, commit. The frames we had already consumed reappear in that
reset -- and are swallowed by the `_seen` set inherited from `FileTransport`,
which exists for exactly this ("a delete that silently failed"), with `msg_id`
admission in `room.py` behind it as a second net. Recovery therefore costs a
redundant read, never a duplicated message and never a lost one.

The same path handles a rewritten history: `git-prune` squashes the branch to a
single root commit and force-pushes it, and every other member sees a tip with
no common ancestor, which is not a conflict git can resolve and is the one case
where reset-and-restore is not a fallback but the correct answer.

## What this costs

A commit per sync round that has something to say, and a heartbeat that has to
land in a commit of its own to be seen. `git_presence_s` is 45 seconds rather
than the folder's 5 for that reason alone: an idle room should not write a
thousand commits a day. History still grows, and nothing in here truncates it
automatically -- rewriting shared history is not a thing to do behind somebody's
back. `agent-link git-prune` does it when asked.

## What the host can see

The repository holds ciphertext and nothing else, so GitHub cannot read a
message. It can read the routing: which device ids exist, who writes to whom,
how often, and at what times -- the same social graph a relay sees, except that
git history keeps it after the fact and a public repo publishes it to everyone.
Use a private repo. `doctor` says so if it can tell.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

from .transport_file import (
    FileTransport,
    RETENTION_S,
    _queued_at_ms,
    out_root,
)
from .util import now_ms

# The subtree this transport owns inside the repository. Everything git is told
# to stage is scoped to it, so a branch shared with other content is never
# swept up by an `add -A`.
REPO_SUBDIR = "claude-link"

DEFAULT_BRANCH = "claude-link"
DEFAULT_SYNC_MS = 3000
DEFAULT_PRESENCE_S = 45.0

GIT_LOCAL_TIMEOUT_S = 30.0        # add, commit, rebase: no network involved
GIT_NET_TIMEOUT_S = 60.0          # fetch, push, ls-remote
PUSH_ATTEMPTS = 5                 # a lost race costs one more fetch-rebase round
SYNC_DEBOUNCE_S = 0.15            # let a burst of sends batch into one commit
LOCK_STALE_S = 120.0              # an index.lock older than this is from a corpse

# How far back a recovery reaches when putting our outbound frames back. Older
# copies have almost certainly been collected already, and restoring one only
# makes the recipient read and discard it again. Ten minutes is well past any
# plausible sync round and well short of the retention window.
RESTORE_WINDOW_MS = 10 * 60 * 1000

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_UNSAFE_IN_SLUG = re.compile(r"[^A-Za-z0-9_.-]")

# Push failures worth retrying: somebody else's push landed between our fetch
# and ours. Anything else -- no credentials, no network, no such repo -- will
# fail identically five times, so it is raised on the first attempt instead.
_RACE_MARKERS = (
    "non-fast-forward",
    "fetch first",
    "failed to push some refs",
    "cannot lock ref",
    "reference already exists",
    "stale info",
)


# `git add` touches a file three times -- it walks the directory, stats what it
# found, then opens and reads it -- and the working tree can move between any
# two of those. `_sync_once` stages from a worker thread while delete-on-read
# removes collected frames and the heartbeat is rewritten on the event loop, so
# they interleave, and git has a different complaint for each gap:
#
#   gone before the stat   fatal: unable to stat '<path>': No such file...
#   gone before the open   error: open("<path>"): No such file or directory
#   replaced before read   error: short read while indexing <path>
#
# The first is `die_errno`, so `--ignore-errors` cannot carry the staging past
# it and only starting the `add` again can. All three are the same event and
# all three are harmless: the tree has moved on, and the next attempt sees the
# state that replaced it.
_ADD_RACES = (
    re.compile(r"^fatal: unable to stat '.+': No such file or directory$"),
    re.compile(r'^error: open\(".+"\): No such file or directory$'),
    re.compile(r"^error: short read while indexing .+$"),
)
# git prints this beside any of the above, naming the file it gave up on. On its
# own it says nothing about why, so it is tolerated rather than matched.
_ADD_UNINDEXED = re.compile(r"^error: unable to index file .+$")

# Three, because the set of frames being collected is bounded and a second look
# almost always finds the tree still. Losing all three means something is
# rewriting the subtree continuously, which is not a thing to keep retrying
# inside one round.
ADD_ATTEMPTS = 3


# The same race, one step further on. `_commit_local` stages and commits from a
# worker thread; delete-on-read and the heartbeat rewrite keep running on the
# event loop, so the subtree can move again in the moment between that commit
# and the rebase underneath it. `git rebase` then refuses to start:
#
#   worktree moved   error: cannot rebase: You have unstaged changes.
#   index moved      error: cannot rebase: Your index contains uncommitted changes.
#
# It exits 1 for this, which is also what a real conflict exits, and reading the
# two as one is expensive: `_integrate` answered both by rebuilding the clone
# from the remote. That is a copy of every unsent frame out, a `reset --hard`, a
# `clean -qfd`, and only the last ten minutes of frames put back. Ordinary
# housekeeping was being answered with the recovery path reserved for a branch
# somebody had squashed -- five times in forty seconds on an idle two-member
# channel, on a developer machine.
#
# It also made `recoveries` useless as a signal, which is the part that hid it:
# the counter meant to say "somebody rewrote history" was mostly counting this.
_REBASE_DIRTY = (
    re.compile(r"^error: cannot rebase: You have unstaged changes\.$"),
    re.compile(r"^error: cannot rebase: Your index contains uncommitted changes\.$"),
)
# git prints this under either of the above. It names no cause, so it is
# tolerated rather than matched -- the same treatment as `_ADD_UNINDEXED`.
_REBASE_DIRTY_ADVICE = re.compile(r"^error: Please commit or stash them\.$")

# Three, for the reason ADD_ATTEMPTS is three: committing what moved settles it,
# unless something is rewriting the subtree continuously, and that is a state to
# recover from rather than to keep retrying inside one round.
REBASE_ATTEMPTS = 3


def _rebase_race(text: str) -> str | None:
    """git's complaint that the tree moved under the rebase, or None.

    None for anything unrecognised, on `_add_race`'s reasoning: a real conflict,
    a corrupt index and a non-zero exit with nothing to say must all reach
    `_recover`, not be retried into it.
    """
    found = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = next((p.match(line) for p in _REBASE_DIRTY if p.match(line)), None)
        if match:
            found = found or match.group(0)
        elif not _REBASE_DIRTY_ADVICE.match(line):
            return None
    return found


def _add_race(text: str) -> str | None:
    """The concurrent modification git complained about, or None.

    None means "this was not just the tree moving", and that is the answer for
    anything unrecognised: a corrupt index, a pathspec that matched nothing, a
    permission the clone does not have. Empty output is None as well -- a
    non-zero exit with nothing to say is a failure nobody has explained, and
    treating silence as routine is how the next one goes unnoticed for a month.
    """
    found = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = next((p.match(line) for p in _ADD_RACES if p.match(line)), None)
        if match:
            found = found or match.group(0)
        elif not _ADD_UNINDEXED.match(line):
            return None
    return found


class GitError(RuntimeError):
    """A git command failed, with its own stderr as the message."""


# --------------------------------------------------------------------------- #
# running git
# --------------------------------------------------------------------------- #


def _git_env() -> dict[str, str]:
    """An environment where git fails instead of asking a human something.

    There is no terminal behind the daemon, so every interactive path is a hang
    rather than a prompt: a credential helper waiting on a GUI, `ssh` asking for
    a passphrase, a pager that never exits. Each of those is disabled here and
    the subprocess timeout is what catches whatever is left.

    The user's credential configuration is deliberately *not* cleared. `gh auth
    setup-git`, a stored PAT, an ssh-agent -- whichever of those is already
    working for `git push` in a terminal is the thing that has to keep working
    here, and reaching around it would mean inventing a second way to hold a
    GitHub credential.
    """
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GCM_INTERACTIVE": "never",
        "SSH_ASKPASS_REQUIRE": "never",
        "LC_ALL": "C",
    })
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    # Inherited from a shell that was inside another repository, these would
    # silently redirect every command below at that one.
    for stray in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_COUNT",
        # Identity, which outranks the repo-local `user.name` and `user.email`
        # this transport sets. Left in place, every commit on the channel branch
        # carried the operator's real name and work email to the git host and to
        # every other room member -- and the point of the pseudonymous committer
        # is that it does not.
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
        "EMAIL",
    ):
        env.pop(stray, None)
    return env


def run_git(
    args: list[str],
    cwd: str | None = None,
    timeout: float = GIT_LOCAL_TIMEOUT_S,
    check: bool = True,
) -> tuple[int, str, str]:
    """Run one git command. Blocking; callers hand it to a worker thread.

    Returns `(returncode, stdout, stderr)`. With `check=True` a non-zero exit
    raises `GitError` carrying git's own stderr, which is almost always the
    sentence a user needs to see.
    """
    if cwd is not None and not os.path.isdir(cwd):
        # Otherwise `subprocess` raises FileNotFoundError for a missing cwd and
        # the handler below reports it as git not being installed, which sends
        # whoever reads it a very long way in the wrong direction.
        raise GitError(f"{cwd} is not a directory")
    try:
        proc = subprocess.run(
            # The allow-list is second-line defence behind `check_remote`. git's
            # helper transports (`ext::`, and anything else outside this set)
            # execute commands, so they are switched off at the source rather
            # than merely refused at the door.
            ["git", "-c", "protocol.allow=never",
             "-c", "protocol.https.allow=always",
             "-c", "protocol.http.allow=always",
             "-c", "protocol.ssh.allow=always",
             "-c", "protocol.git.allow=always",
             "-c", "protocol.file.allow=always",
             "-c", "protocol.ext.allow=never",
             *args],
            cwd=cwd,
            env=_git_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed, or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(
            f"git {args[0]} timed out after {timeout:.0f}s -- the remote did not "
            f"answer, or something is waiting for a credential"
        ) from exc

    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])}: {_first_line(err or out)}")
    return proc.returncode, out, err


def git_version() -> str | None:
    try:
        _rc, out, _err = run_git(["--version"], timeout=10.0)
        return out.replace("git version", "").strip() or out
    except GitError:
        return None


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


class BadRemote(ValueError):
    """A remote URL this will not hand to git."""


# https/ssh/git URLs, the scp-style shorthand, or a local path. Anything else is
# refused rather than passed through and hoped about.
_URL_REMOTE = re.compile(r"\A(https?|ssh|git)://[^\s]+\Z", re.I)
_SCP_REMOTE = re.compile(r"\A[A-Za-z0-9._~-]+@[A-Za-z0-9._-]+:[^\s]+\Z")


_BRANCH_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$")
# Branch names people use for their own code. Pointing the channel at one is
# never what somebody meant, and the consequences are not recoverable by
# undoing the setting.
_CODE_BRANCHES = frozenset({
    "main", "master", "trunk", "develop", "dev", "release", "stable",
    "production", "prod", "gh-pages", "HEAD",
})


def check_branch(branch: str | None) -> str:
    """The branch the channel commits to. Never one of somebody's code branches.

    Nothing validated this, while the remote beside it was checked carefully.
    `config --set git_branch=main` -- also reachable from `CLAUDE_LINK_GIT_BRANCH`
    and from the control socket's join -- put presence heartbeats and sealed
    frames straight onto the user's `main`, and then `git-prune` compacted that
    branch's entire history into a single orphan commit and force-pushed it,
    with the lease satisfied because the daemon's own heartbeat was the tip.
    Three commits of somebody's work, gone from the remote.

    Refused rather than warned about: a warning printed during setup is read
    once, and this destroys history.
    """
    name = (branch or DEFAULT_BRANCH).strip()
    if not _BRANCH_OK.match(name) or ".." in name or name.endswith(".lock"):
        raise BadRemote(f"not a usable branch name: {name!r}")
    if name in _CODE_BRANCHES or name.lower() in _CODE_BRANCHES:
        raise BadRemote(
            f"refusing to run the channel on {name!r}: that is a branch people "
            f"keep code on, and this writes to it every heartbeat and rewrites "
            f"it on `git-prune`. Use the default, {DEFAULT_BRANCH!r}, or another "
            f"branch nothing else uses")
    return name


def check_remote(remote: str) -> str:
    """Validate a remote before it reaches git. Raises BadRemote.

    git's remote *helpers* are the reason this exists. `ext::sh -c '<cmd>'` is
    not a URL, it is a command git runs, and `protocol.ext.allow` defaults to
    `user` -- permitted for exactly the kind of direct invocation this makes. So
    `git_remote` was a code-execution primitive, reachable through `link_join`,
    through `config --set`, and through anything that could talk to the control
    socket. Belt and braces: `run_git` also pins the protocol allow-list, so a
    scheme that slips past this still cannot start a helper.

    A leading `-` is refused for the ordinary reason: git would read it as an
    option, not as a remote.
    """
    if not isinstance(remote, str):
        raise BadRemote("git remote must be a string")
    value = remote.strip()
    if not value:
        raise BadRemote("git remote must not be empty")
    if any(ch in value for ch in "\x00\n\r\t"):
        raise BadRemote("git remote contains a control character")
    if value.startswith("-"):
        raise BadRemote("git remote must not start with '-'; git would read it as an option")
    if "::" in value:
        raise BadRemote(
            "git remote uses a remote helper (the '::' form), which can run "
            "arbitrary commands; use an https, ssh or git URL"
        )
    if _URL_REMOTE.match(value) or _SCP_REMOTE.match(value):
        return value
    # An absolute path is a usable remote shape whether or not it exists today.
    # Deliberately not `isdir`: "the repo is not there" is a *reachability*
    # problem, and reachability is reported as `setup_error` on a room that came
    # up anyway. Refusing it here would turn a mistyped path into a room that
    # never starts, which is the failure `_attach_git` exists to prevent.
    if value.startswith("//") or value.startswith("\\\\"):
        # `os.path.isabs` says yes to a UNC path on Windows, and Windows then
        # authenticates to whatever host it names. The remote is settable from
        # `link_join`, from `config --set` and from the control socket, so this
        # is somebody else choosing who your machine logs in to.
        raise BadRemote(
            f"refusing a UNC path as a git remote: {redact_remote(value)!r}. "
            "Windows resolves it over SMB and authenticates to whoever it names")
    if os.path.isabs(value):
        return value
    raise BadRemote(
        f"not a usable git remote: {redact_remote(value)!r}. Expected "
        "https://host/path, ssh://host/path, user@host:path, or an absolute "
        "path to a repository on this machine"
    )


def redact_remote(remote: str | None) -> str:
    """A remote URL safe to print, log and put in a model's context.

    `https://<user>:<token>@github.com/...` is a perfectly ordinary way to
    configure this and a perfectly ordinary way to leak a personal access token
    into a transcript, a status line and everything downstream of them.
    """
    if not remote:
        return ""
    if "://" not in remote:
        return remote                       # scp-style or a local path: no userinfo
    try:
        parts = urlsplit(remote)
    except ValueError:
        return "<unparseable remote>"
    if not parts.username and not parts.password:
        return remote
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"***@{host}", parts.path, "", ""))


# --------------------------------------------------------------------------- #
# where the clone lives
# --------------------------------------------------------------------------- #


def clone_dir(base: str, remote: str, branch: str) -> str:
    """The local checkout for one (remote, branch), under the link's own home.

    Never inside a directory the user works in. The working tree is machinery --
    it is written to every few seconds, reset when a history is rewritten, and
    would be a confusing thing to find in a projects folder.

    The readable half of the name is for whoever goes looking; the digest is
    what makes it unique, because two remotes can easily end in the same repo
    name and two branches on one remote must not share a checkout.
    """
    digest = hashlib.sha256(f"{remote}\n{branch}".encode("utf-8")).hexdigest()[:12]
    tail = remote.rstrip("/").rsplit("/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    tail = _UNSAFE_IN_SLUG.sub("-", tail).strip("-")[:32] or "repo"
    return os.path.join(base, "git", f"{tail}-{digest}")


# --------------------------------------------------------------------------- #
# the transport
# --------------------------------------------------------------------------- #

# One lock per working tree. Rooms get a transport each and they all share one
# clone, so without this two rooms starting together race for `.git/index.lock`
# and one of them loses with an error that reads like a broken repository.
_REPO_LOCKS: dict[str, asyncio.Lock] = {}


def _repo_lock(workdir: str) -> asyncio.Lock:
    key = os.path.normcase(os.path.abspath(workdir))
    lock = _REPO_LOCKS.get(key)
    if lock is None:
        lock = _REPO_LOCKS[key] = asyncio.Lock()
    return lock


class GitTransport(FileTransport):
    """One room's view of one git branch, for one device.

    Reads and writes exactly as `FileTransport` does, against a local clone, and
    runs a second loop that keeps that clone level with the remote. The two are
    deliberately independent: the inherited poll is local-disk cheap and stays at
    250 ms, while the sync round is a network round trip and runs on its own,
    slower clock.
    """

    def __init__(
        self,
        remote: str,
        branch: str,
        workdir: str,
        room_id: str,
        device_id: str,
        on_frame: Callable[[dict[str, Any]], Awaitable[None]],
        poll_ms: int = 250,
        sync_ms: int = DEFAULT_SYNC_MS,
        presence_s: float = DEFAULT_PRESENCE_S,
        depth: int = 0,
        retention_s: float = RETENTION_S,
        log: Callable[[str], None] | None = None,
        presence: bool = True,
    ) -> None:
        super().__init__(
            shared_dir=workdir,
            room_id=room_id,
            device_id=device_id,
            on_frame=on_frame,
            poll_ms=poll_ms,
            retention_s=retention_s,
            log=log,
            presence=presence,
        )
        # A heartbeat is a commit here, not a file write. Refreshing it every
        # five seconds would fill the branch with commits that say nothing.
        self.presence_refresh_s = max(5.0, float(presence_s))

        # Checked here as well as at the boundary that set it, so the transport
        # is safe on its own terms rather than because every caller remembered.
        self.remote = check_remote(remote)
        self.branch = check_branch(branch)
        self.workdir = workdir
        self.sync_s = max(0.5, sync_ms / 1000.0)
        self.depth = max(0, int(depth))

        self._lock = _repo_lock(workdir)
        self._sync_task: asyncio.Task | None = None
        self._sync_stop = asyncio.Event()
        self._nudge = asyncio.Event()

        self.sync_error: str | None = "not synced yet"
        self.last_sync_at: float = 0.0
        self.commits = 0
        self.pushes = 0
        self.fetches = 0
        # Staging attempts lost to the reader. Counted rather than only logged:
        # this is expected at a low rate and pathological at a high one, and
        # nothing else would tell the two apart.
        self.add_races = 0
        # Rebases that refused because the tree had moved since the commit. Same
        # reasoning as `add_races`, and worth its own counter rather than being
        # folded in: these used to arrive as `recoveries`, which is what made a
        # rebuilt clone look ordinary.
        self.rebase_races = 0
        self.recoveries = 0

    # -- lifecycle --------------------------------------------------------- #

    async def start(self) -> None:
        """Prepare the clone, sync once, then run like the folder transport.

        The first sync happens before this returns, and its failure is allowed
        to propagate. A remote nobody can reach has to be a startup error the
        daemon reports as `setup_error`, not a room that comes up looking
        healthy and quietly reaches nobody -- which is the exact shape of the
        defect this project has already been bitten by twice.
        """
        async with self._lock:
            await asyncio.to_thread(self._prepare_repo)
        await super().start()
        try:
            await self._locked_sync()
        except Exception:
            # The inherited poll loop is running by now, and leaving it behind
            # would be a transport nobody holds a reference to still reading a
            # directory every 250 ms. Deliberately not `BaseException`: on a
            # cancellation -- the daemon's startup budget expiring -- awaiting
            # anything here can re-raise before the cleanup finishes, so that
            # path is handled by the caller's explicit `stop(flush=False)`
            # instead, which runs outside the cancelled context.
            await super().stop()
            raise
        self._sync_stop.clear()
        self._sync_task = asyncio.create_task(
            self._sync_loop(), name=f"git-{self.room_id[:12]}"
        )

    async def stop(self, flush: bool = True) -> None:
        """Shut the channel down, publishing whatever is still local first.

        `flush=False` is for aborting a startup that ran past its budget: there
        the remote is the thing that is not answering, so a last round would
        wait out the same deadline a second time.
        """
        self._sync_stop.set()
        self._nudge.set()
        task, self._sync_task = self._sync_task, None
        if task:
            # Waited out rather than cancelled. Cancelling a task that is inside
            # `to_thread` returns immediately while the git it launched carries
            # on running, and the lock is released the moment the await is
            # interrupted -- so the final round below would start a second git
            # in the same working tree and both would lose to `index.lock`. The
            # loop checks its stop event between rounds, so this is short.
            try:
                await asyncio.wait_for(task, timeout=GIT_NET_TIMEOUT_S)
            except Exception:
                task.cancel()       # a round that overran: nothing left to wait for
        await super().stop()
        if not flush:
            return
        # A last round, so a frame written a moment before shutdown is on the
        # remote rather than sitting in a clone nobody will look at again until
        # this machine comes back. Best effort: shutdown must not hang on it.
        # `wait_for` rather than `asyncio.timeout`, which is 3.11 and this runs
        # on 3.10.
        try:
            await asyncio.wait_for(self._locked_sync(), timeout=GIT_NET_TIMEOUT_S)
        except Exception:
            pass

    async def _locked_sync(self) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._sync_once)

    @property
    def online(self) -> bool:
        """The clone is running *and* the remote answered on the last round.

        Stricter than the folder transport, which counts as online whenever the
        directory is readable. Here the directory being readable says nothing --
        it is a local clone and it is always readable. Whether anything reaches
        another machine is the sync, so that is what this reports.
        """
        return bool(super().online and self.sync_error is None and self.last_sync_at)

    # -- sending ----------------------------------------------------------- #

    async def send(
        self,
        frame: dict[str, Any],
        recipients: Iterable[str] | None = None,
        msg_id: str | None = None,
    ) -> bool:
        """Write the copies locally, then wake the sync loop to publish them.

        True means the frames are on disk in the clone, not that they are on
        GitHub -- the same promise the folder transport makes when it writes
        into a directory a sync client has not uploaded yet. Delivery is a sync
        round away, and a failed round shows up on `online` and in `stats()`.
        """
        # Under the same lock the sync round takes. `_recover` copies
        # `out/<me>` aside, resets, and then runs `clean -qfd` over the subtree:
        # a frame written between the copy and the clean is untracked, survives
        # the reset, is deleted by the clean, and is not in the stash for
        # `_restore_recent` to put back. `send` has already returned True and
        # the room has already counted it as sent. The lock only ever guarded
        # sync rounds against each other, never against the writer.
        async with self._lock:
            ok = await super().send(frame, recipients, msg_id)
        self._nudge.set()
        return ok

    async def publish_files(self, writes):
        """Like FileTransport.publish_files, but under the repo lock —
        `_recover`'s `clean -qfd` deletes untracked files, and a door write
        racing a recovery is exactly that — and with a sync nudged after."""
        async with self._lock:
            result = await asyncio.to_thread(writes)
        self._nudge.set()
        return result

    # -- the sync loop ------------------------------------------------------ #

    async def _sync_loop(self) -> None:
        while not self._sync_stop.is_set():
            await self._wait_for_round()
            if self._sync_stop.is_set():
                break
            try:
                await self._locked_sync()
            except asyncio.CancelledError:
                raise
            except GitError as exc:
                self.sync_error = str(exc)
                self.log(f"[git] {exc}")
            except Exception as exc:
                self.sync_error = f"{type(exc).__name__}: {exc}"
                self.log(f"[git] sync failed: {self.sync_error}")

    async def _wait_for_round(self) -> None:
        """Sleep until the next round is due, or until a send asks for one."""
        try:
            await asyncio.wait_for(self._nudge.wait(), timeout=self.sync_s)
            # Woken by a send. A pause here is not politeness: an agent writing
            # three messages in a row should pay for one commit, not three.
            await asyncio.sleep(SYNC_DEBOUNCE_S)
        except asyncio.TimeoutError:
            pass
        self._nudge.clear()

    def _sync_once(self) -> bool:
        """One commit / fetch / rebase / push round. Blocking.

        Returns True if the working tree moved, either because we committed
        something or because the remote had something for us. Raises `GitError`
        if the round could not be completed, which is what sets `sync_error` and
        takes this transport out of `live_transports`.
        """
        changed = False
        why_rejected = ""
        for attempt in range(PUSH_ATTEMPTS):
            changed |= self._commit_local()
            changed |= self._integrate()
            if not self._ahead_of_remote():
                break                       # nothing of ours left to publish
            ok, why_rejected, raced = self._push()
            if ok:
                break
            if not raced:
                raise GitError(f"push failed: {why_rejected}")
            if attempt == PUSH_ATTEMPTS - 1:
                raise GitError(
                    f"push lost {PUSH_ATTEMPTS} races in a row: {why_rejected}"
                )
            self.log(f"[git] push rejected, re-syncing ({why_rejected})")

        self.last_sync_at = time.time()
        self.sync_error = None
        return changed

    # -- the four steps ----------------------------------------------------- #

    def _commit_local(self) -> bool:
        """Stage and commit whatever the file transport has written. True if any.

        The directory is ensured first because `git add` treats a pathspec that
        matches nothing as fatal, and there is a real path to that: a recovery
        resets to a branch that has no `claude-link/` yet -- a channel where
        nobody has sent anything -- and `clean` then takes the empty directories
        with it. One `makedirs` is cheaper than teaching the caller to read
        git's exit codes, and an empty directory costs git nothing because it
        cannot track one anyway.

        The `add` itself races the reader, which is the second thing here that
        looks like a git quirk and is really a threading one. This runs on a
        worker thread; delete-on-read and the heartbeat rewrite run on the event
        loop; git walks, stats, and reads each file, and the tree can move
        between any two of those. It used to abort the round with an exception.
        It is retried now, because the set of frames being collected is bounded
        and the tree stops moving, while every other reason for a non-zero `add`
        still raises on the first attempt.

        Losing every attempt returns False rather than raising. The frames are
        still on disk and the next round stages them; killing the sync loop over
        transient housekeeping would not.
        """
        os.makedirs(os.path.join(self.workdir, REPO_SUBDIR), exist_ok=True)
        # `--ignore-errors` so one lost file does not abandon the staging of
        # every other one, and `check=False` because git still exits non-zero
        # when it has ignored something. What it complained about is then the
        # question, and `_add_race` is where it is answered.
        for attempt in range(1, ADD_ATTEMPTS + 1):
            rc, out, err = self._git(
                ["add", "-A", "--ignore-errors", "--", REPO_SUBDIR], check=False)
            if rc == 0:
                break
            why = _add_race(f"{err}\n{out}")
            if why is None:
                raise GitError(f"git add: {_first_line(err or out)}")
            self.add_races += 1
            self.log(f"[git] the tree moved while staging ({why}); "
                     f"attempt {attempt} of {ADD_ATTEMPTS}")
        else:
            self.log("[git] staging kept losing to the reader; leaving this "
                     "round's changes for the next one")
            return False
        rc, _out, _err = self._git(["diff", "--cached", "--quiet"], check=False)
        if rc == 0:
            return False                     # `--quiet` exits 1 when there is a diff
        _rc, names, _err = self._git(
            ["diff", "--cached", "--name-only"], check=False
        )
        count = len([n for n in names.splitlines() if n.strip()])
        self._git([
            "commit", "--quiet", "--no-verify", "--no-gpg-sign",
            "-m", f"link {self.device_id[:12]}: {count} change(s)",
        ])
        self.commits += 1
        return True

    def _integrate(self) -> bool:
        """Bring the remote tip in. True if our HEAD moved as a result."""
        before = self._rev("HEAD")
        self._fetch()
        remote_ref = f"refs/remotes/origin/{self.branch}"
        remote = self._rev(remote_ref)
        if remote is None:
            return False                     # the branch is ours to create

        if self._is_ancestor(remote, "HEAD"):
            return False                     # we already contain everything they have

        if not self._shares_history("HEAD", remote_ref):
            # No merge base at all. Not a conflict -- a different history, which
            # means somebody squashed the branch. Rebasing onto it would replay
            # every commit we have ever made and resurrect frames deleted months
            # ago, so the only correct move is to adopt theirs.
            self._recover(remote_ref, "the branch was rewritten")
            return True

        for attempt in range(1, REBASE_ATTEMPTS + 1):
            rc, out, err = self._git(["rebase", remote_ref], check=False,
                                     timeout=GIT_LOCAL_TIMEOUT_S)
            if rc == 0:
                return self._rev("HEAD") != before
            # Harmless whether or not a rebase started: it did not when the
            # refusal was a dirty tree, and `--abort` on nothing is a no-op.
            self._git(["rebase", "--abort"], check=False)
            why = _rebase_race(f"{err}\n{out}")
            if why is None:
                self._recover(remote_ref,
                              f"rebase conflict: {_first_line(err or out)}")
                return True
            if attempt == REBASE_ATTEMPTS:
                # Not a conflict, and saying so would send whoever reads this
                # log looking for one. Something is rewriting the subtree faster
                # than a round can commit it, which is its own problem.
                self._recover(remote_ref,
                              f"the subtree kept moving under the rebase ({why})")
                return True
            self.rebase_races += 1
            self.log(f"[git] the tree moved before the rebase ({why}); "
                     f"committing it and retrying, attempt {attempt} of "
                     f"{REBASE_ATTEMPTS}")
            # What moved is a frame collected or a heartbeat rewritten since the
            # commit above. Committing it is the whole fix; the rebase then has
            # a clean tree to work from. Deliberately not `--autostash`: that
            # would stash a delete-on-read removal and pop it back afterwards,
            # resurrecting a frame the reader had already taken.
            self._commit_local()
        return True                          # unreachable: the loop returns

    def _fetch(self) -> None:
        """Update `origin/<branch>`, treating an absent branch as normal.

        An explicit refspec rather than a bare `fetch origin`: on a repository
        that also holds code, fetching every branch would pull down history this
        has no use for, and on any repository it makes what lands in
        `refs/remotes/origin/<branch>` a fact rather than a configuration
        question.
        """
        args = ["fetch", "--quiet", "--no-tags"]
        if self.depth:
            args += ["--depth", str(self.depth)]
        args += ["origin", f"+refs/heads/{self.branch}:refs/remotes/origin/{self.branch}"]
        rc, out, err = self._git(args, check=False, timeout=GIT_NET_TIMEOUT_S)
        self.fetches += 1
        if rc == 0:
            return
        message = _first_line(err or out)
        if "couldn't find remote ref" in message.lower():
            return          # nobody has created the branch yet; we will
        raise GitError(f"fetch failed: {message}")

    def _push(self) -> tuple[bool, str, bool]:
        """Publish our commits. Returns (pushed, why not, worth retrying).

        `git push` is the one command here whose first line of output is never
        the reason: it leads with `To <url>` and puts the verdict underneath.
        Reading it the way every other command is read gives back "To
        https://..." -- which tells the user nothing and, worse, hides the words
        that distinguish a lost race from a dead remote, so every race got
        raised as a hard failure instead of retried.
        """
        rc, out, err = self._git(
            ["push", "--quiet", "origin", f"HEAD:refs/heads/{self.branch}"],
            check=False, timeout=GIT_NET_TIMEOUT_S,
        )
        if rc == 0:
            self.pushes += 1
            return True, "", False
        text = f"{err}\n{out}"
        return False, _push_reason(text), _is_race(text)

    # -- recovery ----------------------------------------------------------- #

    def _recover(self, remote_ref: str, why: str) -> None:
        """Adopt the remote's history without losing mail we have not sent.

        The order is the whole of it. Our outbound frames are copied out
        *before* anything destructive runs, the tree is reset to the remote, and
        the recent half of that copy goes back. What comes back with the reset --
        frames addressed to us that we had already read and deleted -- is
        absorbed by the inherited `_seen` set, and by `msg_id` admission in
        `room.py` behind it.
        """
        self.recoveries += 1
        self.log(f"[git] {why}; rebuilding this clone from {remote_ref}")

        mine = out_root(self.workdir, self.room_id, self.device_id)
        stash = tempfile.mkdtemp(prefix="claude-link-git-")
        try:
            if os.path.isdir(mine):
                shutil.copytree(mine, os.path.join(stash, "out"), dirs_exist_ok=True)

            self._git(["rebase", "--abort"], check=False)
            self._git(["reset", "--hard", "--quiet", remote_ref])
            self._git(["clean", "-qfd", "--", REPO_SUBDIR], check=False)

            restored, dropped = _restore_recent(os.path.join(stash, "out"), mine)
            if restored:
                self.log(f"[git] kept {restored} unsent frame(s) through the rebuild")
            if dropped:
                self.log(f"[git] DISCARDED {dropped} outbound frame(s) older than "
                         f"{RESTORE_WINDOW_MS // 60000} minutes during the rebuild. "
                         f"If pushes have been failing for longer than that, those "
                         f"were never delivered.")

            # Our heartbeat is untracked until it is committed, so `clean` takes
            # it. Left at that, this device drops off every other member's
            # roster until the next refresh -- forty-five seconds of being owed
            # no mail, caused by our own housekeeping. Rewriting is better than
            # preserving: the file wants to say "now" anyway.
            self._presence_tick(True)
            self._commit_local()
        finally:
            _rmtree(stash)

    # -- repository setup --------------------------------------------------- #

    def _prepare_repo(self) -> None:
        """Make `workdir` a clone of `remote` sitting on `branch`. Blocking."""
        os.makedirs(self.workdir, exist_ok=True)
        if not self._is_repo():
            # A `.git` that git itself will not accept: a delete that got half
            # way, a clone killed part-written, a backup tool that copied the
            # working tree and not the metadata. Asking the directory whether it
            # has a `.git` says yes to all three and then every later command
            # fails with "not a git repository" forever, so ask git instead.
            #
            # Only the metadata is discarded. Frames sitting in the working tree
            # are left where they are and get committed by the first round.
            git_dir = os.path.join(self.workdir, ".git")
            if os.path.exists(git_dir):
                self.log("[git] the clone's .git is unusable; rebuilding it")
                _rmtree(git_dir)
            run_git(
                ["-c", f"init.defaultBranch={self.branch}", "init", "--quiet",
                 self.workdir],
                timeout=GIT_LOCAL_TIMEOUT_S,
            )
        self._clear_stale_lock()
        # A previous run killed mid-rebase leaves the repository in a state where
        # every later command refuses to do anything and says so obscurely.
        self._git(["rebase", "--abort"], check=False)
        self._configure()
        self._set_remote()

        self._fetch()
        remote_ref = f"refs/remotes/origin/{self.branch}"
        remote = self._rev(remote_ref)

        if self._rev("HEAD") is None:
            # Nothing committed here yet: a fresh clone directory, the very
            # first member of a brand new channel, or a clone whose `.git` was
            # rebuilt a moment ago and whose files are all untracked again.
            if remote:
                # `reset --hard` rather than `checkout -B`. Checkout refuses to
                # overwrite an untracked file, which is the right instinct for a
                # user's source tree and useless here: after a rebuild every
                # frame in the directory is untracked and the checkout simply
                # never happens. Reset adopts the remote's tree and leaves
                # anything the remote does not have -- which is exactly the
                # unsent mail that must not be dropped.
                self._git(["symbolic-ref", "HEAD", f"refs/heads/{self.branch}"])
                self._git(["reset", "--hard", "--quiet", remote_ref])
            else:
                self._seed()
        elif self._current_branch() != self.branch:
            # Keep the commits, move the branch name onto them. Checking out the
            # remote instead would silently discard anything this machine wrote
            # while it was offline.
            self._git(["checkout", "--quiet", "-B", self.branch])

    def _configure(self) -> None:
        """Repo-local settings, so the daemon never depends on a global one.

        A machine with no `user.email` configured cannot commit, a machine with
        `commit.gpgsign` on cannot commit without a passphrase, and a Windows
        machine with `core.autocrlf` on rewrites every sealed frame on checkout.
        All three are somebody's perfectly reasonable global configuration, and
        all three would break this. None of it leaves the clone.
        """
        for key, value in (
            ("user.name", "agent-link"),
            ("user.email", "agent-link@localhost"),
            ("commit.gpgsign", "false"),
            ("tag.gpgsign", "false"),
            ("core.autocrlf", "false"),
            ("core.safecrlf", "false"),
            ("core.longpaths", "true"),      # room id + device id nest deeply
            ("core.fsmonitor", "false"),
            ("advice.detachedHead", "false"),
            ("gc.auto", "0"),                # never rewrite history under the loop
        ):
            self._git(["config", "--local", key, value], check=False)

    def _set_remote(self) -> None:
        rc, current, _err = self._git(["remote", "get-url", "origin"], check=False)
        if rc != 0:
            self._git(["remote", "add", "--", "origin", self.remote])
        elif current.strip() != self.remote:
            # The configured remote changed under a clone that already exists.
            self._git(["remote", "set-url", "--", "origin", self.remote])

    def _seed(self) -> None:
        """Create the branch as a root commit carrying only an explanation.

        Committed but deliberately not pushed. The first sync round pushes it,
        and if somebody else seeded the branch in the meantime that round sees
        two unrelated histories and takes theirs -- which is the same code path
        as a rewritten history, tested once and used twice.
        """
        self._git(["checkout", "--quiet", "-B", self.branch])
        for name, body in _SEED_FILES.items():
            path = os.path.join(self.workdir, name)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(body)
            self._git(["add", "--", name])
        self._git([
            "commit", "--quiet", "--no-verify", "--no-gpg-sign",
            "-m", "agent-link: open this branch as a channel",
        ])
        self.commits += 1

    def _clear_stale_lock(self) -> None:
        """Remove an `index.lock` left by a process that is no longer running.

        Git's own advice is to check nothing else is using the repository and
        delete it, which is advice for a human at a terminal. A daemon killed
        mid-commit -- and this project already tests recovery after `SIGKILL` --
        would otherwise leave a clone that refuses every command forever. Only
        an old lock is removed, so a genuinely concurrent git is left alone.
        """
        lock = os.path.join(self.workdir, ".git", "index.lock")
        try:
            age = time.time() - os.stat(lock).st_mtime
        except OSError:
            return
        if age < LOCK_STALE_S:
            return
        try:
            os.unlink(lock)
            self.log(f"[git] removed a stale index.lock ({age:.0f}s old)")
        except OSError:
            pass

    # -- small git queries -------------------------------------------------- #

    def _git(self, args: list[str], check: bool = True,
             timeout: float = GIT_LOCAL_TIMEOUT_S) -> tuple[int, str, str]:
        return run_git(args, cwd=self.workdir, timeout=timeout, check=check)

    def _is_repo(self) -> bool:
        """Whether `workdir` is a repository *of its own*.

        Asked of git rather than of the filesystem, because a `.git` that git
        will not accept -- a half-finished delete, a clone killed part-written,
        a backup tool that copied the working tree and not the metadata -- looks
        fine to `os.path.isdir` and then fails every later command forever.

        But it has to be asked about this directory and not about the answer git
        volunteers, because `rev-parse` walks **up**. Any workdir underneath a
        repository answered yes, so `git init` was skipped and every command
        after it drove the enclosing one: `remote set-url origin` repointed the
        user's remote at the channel, `checkout -B` moved them off their branch,
        their `user.email` was rewritten, their working tree was committed and
        pushed to a repo their room can read, and the first `_recover` ran
        `reset --hard` and `clean -qfd` in it. The default clone lives under
        `~/.claude/`, so this needed nothing more exotic than keeping dotfiles
        in git.
        """
        if not os.path.isdir(self.workdir):
            return False
        rc, out, _err = self._git(["rev-parse", "--absolute-git-dir"], check=False)
        if rc != 0:
            return False
        try:
            return os.path.samefile(out.strip(), os.path.join(self.workdir, ".git"))
        except OSError:
            return False        # it named something that is not there any more

    def _rev(self, ref: str) -> str | None:
        rc, out, _err = self._git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                                  check=False)
        return out.strip() if rc == 0 and out.strip() else None

    def _current_branch(self) -> str:
        _rc, out, _err = self._git(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
        return out.strip()

    def _is_ancestor(self, maybe_ancestor: str, descendant: str) -> bool:
        rc, _out, _err = self._git(
            ["merge-base", "--is-ancestor", maybe_ancestor, descendant], check=False
        )
        return rc == 0

    def _shares_history(self, a: str, b: str) -> bool:
        rc, out, _err = self._git(["merge-base", a, b], check=False)
        return rc == 0 and bool(out.strip())

    def _ahead_of_remote(self) -> bool:
        remote = self._rev(f"refs/remotes/origin/{self.branch}")
        if remote is None:
            return self._rev("HEAD") is not None      # the branch does not exist yet
        return not self._is_ancestor("HEAD", remote)

    def unpushed(self) -> int:
        """Commits sitting in this clone that the remote has not taken."""
        remote = f"refs/remotes/origin/{self.branch}"
        if self._rev(remote) is None:
            rc, out, _err = self._git(["rev-list", "--count", "HEAD"], check=False)
            return int(out) if rc == 0 and out.isdigit() else 0
        rc, out, _err = self._git(["rev-list", "--count", f"{remote}..HEAD"], check=False)
        return int(out) if rc == 0 and out.isdigit() else 0

    # -- introspection ------------------------------------------------------ #

    def stats(self) -> dict[str, Any]:
        stats = super().stats()
        stats.pop("shared_dir", None)
        stats.update({
            "remote": redact_remote(self.remote),
            "branch": self.branch,
            "clone": self.workdir,
            "sync_ms": int(self.sync_s * 1000),
            "commits": self.commits,
            "pushes": self.pushes,
            "fetches": self.fetches,
            "add_races": self.add_races,
            "rebase_races": self.rebase_races,
            "recoveries": self.recoveries,
            "last_sync_s": (round(time.time() - self.last_sync_at, 1)
                            if self.last_sync_at else None),
            "sync_error": self.sync_error,
        })
        return stats


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _is_race(message: str) -> bool:
    low = (message or "").lower()
    return any(marker in low for marker in _RACE_MARKERS)


def _push_reason(text: str) -> str:
    """The line of a failed push that actually says what happened.

    Skips the `To <url>` header git always leads with, and the `hint:` block it
    always trails with, both of which are true and neither of which is the
    answer. What is left is the `! [rejected] ... (non-fast-forward)` line, or
    whatever the transport failed with.
    """
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("To ", "hint:", "Everything up-to-date")):
            continue
        return stripped
    return _first_line(text)


def _rmtree(path: str) -> None:
    """Delete a tree, including the read-only files git leaves under `.git`.

    On Windows a packed object is marked read-only and `rmtree` raises on it,
    which would leak a scratch clone into the temp directory on every prune.
    Written by hand rather than with `onerror=`, which is deprecated in 3.12 and
    named differently in 3.12+ -- this project spans 3.10 to 3.14.
    """
    try:
        shutil.rmtree(path)
        return
    except OSError:
        pass
    for dirpath, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.chmod(os.path.join(dirpath, name), stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def _restore_recent(stash_root: str, dest_root: str,
                    window_ms: int = RESTORE_WINDOW_MS) -> tuple[int, int]:
    """Put back outbound frames young enough to still be undelivered.

    Age comes from the filename, which leads with the millisecond the frame was
    queued -- the same reading the folder transport's own sweep uses, and the
    reason neither of them has to trust a mtime that a checkout just rewrote.
    """
    if not os.path.isdir(stash_root):
        return 0, 0
    cutoff = now_ms() - window_ms
    restored = 0
    dropped = 0
    for dirpath, _dirs, names in os.walk(stash_root):
        rel = os.path.relpath(dirpath, stash_root)
        target_dir = dest_root if rel == "." else os.path.join(dest_root, rel)
        for name in names:
            if not name.endswith(".json") or name.startswith(".tmp-"):
                continue
            if _queued_at_ms(dirpath, name) < cutoff:
                # "Almost certainly collected already" is true of frames that
                # reached the remote. A device whose pushes have been failing
                # for longer than the window is holding frames nobody has seen,
                # and the first recovery after the network returns eats them.
                # Counted, so at least the log says how many.
                dropped += 1
                continue
            target = os.path.join(target_dir, name)
            if os.path.exists(target):
                continue
            try:
                os.makedirs(target_dir, exist_ok=True)
                shutil.copy2(os.path.join(dirpath, name), target)
                restored += 1
            except OSError:
                continue
    return restored, dropped


_SEED_FILES = {
    ".gitattributes": (
        "# Sealed frames are bytes, not text. Any end-of-line translation would\n"
        "# rewrite them on checkout, and a diff of ciphertext is noise.\n"
        "* -text -diff\n"
    ),
    ".gitignore": (
        "# Temp files from an atomic write, visible for microseconds and never\n"
        "# something to commit.\n"
        ".tmp-*\n"
        "*.part\n"
    ),
    "README.md": (
        "# agent-link channel\n"
        "\n"
        "This branch is a message channel between coding agents, not source code.\n"
        "It is an orphan branch: it has no commit in common with your default\n"
        "branch, nothing here is merged anywhere, and none of your code is on it.\n"
        "Deleting the branch removes the channel and touches nothing else.\n"
        "\n"
        "**If this repository runs CI, add `branches-ignore: [claude-link]` to any\n"
        "workflow that triggers on push.** Presence heartbeats commit here about\n"
        "once every 45 seconds while a room is live, and a workflow with no branch\n"
        "filter will build on every one of them.\n"
        "\n"
        "Every message *body* under `claude-link/` is AES-256-GCM ciphertext\n"
        "sealed under a key that lives only on the members' machines and is never\n"
        "committed here. Whoever hosts this repository cannot read a message.\n"
        "\n"
        "Everything around the body is in clear, and that is more than it sounds.\n"
        "The path names carry the room, the sender, the recipient and the\n"
        "millisecond; each frame carries its `kind` and `seq`; the presence files\n"
        "are plain JSON; and because collecting a message is itself a commit, the\n"
        "history is a read-receipt log with timestamps. Git keeps all of it after\n"
        "the fact, so **use a private repository**.\n"
        "\n"
        "Nothing here is meant to be edited by hand. Files appear and disappear as\n"
        "messages are sent and collected.\n"
        "\n"
        "    claude-link/<room>/out/<sender>/<recipient>/<time>-<id>.json\n"
        "    claude-link/<room>/presence/<device>.json\n"
        "\n"
        "History grows one commit at a time and nothing truncates it on its own.\n"
        "`agent-link git-prune` squashes this branch to a single commit when it\n"
        "gets long; the other members notice and rebuild automatically.\n"
    ),
}


# --------------------------------------------------------------------------- #
# diagnostics and maintenance
# --------------------------------------------------------------------------- #


def probe_git_remote(remote: str, branch: str = DEFAULT_BRANCH,
                     timeout: float = GIT_NET_TIMEOUT_S) -> tuple[bool, str]:
    """Check the remote is reachable and writable-looking, without cloning it.

    Deliberately does not create anything. The parallel with `probe_shared_dir`
    is exact: a remote that is not there is a typo or a missing credential, and
    the useful answer names which. `ls-remote` needs the same read access a
    fetch does and costs one round trip.
    """
    if not remote:
        return False, "no git_remote configured"
    if git_version() is None:
        return False, "git is not installed, or not on PATH"
    try:
        remote = check_remote(remote)
    except BadRemote as exc:
        return False, str(exc)

    try:
        rc, out, err = run_git(
            ["ls-remote", "--heads", "--", remote, branch],
            timeout=timeout, check=False,
        )
    except GitError as exc:
        return False, str(exc)

    if rc != 0:
        message = _first_line(err or out)
        low = message.lower()
        if ("authentication" in low or "could not read username" in low
                or "permission denied" in low or "terminal prompts disabled" in low
                or "invalid username" in low):
            return False, (
                f"{message} -- git has no working credential for this remote. "
                f"`gh auth login` and `gh auth setup-git`, an ssh key, or a PAT "
                f"in the URL will each fix it; test with "
                f"`git ls-remote {redact_remote(remote)}`."
            )
        if "not found" in low or "does not exist" in low or "repository not found" in low:
            return False, f"{message} -- check the URL, and that the repo exists"
        return False, message

    if out.strip():
        return True, f"ok, branch '{branch}' exists"
    return True, f"ok, branch '{branch}' will be created on first use"


def remote_branches(remote: str, timeout: float = GIT_NET_TIMEOUT_S) -> list[str] | None:
    """Every branch name on the remote, or None when it cannot be read.

    One `ls-remote`, the same round trip `probe_git_remote` already pays. It
    answers the only question that matters before writing into somebody's
    repository: does anything else live there.
    """
    if not remote:
        return None
    if git_version() is None:
        return None
    try:
        remote = check_remote(remote)
    except BadRemote:
        return None
    try:
        rc, out, _err = run_git(["ls-remote", "--heads", "--", remote],
                                timeout=timeout, check=False)
    except GitError:
        return None
    if rc != 0:
        return None
    names = []
    for line in out.splitlines():
        _sha, _tab, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            names.append(ref[len("refs/heads/"):].strip())
    return names


def github_workflows(remote: str) -> list[str] | None:
    """Workflow filenames on the remote's default branch, if `gh` can say.

    None means "cannot tell", which is not the same as "none" and must not be
    reported as one: no `gh`, no auth and a host that is not GitHub all land
    here. A 404 on the directory is a real answer, and the real answer is empty.
    """
    if not remote or "github.com" not in remote:
        return None
    if shutil.which("gh") is None:
        return None
    slug = _github_slug(remote)
    if not slug:
        return None
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{slug}/contents/.github/workflows",
             "-q", ".[].name"],
            env=_git_env(), stdin=subprocess.DEVNULL, capture_output=True,
            timeout=20.0, creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace")
        # The directory not existing is the answer, not a failure to get one.
        return [] if "404" in stderr or "Not Found" in stderr else None
    return [line.strip() for line in
            proc.stdout.decode("utf-8", "replace").splitlines() if line.strip()]


def shared_repo_warning(remote: str, branch: str = DEFAULT_BRANCH,
                        presence_s: float = 45.0,
                        timeout: float = GIT_NET_TIMEOUT_S) -> str | None:
    """Warn before the channel starts running somebody else's CI.

    Attaching to a repository the members already share is the cheapest setup
    there is, and the orphan branch makes it safe for their *code*. It is not
    automatically safe for their *automation*. Presence heartbeats land in a
    commit of their own every `presence_s` seconds and each one is a push to
    `refs/heads/<branch>`, so a workflow with `on: push` and no branch filter
    fires on all of them: at the default 45 s that is getting on for two
    thousand builds a day, on a repository whose owner never asked for them.
    This project's own `tests.yml` is `branches: ["**"]`, which is the common
    case rather than an exotic one.

    Nothing downstream would notice, and the person who finds out is the
    colleague whose Actions minutes are gone. Best effort in the same way
    `github_visibility` is: what cannot be determined produces no warning
    rather than a false one.
    """
    branches = remote_branches(remote, timeout=timeout)
    if not branches:
        # Unreadable, or an empty repo that exists only to be a channel.
        return None
    if not [b for b in branches if b != branch]:
        return None

    per_day = int(86400 // max(1.0, float(presence_s)))
    workflows = github_workflows(remote)
    if workflows is None:
        return (
            f"this repo holds other branches, so it is somebody's project. If it "
            f"runs CI on every branch, the channel will trigger it: presence "
            f"heartbeats push to '{branch}' about {per_day} times a day. Add "
            f"`branches-ignore: [{branch}]` to any workflow that runs on push."
        )
    if not workflows:
        return None
    return (
        f"this repo runs {len(workflows)} GitHub Actions workflow(s) "
        f"({', '.join(sorted(workflows)[:3])}"
        f"{', ...' if len(workflows) > 3 else ''}). Presence heartbeats push to "
        f"'{branch}' about {per_day} times a day, and any workflow with `on: "
        f"push` and no branch filter will run on every one of them. Add "
        f"`branches-ignore: [{branch}]` to those workflows first, or point "
        f"git_remote at a repo that has no CI."
    )


def github_visibility(remote: str) -> str | None:
    """PUBLIC / PRIVATE / INTERNAL for a GitHub remote, if `gh` can say.

    Best effort and never load-bearing: no `gh`, no auth, or a host that is not
    GitHub all read as "cannot tell" rather than as a problem. It exists for one
    warning, and a warning that cannot be produced is better than a false one.
    """
    if not remote or "github.com" not in remote:
        return None
    if shutil.which("gh") is None:
        return None
    slug = _github_slug(remote)
    if not slug:
        return None
    try:
        proc = subprocess.run(
            ["gh", "repo", "view", slug, "--json", "visibility", "-q", ".visibility"],
            env=_git_env(), stdin=subprocess.DEVNULL, capture_output=True,
            timeout=20.0, creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip().upper() or None


def _github_slug(remote: str) -> str | None:
    """`owner/repo` from either URL form GitHub hands out."""
    text = remote.strip()
    if text.endswith(".git"):
        text = text[: -len(".git")]
    if text.startswith("git@") or (":" in text and "://" not in text):
        _host, _sep, path = text.partition(":")
        parts = [p for p in path.split("/") if p]
    else:
        try:
            parts = [p for p in urlsplit(text).path.split("/") if p]
        except ValueError:
            return None
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


def prune_history(remote: str, branch: str = DEFAULT_BRANCH,
                  log: Callable[[str], None] | None = None) -> str:
    """Squash the branch to one root commit holding the current tree.

    Run in a scratch clone of its own rather than in the daemon's working tree,
    so it never has to take a lock the daemon is holding and cannot leave the
    daemon's clone in a half-rewritten state if it fails.

    The push carries `--force-with-lease` against the tip this call actually
    read, so a member who sends something between the read and the push wins and
    the prune fails cleanly. Losing a message to housekeeping would be a poor
    trade for a smaller repository.
    """
    say = log or (lambda _m: None)
    remote = check_remote(remote)
    scratch = tempfile.mkdtemp(prefix="claude-link-prune-")
    work = os.path.join(scratch, "repo")
    try:
        run_git(["clone", "--quiet", "--branch", branch, "--single-branch",
                 "--", remote, work], timeout=GIT_NET_TIMEOUT_S * 3)
        for key, value in (("user.name", "agent-link"),
                           ("user.email", "agent-link@localhost"),
                           ("commit.gpgsign", "false"),
                           ("core.autocrlf", "false")):
            run_git(["config", "--local", key, value], cwd=work, check=False)

        _rc, before, _err = run_git(["rev-parse", "HEAD"], cwd=work)
        _rc, count, _err = run_git(["rev-list", "--count", "HEAD"], cwd=work)

        run_git(["checkout", "--quiet", "--orphan", "__link_prune"], cwd=work)
        run_git(["add", "-A"], cwd=work)
        run_git(["commit", "--quiet", "--no-verify", "--no-gpg-sign",
                 "-m", "agent-link: history compacted"], cwd=work)
        rc, out, err = run_git(
            ["push", "--quiet",
             f"--force-with-lease=refs/heads/{branch}:{before}",
             "origin", f"HEAD:refs/heads/{branch}"],
            cwd=work, timeout=GIT_NET_TIMEOUT_S, check=False,
        )
        if rc != 0:
            raise GitError(
                f"prune not applied: {_first_line(err or out)} -- somebody pushed "
                f"while this was running, which is exactly when not to force. "
                f"Nothing was changed; try again."
            )
        say(f"compacted {count} commits into 1 on '{branch}'")
        return f"compacted {count} commit(s) into 1 on '{branch}'"
    finally:
        _rmtree(scratch)
