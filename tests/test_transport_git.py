"""The git transport, against a real repository.

Every test here drives two `GitTransport`s and a bare repo that stands in for
GitHub. Nothing is faked: git is invoked, commits are made, pushes race and
histories are rewritten, because the interesting failures in this transport are
all in what git does rather than in what the code says about it.

The folder transport's own history is why. Its tests passed against a local temp
directory for months while the medium it actually shipped on -- OneDrive, with
sync latency and conflict copies -- had never carried a message. A bare repo on
disk is not GitHub either, but it is the same git, with the same ref locking,
the same rebase and the same non-fast-forward rejection, which is where the
concurrency lives. No test here touches the network.
"""

import asyncio
import collections
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from link import crypto                                              # noqa: E402
from link.envelope import make_envelope, make_origin, open_and_verify, seal_frame  # noqa: E402
from link.identity import Identity                                   # noqa: E402
from link.transport_git import (                                     # noqa: E402
    BadRemote,
    GitTransport,
    check_remote,
    _git_env,
    _github_slug,
    _is_race,
    _push_reason,
    _rmtree,
    clone_dir,
    git_version,
    probe_git_remote,
    prune_history,
    redact_remote,
    run_git,
    shared_repo_warning,
    GitError,
    REPO_SUBDIR,
    REBASE_ATTEMPTS,
    _add_race,
    _rebase_race,
)

HAS_GIT = shutil.which("git") is not None
# Shared CI runners spawn processes slowly and git is a great many processes.
TIMEOUT = 90.0 if os.environ.get("CI") else 40.0

# The installers run this suite as their self-test, inside a CI job with a much
# shorter ceiling than the test job's. The cases below spawn several hundred git
# processes between them, so the installers leave them out -- and say on screen
# that they did. Never silently: this project has already had fifty-two tests
# stop being collected with nothing to show for it.
_SKIP = ("git is not installed" if not HAS_GIT
         else "CLAUDE_LINK_SKIP_GIT_TESTS=1 -- run "
              "`python3 -m unittest tests.test_transport_git` to include them"
         if os.environ.get("CLAUDE_LINK_SKIP_GIT_TESTS") == "1" else "")


def a_device(label):
    key = crypto.generate_device_key()
    public = crypto.public_bytes(key)
    return Identity(crypto.device_id_for(public), public, label, "cli", key)


@unittest.skipIf(_SKIP, _SKIP)
class GitTransportCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base = tempfile.mkdtemp(prefix="claude-link-git-test-")
        self.origin = os.path.join(self.base, "origin.git")
        run_git(["init", "--bare", "--quiet", self.origin])
        # Forward slashes: git takes either on Windows, and a backslashed path
        # inside a failure message is unreadable exactly when it matters.
        self.remote = self.origin.replace("\\", "/")
        self.keys = crypto.derive_room("git-room", "a-secret-for-the-repo")
        self.transports = []

    async def asyncTearDown(self):
        for transport in self.transports:
            try:
                await transport.stop()
            except Exception:
                pass
        shutil.rmtree(self.base, ignore_errors=True)

    async def member(self, identity, sync_ms=250, presence_s=5.0):
        received = []
        # Kept rather than thrown away. The `[git]` lines are the only place the
        # sync loop says it lost a race, rebuilt a clone or gave up on a push,
        # and a wait that runs out on a CI runner nobody can log into has
        # nothing else behind it. Bounded, so a long test cannot grow it without
        # limit. Read by `diagnostics`.
        loglines = collections.deque(maxlen=200)

        async def on_frame(frame):
            received.append(frame)

        transport = GitTransport(
            remote=self.remote,
            branch="claude-link",
            workdir=os.path.join(self.base, f"clone-{identity.label}"),
            room_id=self.keys.room_id,
            device_id=identity.device_id,
            on_frame=on_frame,
            poll_ms=50,
            sync_ms=sync_ms,
            presence_s=presence_s,
            log=loglines.append,
        )
        transport.identity = identity
        transport.received = received
        transport.loglines = loglines
        await transport.start()
        self.transports.append(transport)
        return transport

    async def freeze_sync(self, *members):
        """Stop members' sync loops without stopping the members.

        Leaves each one writing into its clone and reading from it while nothing
        reaches or arrives from the remote, which is the state a machine is in
        between rounds and the only way to test what happens to work that was
        never published.

        Waits the loop out rather than cancelling it: a cancel returns while the
        git it launched is still running, and a test that then drives a round by
        hand would put two of them in one working tree.
        """
        for member in members:
            member._sync_stop.set()
            member._nudge.set()
            if member._sync_task:
                try:
                    await asyncio.wait_for(member._sync_task, timeout=30.0)
                except Exception:
                    member._sync_task.cancel()

    def seed_code_branch(self):
        """Put an ordinary `main` with a commit on it into the bare repo.

        So that "the channel branch shares no history with the code" is a claim
        about two real branches rather than about one branch and an absence.
        """
        work = os.path.join(self.base, "code")
        run_git(["init", "--quiet", work])
        for key, value in (("user.name", "test"), ("user.email", "test@localhost"),
                           ("commit.gpgsign", "false")):
            run_git(["config", "--local", key, value], cwd=work)
        with open(os.path.join(work, "app.py"), "w", encoding="utf-8") as fh:
            fh.write("print('a repository that also holds code')\n")
        run_git(["add", "-A"], cwd=work)
        run_git(["commit", "--quiet", "--no-gpg-sign", "-m", "the code"], cwd=work)
        run_git(["remote", "add", "origin", self.remote], cwd=work)
        run_git(["push", "--quiet", "origin", "HEAD:refs/heads/main"], cwd=work)

    def a_frame(self, identity, text, seq=1):
        envelope = make_envelope("msg", self.keys.room_id, identity.device_id, seq,
                                 make_origin(identity), body={"text": text})
        return seal_frame(self.keys, identity, envelope), envelope

    async def until(self, predicate, what, timeout=TIMEOUT):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.05)
        raise AssertionError(
            f"timed out after {timeout:.0f}s waiting for {what}\n"
            f"{self.diagnostics()}"
        )

    def diagnostics(self):
        """What each member was doing at the moment a wait ran out.

        `test_a_burst_keeps_its_order` timed out on `windows-latest / py3.12` in
        run 31267434784 and the job carried one line: which wait gave up. Not
        how many of the six had arrived, not whether either side had synced at
        all. The same test takes 13 s on a developer machine and had 90 s, so
        "the runner was slow" is a guess, and there is nothing in the job to
        confirm or kill it.

        `sync_error`, `add_races` and `recoveries` are what tell a slow runner
        apart from a sync loop that is stuck, and none of them outlive the
        process. Print them, and the tail of each member's log, on the way out.
        """
        report = []
        for transport in self.transports:
            stats = transport.stats()
            fields = ("sync_error", "add_races", "recoveries", "commits",
                      "pushes", "fetches", "last_sync_s")
            summary = " ".join(f"{name}={stats.get(name)!r}" for name in fields)
            report.append(
                f"  [{transport.identity.label}] "
                f"received={len(transport.received)} online={transport.online} "
                f"{summary}"
            )
            report.extend(f"      {line}" for line in list(transport.loglines)[-15:])
        return "\n".join(report) or "  (no members)"

    def text_of(self, frame):
        return open_and_verify(self.keys, frame)["body"]["text"]

    def on_remote(self, fragment):
        """Paths on the channel branch of the bare repo containing `fragment`."""
        rc, out, _err = run_git(
            ["ls-tree", "-r", "--name-only", "claude-link"],
            cwd=self.origin, check=False,
        )
        if rc != 0:
            return []
        return [line for line in out.splitlines() if fragment in line]


class TestDelivery(GitTransportCase):
    async def test_a_message_crosses_the_repo(self):
        a = await self.member(a_device("a"))
        b = await self.member(a_device("b"))
        await self.until(lambda: b.identity.device_id in a.roster(),
                         "b's heartbeat to come through the repo")

        frame, envelope = self.a_frame(a.identity, "over the wire, via git")
        self.assertTrue(await a.send(frame, [b.identity.device_id], envelope["msg_id"]))
        await self.until(lambda: b.received, "b to receive")

        self.assertEqual(self.text_of(b.received[0]), "over the wire, via git")
        self.assertEqual(len(a.received), 0, "the sender must not read its own copy")

    async def test_a_repo_that_also_holds_code_is_left_alone(self):
        """The channel branch is a root commit, so it cannot merge into anything.

        This is what makes it safe to point at a repository somebody works in:
        `main` is untouched, and git will never offer the channel as something
        to merge, because the two share no history at all.
        """
        self.seed_code_branch()
        await self.member(a_device("solo"))

        rc, out, _err = run_git(["rev-list", "--max-parents=0", "--count", "claude-link"],
                                cwd=self.origin, check=False)
        self.assertEqual(rc, 0, "the channel branch was never pushed")
        self.assertEqual(out.strip(), "1", "expected exactly one root commit")

        rc, _out, _err = run_git(["merge-base", "claude-link", "main"],
                                 cwd=self.origin, check=False)
        self.assertNotEqual(rc, 0, "the channel branch shares history with the code")

        rc, files, _err = run_git(["ls-tree", "-r", "--name-only", "main"],
                                  cwd=self.origin)
        self.assertEqual(files.strip(), "app.py", "the code branch was modified")

    async def test_a_delivered_frame_is_removed_from_the_repo(self):
        """Delete-on-read has to reach the remote, or the branch grows forever."""
        a = await self.member(a_device("a"))
        b = await self.member(a_device("b"))
        await self.until(lambda: b.identity.device_id in a.roster(), "b to appear")

        frame, envelope = self.a_frame(a.identity, "collect me")
        await a.send(frame, [b.identity.device_id], envelope["msg_id"])
        await self.until(lambda: b.received, "delivery")
        await self.until(lambda: not self.on_remote(f"/{b.identity.device_id}/"),
                         "b's collected copy to disappear from the remote")

    async def test_a_message_is_delivered_once(self):
        a = await self.member(a_device("a"))
        b = await self.member(a_device("b"))
        await self.until(lambda: b.identity.device_id in a.roster(), "b to appear")

        frame, envelope = self.a_frame(a.identity, "exactly once")
        await a.send(frame, [b.identity.device_id], envelope["msg_id"])
        await self.until(lambda: b.received, "delivery")
        await asyncio.sleep(2.0)                    # several more sync rounds
        self.assertEqual(len(b.received), 1)

    async def test_a_stopped_member_finds_its_mail_on_return(self):
        """Offline is not gone: the copy waits in the repo until collected."""
        a = await self.member(a_device("a"))
        b = await self.member(a_device("b"))
        await self.until(lambda: b.identity.device_id in a.roster(), "b to appear")
        await b.stop()
        self.transports.remove(b)
        _rmtree(b.workdir)                     # as if b were on another machine

        frame, envelope = self.a_frame(a.identity, "waiting for you")
        self.assertTrue(await a.send(frame, [b.identity.device_id], envelope["msg_id"]))
        await self.until(lambda: self.on_remote(f"/{b.identity.device_id}/"),
                         "the copy to reach the remote")

        again = await self.member(b.identity)
        await self.until(lambda: again.received, "the queued copy after a fresh clone")
        self.assertEqual(self.text_of(again.received[0]), "waiting for you")

    async def test_a_clone_with_a_broken_git_dir_repairs_itself(self):
        """`.git` gone or half-deleted must not strand the channel forever.

        A delete that got part way, a clone killed mid-write, a backup tool that
        copied the files and not the metadata: all three leave a directory that
        *has* a `.git` and that git refuses to work in, and every later command
        then fails with "not a git repository" until somebody deletes it by hand.
        The frames in the working tree have to survive the repair.
        """
        a = await self.member(a_device("a"))
        b = await self.member(a_device("b"))
        await self.until(lambda: b.identity.device_id in a.roster(), "b to appear")
        workdir = b.workdir
        await b.stop()
        self.transports.remove(b)

        # Gut the metadata, leave the working tree standing.
        _rmtree(os.path.join(workdir, ".git", "refs"))
        with open(os.path.join(workdir, ".git", "HEAD"), "w", encoding="utf-8") as fh:
            fh.write("this is not a ref\n")

        frame, envelope = self.a_frame(a.identity, "after the repair")
        await a.send(frame, [b.identity.device_id], envelope["msg_id"])

        again = await self.member(b.identity)
        await self.until(lambda: again.received, "delivery into the repaired clone")
        self.assertEqual(self.text_of(again.received[0]), "after the repair")


class TestConcurrency(GitTransportCase):
    async def test_both_members_sending_at_once_both_arrive(self):
        """One of these two pushes is rejected. Neither message may be lost."""
        a = await self.member(a_device("a"))
        b = await self.member(a_device("b"))
        await self.until(lambda: b.identity.device_id in a.roster(), "b to appear")
        await self.until(lambda: a.identity.device_id in b.roster(), "a to appear")

        frame_a, env_a = self.a_frame(a.identity, "from a")
        frame_b, env_b = self.a_frame(b.identity, "from b")
        await asyncio.gather(
            a.send(frame_a, [b.identity.device_id], env_a["msg_id"]),
            b.send(frame_b, [a.identity.device_id], env_b["msg_id"]),
        )

        await self.until(lambda: a.received and b.received, "both to arrive")
        self.assertEqual(self.text_of(b.received[0]), "from a")
        self.assertEqual(self.text_of(a.received[0]), "from b")

    async def test_a_burst_keeps_its_order(self):
        a = await self.member(a_device("a"))
        b = await self.member(a_device("b"))
        await self.until(lambda: b.identity.device_id in a.roster(), "b to appear")

        for i in range(6):
            frame, envelope = self.a_frame(a.identity, f"ordered {i}", seq=i + 1)
            await a.send(frame, [b.identity.device_id], envelope["msg_id"])
        await self.until(lambda: len(b.received) >= 6, "all six")
        self.assertEqual([self.text_of(f) for f in b.received[:6]],
                         [f"ordered {i}" for i in range(6)])


class TestRewrittenHistory(GitTransportCase):
    """A squashed branch, which is the one case a rebase cannot be asked to fix.

    Members here run with the heartbeat effectively switched off (`presence_s`
    is an hour) so that the only commits are the ones a test makes. A prune is a
    lease-protected force-push, and a heartbeat landing in the middle of one
    would cancel it -- correctly, but at random, which is not a test.
    """

    async def two_frozen_members(self):
        a = await self.member(a_device("a"), presence_s=3600)
        b = await self.member(a_device("b"), presence_s=3600)
        await self.until(lambda: b.identity.device_id in a.roster(), "b to appear")
        return a, b

    async def quiesce(self, *members):
        """Stop the sync loops, so nothing pushes while a prune is in flight."""
        await self.freeze_sync(*members)

    async def test_a_pruned_branch_does_not_break_the_channel(self):
        """`git-prune` force-pushes a new root. Members must rebuild, not stall.

        Rebasing onto a history with no common ancestor would replay every
        commit this member ever made and resurrect frames collected and deleted
        weeks ago. Adopting the remote instead is the only correct answer, and
        this is the test that says so.
        """
        a, b = await self.two_frozen_members()
        frame, envelope = self.a_frame(a.identity, "before the prune")
        await a.send(frame, [b.identity.device_id], envelope["msg_id"])
        await self.until(lambda: b.received, "the first message")
        await self.quiesce(a, b)

        summary = await asyncio.to_thread(prune_history, self.remote, "claude-link")
        self.assertIn("compacted", summary)
        _rc, out, _err = run_git(["rev-list", "--count", "claude-link"], cwd=self.origin)
        self.assertEqual(out.strip(), "1", "the prune did not squash the branch")

        frame, envelope = self.a_frame(a.identity, "after the prune", seq=2)
        await a.send(frame, [b.identity.device_id], envelope["msg_id"])
        await a._locked_sync()
        await b._locked_sync()
        await self.until(lambda: len(b.received) >= 2, "delivery after the rewrite")

        self.assertEqual(self.text_of(b.received[1]), "after the prune")
        self.assertEqual(a.recoveries, 1, "a did not notice the rewrite")
        self.assertEqual(b.recoveries, 1, "b did not notice the rewrite")

    async def test_an_unsent_frame_survives_a_rebuild(self):
        """The recovery path resets hard. Outbound mail must come back with it."""
        a, b = await self.two_frozen_members()
        await self.quiesce(a)

        # Written into a's clone while nothing of a's is reaching the remote, so
        # it exists only locally when the history underneath it is rewritten.
        frame, envelope = self.a_frame(a.identity, "written but never pushed")
        await a.send(frame, [b.identity.device_id], envelope["msg_id"])
        await asyncio.to_thread(prune_history, self.remote, "claude-link")

        await a._locked_sync()
        self.assertEqual(a.recoveries, 1, "a should have rebuilt from the new root")
        await self.until(lambda: b.received, "the frame that survived the rebuild")
        self.assertEqual(self.text_of(b.received[0]), "written but never pushed")

    async def test_a_rebuild_with_nothing_of_ours_in_the_tree(self):
        """The rebuild has to survive having nothing to put back.

        Two things go wrong here and neither is visible in the happy path. The
        reset lands on a branch with no `claude-link/` at all -- a channel where
        nobody has sent anything yet -- and `clean` then removes the empty
        directories under it, so the next `git add` has a pathspec matching
        nothing, which git treats as fatal and which would kill the transport
        for good. It also removes our own heartbeat, because that file is
        untracked until it is committed, dropping this device off every other
        member's roster.
        """
        a = await self.member(a_device("a"), presence_s=3600)
        await self.freeze_sync(a)

        # A branch with only the seed files on it: no frames, no heartbeats.
        work = os.path.join(self.base, "bare-channel")
        run_git(["clone", "--quiet", "--branch", "claude-link", "--single-branch",
                 self.remote, work])
        for key, value in (("user.name", "test"), ("user.email", "test@localhost"),
                           ("commit.gpgsign", "false")):
            run_git(["config", "--local", key, value], cwd=work)
        run_git(["rm", "-r", "--quiet", "--ignore-unmatch", "--", "claude-link"],
                cwd=work, check=False)
        run_git(["checkout", "--quiet", "--orphan", "__empty"], cwd=work)
        run_git(["add", "-A"], cwd=work)
        run_git(["commit", "--quiet", "--no-gpg-sign", "-m", "an empty channel"],
                cwd=work)
        run_git(["push", "--quiet", "--force", "origin", "HEAD:refs/heads/claude-link"],
                cwd=work)
        self.assertEqual(self.on_remote("claude-link/"), [],
                         "the fixture left channel content on the branch")

        await a._locked_sync()                      # must not raise
        self.assertEqual(a.recoveries, 1)
        self.assertIsNone(a.sync_error, "the rebuild took the transport offline")

        # And we are back on the branch as a member others will write to.
        self.assertTrue(self.on_remote(f"presence/{a.device_id}.json"),
                        "the rebuild dropped our own heartbeat and did not restore it")

        b = await self.member(a_device("b"), presence_s=3600)
        await self.until(lambda: a.device_id in b.roster(),
                         "a to still be discoverable after its rebuild")

    async def test_a_prune_that_races_a_send_is_refused(self):
        """Housekeeping must never be the thing that eats a message.

        The force-push carries a lease against the tip the prune actually read.
        Somebody pushing in between invalidates it, and the branch is left
        exactly as it was rather than rewound over a message nobody has read.
        """
        a, b = await self.two_frozen_members()

        # Everything `prune_history` does, stopped just before the push, so a
        # message can be made to land in the window it is meant to lose.
        work = os.path.join(self.base, "pruner")
        run_git(["clone", "--quiet", "--branch", "claude-link", "--single-branch",
                 self.remote, work])
        for key, value in (("user.name", "test"), ("user.email", "test@localhost"),
                           ("commit.gpgsign", "false")):
            run_git(["config", "--local", key, value], cwd=work)
        _rc, stale, _err = run_git(["rev-parse", "HEAD"], cwd=work)

        frame, envelope = self.a_frame(a.identity, "lands mid-prune")
        await a.send(frame, [b.identity.device_id], envelope["msg_id"])
        await self.until(lambda: b.received, "the message to land first")
        await self.quiesce(a, b)
        _rc, moved, _err = run_git(["rev-parse", "claude-link"], cwd=self.origin)
        self.assertNotEqual(stale, moved, "nothing was pushed, so nothing is at risk")

        run_git(["checkout", "--quiet", "--orphan", "__link_prune"], cwd=work)
        run_git(["add", "-A"], cwd=work)
        run_git(["commit", "--quiet", "--no-gpg-sign", "-m", "compacted"], cwd=work)
        rc, _out, _err = run_git(
            ["push", f"--force-with-lease=refs/heads/claude-link:{stale}",
             "origin", "HEAD:refs/heads/claude-link"],
            cwd=work, check=False,
        )
        self.assertNotEqual(rc, 0, "a stale lease was accepted")
        _rc, after, _err = run_git(["rev-parse", "claude-link"], cwd=self.origin)
        self.assertEqual(moved, after, "the refused push moved the branch anyway")


class TestProbe(unittest.TestCase):
    @unittest.skipUnless(HAS_GIT, "git is not installed")
    def test_a_real_repo_probes_clean(self):
        base = tempfile.mkdtemp(prefix="claude-link-probe-git-")
        try:
            repo = os.path.join(base, "origin.git")
            run_git(["init", "--bare", "--quiet", repo])
            ok, why = probe_git_remote(repo.replace("\\", "/"), "claude-link")
            self.assertTrue(ok, why)
            self.assertIn("will be created", why)
        finally:
            shutil.rmtree(base, ignore_errors=True)

    @unittest.skipUnless(HAS_GIT, "git is not installed")
    def test_a_remote_that_is_not_there_is_reported_not_raised(self):
        base = tempfile.mkdtemp(prefix="claude-link-probe-missing-")
        try:
            ok, why = probe_git_remote(
                os.path.join(base, "no-such-repo.git").replace("\\", "/"))
            self.assertFalse(ok, "a remote that is not there must not read as healthy")
            self.assertTrue(why, "a failure with no explanation is not a diagnosis")
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_no_remote_configured_is_not_an_error_state(self):
        ok, why = probe_git_remote("")
        self.assertFalse(ok)
        self.assertIn("no git_remote", why)

    @unittest.skipUnless(HAS_GIT, "git is not installed")
    def test_git_reports_its_version(self):
        self.assertTrue(git_version())


class TestUrlHandling(unittest.TestCase):
    def test_a_token_in_the_remote_never_reaches_a_transcript(self):
        """This string goes into link_status, the daemon log and `.conv/`."""
        dirty = "https://rick:ghp_averyrealtokenvalue@github.com/rick/channel.git"
        clean = redact_remote(dirty)
        self.assertNotIn("ghp_averyrealtokenvalue", clean)
        self.assertNotIn("rick:", clean)
        self.assertIn("github.com/rick/channel.git", clean)

    def test_an_ordinary_remote_is_left_alone(self):
        for remote in ("https://github.com/rick/channel.git",
                       "git@github.com:rick/channel.git",
                       "/srv/git/channel.git"):
            self.assertEqual(redact_remote(remote), remote)

    def test_the_github_slug_is_read_from_either_url_form(self):
        for remote in ("https://github.com/rick/channel.git",
                       "https://github.com/rick/channel",
                       "git@github.com:rick/channel.git",
                       "ssh://git@github.com/rick/channel.git"):
            self.assertEqual(_github_slug(remote), "rick/channel", remote)

    def test_two_branches_of_one_repo_do_not_share_a_clone(self):
        base = os.path.join("home", "claude-link")
        one = clone_dir(base, "https://github.com/rick/channel.git", "claude-link")
        two = clone_dir(base, "https://github.com/rick/channel.git", "other")
        three = clone_dir(base, "https://github.com/someone/channel.git", "claude-link")
        self.assertNotEqual(one, two)
        self.assertNotEqual(one, three)
        self.assertIn("channel", os.path.basename(one))

    def test_the_clone_never_lands_in_a_directory_someone_works_in(self):
        path = clone_dir(os.path.join("home", "claude-link"),
                         "https://github.com/rick/channel.git", "claude-link")
        self.assertIn(os.path.join("claude-link", "git"), path)


class TestReadingAFailedPush(unittest.TestCase):
    """Telling a lost race apart from a dead remote, which is not cosmetic.

    A race is retried; anything else is raised and takes the channel offline.
    Read the wrong line of git's output and every race becomes a hard failure,
    which is what happened: `git push` leads with `To <url>` and puts the
    verdict underneath, so the obvious first-line read gave back the URL.
    """

    REJECTED = (
        "To https://github.com/rick/channel.git\n"
        " ! [rejected]        HEAD -> claude-link (non-fast-forward)\n"
        "error: failed to push some refs to "
        "'https://github.com/rick/channel.git'\n"
        "hint: Updates were rejected because the tip of your current branch is "
        "behind\n"
    )
    NO_CREDENTIAL = (
        "fatal: could not read Username for 'https://github.com': "
        "terminal prompts disabled\n"
    )
    STALE_LEASE = (
        "To https://github.com/rick/channel.git\n"
        " ! [rejected]        HEAD -> claude-link (stale info)\n"
    )

    def test_a_rejected_push_is_read_as_a_race(self):
        self.assertTrue(_is_race(self.REJECTED))
        self.assertTrue(_is_race(self.STALE_LEASE))

    def test_a_missing_credential_is_not_read_as_a_race(self):
        self.assertFalse(_is_race(self.NO_CREDENTIAL),
                         "retrying this four more times helps nobody")

    def test_the_reason_shown_is_not_the_url(self):
        reason = _push_reason(self.REJECTED)
        self.assertIn("non-fast-forward", reason)
        self.assertFalse(reason.startswith("To "), f"reported the header: {reason}")
        self.assertFalse(reason.startswith("hint:"))

    def test_a_reason_with_nothing_to_skip_still_comes_back(self):
        self.assertIn("could not read Username", _push_reason(self.NO_CREDENTIAL))
        self.assertEqual(_push_reason(""), "")


class TestGitIsNotInteractive(unittest.TestCase):
    """There is no terminal behind the daemon, so a prompt is a deadlock.

    Every one of these turns a question git might ask into an error it reports
    instead. The subprocess timeout is the backstop; this is the intent.
    """

    def test_nothing_can_ask_for_a_credential(self):
        env = _git_env()
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "never")
        self.assertEqual(env["GCM_INTERACTIVE"], "never")
        self.assertIn("BatchMode=yes", env["GIT_SSH_COMMAND"])

    def test_a_users_own_ssh_command_is_not_overridden(self):
        os.environ["GIT_SSH_COMMAND"] = "ssh -i /home/me/.ssh/work"
        try:
            self.assertEqual(_git_env()["GIT_SSH_COMMAND"], "ssh -i /home/me/.ssh/work")
        finally:
            os.environ.pop("GIT_SSH_COMMAND", None)

    def test_a_stray_git_dir_in_the_environment_cannot_redirect_us(self):
        """Inherited from a shell inside another repo, this would aim every
        command in this module at that repository instead."""
        os.environ["GIT_DIR"] = os.path.join("somewhere", "else", ".git")
        os.environ["GIT_WORK_TREE"] = os.path.join("somewhere", "else")
        try:
            env = _git_env()
            self.assertNotIn("GIT_DIR", env)
            self.assertNotIn("GIT_WORK_TREE", env)
        finally:
            os.environ.pop("GIT_DIR", None)
            os.environ.pop("GIT_WORK_TREE", None)


class TestWhatIsAllowedToBeARemote(unittest.TestCase):
    """`git_remote` reaches `git` as an argument, and git's remote *helpers* run
    commands. `ext::sh -c '<cmd>'` is not a URL, it is a shell line git will
    execute, and git's default `protocol.ext.allow=user` permits it for exactly
    the kind of direct invocation this module makes.

    That mattered because the value is settable from `link_join`, from
    `config --set`, and from anything that could reach the daemon's control
    socket -- which, before the token, was any local process and a web page.
    """

    def test_a_remote_helper_is_refused(self):
        for hostile in ("ext::sh -c 'id > /tmp/pwned'",
                        "ext::whoami",
                        "fd::7/repo"):
            with self.assertRaises(BadRemote, msg=hostile):
                check_remote(hostile)

    def test_something_git_would_read_as_an_option_is_refused(self):
        for hostile in ("--upload-pack=id", "-x", "--exec=touch /tmp/x"):
            with self.assertRaises(BadRemote, msg=hostile):
                check_remote(hostile)

    def test_control_characters_are_refused(self):
        with self.assertRaises(BadRemote):
            check_remote("https://example.com/a\nb")

    def test_the_ordinary_forms_are_accepted(self):
        for good in ("https://github.com/you/repo.git",
                     "http://localhost:3000/repo",
                     "ssh://git@github.com/you/repo.git",
                     "git://example.com/repo",
                     "git@github.com:you/repo.git",
                     "https://user:token@github.com/you/repo.git"):
            self.assertEqual(check_remote(good), good, good)

    def test_an_absolute_path_is_accepted_even_when_it_does_not_exist(self):
        """A missing repo is a reachability problem, reported as `setup_error`
        on a room that came up anyway. Refusing it here would turn a mistyped
        path into a room that never starts -- which is precisely the failure
        TestABrokenRepoDoesNotTakeTheRoomDown exists to prevent."""
        missing = os.path.abspath(os.path.join("no", "such", "repo.git"))
        self.assertEqual(check_remote(missing), missing)

    def test_the_protocol_allow_list_is_pinned_on_every_invocation(self):
        """Belt and braces behind check_remote: even a scheme that slips past
        the validator cannot start a helper."""
        with patch("link.transport_git.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
            run_git(["status"], cwd=os.getcwd(), check=False)
        argv = run.call_args[0][0]
        self.assertIn("protocol.ext.allow=never", argv)
        self.assertIn("protocol.allow=never", argv)


class TestTheStagingRace(unittest.TestCase):
    """`git add -A` stats the subtree, then opens what it found.

    `_sync_once` runs on a worker thread while delete-on-read removes collected
    frames on the event loop, so a frame can disappear between git's readdir
    and its open. git reports a file it has just listed as missing and exits
    non-zero, and the exception used to take the whole sync round with it. Once
    in four runs of `ubuntu-latest / py3.14`; the first fixture line below is
    copied verbatim from the log of the run that caught it (job 93084377016).

    Classified rather than swallowed. A non-zero `add` that says anything other
    than "this file is gone" is still a failure, and so is one that says
    nothing at all.
    """

    FRAME = "claude-link/room_x/out/dev_a/dev_b/1786180120055-msg_2.json"
    HEARTBEAT = "claude-link/room_x/presence/dev_g25vxyru2rqd7rwq.json"

    # Three faces, because git touches a file three times and the tree can move
    # between any two of them. All three are real, all three copied from CI.
    STAT = f"fatal: unable to stat '{FRAME}': No such file or directory\n"
    OPEN = (
        f'error: open("{FRAME}"): No such file or directory\n'
        f"error: unable to index file '{FRAME}'\n"
    )
    SHORT = (
        f"error: short read while indexing {HEARTBEAT}\n"
        f"error: unable to index file '{HEARTBEAT}'\n"
    )

    def test_a_frame_gone_before_the_stat_is_a_race(self):
        """`die_errno`, so `--ignore-errors` cannot help and only a retry can.
        From job 93086743582, ubuntu-latest / py3.12."""
        self.assertTrue(_add_race(self.STAT))

    def test_a_frame_gone_between_the_stat_and_the_open_is_a_race(self):
        """From job 93084377016, ubuntu-latest / py3.14."""
        self.assertTrue(_add_race(self.OPEN))

    def test_a_heartbeat_replaced_mid_read_is_a_race(self):
        """An atomic write landing between git's stat and its read: it sizes
        the file, then reads fewer bytes than it was promised. From job
        93086747240, windows-latest / py3.12."""
        self.assertTrue(_add_race(self.SHORT))

    def test_several_at_once_are_still_a_race(self):
        self.assertTrue(_add_race(self.OPEN + self.SHORT))

    def test_an_unrelated_failure_is_not_a_race(self):
        self.assertIsNone(_add_race("fatal: not a git repository"))

    def test_a_race_beside_a_real_failure_is_not_a_race(self):
        """`--ignore-errors` keeps going, so this summary means something else
        went wrong as well and the round must still fail."""
        self.assertIsNone(_add_race(self.OPEN + "fatal: adding files failed\n"))

    def test_a_different_reason_for_not_opening_it_is_not_a_race(self):
        """Permission denied is not a collected frame. It is a broken clone,
        and reporting it as routine housekeeping would hide it forever."""
        self.assertIsNone(_add_race(f'error: open("{self.FRAME}"): Permission denied\n'))

    def test_silence_is_not_a_race(self):
        """A non-zero exit with nothing to say must never be swallowed."""
        self.assertIsNone(_add_race(""))
        self.assertIsNone(_add_race("  \n\n"))


@unittest.skipIf(_SKIP, _SKIP)
class TestStagingSurvivesTheRace(GitTransportCase):
    """The same thing against a real repository.

    Only the one `add` invocation is stood in for, because the interleaving
    itself cannot be produced on demand: git decides when it reads the
    directory. Everything either side of it is real git.
    """

    async def a_clone(self):
        member = await self.member(a_device("a"))
        await self.freeze_sync(member)
        return member

    def flaky_add(self, member, stderr, times):
        """`member._git`, with the first `times` adds losing to the reader."""
        real_git = member._git
        state = {"left": times}

        def fake(args, **kw):
            if args[:2] != ["add", "-A"] or state["left"] <= 0:
                return real_git(args, **kw)
            state["left"] -= 1
            if kw.get("check", True):
                raise GitError(f"git add -A: {stderr.splitlines()[0]}")
            return (1, "", stderr)

        return fake

    async def test_a_retry_gets_the_staging_through(self):
        """The tree stops moving. It is a bounded set of frames being collected,
        not a permanent condition, so the second attempt is the normal outcome."""
        member = await self.a_clone()
        frame = os.path.join(member.workdir, REPO_SUBDIR, "sent.json")
        os.makedirs(os.path.dirname(frame), exist_ok=True)
        with open(frame, "w", encoding="utf-8") as fh:
            fh.write("{}")

        fake = self.flaky_add(member, TestTheStagingRace.STAT, times=1)
        with patch.object(member, "_git", side_effect=fake):
            self.assertTrue(await asyncio.to_thread(member._commit_local))
        _rc, tracked, _err = member._git(["ls-files", "--", REPO_SUBDIR])
        self.assertIn("sent.json", tracked)

    async def test_losing_every_attempt_leaves_it_for_the_next_round(self):
        """Not an exception. The frames are still on disk and the round after
        this one will stage them; killing the sync loop would not."""
        member = await self.a_clone()
        fake = self.flaky_add(member, TestTheStagingRace.OPEN, times=99)
        with patch.object(member, "_git", side_effect=fake):
            self.assertFalse(await asyncio.to_thread(member._commit_local))

    async def test_a_short_read_is_survived_too(self):
        member = await self.a_clone()
        fake = self.flaky_add(member, TestTheStagingRace.SHORT, times=1)
        with patch.object(member, "_git", side_effect=fake):
            await asyncio.to_thread(member._commit_local)

    async def test_a_real_failure_while_staging_still_raises(self):
        member = await self.a_clone()
        fake = self.flaky_add(member, "fatal: index file corrupt\n", times=99)
        with patch.object(member, "_git", side_effect=fake):
            with self.assertRaises(GitError):
                await asyncio.to_thread(member._commit_local)

    async def test_the_deletion_is_recorded_on_the_next_round(self):
        """Nothing is lost by not staging it now. The file is gone from the
        working tree, so the following `add -A` stages it as a deletion."""
        member = await self.a_clone()
        frame = os.path.join(member.workdir, REPO_SUBDIR, "kept.json")
        os.makedirs(os.path.dirname(frame), exist_ok=True)
        with open(frame, "w", encoding="utf-8") as fh:
            fh.write("{}")
        await asyncio.to_thread(member._commit_local)

        os.remove(frame)
        self.assertTrue(await asyncio.to_thread(member._commit_local))
        _rc, tracked, _err = member._git(["ls-files", "--", REPO_SUBDIR])
        self.assertNotIn("kept.json", tracked)


class TestTheRebaseRace(unittest.TestCase):
    """`git rebase` refuses to start when the tree moved since the commit.

    The same event as the staging race, one step further on: `_commit_local`
    commits on a worker thread, then delete-on-read or the heartbeat rewrite
    moves the subtree again on the event loop before the rebase underneath it
    runs. git exits 1, which is also what a real conflict exits.

    Reading the two as one was expensive. Every one of these was answered by
    `_recover` -- copy every unsent frame out, `reset --hard`, `clean -qfd`, put
    only the last ten minutes back -- and counted as a recovery, so the counter
    that was supposed to mean "somebody squashed the branch" was mostly counting
    housekeeping. Five in forty seconds on an idle two-member channel, on a
    developer machine, which is how it surfaced.

    Both fixtures come from real git. 2.53.0.windows.1 locally and
    2.55.0.windows.3 on the CI runner word them identically.
    """

    UNSTAGED = ("error: cannot rebase: You have unstaged changes.\n"
                "error: Please commit or stash them.\n")
    INDEX = ("error: cannot rebase: Your index contains uncommitted changes.\n"
             "error: Please commit or stash them.\n")

    def test_a_frame_collected_after_the_commit_is_a_race(self):
        self.assertTrue(_rebase_race(self.UNSTAGED))

    def test_an_index_that_moved_is_a_race_too(self):
        self.assertTrue(_rebase_race(self.INDEX))

    def test_a_real_conflict_is_not_a_race(self):
        """The case `_recover` exists for. Retrying it would spin, and the
        remote's history really does have to be adopted."""
        self.assertIsNone(_rebase_race(
            "CONFLICT (add/add): Merge conflict in claude-link/room_x/out/f.json\n"
            "error: could not apply 0a1b2c3... link dev_a: 1 change(s)\n"))

    def test_a_broken_repository_is_not_a_race(self):
        self.assertIsNone(_rebase_race("fatal: index file corrupt\n"))

    def test_the_advice_line_on_its_own_says_nothing(self):
        """git prints it under either refusal, but it names no cause, and a
        non-zero exit nobody has explained must not be retried into recovery."""
        self.assertIsNone(_rebase_race("error: Please commit or stash them.\n"))

    def test_silence_is_not_a_race(self):
        self.assertIsNone(_rebase_race(""))
        self.assertIsNone(_rebase_race("  \n\n"))


@unittest.skipIf(_SKIP, _SKIP)
class TestRebaseSurvivesTheRace(GitTransportCase):
    """The same thing against a real repository.

    The first test stubs nothing at all: the tree is genuinely dirty when the
    rebase runs, which is the one part of this race that can be produced on
    demand -- unlike the `add` interleaving, where git decides when it reads the
    directory.
    """

    async def diverged(self):
        """`a`, holding a commit the remote does not, and behind one it does.

        Both members are frozen first, so the only rounds after this are the
        ones a test drives by hand. Counters are read *after* it returns and
        compared as deltas, never against zero: both members run live for the
        moment between `start` and `freeze_sync`, and this race is frequent
        enough to fire inside it. That it can is the finding, not an accident of
        the fixture.
        """
        a = await self.member(a_device("a"))
        b = await self.member(a_device("b"))
        await self.freeze_sync(a, b)

        self.write_frame(b, "from-b.json")
        await asyncio.to_thread(b._sync_once)       # now on the remote

        path = self.write_frame(a, "from-a.json")
        await asyncio.to_thread(a._commit_local)    # ours, not published yet
        return a, path

    def write_frame(self, member, name, body="{}"):
        path = os.path.join(member.workdir, REPO_SUBDIR, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    async def test_a_tree_that_moved_is_committed_rather_than_rebuilt(self):
        a, mine = await self.diverged()
        # What delete-on-read and the heartbeat do to the subtree between the
        # commit above and the rebase below. A tracked file, so git refuses.
        with open(mine, "w", encoding="utf-8") as fh:
            fh.write('{"rewritten": true}')

        rebuilds, races = a.recoveries, a.rebase_races
        await asyncio.to_thread(a._integrate)

        self.assertEqual(a.recoveries, rebuilds,
                         "a tree that moved is not a branch somebody rewrote")
        self.assertGreaterEqual(a.rebase_races, races + 1)
        _rc, tracked, _err = a._git(["ls-files", "--", REPO_SUBDIR])
        self.assertIn("from-a.json", tracked, "our own frame survived")
        self.assertIn("from-b.json", tracked, "and theirs came in")
        # What the change did, not what the tree looks like afterwards. Asking
        # for a clean tree here failed on `windows-latest / py3.12` in run
        # 31309678360 with `M claude-link/room_.../presence/...`, and the daemon
        # was right: `freeze_sync` stops the git sync loop, not the poll loop it
        # inherits from `FileTransport`, so the heartbeat keeps being rewritten.
        # On a slow enough runner one lands between `_integrate` returning and
        # the next line. The tree moving is the premise of this whole test; an
        # assertion that it has stopped is asserting the bug is absent.
        _rc, committed, _err = a._git(["show", f"HEAD:{REPO_SUBDIR}/from-a.json"])
        self.assertIn("rewritten", committed,
                      "the change that blocked the rebase is in the history")

    async def test_a_real_conflict_still_rebuilds_the_clone(self):
        """The fix must not cost the recovery path. A rebase that fails for a
        reason retrying cannot settle has to reach `_recover` on the first
        attempt, exactly as before."""
        a, _mine = await self.diverged()
        conflict = ("CONFLICT (add/add): Merge conflict in x.json\n"
                    "error: could not apply 0a1b2c3\n")
        rebuilds, races = a.recoveries, a.rebase_races
        with patch.object(a, "_git", side_effect=self.rebase_fails(a, conflict)):
            await asyncio.to_thread(a._integrate)
        self.assertEqual(a.recoveries, rebuilds + 1)
        self.assertEqual(a.rebase_races, races, "a conflict is not a race")

    async def test_a_subtree_that_never_settles_falls_back_to_rebuilding(self):
        """Something rewriting it continuously is not a state to keep retrying
        inside one round, and the clone may genuinely be unusable."""
        a, _mine = await self.diverged()
        rebuilds, races = a.recoveries, a.rebase_races
        with patch.object(a, "_git",
                          side_effect=self.rebase_fails(a, TestTheRebaseRace.UNSTAGED)):
            await asyncio.to_thread(a._integrate)
        self.assertEqual(a.recoveries, rebuilds + 1)
        self.assertEqual(a.rebase_races, races + REBASE_ATTEMPTS - 1,
                         "every attempt but the last is counted as a race")

    def rebase_fails(self, member, stderr):
        """`member._git`, with every `rebase` refusing. `--abort` stays real."""
        real_git = member._git

        def fake(args, **kw):
            if args[:1] == ["rebase"] and args[1:2] != ["--abort"]:
                return (1, "", stderr)
            return real_git(args, **kw)

        return fake


class TestTheSharedRepoWarning(unittest.TestCase):
    """Attaching to a repo the members already share is the cheapest setup
    there is, and the orphan branch makes it safe for their code. It is not
    safe for their CI: a heartbeat lands in a commit of its own every 45 s and
    each one is a push, so a workflow with `on: push` and no branch filter runs
    about nineteen hundred times a day on somebody else's Actions bill.

    The two lookups are network calls, so they are stubbed here and the
    decision they feed is what gets tested. What matters most is the silence:
    a warning that fires on a dedicated channel repo, or on a repository nobody
    could read, is noise that teaches people to ignore the real one.
    """

    def warn(self, branches, workflows, **kw):
        with patch("link.transport_git.remote_branches", return_value=branches), \
             patch("link.transport_git.github_workflows", return_value=workflows):
            return shared_repo_warning("https://github.com/you/repo.git", **kw)

    def test_a_project_repo_with_workflows_is_flagged(self):
        note = self.warn(["main", "claude-link"], ["tests.yml", "release.yml"])
        self.assertIsNotNone(note)
        self.assertIn("branches-ignore", note)
        self.assertIn("tests.yml", note)

    def test_the_count_follows_the_heartbeat_interval(self):
        note = self.warn(["main"], ["tests.yml"], presence_s=45.0)
        self.assertIn("1920", note)
        slower = self.warn(["main"], ["tests.yml"], presence_s=3600.0)
        self.assertIn("24", slower)

    def test_a_dedicated_channel_repo_says_nothing(self):
        """Nothing but our own branch: the repo exists to be a channel."""
        self.assertIsNone(self.warn(["claude-link"], ["tests.yml"]))
        self.assertIsNone(self.warn([], None))

    def test_a_project_repo_with_no_workflows_says_nothing(self):
        self.assertIsNone(self.warn(["main", "dev"], []))

    def test_an_unreadable_remote_says_nothing(self):
        """`remote_branches` returning None is "could not tell". A warning that
        cannot be produced is better than one that is invented."""
        self.assertIsNone(self.warn(None, ["tests.yml"]))

    def test_no_gh_still_raises_the_question(self):
        """`github_workflows` is None whenever `gh` is missing, which is most
        machines. Staying silent there would hide the hazard in exactly the
        common case, so the repo holding other branches is enough to say it."""
        note = self.warn(["main", "feature/x"], None)
        self.assertIsNotNone(note)
        self.assertIn("branches-ignore", note)

    def test_the_branch_name_in_the_advice_is_the_configured_one(self):
        note = self.warn(["main"], ["tests.yml"], branch="side-channel")
        self.assertIn("branches-ignore: [side-channel]", note)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTheCloneNeverAdoptsAnEnclosingRepository(GitTransportCase):
    """`_is_repo` asked `git rev-parse --git-dir`, and git answers that by
    walking **up**.

    So a workdir anywhere underneath a repository answered yes, `git init` was
    skipped, and every command after it drove the enclosing repository instead:
    `remote set-url origin` repointed the user's remote at the channel,
    `checkout -B` moved them off their branch, their `user.email` was
    overwritten, their working tree was committed and pushed somewhere every
    room member can read, and the first `_recover` ran `reset --hard` plus
    `clean -qfd` in it.

    The default clone path is under `~/.claude/`, so reaching this needed
    nothing more exotic than keeping dotfiles in git. It falsifies the claim
    the README leads with: that your code is never touched.
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # A repository of somebody's own, with work in it, and the clone
        # directory sitting inside it exactly where the default layout puts it.
        self.theirs = os.path.join(self.base, "their-project")
        os.makedirs(self.theirs)
        run_git(["init", "--quiet", "-b", "main", self.theirs])
        run_git(["remote", "add", "origin", "https://example.invalid/theirs.git"],
                cwd=self.theirs)
        with open(os.path.join(self.theirs, "NOTES.md"), "w", encoding="utf-8") as fh:
            fh.write("their work\n")
        run_git(["add", "-A"], cwd=self.theirs)
        run_git(["-c", "user.email=them@example.com", "-c", "user.name=Them",
                 "commit", "--quiet", "--no-verify", "--no-gpg-sign",
                 "-m", "their commit"], cwd=self.theirs)

    def a_transport(self):
        return GitTransport(
            remote=self.remote, branch="claude-link",
            workdir=os.path.join(self.theirs, ".claude", "claude-link", "git", "c"),
            room_id=self.keys.room_id, device_id="dev_" + "a" * 16,
            on_frame=lambda _f: asyncio.sleep(0),
            poll_ms=50, sync_ms=100_000, presence_s=999.0, log=lambda _m: None)

    async def test_a_directory_inside_a_repository_is_not_that_repository(self):
        t = self.a_transport()
        os.makedirs(t.workdir, exist_ok=True)
        self.assertFalse(
            t._is_repo(),
            "a directory under somebody's repo answered that it was one")

    async def test_it_gets_a_repository_of_its_own(self):
        t = self.a_transport()
        await asyncio.to_thread(t._prepare_repo)
        self.assertTrue(os.path.isdir(os.path.join(t.workdir, ".git")),
                        "no repository was created; git walked up instead")

    async def test_their_remote_and_branch_are_left_alone(self):
        t = self.a_transport()
        await asyncio.to_thread(t._prepare_repo)

        _rc, remote, _e = run_git(["remote", "get-url", "origin"], cwd=self.theirs)
        self.assertEqual(remote.strip(), "https://example.invalid/theirs.git",
                         "their origin was repointed at the channel remote")
        _rc, branch, _e = run_git(["rev-parse", "--abbrev-ref", "HEAD"],
                                  cwd=self.theirs)
        self.assertEqual(branch.strip(), "main", "they were checked out onto the channel")
        _rc, subject, _e = run_git(["log", "-1", "--pretty=%s"], cwd=self.theirs)
        self.assertEqual(subject.strip(), "their commit",
                         "the channel committed into their repository")

    async def test_a_gutted_dot_git_is_still_rebuilt(self):
        """The case `_is_repo` exists for, which must keep working: a `.git`
        the filesystem is happy with and git is not."""
        t = self.a_transport()
        os.makedirs(os.path.join(t.workdir, ".git"), exist_ok=True)
        self.assertFalse(t._is_repo())
        await asyncio.to_thread(t._prepare_repo)
        self.assertTrue(t._is_repo(), "the unusable .git was not rebuilt")


class TestWhatReachesTheSharedBranch(unittest.TestCase):
    """The git host and every room member read this branch. Two things were
    reaching it that the threat model says do not."""

    def test_the_committer_identity_cannot_be_inherited_from_the_shell(self):
        """`_git_env` popped the four `GIT_DIR`-family variables and stopped.
        `GIT_AUTHOR_*` and `GIT_COMMITTER_*` outrank the repo-local `user.name`
        and `user.email` this transport sets, so a daemon started from an
        ordinary developer shell committed the operator's real name and work
        email to a branch their whole room can read. The pseudonymous committer
        exists precisely so that it does not."""
        from link import transport_git as tg

        hostile = {
            "GIT_AUTHOR_NAME": "Real Name", "GIT_AUTHOR_EMAIL": "me@employer.example",
            "GIT_COMMITTER_NAME": "Real Name", "GIT_COMMITTER_EMAIL": "me@employer.example",
            "EMAIL": "me@employer.example", "GIT_CONFIG_GLOBAL": "/tmp/theirs",
            "GIT_CONFIG_COUNT": "1", "GIT_DIR": "/somewhere/else",
        }
        with unittest.mock.patch.dict(os.environ, hostile, clear=False):
            env = tg._git_env()
        for name in hostile:
            self.assertNotIn(name, env, name)

    def test_a_unc_path_is_not_a_usable_remote(self):
        """`os.path.isabs` says yes to one on Windows, and Windows then
        authenticates to whoever it names. Settable from `link_join`, from
        `config --set` and from the control socket."""
        from link.transport_git import BadRemote, check_remote

        for hostile in ("//attacker.example/share/repo",
                        "\\attacker.example\share\repo"):
            with self.assertRaises(BadRemote, msg=hostile):
                check_remote(hostile)

    def test_an_ordinary_remote_still_works(self):
        from link.transport_git import check_remote

        for good in ("https://github.com/a/b.git", "git@github.com:a/b.git",
                     "ssh://git@host/a/b.git"):
            self.assertEqual(check_remote(good), good)


class TestTheChannelWillNotRunOnACodeBranch(unittest.TestCase):
    """`git_branch` was taken as given while the remote beside it was checked
    carefully. `config --set git_branch=main` put heartbeats and frames on the
    user's `main`, and `git-prune` then compacted that branch's whole history
    into one orphan commit and force-pushed it, the lease satisfied because our
    own heartbeat was the tip. Three commits of somebody's work, gone.

    Refused rather than warned about. A warning during setup is read once; this
    destroys history."""

    def test_the_default_is_fine(self):
        from link.transport_git import DEFAULT_BRANCH, check_branch

        self.assertEqual(check_branch(None), DEFAULT_BRANCH)
        self.assertEqual(check_branch("claude-link"), "claude-link")

    def test_a_branch_of_your_own_is_fine(self):
        from link.transport_git import check_branch

        self.assertEqual(check_branch("team/side-channel"), "team/side-channel")

    def test_the_branches_people_keep_code_on_are_refused(self):
        from link.transport_git import BadRemote, check_branch

        for name in ("main", "master", "MAIN", "develop", "trunk", "gh-pages"):
            with self.assertRaises(BadRemote, msg=name):
                check_branch(name)

    def test_a_name_git_would_read_as_something_else_is_refused(self):
        from link.transport_git import BadRemote, check_branch

        for name in ("-x", "../evil", "a..b", "x.lock", "with space"):
            with self.assertRaises(BadRemote, msg=repr(name)):
                check_branch(name)

    def test_empty_means_the_default_rather_than_an_error(self):
        """`git_branch=` is how somebody clears the setting."""
        from link.transport_git import DEFAULT_BRANCH, check_branch

        self.assertEqual(check_branch(""), DEFAULT_BRANCH)


class TestARebuildSaysWhatItThrewAway(unittest.TestCase):
    """`_recover` restores outbound frames younger than ten minutes and
    silently discarded the rest. "Almost certainly collected already" holds for
    frames that reached the remote; a device whose pushes have been failing for
    longer is holding frames nobody has seen, and the first recovery after the
    network returns eats them without a word."""

    def test_it_counts_what_it_did_not_restore(self):
        from link.transport_git import RESTORE_WINDOW_MS, _restore_recent
        from link.util import now_ms

        stash = tempfile.mkdtemp(prefix="claude-link-stash-")
        dest = tempfile.mkdtemp(prefix="claude-link-dest-")
        self.addCleanup(shutil.rmtree, stash, ignore_errors=True)
        self.addCleanup(shutil.rmtree, dest, ignore_errors=True)

        fresh = os.path.join(stash, f"{now_ms()}-msg_{'a' * 16}.json")
        stale = os.path.join(
            stash, f"{now_ms() - RESTORE_WINDOW_MS - 60_000}-msg_{'b' * 16}.json")
        for path in (fresh, stale):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{}")

        restored, dropped = _restore_recent(stash, dest)
        self.assertEqual((restored, dropped), (1, 1))
