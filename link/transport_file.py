"""Message delivery through a directory, and the engine under the git channel.

**This is no longer offered as a carrier of its own.** Pointing two machines at
a folder they both sync was once the headline setup and was the least proven
thing in the project: every test here drives a local temp directory, which is
instant, atomic and has no opinions, while OneDrive and Dropbox bring sync
latency in minutes, conflict copies nothing parses, partial writes the peer can
see, and clients that skip the dotfiles and `.tmp-` files atomic writes rely on.
It never carried a real message between two machines. `doctor` says so if a
`shared_dir` is still configured, and the path keeps working rather than going
quiet, but nothing recommends it.

What did not change is that this file is load-bearing. `GitTransport` is a
subclass of `FileTransport` pointed at a local clone instead of at a synced
folder, so everything below -- the per-recipient fan-out, delete-on-read, the
dedupe set, presence heartbeats, sender-side garbage collection -- is what the
git channel runs on, and the tests over it earn their place for that reason.
Latency is one poll interval (250 ms by default) rather than sub-millisecond.

Layout under `<shared_dir>/claude-link/<room_id>/`:

    out/<sender>/<recipient>/<ts_ms>-<msg_id>.json   one sealed frame per file
    presence/<device_id>.json                        heartbeat, never deleted

The sender writes **one copy per recipient**. That costs N times the bytes and
buys the invariant the rest of the file rests on: every directory has exactly
one writer (the sender) and exactly one destructive reader (the recipient), so
reading may unlink and no two machines ever contend for the same name. A single
shared queue would be cheaper and wrong -- in a three-member room whichever
member polled first would consume the frame and the third would never see it.

Presence is the exception to "one writer": each device writes only its own file
and nobody ever deletes another's, because absence would be indistinguishable
from a member who has simply not started yet. Staleness carries that signal
instead, and the fan-out set is every device whose heartbeat is younger than the
retention window, minus this one.

Frames are opaque here: the payload is ciphertext, `msg_id` lives inside it, and
this module reads nothing but the routing header it needs to check the file
landed in the directory its path claims.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Iterable

from .crypto import is_device_id
from .envelope import MAX_FRAME_BYTES, validate_frame
from .util import atomic_write_text, new_id, now_iso, now_ms, read_json

PRESENCE_REFRESH_S = 5.0          # how often we rewrite our own heartbeat
PRESENCE_SCAN_S = 1.0             # how often we re-read everyone else's
PRESENCE_STALE_S = 30.0           # heartbeat age past which a member is away
RETENTION_S = 7 * 24 * 3600.0     # matches the relay's mailbox retention
# How long `stop` lets the poll loop finish the batch it has already unlinked
# from the share before giving up on it. Long enough for any real delivery,
# short enough that a wedged `on_frame` cannot turn a shutdown into a hang.
STOP_DRAIN_TIMEOUT_S = 10.0
GC_INTERVAL_S = 300.0
DRAIN_BATCH = 128                 # frames read per sender per poll
SEEN_LIMIT = 5000

_UNSAFE_IN_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def room_root(shared_dir: str, room_id: str) -> str:
    # The on-disk subtree, which every member has to agree on. A wire
    # constant like the branch name, and renamed for neither.
    return os.path.join(shared_dir, "claude-link", room_id)


def out_root(shared_dir: str, room_id: str, sender: str) -> str:
    return os.path.join(room_root(shared_dir, room_id), "out", sender)


def out_dir(shared_dir: str, room_id: str, sender: str, recipient: str) -> str:
    return os.path.join(out_root(shared_dir, room_id, sender), recipient)


def presence_dir(shared_dir: str, room_id: str) -> str:
    return os.path.join(room_root(shared_dir, room_id), "presence")


class FileTransport:
    """One room's view of one shared folder, for one device.

    Fans sealed frames out to every recipient, polls `out/*/<me>/` for the ones
    addressed here, and keeps a heartbeat alive so the other members know to
    include this device in their own fan-out.

    All filesystem calls run in a worker thread: a stalled UNC share must never
    block the daemon's event loop, which is also serving the local MCP clients.
    """

    def __init__(
        self,
        shared_dir: str,
        room_id: str,
        device_id: str,
        on_frame: Callable[[dict[str, Any]], Awaitable[None]],
        poll_ms: int = 250,
        retention_s: float = RETENTION_S,
        log: Callable[[str], None] | None = None,
        presence: bool = True,
    ) -> None:
        self.shared_dir = shared_dir
        self.room_id = room_id
        self.device_id = device_id
        self.on_frame = on_frame
        self.poll_s = max(0.05, poll_ms / 1000.0)
        self.retention_s = max(60.0, float(retention_s))
        self.log = log or (lambda _m: None)
        # False for a knocker: it must sync the carrier without ever
        # heartbeating a room it is not a member of — a heartbeat would put it
        # in everyone's fan-out set and mail it frames it cannot read.
        self.write_presence = bool(presence)
        # An attribute rather than the constant, because a subclass may sit on a
        # medium where writing the heartbeat is not free. Rewriting one file
        # every five seconds costs a synced folder nothing and costs the git
        # transport a commit, so that one raises it. See `transport_git.py`.
        self.presence_refresh_s = PRESENCE_REFRESH_S

        self._seen: OrderedDict[str, float] = OrderedDict()
        self._presence: dict[str, float] = {}
        # Wall-clock time of the last presence scan. The poll loop tracks its
        # own monotonic schedule; this exists so the send path can tell whether
        # the cached roster is fresh enough to fan out against.
        self._presence_at = 0.0
        self._gc_at: float = 0.0
        self._last_ms: int = 0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_error: str | None = None
        self.messages_in = 0
        self.messages_out = 0
        self.copies_out = 0

    # -- lifecycle --------------------------------------------------------- #

    async def start(self) -> None:
        # The heartbeat is written before the loop starts so that a send issued
        # in the same breath as start() already sees a share we are visible on.
        await asyncio.to_thread(self._bootstrap)
        self._stop.clear()
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"file-{self.room_id[:12]}"
        )

    async def stop(self) -> None:
        """Let the loop finish what it is holding, then stop it.

        Waited out rather than cancelled, for the reason written above the
        per-frame try/except in `_poll_loop`: `_drain` unlinks every frame it
        collects *before* delivering any of them, so a batch that is
        interrupted part-way through is a batch whose remainder no longer
        exists anywhere. That was fixed for a frame that raises and not for a
        cancel, and a cancel is the ordinary path -- `daemon._shutdown` stops
        every room, and so does `link_leave`, and so does re-joining a room you
        are already in. Six frames in, five lost.

        `GitTransport.stop` already does this for its sync loop and says why.
        Bounded, because a wedged `on_frame` must not turn a shutdown into a
        hang: that would be worse than the loss it is preventing.
        """
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(asyncio.shield(self._task),
                                       timeout=STOP_DRAIN_TIMEOUT_S)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            except Exception:
                pass
            self._task = None
        # Our heartbeat file stays. A member that shuts down for the night must
        # remain in everyone's fan-out set, or its mail would never be written.

    def _bootstrap(self) -> None:
        mine = out_root(self.shared_dir, self.room_id, self.device_id)
        os.makedirs(mine, exist_ok=True)
        os.makedirs(presence_dir(self.shared_dir, self.room_id), exist_ok=True)
        self._presence_tick(True)

    @property
    def online(self) -> bool:
        """The share answered on the last poll. Says nothing about any member."""
        return (
            self._task is not None
            and not self._task.done()
            and self.last_error is None
        )

    # -- sending ----------------------------------------------------------- #

    async def send(
        self,
        frame: dict[str, Any],
        recipients: Iterable[str] | None = None,
        msg_id: str | None = None,
    ) -> bool:
        """Write one copy of `frame` per recipient. Never raises.

        The fan-out set is the caller's list *union* the share's own roster,
        minus self. Each knows members the other does not -- the caller learns
        the room from the relay, the share learns it from presence files -- and
        a frame is a room broadcast either way, since addressing lives on `to`
        inside the ciphertext and every member holds the same key. Union is also
        what makes this transport usable with no relay at all, where the roster
        the caller can offer starts empty.

        False means at least one copy did not land and the caller should keep
        the frame for a later attempt: a retry that duplicates a copy is
        absorbed upstream -- admission is msg_id-only -- whereas a recipient
        silently skipped is a hole nobody can see.
        """
        # Re-read presence before fanning out if the cache has aged. Sending is
        # the one moment where a stale roster is expensive: a member who
        # appeared since the last poll gets no copy, and nothing later goes back
        # to write one, so the miss is permanent and invisible. One directory
        # listing is a cheap price for closing that window.
        if time.time() - self._presence_at > PRESENCE_SCAN_S:
            try:
                self._presence = await asyncio.to_thread(self._scan_presence)
                self._presence_at = time.time()
            except OSError:
                pass          # fall back to the cache; the share may be flaky

        # `is_device_id` at the path boundary, not only where ids enter the
        # roster. Every target below becomes a directory name, and os.path.join
        # given an absolute component throws the base away: one bad id turns
        # "write into the share" into "write anywhere this process can write",
        # UNC paths included, which on Windows makes the machine authenticate to
        # a host somebody else chose.
        targets = list(dict.fromkeys(
            d for d in [*(recipients or ()), *self.roster()]
            if d and d != self.device_id and is_device_id(d)
        ))
        if not targets:
            self.last_error = "no recipients on the share"
            return False

        try:
            written = await asyncio.to_thread(self._write_copies, frame, targets, msg_id)
        except OSError as exc:
            self.last_error = f"send failed: {exc}"
            self.log(f"[file] {self.last_error}")
            return False

        self.copies_out += written
        if written:
            self.messages_out += 1
        if written < len(targets):
            self.last_error = f"queued {written}/{len(targets)} copies"
            return False
        self.last_error = None
        return True

    def _write_copies(
        self, frame: dict[str, Any], recipients: list[str], msg_id: str | None
    ) -> int:
        """Fan one frame across our own outbox (blocking). Returns copies written."""
        self._gc_if_due(recipients)
        payload = json.dumps(frame, ensure_ascii=False)
        # ts_ms leads the name so a lexical sort is a chronological one -- for
        # the reader draining a directory and for the sweep below alike -- and
        # the id behind it separates two frames minted in the same millisecond.
        # A burst shares a millisecond and the name is all the reader has to
        # sort by, so the stamp is nudged forward rather than repeated: the
        # sender's own order survives, at the price of a few ms of drift.
        stamp = max(now_ms(), self._last_ms + 1)
        self._last_ms = stamp
        name = f"{stamp:013d}-{_file_id(msg_id)}.json"

        written = 0
        for recipient in recipients:
            path = os.path.join(
                out_dir(self.shared_dir, self.room_id, self.device_id, recipient), name
            )
            try:
                atomic_write_text(path, payload)
                written += 1
            except OSError as exc:
                self.log(f"[file] could not queue for {recipient}: {exc}")
        return written

    # -- receiving --------------------------------------------------------- #

    async def _poll_loop(self) -> None:
        last_touch = last_scan = 0.0
        while not self._stop.is_set():
            try:
                # Our own heartbeat is rewritten every few seconds, but everyone
                # else's is re-read more often: a member that has just come up
                # should enter the fan-out set within a poll or two rather than
                # within a heartbeat, and reading is the cheaper half.
                now = time.monotonic()
                touch = now - last_touch > self.presence_refresh_s
                if touch or now - last_scan > PRESENCE_SCAN_S:
                    await asyncio.to_thread(self._presence_tick, touch)
                    last_scan = now
                    last_touch = now if touch else last_touch

                frames = await asyncio.to_thread(self._drain)
                for frame in frames:
                    self.messages_in += 1
                    # Per frame, not per batch. `_drain` has already unlinked
                    # every frame it collected, so one raising here used to take
                    # the untouched remainder of the batch with it -- other
                    # senders' messages, deleted from the share and never
                    # delivered. A bad frame costs itself and nothing else.
                    try:
                        await self.on_frame(frame)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.log(f"[file] delivery failed for one frame: "
                                 f"{type(exc).__name__}: {exc}")
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a share can fail in creative ways
                self.last_error = f"poll failed: {exc}"
                self.log(f"[file] {self.last_error}")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_s)
            except asyncio.TimeoutError:
                pass

    def _drain(self) -> list[dict[str, Any]]:
        """Consume every frame addressed to this device (blocking).

        A scan of `out/*/<me>/`: one directory per sender, each of which this
        device is the only reader of, which is what keeps unlink-on-read safe
        now that a room has more than two members.
        """
        root = os.path.join(room_root(self.shared_dir, self.room_id), "out")
        try:
            senders = sorted(os.listdir(root))
        except OSError:
            return []  # nobody has written into this room yet

        collected: list[tuple[str, dict[str, Any]]] = []
        for sender in senders:
            # Our own fan-out tree. Skipping it makes delivering a copy back to
            # its author structurally impossible rather than merely unlikely.
            if sender == self.device_id:
                continue
            collected.extend(self._drain_sender(sender))

        # Every name starts with ts_ms, so one sort puts the batch in arrival
        # order across senders too. Per-sender order was already lexical.
        collected.sort(key=lambda item: item[0])
        return [frame for _name, frame in collected]

    def _drain_sender(self, sender: str) -> list[tuple[str, dict[str, Any]]]:
        inbox = out_dir(self.shared_dir, self.room_id, sender, self.device_id)
        try:
            names = sorted(
                n for n in os.listdir(inbox)
                if n.endswith(".json") and not n.startswith(".tmp-")
            )
        except OSError:
            return []

        found: list[tuple[str, dict[str, Any]]] = []
        # A week's backlog is drained over several polls rather than in one
        # thread hop, so a member returning from holiday cannot stall the loop.
        for name in names[:DRAIN_BATCH]:
            path = os.path.join(inbox, name)
            if not _is_plain_file(path):
                continue                    # a link here would read somewhere else
            try:
                # Size first, and this is not a micro-optimisation. `json.load`
                # on a multi-gigabyte file raises MemoryError, and on a deeply
                # nested one RecursionError -- neither is caught below, both
                # escape into the poll loop, and the file is never unlinked, so
                # it wedges the transport on every poll forever. Whoever writes
                # into the share can create that file.
                if os.stat(path).st_size > MAX_FRAME_BYTES:
                    self.log(f"[file] dropped {sender}/{name}: larger than "
                             f"{MAX_FRAME_BYTES} bytes")
                    _try_unlink(path)
                    continue
                with open(path, "r", encoding="utf-8") as fh:
                    frame = json.load(fh)
            except (OSError, ValueError):
                # Half-written or already gone; the next poll will pick it up.
                continue
            except Exception as exc:        # MemoryError, RecursionError, ...
                self.log(f"[file] dropped unreadable {sender}/{name}: "
                         f"{type(exc).__name__}")
                _try_unlink(path)
                continue

            key = f"{sender}/{name}"
            if key in self._seen:
                _try_unlink(path)
                continue
            self._remember(key)

            ok, why = _acceptable(frame, self.room_id, sender)
            if not ok:
                self.log(f"[file] dropped {key}: {why}")
                _try_unlink(path)
                continue

            found.append((name, frame))
            _try_unlink(path)
        return found

    def _remember(self, key: str) -> None:
        """Bounded dedupe set: guards against a delete that silently failed.

        Keyed on the path rather than on msg_id, which is sealed inside the
        ciphertext and unreadable here. One path is one copy, so this catches
        exactly the failure it exists for; msg_id dedupe happens above.
        """
        self._seen[key] = time.time()
        while len(self._seen) > SEEN_LIMIT:
            self._seen.popitem(last=False)

    # -- presence ---------------------------------------------------------- #

    def _presence_tick(self, touch: bool) -> None:
        """Write our heartbeat if it is due, then refresh the cached roster.

        One thread hop for both, because the two always run on the same poll and
        a share charges for the round trip rather than for the work.
        """
        if touch and self.write_presence:
            path = os.path.join(
                presence_dir(self.shared_dir, self.room_id), f"{self.device_id}.json"
            )
            atomic_write_text(path, json.dumps({
                "device_id": self.device_id,
                "room_id": self.room_id,
                "ts": now_iso(),
                "epoch": time.time(),
            }))
        self._presence = self._scan_presence()
        self._presence_at = time.time()

    def _scan_presence(self) -> dict[str, float]:
        """device_id -> last heartbeat epoch, straight off the share (blocking).

        Lets OSError out, so a share that has gone away reads as a failure
        rather than as an empty room -- the sweep below deletes on this reading.
        """
        directory = presence_dir(self.shared_dir, self.room_id)
        seen: dict[str, float] = {}
        for name in os.listdir(directory):
            if not name.endswith(".json") or name.startswith(".tmp-"):
                continue
            device = name[: -len(".json")]
            # A filename cannot traverse -- listdir returns one path component --
            # but it can be anything at all, and this is where the roster is
            # built. Anything that is not a device id was not written by a
            # member, so it is not one.
            if not is_device_id(device):
                continue
            path = os.path.join(directory, name)
            data = read_json(path, None)
            epoch = None
            if isinstance(data, dict):
                try:
                    epoch = float(data.get("epoch", 0.0))
                except (TypeError, ValueError):
                    epoch = None
            if epoch is None:
                # The file exists but did not parse. Its owner rewrites it every
                # few seconds, so mtime is an honest stand-in -- and treating it
                # as missing would let the sweep bin a live member's queue.
                try:
                    epoch = os.stat(path).st_mtime
                except OSError:
                    continue
            seen[device] = epoch
        return seen

    def roster(self) -> dict[str, float]:
        """Every other device on this share, device_id -> last heartbeat epoch.

        Served from the cache the poll loop refreshes, so a caller on the event
        loop never touches the share. The window is retention, not liveness: a
        member asleep since yesterday is still owed its copy of what we send.
        """
        cutoff = time.time() - self.retention_s
        return {
            device: epoch for device, epoch in self._presence.items()
            if device != self.device_id and epoch >= cutoff
        }

    def members_online(self) -> list[str]:
        """The subset of the roster heard from within the heartbeat window."""
        now = time.time()
        return sorted(
            device for device, epoch in self.roster().items()
            if now - epoch < PRESENCE_STALE_S
        )

    # -- housekeeping ------------------------------------------------------ #

    def _gc_if_due(self, recipients: list[str]) -> None:
        """Sweep our own outbox, on the write path but not on every write.

        Rate-limited because a burst of sends would otherwise pay a directory
        listing per frame, and listing is the operation a slow share punishes.
        Only ever touches `out/<me>/`: the one tree this device owns.
        """
        now = time.monotonic()
        if self._gc_at and now - self._gc_at < GC_INTERVAL_S:
            return
        self._gc_at = now

        try:
            presence = self._scan_presence()
        except OSError as exc:
            self.log(f"[file] sweep skipped: {exc}")
            return
        # If our own heartbeat is not on the share, the share is not telling the
        # truth; deleting on the strength of that reading is how mail vanishes.
        if self.device_id not in presence:
            return

        alive = {
            device for device, epoch in presence.items()
            if time.time() - epoch < self.retention_s
        }
        keep = set(recipients)
        mine = out_root(self.shared_dir, self.room_id, self.device_id)
        cutoff_ms = (time.time() - self.retention_s) * 1000.0
        try:
            entries = os.listdir(mine)
        except OSError:
            return

        for recipient in entries:
            path = os.path.join(mine, recipient)
            if recipient not in alive and recipient not in keep:
                # A device that reads this share also heartbeats it, so silence
                # for a whole retention window proves nobody is collecting this
                # queue. It is dead weight rather than mail, tree and all.
                _purge_dir(path)
                continue
            _expire(path, cutoff_ms)

    async def publish_files(self, writes: Callable[[], Any]) -> Any:
        """Run blocking carrier writes off the loop. Subclasses that commit
        (git) also take their repo lock and nudge a sync round."""
        return await asyncio.to_thread(writes)

    def stats(self) -> dict[str, Any]:
        roster = self.roster()
        return {
            "shared_dir": self.shared_dir,
            "room_id": self.room_id,
            "poll_ms": int(self.poll_s * 1000),
            "online": self.online,
            "members": len(roster),
            "members_online": self.members_online(),
            "messages_in": self.messages_in,
            "messages_out": self.messages_out,
            "copies_out": self.copies_out,
            "last_error": self.last_error,
        }


# --------------------------------------------------------------------------- #
# filesystem helpers
# --------------------------------------------------------------------------- #


def _file_id(msg_id: str | None) -> str:
    """The unique half of a queue file's name.

    The frame's own msg_id is sealed inside the ciphertext and unreadable here,
    so the sender may pass it in for traceability on the share and anything else
    gets a fresh id. Sanitised because this becomes a path component.
    """
    cleaned = _UNSAFE_IN_NAME.sub("", msg_id or "")[:48]
    return cleaned or new_id("msg")


def _acceptable(frame: Any, room_id: str, sender: str) -> tuple[bool, str]:
    """Structural check before a frame costs the daemon a decryption attempt.

    The header must also agree with the path the file arrived on: a frame in
    `out/<sender>/` claiming another device, or another room, was written by
    something that is not a member behaving.
    """
    ok, why = validate_frame(frame)
    if not ok:
        return False, why
    if frame["room_id"] != room_id:
        return False, f"frame belongs to {frame['room_id']}"
    if frame["device_id"] != sender:
        return False, f"frame claims {frame['device_id']} in {sender}'s outbox"
    return True, ""


def _queued_at_ms(directory: str, name: str) -> float:
    """When a queue file was written, read from its name.

    A stat per file is the one thing a slow share really punishes, so the name
    answers it; only debris that never had a timestamp falls back to metadata.
    """
    head = name.split("-", 1)[0]
    if head.isdigit():
        return float(head)
    try:
        return os.stat(os.path.join(directory, name)).st_mtime * 1000.0
    except OSError:
        return 0.0  # vanished or unreadable: old enough to let go


def _is_plain_file(path: str) -> bool:
    """A real file, not a link to somewhere else.

    Everything below is a *destructive* walk over a directory the threat model
    says a hostile party can write to. A symlink or a Windows junction planted
    at one of these names turns "empty this queue directory" into "empty
    whichever directory the attacker pointed it at", because both listdir and
    unlink follow it. Checking without following is the whole defence.
    """
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _is_plain_dir(path: str) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _expire(directory: str, cutoff_ms: float) -> None:
    """Unlink copies the recipient never collected, and any crashed temp files."""
    if not _is_plain_dir(directory):
        return
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        path = os.path.join(directory, name)
        if not _is_plain_file(path):
            continue
        if _queued_at_ms(directory, name) > cutoff_ms:
            continue
        _try_unlink(path)


def _purge_dir(path: str) -> None:
    """Empty one queue directory and remove it. Deliberately not recursive:
    nothing below a recipient directory is ours, so nothing below it is ours to
    delete."""
    if not _is_plain_dir(path):
        return
    try:
        names = os.listdir(path)
    except OSError:
        return
    for name in names:
        entry = os.path.join(path, name)
        if _is_plain_file(entry):
            _try_unlink(entry)
    try:
        os.rmdir(path)                      # only ever succeeds if it is empty
    except OSError:
        pass


def _try_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def probe_shared_dir(shared_dir: str) -> tuple[bool, str]:
    """Check the share exists and is writable before relying on it.

    The existence check is the load-bearing half, and it must come first. A
    synced folder is created by the sync client, so it is always already there;
    a path that is *not* there is a typo. Writing straight into it would create
    the typo as a fresh local directory, report success, and leave someone
    talking into a folder that syncs to nobody -- indistinguishable, from the
    inside, from a colleague who never joined.

    This is why the probe never creates `shared_dir` itself. It creates only
    the `claude-link/` working directory underneath one that already exists.
    """
    if not shared_dir:
        return False, "no shared_dir configured"

    expanded = os.path.abspath(os.path.expanduser(shared_dir))
    if not os.path.exists(expanded):
        return False, (
            f"{expanded} does not exist. A folder you sync is created by the "
            f"sync client, so a path that is missing is usually a typo -- check "
            f"it against the folder your colleague set."
        )
    if not os.path.isdir(expanded):
        return False, f"{expanded} is a file, not a directory"

    try:
        probe = os.path.join(expanded, "claude-link", ".probe")
        atomic_write_text(probe, now_iso())
        with open(probe, "r", encoding="utf-8") as fh:
            fh.read()
        _try_unlink(probe)
        return True, "ok"
    except OSError as exc:
        return False, f"not writable: {type(exc).__name__}: {exc}"
