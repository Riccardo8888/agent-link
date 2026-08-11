"""The agent-link daemon: one long-lived process per machine.

It owns everything slow or stateful -- the relay connection, reconnect backoff,
the fallback transports, the inbox and the `.conv/` logs -- so the MCP server
can stay a thin, fast client. A tool call never waits on the network: `send`
hands a sealed frame to the daemon and returns, and only the explicit `wait` op
ever blocks, with a caller-supplied timeout.

Control plane: newline-delimited JSON over TCP on 127.0.0.1, one request and one
response per line.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import socket
import sys
import threading
import time
from collections import deque
from typing import Any, Callable

from . import __version__, identity
from .crypto import CryptoError, derive_room, is_device_id, new_invite, parse_invite
from . import door as doorway
from . import gov
from .envelope import (
    K_CHANNEL_CLOSE,
    K_CHANNEL_OPEN,
    K_KNOCK,
    K_MSG,
    K_PRESENCE,
    K_SYSTEM,
    MAX_MESSAGE_BYTES,
    ROLE_ORCHESTRATOR,
    is_channel_id,
    make_envelope,
    make_origin,
    open_and_verify,
)
from .text import clean_label, clean_text
from .room import Room
from .store import (
    TRANSPORT_KEYS,
    ConvLogger,
    clear_daemon_info,
    config_changes,
    config_value_ok,
    daemon_log_path,
    display_name_set,
    load_config,
    load_state,
    root_dir,
    save_config,
    save_state,
    write_daemon_info,
)
from .transport_relay import RelayTransport
from .util import (append_line, cli_invocation, new_id, now_iso,
                   primary_ip, truncate)

PREVIEW_CHARS = 400          # what an inbox entry shows before link_read
STATUS_READY_TIMEOUT_S = 2.0  # below the control client's ordinary 3 s deadline
# How long one `.conv/` write may take before it is abandoned. A sink can be
# any directory the user named, a disconnected share included.
CONV_IO_TIMEOUT_S = 5.0
# What one control connection may cost before it has said anything. The
# token authenticates commands; these bound the socket itself.
CTRL_FIRST_LINE_S = 10.0
CTRL_MAX_CONNS = 64
# A control request is one line, and a `send` carries the whole message, so this
# has to sit above the wire's own message ceiling with room for JSON overhead.
CTRL_LINE_LIMIT = MAX_MESSAGE_BYTES * 2

# What to call each heartbeat-carrying medium when telling a human somebody
# turned up on it. "on the file" would mean nothing.
_MEDIUM = {"file": "shared folder", "git": "git channel"}

# Ops that put a message into a room under this device's signature. Reading the
# inbox from a second agent path is ordinary; signing from one is the thing
# docs/postmortems.md is about.
_SENDING_OPS = {"send", "channel_open", "channel_close"}


def _redacted_remote(remote: str | None) -> str | None:
    """A remote URL safe to put in a status response.

    `https://<user>:<token>@github.com/...` is an ordinary way to configure this
    and status output goes straight into a model's context, a transcript and a
    log. Imported here rather than at the top so a machine that never configures
    git never loads the module.
    """
    if not remote:
        return None
    from .transport_git import redact_remote

    return redact_remote(remote)


LOG_MAX_BYTES = 4 * 1024 * 1024   # one rotation kept, so at most ~8 MiB on disk


def _log(msg: str) -> None:
    line = f"{now_iso()} {msg}"
    path = daemon_log_path()
    try:
        # The daemon is long-lived and logs every dropped frame, every refused
        # control request and every transport hiccup, so "grows forever" is the
        # default outcome. One rotation is enough: this file is read when
        # something has just gone wrong, never as an archive.
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
        append_line(path, line)
    except OSError:
        pass
    print(line, file=sys.stderr, flush=True)


class LinkDaemon:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.identity = identity.load()
        self.started_at = now_iso()
        self.logger = ConvLogger(cfg.get("log_sinks") or [])

        self.rooms: dict[str, Room] = {}
        self.channels: dict[str, dict[str, Any]] = {}
        # Knocks *we* have out at other rooms' doors, room_id -> record.
        # Persisted, so a restart picks the wait back up.
        self.pending_knocks: dict[str, dict[str, Any]] = {}
        self._knock_tasks: dict[str, asyncio.Task] = {}
        # What a roomless startup found already open on the configured carrier.
        self.open_rooms: list[dict[str, Any]] = []
        # agent_kind -> how much it has talked to this daemon. See _note_caller.
        self.callers: dict[str, dict[str, Any]] = {}
        self._warned_shared_identity = False
        self._ctrl_conns = 0
        self.dropped_unread = 0
        # Coerced rather than trusted. A config.json already holding
        # `inbox_max: -5` -- from a typo, a hand edit, or a `config --set`
        # written before that was refused -- used to raise here, which is
        # *before* the control port opens, so every start died with nothing on
        # screen but "daemon failed to start within 12s".
        try:
            inbox_max = int(cfg.get("inbox_max") or 500)
        except (TypeError, ValueError):
            inbox_max = 500
        if inbox_max < 1:
            _log(f"inbox_max is {cfg.get('inbox_max')!r}, which is not a size; using 500")
            inbox_max = 500
        self.inbox: deque[dict[str, Any]] = deque(maxlen=inbox_max)
        self._seq = 0
        self._waiters: list[asyncio.Future] = []
        self._stopping = asyncio.Event()
        self.ctrl_server: asyncio.Server | None = None
        self.actual_ctrl_port: int | None = None
        self.direct_server = None
        self._state_lock = asyncio.Lock()
        self._conv_lock = asyncio.Lock()
        # Not an asyncio lock: `_persist` is reached from a worker thread and
        # from the event loop, and only a threading one covers both. See it.
        self._persist_lock = threading.Lock()
        self._rooms_ready = asyncio.Event()
        # Minted per daemon run and published in daemon.json (mode 0600). The
        # control socket used to accept every op from anything that could reach
        # 127.0.0.1, which on a shared machine is every local user and every
        # unprivileged process -- `status` hands back the room's invite secret,
        # `send` signs as this device, `config` can repoint the git remote. A
        # web page could reach it too: the handler answered each unparseable
        # line and kept reading, so the body of an ordinary cross-origin POST
        # was dispatched as a command.
        self.token = secrets.token_hex(32)

    # -- startup ------------------------------------------------------------- #

    async def run(self) -> None:
        state = load_state()
        self.channels = state.get("channels", {})

        self.ctrl_server = await asyncio.start_server(
            self._on_control, "127.0.0.1", int(self.cfg["ctrl_port"]),
            # asyncio's default StreamReader limit is 64 KiB, and readline()
            # raises a bare ValueError past it -- so a coding agent handing over
            # a diff got "daemon closed the connection" and no explanation.
            # One number for how big a message may be, shared with the wire.
            limit=CTRL_LINE_LIMIT,
        )
        self.actual_ctrl_port = self.ctrl_server.sockets[0].getsockname()[1]

        # Logged the moment the port is listening, before anything that touches
        # the network. A caller that connects but gets no answer needs to know
        # whether the daemon reached this line: without it, a stall during
        # startup is indistinguishable from a daemon that never launched, and
        # both present as an empty log.
        _log(f"daemon {__version__} up as {self.identity.label} "
             f"({self.identity.device_id}) ctrl={self.actual_ctrl_port}")

        # The token goes in the same 0600 file the client already reads for the
        # port, so anything that can reach the daemon's home can talk to it and
        # anything that cannot, cannot. See _authorised.
        write_daemon_info({
            "pid": os.getpid(),
            "device_id": self.identity.device_id,
            "label": self.identity.label,
            "ctrl_port": self.actual_ctrl_port,
            "token": self.token,
            "ip": primary_ip(),
            "host": socket.gethostname(),
            "started_at": self.started_at,
            "version": __version__,
        })

        if state.get("migrated_from_v1"):
            _log(f"v1 pairings archived to {state['migrated_from_v1']} — "
                 f"run link_join to recreate your rooms as v2 rooms")

        if identity.is_world_readable():
            _log(f"WARNING: {identity.identity_path()} is readable by other "
                 f"users; this device's signing key is not private")

        # One listener for every room, and only if asked for: binding a port is
        # the thing the other transports exist to avoid needing.
        if self.cfg.get("direct_enabled"):
            from .transport_direct import DirectServer

            self.direct_server = DirectServer(
                host=self.cfg.get("ws_host", "0.0.0.0"),
                port=int(self.cfg.get("ws_port", 45813)),
                lookup_room=self.rooms.get,
                log=_log,
            )
            try:
                await self.direct_server.start()
            except OSError as exc:
                _log(f"direct links disabled: could not bind "
                     f"{self.cfg.get('ws_host')}:{self.cfg.get('ws_port')} ({exc})")
                self.direct_server = None

        for record in list((state.get("rooms") or {}).values()):
            try:
                await self._start_room(record)
            except Exception as exc:
                _log(f"could not rejoin {record.get('name')}: {type(exc).__name__}: {exc}")
        self.pending_knocks = dict(state.get("pending_knocks") or {})
        for rec in list(self.pending_knocks.values()):
            self._spawn_knock(rec)
        if (not self.rooms and not self.pending_knocks
                and self.cfg.get("git_remote")):
            asyncio.create_task(self._background_discover())
        self._rooms_ready.set()

        try:
            await self._stopping.wait()
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        _log("daemon shutting down")
        # The port goes first. `ControlClient.ensure_daemon` decides the old
        # daemon is gone by asking whether anything still answers here, and a
        # `GitTransport.stop` does a final flush and push, so stopping rooms
        # first can hold the port open across the whole teardown: the new daemon
        # fails to bind and dies, and the old one answers the ping that was
        # meant to prove it had gone.
        if self.ctrl_server:
            self.ctrl_server.close()
        for room in list(self.rooms.values()):
            await room.stop()
        if self.direct_server is not None:
            await self.direct_server.stop()
        clear_daemon_info()

    # -- rooms ---------------------------------------------------------------- #

    # -- blocking store calls -------------------------------------------------- #
    #
    # Everything under here touches a file, and none of it may run on the event
    # loop. A `.conv/` sink is a directory the user named -- `log_sinks` takes a
    # list of them -- so any one can be a network share that stops answering,
    # and every write used to happen inline: once per delivery, once per send,
    # twice more on channel open, and up to 2000 records parsed per room inside
    # `_op_read`. One unreachable share therefore stopped the daemon answering
    # anything at all, `link_status` included, which is the call you would make
    # to find out why. Same shape as the mDNS hang in docs/postmortems.md, and
    # the direct contradiction of the README's claim that `link_send` returns in
    # about a millisecond.

    async def _conv_io(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run one `.conv/` call on a worker thread, serialised.

        Serialised because this is a log, and a log that reorders itself under
        load is worse than a slow one. `asyncio.Lock` wakes waiters in the order
        they arrived, so the order envelopes reach here is the order they reach
        the file.
        """
        async with self._conv_lock:
            try:
                return await asyncio.wait_for(asyncio.to_thread(fn, *args),
                                              timeout=CONV_IO_TIMEOUT_S)
            except asyncio.TimeoutError:
                # Bounded, because `send`, `join` and `leave` all await this and
                # an unbounded wait turns one unreachable share into a daemon
                # that never answers any of them -- `leave` included, which is
                # the way out the documentation points at. Worse for `send`,
                # which awaits this *after* the frame is on the wire, so the
                # model is told FAILED for a message that was delivered and
                # sends it again. A transcript line is not worth that.
                _log(f"[conv] a sink did not answer within {CONV_IO_TIMEOUT_S:.0f}s; "
                     f"dropping one transcript record. Check log_sinks.")
                return None

    def _persist_from_loop(self) -> None:
        """What `Room` gets as its `save_state`. Asks for a write; does not do one.

        `Room` calls this straight from the event loop -- `next_seq` every
        `SEQ_BLOCK` sends, `_remember` every 25th inbound frame -- and it used
        to be `_persist` itself, so the loop did a `load_state` plus an atomic
        write inline. Giving `_persist` a `threading.Lock`, so two threads could
        not race `os.replace` onto `state.json`, made that worse rather than
        better: `Lock.acquire()` on the loop thread cannot yield, so the loop
        then waited out whatever a worker was doing inside it. 5.30 s on a call
        that normally costs 5.4 ms.

        Losing the write instead is not an option, so it is handed to a task.
        No coalescing: `_persist` snapshots the live dicts, both callers are
        rare by construction, and a second write of the same truth is cheaper
        than reasoning about which one landed.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._persist()          # no loop: a synchronous caller, or a test
            return
        task = loop.create_task(self._persist_soon())
        # Nobody awaits this, so nobody would ever see it fail.
        task.add_done_callback(self._persist_done)

    @staticmethod
    def _persist_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log(f"could not write state.json: {type(exc).__name__}: {exc}")

    async def _persist_soon(self) -> None:
        """Write `state.json` off the loop, and still wait for it.

        Deliberately not fire-and-forget: a join that has been answered has to
        survive this process being killed a millisecond later. It does not take
        the `.conv/` lock either, because state.json is always local and a
        stalled network sink must not be able to hold up leaving a room.
        """
        await asyncio.to_thread(self._persist)

    def _persist(self) -> None:
        """Write `state.json`. Safe to call from any thread.

        It was called from the event loop and nowhere else, so it was
        serialised for free; `_persist_soon` took that away. `state.json` is
        written by rename, and two renames racing onto one path is
        `PermissionError: [WinError 5] Access is denied` on Windows -- which
        arrived as `link_join` failing outright with nothing to suggest why.

        `Room.save_state` also lands here, straight from the loop, once every
        `SEQ_BLOCK` sends. So the lock has to be a threading one: an asyncio
        lock would not have seen that caller at all.
        """
        with self._persist_lock:
            state = load_state()
            state["rooms"] = {
                room_id: room.record for room_id, room in self.rooms.items()
            }
            state["channels"] = self.channels
            state["pending_knocks"] = dict(self.pending_knocks)
            save_state(state)

    async def _start_room(self, record: dict[str, Any]) -> Room:
        # Deliberately slow, ~0.5 s: it is the cost that makes a room id useless
        # to anyone trying to work backwards to the secret. Run off the loop, or
        # the daemon stops answering control calls for that half second, and for
        # every room it rejoins at startup.
        keys = await asyncio.to_thread(derive_room, record["name"], record["secret"])
        if keys.room_id in self.rooms:
            await self.rooms[keys.room_id].stop()

        room = Room(
            keys=keys,
            identity=self.identity,
            record=record,
            deliver=self._deliver_envelope,
            save_state=self._persist_from_loop,
            log=_log,
        )
        room.display_name = clean_label(self.cfg.get("display_name") or "", 40)
        self.rooms[keys.room_id] = room

        # A relay is an upgrade, not a requirement. Unconfigured means the room
        # runs on the shared folder alone, which needs nothing deployed.
        relay_url = record.get("relay_url") or self.cfg.get("relay_url")
        if relay_url:
            relay = RelayTransport(
                relay_url=relay_url,
                keys=keys,
                identity=self.identity,
                on_frame=lambda frame, r=room: r.on_frame(frame, "relay"),
                on_presence=lambda device, online, roster, r=room:
                    self._on_relay_presence(r, device, online, roster),
                on_missed=lambda missed, r=room: self._on_missed(r, missed),
                log=_log,
                insecure=bool(self.cfg.get("relay_insecure")),
                reconnect_min_s=float(self.cfg.get("reconnect_min_s", 1)),
                reconnect_max_s=float(self.cfg.get("reconnect_max_s", 30)),
            )
            room.attach("relay", relay)
            await relay.start()

        if self.direct_server is not None:
            from .transport_direct import DirectTransport

            direct = DirectTransport(
                keys=keys,
                identity=self.identity,
                on_frame=lambda frame, r=room: r.on_frame(frame, "direct"),
                endpoint=self._direct_endpoint,
                log=_log,
            )
            room.attach("direct", direct)
            await direct.start()

        shared = record.get("shared_dir") or self.cfg.get("shared_dir")
        if shared:
            from .transport_file import FileTransport, probe_shared_dir

            # Check before touching it. A mistyped path would otherwise be
            # created as a local directory that syncs nowhere, and the room
            # would look healthy while reaching nobody.
            # A disconnected network share can stall filesystem calls. Keep it
            # off the event loop so status can hit its readiness deadline and
            # explain that startup is still in progress.
            usable, why = await asyncio.to_thread(probe_shared_dir, shared)
            if not usable:
                _log(f"[{room.name}] shared folder unusable: {why}")
                room.setup_error = f"shared folder unusable: {why}"
                shared = None

        if shared:
            transport = FileTransport(
                shared_dir=shared,
                room_id=keys.room_id,
                device_id=self.identity.device_id,
                on_frame=lambda frame, r=room: r.on_frame(frame, "file"),
                poll_ms=int(self.cfg.get("file_poll_ms", 250)),
                log=_log,
            )
            try:
                await transport.start()
                room.attach("file", transport)
                _log(f"[{room.name}] shared-folder fallback active on {shared}")
            except OSError as exc:
                _log(f"[{room.name}] shared folder unavailable: {exc}")

        await self._attach_git(room, record, keys)

        await self._conv_io(self.logger.write_meta, keys.room_id, {
            "room_id": keys.room_id,
            "room": keys.room_name,
            "kind": "room",
            "local_device": self.identity.device_id,
            "local_label": self.identity.label,
            "local_host": socket.gethostname(),
            "joined_at": record.get("joined_at"),
        })

        asyncio.create_task(self._announce_when_ready(room))
        return room

    async def _attach_git(self, room: Room, record: dict[str, Any], keys) -> None:
        """Bring up the git transport for a room, or say why it did not.

        Kept apart from `_start_room` because it is the one transport whose
        startup does real work -- an init, a fetch, possibly a first push -- and
        every one of those has a failure a user has to be told about rather than
        left to infer from a quiet room. A failure here is recorded as
        `setup_error`, which `link_status` prints, instead of being logged and
        forgotten.
        """
        remote = record.get("git_remote") or self.cfg.get("git_remote")
        if not remote:
            return

        from .transport_git import (
            DEFAULT_BRANCH,
            BadRemote,
            GitError,
            GitTransport,
            check_remote,
            clone_dir,
            redact_remote,
        )

        # Refused *before* a clone directory is derived from it, and reported
        # the same way an unreachable repo is: a remote this will not hand to
        # git costs you the repo, never the room.
        try:
            remote = check_remote(remote)
        except BadRemote as exc:
            _log(f"[{room.name}] git channel unusable: {exc}")
            room.setup_error = "; ".join(
                p for p in (room.setup_error, f"git channel unusable: {clean_text(str(exc), 200)}") if p
            )
            return

        branch = (record.get("git_branch") or self.cfg.get("git_branch")
                  or DEFAULT_BRANCH)
        workdir = self.cfg.get("git_dir") or clone_dir(root_dir(), remote, branch)

        transport = GitTransport(
            remote=remote,
            branch=branch,
            workdir=os.path.abspath(os.path.expanduser(workdir)),
            room_id=keys.room_id,
            device_id=self.identity.device_id,
            on_frame=lambda frame, r=room: r.on_frame(frame, "git"),
            poll_ms=int(self.cfg.get("file_poll_ms", 250)),
            sync_ms=int(self.cfg.get("git_sync_ms", 3000)),
            presence_s=float(self.cfg.get("git_presence_s", 45)),
            depth=int(self.cfg.get("git_depth", 0)),
            log=_log,
        )
        # Bounded, because this is the only transport whose startup talks to the
        # network before the room exists. `link_join` gives the daemon 30 s over
        # the control socket, and a remote that is simply a black hole would
        # otherwise hold the room open through three consecutive git deadlines
        # and turn a room that was created into a tool call that looks failed.
        budget = float(self.cfg.get("git_start_timeout_s", 20))
        try:
            await asyncio.wait_for(transport.start(), timeout=budget)
        except (GitError, OSError, asyncio.TimeoutError) as exc:
            if isinstance(exc, asyncio.TimeoutError):
                exc = TimeoutError(
                    f"the repo did not answer within {budget:.0f}s. `link.cli "
                    f"doctor` says whether it is reachable from here; raise "
                    f"git_start_timeout_s if the first clone is simply slow"
                )
                # The startup was cancelled part-way, so tear down what it did
                # bring up. Without a flush: the remote is the thing that is not
                # answering, and a final round would wait out the same deadline.
                await transport.stop(flush=False)
            # Never fatal to the room: a relay or a shared folder may still be
            # carrying it, and a room that refuses to start is worse than one
            # that starts and says which of its transports is broken.
            _log(f"[{room.name}] git channel unusable: {exc}")
            # Appended, not assigned: a room can have a broken folder *and* a
            # broken repo, and hiding the first behind the second would send
            # somebody to fix one thing and leave the other in place.
            room.setup_error = "; ".join(
                p for p in (room.setup_error, f"git channel unusable: {clean_text(str(exc), 200)}") if p
            )
            return
        room.attach("git", transport)
        _log(f"[{room.name}] git channel active on {branch} of "
             f"{redact_remote(remote)}")

    def _direct_endpoint(self) -> dict[str, Any] | None:
        """Where members should dial us, or None when direct links are off."""
        if self.direct_server is None or self.direct_server.actual_port is None:
            return None
        return {"host": primary_ip(), "port": self.direct_server.actual_port}

    async def _announce_when_ready(self, room: Room) -> None:
        """Say hello once a transport is up, then push anything that queued."""
        for _ in range(120):
            if room.live_transports:
                break
            await asyncio.sleep(0.25)
        await room.announce()
        flushed = await room.flush()
        if flushed:
            _log(f"[{room.name}] flushed {flushed} queued message(s)")
        asyncio.create_task(self._track_folder_roster(room))

    async def _track_folder_roster(self, room: Room) -> None:
        """Keep the roster fed from the heartbeat files members write.

        Covers both mediums that have no server to announce anyone: the shared
        folder and the git branch. Their heartbeats are separate files with
        separate timestamps even when one device writes both, so each source
        keeps its own memory of what it last read -- shared memory would read the
        alternation between them as a member refreshing constantly, which is the
        same lie in a new costume.

        Without a relay there is nobody to announce arrivals, so presence has to
        come from the only shared thing there is: the heartbeat files each
        member refreshes.

        Two windows are in play and they are not interchangeable. The roster is
        retention -- who is still owed a copy of what we send -- and holds a
        member for days after it stops writing. Liveness is a matter of
        seconds. Reading the first as the second reports a member whose daemon
        has been dead since yesterday as answering right now.

        So liveness here is the heartbeat *changing*, timed by our own clock.
        The alternative -- believing the timestamp inside the file -- puts a
        colleague's clock in charge of whether they appear to exist, and two
        laptops disagree by minutes often enough that it would strand them.
        Their timestamp is used for one thing only: dating a member we find
        already on the share at startup, where there is no earlier reading to
        compare against and their word is the only evidence there is.
        """
        sources = [(name, room.transport(name)) for name in ("file", "git")]
        sources = [(name, t) for name, t in sources if t is not None]
        if not sources:
            return
        known: set[str] = set()
        beats: dict[tuple[str, str], float] = {}
        # Identity, not id. `_start_room` replaces the Room object when a
        # room is re-joined, and testing the id left the old task running
        # forever against a dead Room -- still announcing, still delivering
        # presence notices, one leaked task per re-join.
        while self.rooms.get(room.room_id) is room:
            for name, source in sources:
                try:
                    roster = source.roster()
                except Exception:
                    roster = {}
                for device_id, heartbeat in roster.items():
                    if device_id == self.identity.device_id:
                        continue
                    previous = beats.get((name, device_id))
                    beats[(name, device_id)] = heartbeat
                    if previous is None:
                        # First sight. Clamped, because a clock running ahead
                        # would otherwise date them in the future and read as
                        # forever fresh -- the failure this loop exists to avoid.
                        room.note_seen(device_id, name, at=min(heartbeat, time.time()))
                    elif heartbeat != previous:
                        room.note_seen(device_id, name)
                    # Unchanged: deliberately not stamped, so a member that has
                    # stopped writing ages out instead of being held at "just seen".

                    member = room.members.get(device_id)
                    if member is not None and member.online and device_id not in known:
                        known.add(device_id)
                        who = member.label if member.label != device_id else device_id
                        await self._deliver_envelope(
                            room.local_event(K_PRESENCE, f"{who} is on the {_MEDIUM[name]}",
                                             device_id=device_id, online=True),
                            "local",
                        )
                        # They may have arrived after our hello went out.
                        await room.announce()
                        # And anything queued was queued *because* nobody was
                        # reachable. `Room.send`'s docstring says a queued
                        # message "goes out on the next reconnect", and until
                        # now `flush` was called exactly once in the process's
                        # life, from `_announce_when_ready`. So a message sent
                        # between creating a room and the colleague's first
                        # heartbeat -- which is most first messages -- sat in
                        # the outbox for ever with `queued: 1` on the status
                        # line and nothing ever retrying it.
                        flushed = await room.flush()
                        if flushed:
                            _log(f"[{room.name}] {who} arrived; flushed "
                                 f"{flushed} queued message(s)")
            try:
                await self._scan_knocks(room)
            except Exception as exc:      # advisory; never kill the roster loop
                _log(f"[{room.name}] knock scan failed: "
                     f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(2.0)

    async def _scan_knocks(self, room: Room) -> None:
        """Publish our door entry and surface new knocks, on every carrier
        this room has. Runs from the roster loop; every file touch is off the
        loop, and door writes go through publish_files so the git carrier
        takes its repo lock and commits them."""
        for name in ("file", "git"):
            source = room.transport(name)
            if source is None:
                continue
            base = source.shared_dir
            entry = doorway.door_entry(self.identity, room.room_id)
            try:
                await source.publish_files(
                    lambda: (doorway.write_door_entry(base, room.room_id, entry),
                             doorway.gc_stale_knocks(base, room.room_id)))
                knocks = await asyncio.to_thread(
                    doorway.read_knock_files, base, room.room_id)
            except OSError as exc:
                _log(f"[{room.name}] door scan failed on {name}: {exc}")
                continue
            for raw in knocks:
                try:
                    info = doorway.read_knock(self.identity, room.room_id, raw)
                except CryptoError as exc:
                    _log(f"[{room.name}] dropped an unreadable knock: {exc}")
                    continue
                seen = room.record.setdefault("knocks_seen", {})
                if seen.get(info["device_id"]) == info["ts"]:
                    room.pending_knocks.setdefault(info["device_id"], info)
                    continue
                seen[info["device_id"]] = info["ts"]
                room.save_state()
                room.pending_knocks[info["device_id"]] = info
                who = clean_label(info["name"], 40) or info["device_id"]
                await self._deliver_envelope(
                    room.local_event(
                        K_KNOCK,
                        f"{who} wants to join {room.name}. Let them in? "
                        f"link_grant(device=\"{info['device_id']}\", "
                        f"allow=true) or allow=false",
                        device_id=info["device_id"], name=who),
                    "local")
            try:
                await self._scan_gov(room, source, base)
            except Exception as exc:      # advisory; never kill the scan
                _log(f"[{room.name}] gov scan failed on {name}: "
                     f"{type(exc).__name__}: {exc}")
            if room.room_id not in self.rooms:
                return    # a removal migrated or ended this room mid-scan

    async def _scan_gov(self, room: Room, source, base: str) -> None:
        """Publish our genesis if this room owes one, refresh the room's
        governance state from the carrier's records, and act on a winning
        removal: the target is told and the room ends; everyone else follows
        the rekey to the successor."""
        if room.record.get("gov_creator") and not room.gov_state.order:
            genesis = gov.build_genesis(self.identity, room.room_id)
            await source.publish_files(
                lambda: gov.write_gov_record(base, room.room_id, genesis))
        recs = await asyncio.to_thread(gov.read_gov_records, base, room.room_id)
        if recs or room.gov_state.order:
            room.gov_state = gov.evaluate(recs, room.room_id)
        rec = room.gov_state.removal
        if rec is None or room.room_id not in self.rooms:
            return
        if rec["target"] == self.identity.device_id:
            try:
                notice = gov.open_notice_box(self.identity, rec)
                text = notice.get("text") or "You were removed from this room."
            except CryptoError:
                text = "You were removed from this room."
            await self._notice(room.room_id, f"{text} ({room.name})")
            async with self._state_lock:
                await room.stop()
                self.rooms.pop(room.room_id, None)
                await self._persist_soon()
            return
        try:
            opened = gov.open_rekey_box(self.identity, rec)
        except CryptoError:
            _log(f"[{room.name}] a removal carries no rekey box for this "
                 "device; staying put until one does")
            return
        await self._migrate_after_removal(room, rec,
                                          opened["name"], opened["secret"])

    async def _on_relay_presence(self, room: Room, device_id: str, online: bool,
                                 roster: list[str]) -> None:
        """The relay knows who is connected; it does not know who they are."""
        for known in roster:
            room.note_seen(known, "relay")
        if not device_id:
            return
        if online:
            room.note_seen(device_id, "relay")
        member = room.members.get(device_id)
        who = member.label if member and member.label != device_id else device_id
        envelope = room.local_event(
            K_PRESENCE,
            f"{who} {'joined' if online else 'left'} {room.name}",
            device_id=device_id, online=online, roster=roster,
        )
        await self._deliver_envelope(envelope, "local")
        if online:
            # A new arrival has not seen our hello, so send it again. Cheap, and
            # it is what turns a bare device id into a name on their side.
            await room.announce()

    async def _on_missed(self, room: Room, missed: dict[str, Any]) -> None:
        """Retention ate messages we never saw. Say so rather than hide the hole."""
        count = missed.get("count", "some")
        envelope = room.local_event(
            K_SYSTEM,
            f"{count} message(s) in {room.name} were dropped by the relay's "
            f"retention limit before this device reconnected, and cannot be recovered",
            missed=missed,
        )
        await self._deliver_envelope(envelope, "local")

    # -- delivery -------------------------------------------------------------- #

    async def _deliver_envelope(self, envelope: dict[str, Any], transport: str) -> None:
        # Everything in memory first, and with no await in the way, so that the
        # order envelopes arrive in is the order the inbox hands them out in and
        # a channel is registered before anything can ask about it, however slow
        # a sink gets. The disk comes after, off the loop.
        channel = envelope.get("channel")
        meta: dict[str, Any] | None = None
        state_changed = False

        if envelope.get("kind") == K_CHANNEL_OPEN:
            meta = self._register_channel(envelope)
            state_changed = meta is not None
        elif envelope.get("kind") == K_CHANNEL_CLOSE:
            record = self.channels.get((envelope.get("body") or {}).get("channel_id") or "")
            if record:
                record["status"] = "closed"
                record["closed_at"] = now_iso()
                state_changed = True

        # A full deque drops from the left with no sound. The outbox counts and
        # logs its overflow; this one did neither, so a busy peer during twenty
        # minutes of the agent's own work silently ate the oldest unread
        # messages and `unread_remaining` still read zero.
        if len(self.inbox) == self.inbox.maxlen and self.inbox:
            if not self.inbox[0]["read"]:
                self.dropped_unread += 1
                if self.dropped_unread in (1, 10, 100) or self.dropped_unread % 500 == 0:
                    _log(f"inbox is full at {self.inbox.maxlen}; "
                         f"{self.dropped_unread} unread message(s) discarded so "
                         f"far. Raise inbox_max, or drain it more often.")

        self._seq += 1
        self.inbox.append({
            "seq": self._seq,
            "received_at": now_iso(),
            "transport": transport,
            "room_id": envelope.get("room_id"),
            "channel": channel,
            "read": False,
            "env": envelope,
        })
        for fut in self._waiters:
            if not fut.done():
                fut.set_result(None)
        self._waiters.clear()

        await self._conv_io(self.logger.log, envelope,
                            "in" if not envelope.get("_local") else "local",
                            transport)
        if state_changed:
            await self._persist_soon()
        if meta is not None:
            await self._conv_io(self.logger.write_meta, meta["conv_id"], meta)

    def _register_channel(self, envelope: dict[str, Any]) -> dict[str, Any] | None:
        """Record a channel a peer opened. Returns the `meta.json` to write.

        The writes are the caller's job, because this runs on the event loop and
        they do not belong there. What has to happen here and now is the
        in-memory record, so that a `link_channel list` arriving in the same
        moment cannot miss a channel whose open message is already in the inbox.
        """
        body = envelope.get("body") or {}
        channel_id = body.get("channel_id")
        # The peer picks this, it is persisted to state.json, and it becomes a
        # directory name under every `.conv/` sink. `is_channel_id` is the same
        # check `validate_envelope` makes on the envelope's own `channel`.
        if not is_channel_id(channel_id):
            return None
        origin = envelope.get("origin") or {}
        self.channels[channel_id] = {
            "channel_id": channel_id,
            "room_id": envelope.get("room_id"),
            "topic": clean_text(body.get("topic", ""), 200),
            "opened_by": clean_label(origin.get("label")) or envelope.get("device_id"),
            "opened_by_device": envelope.get("device_id"),
            "opened_by_role": origin.get("role"),
            "opened_by_agent": origin.get("agent"),
            "created_at": envelope.get("ts"),
            "status": "open",
            "side": "remote",
        }
        return {
            "conv_id": channel_id,
            "kind": "channel",
            "parent": envelope.get("room_id"),
            "topic": body.get("topic", ""),
            "opened_by": origin.get("label"),
        }

    # -- control plane ----------------------------------------------------------- #

    def _authorised(self, req: Any) -> bool:
        """Does this request carry the token from daemon.json?

        The token is the whole of the authentication, and that is deliberate:
        the secret lives in a 0600 file inside the link home, so "can read the
        daemon's home" is exactly the permission being checked, and the OS
        enforces it. A browser cannot read a file, which is what makes the
        cross-origin POST shape harmless even before the connection is dropped.
        """
        if not isinstance(req, dict):
            return False
        supplied = req.get("token")
        return isinstance(supplied, str) and hmac.compare_digest(supplied, self.token)

    async def _on_control(self, reader, writer) -> None:
        # The token gates *ops*, not resources. A connection that never sends a
        # newline was held for ever and could buffer up to the line limit first,
        # and nothing capped how many there were: 250 idle sockets took the
        # daemon from 35 MB to 163 MB. A different local user, who by design
        # cannot read the token out of the 0600 daemon.json, could still do it.
        if self._ctrl_conns >= CTRL_MAX_CONNS:
            try:
                writer.write(b'{"ok": false, "error": "too many connections"}\n')
                await writer.drain()
            except OSError:
                pass
            writer.close()
            return
        self._ctrl_conns += 1
        first = True
        try:
            while True:
                try:
                    if first:
                        # Only the first line is on a clock. After that the
                        # caller has authenticated, and `wait` legitimately
                        # holds a connection open for minutes.
                        line = await asyncio.wait_for(reader.readline(),
                                                      timeout=CTRL_FIRST_LINE_S)
                        first = False
                    else:
                        line = await reader.readline()
                except asyncio.TimeoutError:
                    break
                except ValueError:
                    # Past the limit. Say so rather than dropping the connection
                    # with no explanation, which is what asyncio's unhandled
                    # ValueError used to do.
                    writer.write(b'{"ok": false, "error": "request too large"}\n')
                    await writer.drain()
                    break
                if not line:
                    break
                try:
                    req = json.loads(line.decode("utf-8"))
                except ValueError:
                    # One strike. Answering an unparseable line and reading on
                    # is what let an HTTP request smuggle a command through in
                    # its body: the request line and headers each drew an error,
                    # and then the body parsed and ran.
                    writer.write(b'{"ok": false, "error": "invalid JSON request"}\n')
                    await writer.drain()
                    break
                if not self._authorised(req):
                    _log("control request refused: bad or missing token")
                    writer.write(b'{"ok": false, "error": "unauthorised: '
                                 b'no valid control token"}\n')
                    await writer.drain()
                    break
                try:
                    resp = await self._dispatch(req)
                except Exception as exc:      # never kill the control server
                    _log(f"control op failed: {type(exc).__name__}: {exc}")
                    resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._ctrl_conns -= 1
            try:
                writer.close()
            except OSError:
                pass

    async def _dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        op = req.get("op")
        handler = getattr(self, f"_op_{op}", None) if isinstance(op, str) else None
        if handler is None:
            return {"ok": False, "error": f"unknown op: {op!r}"}
        self._note_caller(req.get("agent_kind"), sending=op in _SENDING_OPS)
        return await handler(req)

    # -- who is talking to this daemon ------------------------------------------ #

    def _note_caller(self, kind: Any, sending: bool) -> None:
        """Record which agent path made a control call, and warn about two.

        `self.identity.agent_kind` answers about the daemon's own environment,
        which is whoever happened to spawn it. It says nothing about who has
        been *talking* to it, and that is the question docs/postmortems.md
        opens with: two agents sharing one home are one device and one room
        member, their sends are never echoed back to either inbox, and every
        other symptom is of a room that is working perfectly.

        Sending is the line, not calling. A human running `doctor` in a
        terminal is a second agent kind and is not a second agent, and a
        warning that everyone who has ever used the CLI sees forever is one
        nobody reads.
        """
        # Input, from the control socket, on its way to a log line, a status
        # response and a model's context.
        label = clean_label(kind, 24) or "unknown"
        seen = self.callers.get(label)
        if seen is None:
            seen = self.callers[label] = {"agent_kind": label, "calls": 0,
                                          "sends": 0, "first_seen": now_iso(),
                                          "last_seen": None}
        seen["calls"] += 1
        seen["last_seen"] = now_iso()
        if not sending:
            return

        seen["sends"] += 1
        senders = sorted(k for k, v in self.callers.items() if v["sends"])
        if len(senders) > 1 and not self._warned_shared_identity:
            self._warned_shared_identity = True
            _log(f"WARNING: this identity ({self.identity.device_id}) has now "
                 f"sent under more than one agent path: {', '.join(senders)}. "
                 f"If that is two agents sharing one CLAUDE_LINK_HOME they are "
                 f"one room member, and neither will ever see the other's "
                 f"messages. See `agent-link doctor`.")

    def _identity_shared(self) -> dict[str, Any] | None:
        """More than one agent path has sent as this device. Stated, not judged.

        It can be innocent -- one person, one agent, and an `agent-link send`
        typed into a terminal. It can also be the hour-long silence in
        docs/postmortems.md. What the daemon can see is the fact, so the fact
        is what it reports, with the one check that separates the two.
        """
        senders = sorted(k for k, v in self.callers.items() if v["sends"])
        if len(senders) < 2:
            return None
        return {
            "kinds": senders,
            "problem": (f"messages have been sent as this one device "
                        f"({self.identity.device_id}) by more than one agent "
                        f"path: {', '.join(senders)}"),
            "fix": ("If that is you running the CLI yourself, nothing is wrong. "
                    "If it is two agents, they are sharing one identity and so "
                    "are one room member, which means neither can see the "
                    "other. Each agent needs its own CLAUDE_LINK_HOME and "
                    "CLAUDE_LINK_CTRL_PORT; re-running the installer sets "
                    "both."),
        }

    # -- ops ---------------------------------------------------------------------- #

    async def _op_ping(self, req) -> dict[str, Any]:
        return {"ok": True, "pid": os.getpid(), "version": __version__,
                "device_id": self.identity.device_id}

    def _resolve_room(self, ref: str | None) -> tuple[Room | None, str | None, str | None]:
        """Accept a room id, a room name, or a channel id. Returns (room, channel, error)."""
        if not ref:
            if len(self.rooms) == 1:
                return next(iter(self.rooms.values())), None, None
            if not self.rooms:
                return None, None, "no rooms joined; run link_join first"
            return None, None, ("several rooms joined; pass room= "
                                f"({', '.join(r.name for r in self.rooms.values())})")
        if ref in self.rooms:
            return self.rooms[ref], None, None
        channel = self.channels.get(ref)
        if channel:
            room = self.rooms.get(channel.get("room_id") or "")
            if room is None:
                return None, None, f"channel {ref} belongs to a room you have left"
            return room, ref, None
        for room in self.rooms.values():
            if room.name == ref.strip().lower():
                return room, None, None
        return None, None, f"unknown room: {ref}"

    async def _op_status(self, req) -> dict[str, Any]:
        # The control port deliberately opens before persisted rooms are
        # rejoined so clients can tell that the daemon is alive. Room key
        # derivation is intentionally slow, though, and without a gate the first
        # status call can mistake "still loading" for "never joined". The gate
        # is bounded because a disconnected network share may never finish
        # starting; status is the diagnostic call and must still answer.
        try:
            await asyncio.wait_for(
                self._rooms_ready.wait(), timeout=STATUS_READY_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            pass
        loading = not self._rooms_ready.is_set()
        verbose = bool(req.get("verbose"))
        unread = sum(1 for r in self.inbox
                     if not r["read"] and r["env"].get("kind") == K_MSG)
        unread_system = sum(1 for r in self.inbox
                            if not r["read"] and r["env"].get("kind") != K_MSG)
        # Once, off the loop, and reused by everything below that needs it.
        # `_setup_hint` consults it too, and it is called from here and from
        # `_op_join`, both of which run on the loop.
        # Bounded. Every off-loop call in the project shares the loop's default
        # executor, so a handful of stalled filesystem calls park its workers and
        # everything later queues behind them -- including this, in the one call
        # whose whole job is to answer when nothing else is working.
        try:
            drift = await asyncio.wait_for(
                asyncio.to_thread(self._config_drift), timeout=1.0)
        except (asyncio.TimeoutError, OSError, ValueError):
            drift = None
        out = {
            "ok": True,
            "device_id": self.identity.device_id,
            "label": self.identity.label,
            "agent_kind": self.identity.agent_kind,
            "version": __version__,
            "relay_url": self.cfg.get("relay_url"),
            "unread": unread,
            "shared_dir": self.cfg.get("shared_dir"),
            "git_remote": _redacted_remote(self.cfg.get("git_remote")),
            "config_stale": drift,
            # Which agent paths have actually used this daemon, and whether more
            # than one of them has signed as this device. `agent_kind` above is
            # only the daemon's own environment.
            "agent_paths": sorted(self.callers.values(),
                                  key=lambda c: c["agent_kind"]),
            "identity_shared": self._identity_shared(),
            "rooms": [room.status(verbose) for room in self.rooms.values()],
            "open_rooms": (self.open_rooms or None) if not self.rooms else None,
            "knocking": [
                {"room": rec["name"], "room_id": rid,
                 "knocked_at": rec.get("knocked_at")}
                for rid, rec in self.pending_knocks.items()
            ],
            "channels": [c for c in self.channels.values() if c.get("status") == "open"],
            "setup_needed": self._setup_hint(drift),
            "loading": loading,
        }
        if verbose:
            out.update({
                "unread_system": unread_system,
                "fingerprint": self.identity.fingerprint(),
                "host": socket.gethostname(),
                "ip": primary_ip(),
                "ctrl_port": self.actual_ctrl_port,
                "started_at": self.started_at,
                "log_sinks": self.logger.sinks,
                "inbox_size": len(self.inbox),
                "dropped_unread": self.dropped_unread,
            })
        return out

    async def _op_invite(self, req) -> dict[str, Any]:
        """Print a room's invite string, on purpose.

        Split out of `status` so that obtaining the room's master secret is
        always an explicit act. A diagnostic that hands it back turns "show me
        the state of the link" into "write the key to every room into a
        transcript", and nobody reads a status line expecting that.
        """
        room, _channel, error = self._resolve_room(req.get("room"))
        if error:
            return {"ok": False, "error": error}
        return {"ok": True, "room": room.name, "invite": room.invite(),
                # The secretless half: knocking at it needs a member's yes.
                "door": doorway.door_code(room.name, room.room_id)}

    async def _op_join(self, req) -> dict[str, Any]:
        """Create or join a room. With no secret, one is generated."""
        raw = (req.get("invite") or "").strip()
        name = (req.get("room") or "").strip()
        secret = (req.get("passphrase") or req.get("secret") or "").strip()
        warning = None
        created = False

        # A room is people, so entering one starts with a name. `name=` both
        # satisfies the gate and saves the answer, so the agent can ask the
        # human once ("Your name?") and never again.
        if req.get("name"):
            chosen = clean_label(req["name"], 40)
            if chosen:
                await asyncio.to_thread(
                    save_config, {**load_config(), "display_name": chosen})
                self.cfg["display_name"] = chosen
                for existing_room in self.rooms.values():
                    existing_room.display_name = chosen
        if not await asyncio.to_thread(display_name_set):
            return {"ok": False, "need_name": True,
                    "error": "no display name set for this install"}

        # A door code is not an invite: it carries no secret, so it becomes a
        # knock rather than a join. Dispatched before parse_invite, or the
        # code would be derived as a secret and mint a valid, empty room.
        if raw:
            try:
                at_door = doorway.parse_door_code(raw)
            except CryptoError as exc:
                return {"ok": False, "error": str(exc)}
            if at_door:
                return await self._begin_knock(at_door, req)

        try:
            if raw:
                name, secret = parse_invite(raw)
            elif name and secret:
                warning = ("a typed passphrase is weaker than a generated invite: "
                           "the relay sees the room id, which is derived from it. "
                           "Prefer link_join with no secret, then share the invite.")
            elif name:
                gate = await self._join_or_create_gate(req)
                if gate is not None:
                    return gate
                name, secret = parse_invite(new_invite(name))
                created = True
            else:
                return {"ok": False, "error": "pass room= to create one, or invite= to join one"}
        except CryptoError as exc:
            return {"ok": False, "error": str(exc)}

        async with self._state_lock:
            # Only an explicit override is pinned to the room. Copying the
            # configured relay in here would freeze it: move the relay to a new
            # host and every room already joined would keep dialling the old
            # address, with no way out but leaving and re-joining each one.
            # Re-joining a room you are already in must not rewind it. A fresh
            # record resets `seq_reserved_to` to 0 and empties `seen`, so the
            # outbound counter repeats numbers the room has already seen and
            # every remembered msg_id is forgotten -- which means anything still
            # in the relay's mailbox or sitting in the shared folder arrives
            # again, as new, in the inbox and the transcript.
            existing = next(
                (r for r in (load_state().get("rooms") or {}).values()
                 if r.get("name") == name and r.get("secret") == secret),
                None,
            )
            record = {
                "name": name,
                "secret": secret,
                "joined_at": (existing or {}).get("joined_at") or now_iso(),
                "seq_reserved_to": int((existing or {}).get("seq_reserved_to") or 0),
                "seen": list((existing or {}).get("seen") or []),
            }
            # Creating the room makes this device its governance genesis
            # author -- and only creating it. A joiner publishing a second
            # genesis would race the real one for the lower hash.
            if created or (existing or {}).get("gov_creator"):
                record["gov_creator"] = True
            if req.get("relay_url"):
                record["relay_url"] = req["relay_url"]
            if req.get("shared_dir"):
                record["shared_dir"] = req["shared_dir"]
            if req.get("git_remote"):
                record["git_remote"] = req["git_remote"]
            if req.get("git_branch"):
                record["git_branch"] = req["git_branch"]
            try:
                room = await self._start_room(record)
            except (CryptoError, ValueError) as exc:
                return {"ok": False, "error": f"could not join: {exc}"}
            await self._persist_soon()

        # Give a transport a moment so the answer can state who is actually
        # there. A room you expected to share and joined alone is almost always
        # a typo in the invite, and saying so now saves a long confusion.
        relay = room.transport("relay")
        shared = room.transport("file")
        repo = room.transport("git")
        if relay is None and shared is None and repo is None:
            # Nothing configured, so the loop below can never come true and used
            # to sleep out all forty iterations. Four and a half seconds, on the
            # very first command somebody runs, to be told they are alone.
            pass
        else:
            for _ in range(40):
                if ((relay is not None and relay.online)
                        or shared is not None or repo is not None):
                    break
                await asyncio.sleep(0.1)

        roster = list(getattr(relay, "roster", []) or [])
        for medium in (shared, repo):
            if medium is not None:
                roster += [d for d in medium.roster() if d not in roster]

        # Somebody has just pointed the channel at a repository. If that
        # repository runs CI on every branch, the heartbeat is about to run it a
        # couple of thousand times a day, and the person who finds out is
        # whoever owns the Actions bill.
        if req.get("git_remote"):
            note = await self._shared_repo_note(req["git_remote"],
                                                req.get("git_branch"))
            if note:
                warning = f"{warning} {note}" if warning else note

        return {
            "ok": True,
            "room": room.name,
            "room_id": room.room_id,
            "invite": room.invite(),
            "members_online": len(roster),
            "roster": roster,
            "transport": (room.live_transports or ["offline"])[0],
            # The reason the daemon already had and never handed over. Without
            # it the renderer fell back to blaming the invite, which is right
            # only when the transport is healthy.
            "setup_error": room.setup_error,
            "relay_error": getattr(relay, "last_error", None),
            "warning": warning,
            "setup_needed": (self._setup_hint(await asyncio.to_thread(self._config_drift))
                             if not room.live_transports else None),
        }

    async def _begin_knock(self, at_door: tuple[str, str],
                           req: dict[str, Any]) -> dict[str, Any]:
        room_name, room_id = at_door
        if room_id in self.rooms:
            return {"ok": True, "room": room_name, "room_id": room_id,
                    "already_joined": True}
        rec = {
            "room_id": room_id,
            "name": room_name,
            "git_remote": req.get("git_remote") or self.cfg.get("git_remote"),
            "git_branch": (req.get("git_branch") or self.cfg.get("git_branch")
                           or "claude-link"),
            "shared_dir": req.get("shared_dir") or self.cfg.get("shared_dir"),
            "knocked_at": now_iso(),
        }
        if not rec["git_remote"] and not rec["shared_dir"]:
            return {"ok": False,
                    "error": "a door code needs a carrier; set git_remote to "
                             "the repo this room lives on, then retry"}
        self.pending_knocks[room_id] = rec
        await self._persist_soon()
        self._spawn_knock(rec)
        return {"ok": True, "knocked": True, "room": room_name,
                "room_id": room_id}

    def _spawn_knock(self, rec: dict[str, Any]) -> None:
        room_id = rec["room_id"]
        if room_id in self._knock_tasks and not self._knock_tasks[room_id].done():
            return
        task = asyncio.get_running_loop().create_task(
            self._knock_worker(rec), name=f"knock-{room_id[:12]}")
        self._knock_tasks[room_id] = task
        task.add_done_callback(
            lambda t: not t.cancelled() and t.exception() is not None
            and _log(f"knock worker for {room_id} died: {t.exception()!r}"))

    async def _knock_worker(self, rec: dict[str, Any]) -> None:
        """Knock at a door and wait to be let in. Survives restarts: the
        pending record is persisted and this task is respawned from run()."""
        room_id = rec["room_id"]

        async def swallow(_frame):
            return None

        async def carrier():
            """A presence-less transport on the room being knocked at: it must
            sync files without ever heartbeating a room we are not in."""
            if rec.get("git_remote"):
                from .transport_git import GitTransport, check_remote, clone_dir

                remote = check_remote(rec["git_remote"])
                workdir = self.cfg.get("git_dir") or clone_dir(
                    root_dir(), remote, rec["git_branch"])
                t = GitTransport(
                    remote=remote, branch=rec["git_branch"],
                    workdir=os.path.abspath(os.path.expanduser(workdir)),
                    room_id=room_id, device_id=self.identity.device_id,
                    on_frame=swallow, presence=False, log=_log,
                    sync_ms=int(self.cfg.get("git_sync_ms", 3000)))
                await t.start()
                return t
            from .transport_file import FileTransport

            t = FileTransport(shared_dir=rec["shared_dir"], room_id=room_id,
                              device_id=self.identity.device_id,
                              on_frame=swallow, presence=False, log=_log)
            await t.start()
            return t

        try:
            transport = await carrier()
        except Exception as exc:
            _log(f"knock at {room_id} cannot reach its carrier: {exc}")
            return                      # respawned on next daemon start
        base = transport.shared_dir
        try:
            knocked = False
            while room_id in self.pending_knocks:
                grant_raw, entries, own = await asyncio.to_thread(
                    lambda: (doorway.read_grant_file(base, room_id,
                                                     self.identity.device_id),
                             doorway.read_door_entries(base, room_id),
                             doorway.read_knock_files(base, room_id)))
                if grant_raw is not None:
                    if await self._finish_knock(rec, transport, grant_raw):
                        return
                elif not knocked:
                    mine = [k for k in own
                            if k.get("device_id") == self.identity.device_id]
                    if mine:
                        knocked = True     # a restart found our knock in place
                    elif entries:
                        name = clean_label(self.cfg.get("display_name") or "", 40)
                        knock = doorway.build_knock(
                            self.identity, room_id, name, entries)
                        await transport.publish_files(
                            lambda: doorway.write_knock_file(base, room_id, knock))
                        knocked = True
                await asyncio.sleep(3.0)
        finally:
            await transport.stop()

    async def _finish_knock(self, rec, transport, grant_raw) -> bool:
        """True when the pending knock is finished (joined or declined)."""
        room_id = rec["room_id"]
        try:
            opened = doorway.read_grant(self.identity, room_id, grant_raw)
        except CryptoError as exc:
            _log(f"unreadable grant at {room_id}: {exc}")
            return False
        base = transport.shared_dir
        if opened.get("denied"):
            # The notice lands first: nothing watching pending_knocks may ever
            # see the knock end without the reason already in the inbox.
            await self._notice(room_id,
                               f"The room declined: {rec['name']} said no to "
                               f"this device joining.")
            self.pending_knocks.pop(room_id, None)
            await transport.publish_files(
                lambda: doorway.remove_join_files(base, room_id,
                                                  self.identity.device_id))
            await self._persist_soon()
            return True
        ok = await asyncio.to_thread(doorway.grant_matches_room,
                                     opened["name"], opened["secret"], room_id)
        if not ok:
            # A forged or corrupt grant. Say so once, drop the file so a real
            # one can land, and keep waiting.
            _log(f"a grant at {room_id} did not derive to that room; ignoring")
            await transport.publish_files(
                lambda: doorway.remove_join_files(base, room_id,
                                                  self.identity.device_id))
            return False
        await transport.publish_files(
            lambda: doorway.remove_join_files(base, room_id,
                                              self.identity.device_id))
        await transport.stop()          # before _start_room reuses the clone
        self.pending_knocks.pop(room_id, None)
        record = {"name": opened["name"], "secret": opened["secret"],
                  "joined_at": now_iso(), "seq_reserved_to": 0, "seen": []}
        for key in ("git_remote", "git_branch", "shared_dir"):
            if rec.get(key) and rec[key] != self.cfg.get(key):
                record[key] = rec[key]
        async with self._state_lock:
            room = await self._start_room(record)
            await self._persist_soon()
        asyncio.create_task(self._arrival_notice(room))
        return True

    async def _arrival_notice(self, room: Room) -> None:
        """“You're in.” — with names, once the hellos have had a moment."""
        for _ in range(16):
            if any(m.name or m.label != m.device_id
                   for m in room.members.values()):
                break
            await asyncio.sleep(0.5)
        others = sorted((m.name or m.label) for m in room.members.values())
        text = "You're in." if not others else (
            f"You're in. {len(others)} other(s) here: {', '.join(others)}.")
        # K_KNOCK, not K_SYSTEM: door-lifecycle events must reach the default
        # inbox fetch (and so the hook), or the human never hears the answer.
        await self._deliver_envelope(
            room.local_event(K_KNOCK, text), "local")

    async def _notice(self, room_id: str, text: str) -> None:
        """A local inbox notice for a room we are not (or no longer) in."""
        envelope = make_envelope(
            K_KNOCK, room_id, self.identity.device_id, 0,
            make_origin(self.identity, agent="link"), body={"text": text})
        envelope["_verified"] = True
        envelope["_local"] = True
        await self._deliver_envelope(envelope, "local")

    async def _discover(self, remote: str, branch: str | None):
        """Bounded, advisory, off the loop. None means "could not tell"."""
        from .discover import discover_rooms
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(discover_rooms, remote,
                                  branch or self.cfg.get("git_branch")
                                  or "claude-link"),
                timeout=20.0)
        except Exception:
            return None

    async def _background_discover(self) -> None:
        """Roomless with a carrier configured: find out, once, what is open
        there, so the first link_status can ask join-or-create instead of
        suggesting a room be invented."""
        found = await self._discover(self.cfg["git_remote"], None)
        if not found:
            return
        state = await asyncio.to_thread(load_state)
        declined = state.get("rooms_declined") or {}
        self.open_rooms = [r for r in found if r["room_id"] not in declined]

    async def _join_or_create_gate(self, req) -> dict[str, Any] | None:
        """One question, asked once: this carrier already has an open room —
        join it, or really make a new one?"""
        remote = req.get("git_remote") or self.cfg.get("git_remote")
        if not remote:
            return None
        state = await asyncio.to_thread(load_state)
        declined = state.get("rooms_declined") or {}
        found = await self._discover(remote, req.get("git_branch"))
        open_rooms = [r for r in (found or [])
                      if r["room_id"] not in self.rooms
                      and r["room_id"] not in self.pending_knocks
                      and r["room_id"] not in declined]
        if not open_rooms:
            return None
        if req.get("create_anyway"):
            # The human chose "new room": remember it per room so this is
            # asked once, not every session.
            def remember():
                st = load_state()
                for r in open_rooms:
                    st.setdefault("rooms_declined", {})[r["room_id"]] = now_iso()
                save_state(st)
            await asyncio.to_thread(remember)
            return None
        return {"ok": False, "needs_decision": "join_or_create",
                "open_rooms": open_rooms,
                "error": "this carrier already has an open room"}

    async def _shared_repo_note(self, remote: str,
                                branch: str | None) -> str | None:
        """Whether pointing the channel at this repo will run somebody's CI.

        Off the event loop and bounded. It is two network round trips, this is
        the loop that has to keep answering `link_inbox` in a millisecond, and a
        diagnostic that delays a join is worse than one that does not appear.
        Every failure reads as "no warning": a join must never fail because the
        advisory could not be produced.
        """
        from .transport_git import DEFAULT_BRANCH, shared_repo_warning

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    shared_repo_warning, remote, branch or DEFAULT_BRANCH,
                    float(self.cfg.get("git_presence_s", 45)),
                ),
                timeout=15.0,
            )
        except Exception:                    # noqa: BLE001 - advisory only
            return None

    def _config_drift(self) -> dict[str, Any] | None:
        """What config.json says now that this daemon does not know. Blocking.

        The transports are built once, from the config as it was at startup, so
        a `config --set` afterwards changes the file and nothing else. Nothing
        used to notice: the room came up with no transport, no `setup_error`,
        and a status that reads exactly like a quiet room, under advice telling
        you to set the value you had just set.
        """
        try:
            changed = config_changes(self.cfg)
        except (OSError, ValueError):
            # An unreadable or unparseable config is `doctor`'s to report. It is
            # not evidence that this daemon is out of date.
            return None
        if not changed:
            return None
        transport = sorted(k for k in changed if k in TRANSPORT_KEYS)
        return {
            "problem": (
                "config.json has changed since this daemon started"
                + (f", including {', '.join(transport)}, which it acts on only "
                   "at startup" if transport else "")
            ),
            "changed": sorted(changed),
            "affects_transport": transport,
            "fix": "restart the daemon: `agent-link restart`",
        }

    def _stale_setup(self, drift: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Drift that has left this daemon with no way to carry a message.

        Narrower than `_config_drift` on purpose. A daemon that already has a
        transport and a newer file is merely out of date, and says so in
        `link_status`. A daemon with *no* transport, against a file that has
        one, is the silent failure: everything reads healthy and nothing moves.

        `drift` is passed in by callers that already computed it off the loop.
        Recomputing here would put a config read back on the event loop, which
        is the thing `_op_status` threads it out to avoid.
        """
        if (self.cfg.get("shared_dir") or self.cfg.get("relay_url")
                or self.cfg.get("git_remote")):
            return None
        if drift is None:
            drift = self._config_drift()
        if not drift or not drift["affects_transport"]:
            return None
        return drift

    def _setup_hint(self, drift: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """What to tell someone whose room has no way to reach anybody.

        Two machines on different networks, neither with an open inbound port,
        need something between them, and there is one answer: a private repo
        they can both push to. A synced folder used to be offered first and is
        no longer offered at all -- it was the headline setup and the least
        proven path here, tested only against a local temp directory that is
        instant, atomic and has no opinions, while OneDrive and Dropbox bring
        sync latency in minutes, conflict copies nothing parses, and partial
        writes the peer can see. `FileTransport` remains because `GitTransport`
        is built on it; recommending it does not.
        """
        if (self.cfg.get("git_remote") or self.cfg.get("relay_url")
                or self.cfg.get("shared_dir")):
            return None
        # Before advising anyone to configure a transport, check whether they
        # already have. Telling someone to run the command they just ran is
        # worse than saying nothing: it sends them to look at the value, find it
        # correct, and conclude the fault is somewhere else entirely.
        stale = self._stale_setup(drift)
        if stale:
            return {
                "problem": (
                    "a transport is configured in config.json, but this daemon "
                    "started before that and is still running without one, so "
                    "no room can reach anyone"
                ),
                "fix": stale["fix"],
                "changed_since_startup": stale["affects_transport"],
            }
        # One carrier, and it is the repository they already share. The channel
        # is an orphan branch scoped to `claude-link/`, and the transport
        # fetches only that branch, so their code is never cloned or touched.
        return {
            "problem": "no git repository configured, so this room cannot "
                       "reach anyone yet",
            "fix": "point it at a private repo you and your colleague can both "
                   "push to, on both machines",
            # Quoted, and named after whatever actually runs on this machine.
            # It was `agent-link config --set git_remote=<...>`: the `<` is a
            # redirect in every shell, the placeholder has spaces in it, and
            # `agent-link` is not on PATH on a normal Windows install. Three
            # ways to fail in one line, in the string every `link_status` on a
            # fresh install prints.
            "command": (f'{cli_invocation()} config --set '
                        f'git_remote="<the private repo you already share>"'),
            "note_on_that_repo": (
                "an existing project repo is fine and nothing has to be created, "
                "but if it runs CI on every branch, add "
                "`branches-ignore: [claude-link]` to those workflows first: "
                "presence heartbeats push about 1900 times a day"
            ),
        }

    async def _op_leave(self, req) -> dict[str, Any]:
        room, _, err = self._resolve_room(req.get("room"))
        if err:
            return {"ok": False, "error": err}
        async with self._state_lock:
            await room.stop()
            self.rooms.pop(room.room_id, None)
            await self._persist_soon()
        return {"ok": True, "left": room.name, "room_id": room.room_id}

    async def _op_grant(self, req) -> dict[str, Any]:
        """Answer a knock. allow=true sends the key, sealed to the knocker."""
        room, _channel, err = self._resolve_room(req.get("room"))
        if err:
            return {"ok": False, "error": err}
        ref = (req.get("device") or "").strip()
        info = room.pending_knocks.get(ref)
        if info is None:
            named = [d for d, k in room.pending_knocks.items()
                     if (k.get("name") or "").lower() == ref.lower()]
            if len(named) == 1:
                ref = named[0]
                info = room.pending_knocks[ref]
            elif len(named) > 1:
                return {"ok": False, "error": f"two knocks match {ref!r}; "
                                              f"pass the device id"}
        if info is None:
            return {"ok": False, "error": f"no pending knock from {ref!r}"}
        allow = bool(req.get("allow", True))
        grant = doorway.build_grant(
            self.identity, room.room_id, ref, info["box_key"],
            room_name=room.name, secret=room.record.get("secret"),
            denied=not allow)
        source = room.transport("git") or room.transport("file")
        if source is None:
            return {"ok": False, "error": "this room has no carrier to answer on"}
        base = source.shared_dir
        await source.publish_files(
            lambda: doorway.write_grant_file(base, room.room_id, grant))
        room.pending_knocks.pop(ref, None)
        return {"ok": True, "room": room.name, "device": ref,
                "name": info.get("name"),
                "granted" if allow else "denied": True}

    def _gov_guard(self, room: Room) -> str | None:
        """None when this room supports governance and we are an admin;
        otherwise the refusal text."""
        if not room.gov_state.order:
            return ("This room predates roles. Recreate it (link_join) to "
                    "get removal.")
        if self.identity.device_id not in room.gov_state.admins:
            return "Only an admin can do that in this room."
        return None

    def _resolve_member(self, room: Room, ref: Any) -> str | None:
        """A device id passes through -- the target of a role or a removal
        need not have spoken yet. A display name resolves against the roster
        when exactly one member carries it."""
        ref = str(ref or "").strip()
        if is_device_id(ref):
            return ref
        named = [d for d, m in room.members.items()
                 if (m.name or "").lower() == ref.lower()]
        if len(named) == 1:
            return named[0]
        return None

    async def _op_role(self, req) -> dict[str, Any]:
        """Make a member admin, or member again. Admins only; rooms with a
        governance chain only."""
        room, _channel, err = self._resolve_room(req.get("room"))
        if err:
            return {"ok": False, "error": err}
        refusal = self._gov_guard(room)
        if refusal:
            return {"ok": False, "error": refusal}
        role = req.get("role")
        if role not in gov.ROLES:
            return {"ok": False, "error": f"role must be one of {list(gov.ROLES)}"}
        target = self._resolve_member(room, req.get("device"))
        if target is None:
            return {"ok": False,
                    "error": f"no member matches {req.get('device')!r}"}
        rec = gov.build_role(self.identity, room.room_id,
                             prev=room.gov_state.head, target=target, role=role)
        source = room.transport("git") or room.transport("file")
        if source is None:
            return {"ok": False, "error": "this room has no carrier to write on"}
        base = source.shared_dir
        await source.publish_files(
            lambda: gov.write_gov_record(base, room.room_id, rec))
        room.gov_state = gov.evaluate(
            await asyncio.to_thread(gov.read_gov_records, base, room.room_id),
            room.room_id)
        who = req.get("device")
        await self._deliver_envelope(
            room.local_event(K_KNOCK, f"{who} is now {role}."), "local")
        return {"ok": True, "device": target, "role": role}

    async def _op_remove(self, req) -> dict[str, Any]:
        """Remove a member: rekey the room to a successor sealed to everyone
        but them, tell them, and migrate. Admins only."""
        room, _channel, err = self._resolve_room(req.get("room"))
        if err:
            return {"ok": False, "error": err}
        refusal = self._gov_guard(room)
        if refusal:
            return {"ok": False, "error": refusal}
        target = self._resolve_member(room, req.get("device"))
        if target is None:
            return {"ok": False,
                    "error": f"no member matches {req.get('device')!r}"}
        if target == self.identity.device_id:
            return {"ok": False, "error": "removing yourself is `link_leave`"}
        source = room.transport("git") or room.transport("file")
        if source is None:
            return {"ok": False, "error": "this room has no carrier to write on"}
        base = source.shared_dir

        entries = await asyncio.to_thread(
            doorway.read_door_entries, base, room.room_id)
        box_keys: dict[str, str] = {}
        for entry in entries:
            try:
                box_keys[entry["device_id"]] = doorway.verify_door_entry(
                    entry, room.room_id)
            except CryptoError:
                continue
        known = set(room.members) | {self.identity.device_id}
        missing = sorted(known - set(box_keys) - {target})
        if missing:
            return {"ok": False, "error":
                    f"no door key on the carrier for {', '.join(missing)}; "
                    "they must sync once before a removal can include them"}

        succ_name, succ_secret = parse_invite(new_invite(room.name))
        rec = gov.build_removal(
            self.identity, room.room_id, prev=room.gov_state.head,
            target=target,
            successor_name=succ_name, successor_secret=succ_secret,
            admins=sorted(room.gov_state.admins),
            box_keys=box_keys,
            removed_by_name=self.cfg.get("display_name") or self.identity.label)
        await source.publish_files(
            lambda: gov.write_gov_record(base, room.room_id, rec))
        removed_label = req.get("device")
        new_room = await self._migrate_after_removal(
            room, rec, succ_name, succ_secret)
        return {"ok": True, "removed": target,
                "successor_room_id": new_room.room_id if new_room else None,
                "note": ("Door codes shared before this are void; share a new "
                         "one with `agent-link invite --door`. Also revoke "
                         f"{removed_label}'s push access to the carrier repo "
                         "-- git controls that, not the protocol.")}

    async def _migrate_after_removal(self, room: Room, rec: dict[str, Any],
                                     succ_name: str,
                                     succ_secret: str) -> Room | None:
        """Stop the predecessor, start the successor, carry the carrier
        config. The predecessor's transcript stays in .conv/. Used by the
        remover right away and by every survivor when their scan sees the
        removal."""
        old_record = room.record
        old_keys = room.keys
        queued = list(room.outbox)
        await self._notice(room.room_id,
                           f"{rec['device_id']} removed {rec['target']} from "
                           f"{room.name}. Door codes are void; reshare with "
                           "`agent-link invite --door`.")
        async with self._state_lock:
            await room.stop()
            self.rooms.pop(room.room_id, None)
            record = {"name": succ_name, "secret": succ_secret,
                      "joined_at": now_iso(), "seq_reserved_to": 0, "seen": [],
                      "gov_predecessor": room.room_id}
            if rec["device_id"] == self.identity.device_id:
                # The remover seeds the successor's chain; nobody else may.
                record["gov_creator"] = True
            for key in ("git_remote", "git_branch", "shared_dir", "relay_url"):
                if old_record.get(key):
                    record[key] = old_record[key]
            try:
                new_room = await self._start_room(record)
            except (CryptoError, ValueError) as exc:
                _log(f"[{room.name}] could not start the successor room: {exc}")
                await self._persist_soon()
                return None
            await self._persist_soon()

        # Until the carrier syncs the successor's chain, authority is what the
        # removal record embeds -- that is why it embeds it.
        new_room.gov_state = gov.GovState(
            admins=set(rec["successor"]["admins"]))

        source = new_room.transport("git") or new_room.transport("file")
        if source is not None and rec["device_id"] == self.identity.device_id:
            base = source.shared_dir

            def seed():
                genesis = gov.build_genesis(self.identity, new_room.room_id)
                gov.write_gov_record(base, new_room.room_id, genesis)
                prev = gov.record_hash(genesis)
                for dev in rec["successor"]["admins"]:
                    if dev == self.identity.device_id:
                        continue
                    r = gov.build_role(self.identity, new_room.room_id,
                                       prev, dev, "admin")
                    gov.write_gov_record(base, new_room.room_id, r)
                    prev = gov.record_hash(r)
            await source.publish_files(seed)
            recs = await asyncio.to_thread(
                gov.read_gov_records, base, new_room.room_id)
            new_room.gov_state = gov.evaluate(recs, new_room.room_id)

        # Whatever sat queued for the predecessor re-targets the successor.
        for frame in queued:
            try:
                env = open_and_verify(old_keys, frame)
            except CryptoError:
                continue
            if env.get("kind") != K_MSG:
                continue
            await new_room.send(new_room.build(
                K_MSG, env.get("body"), to=env.get("to", "*")))
        return new_room

    async def _op_knocks(self, req) -> dict[str, Any]:
        out = []
        for room in self.rooms.values():
            for dev, info in room.pending_knocks.items():
                out.append({"room": room.name, "device_id": dev,
                            "name": info.get("name"), "ts": info.get("ts")})
        return {"ok": True, "knocks": out, "count": len(out)}

    async def _op_send(self, req) -> dict[str, Any]:
        room, channel, err = self._resolve_room(req.get("room") or req.get("conv"))
        if err:
            return {"ok": False, "error": err}

        text = req.get("text") or ""
        # Checked here because nothing else checks it going out. The limit is
        # enforced on *receive* -- `validate_envelope`, the folder transport's
        # drain, the relay's `max_payload` -- and the control line limit is
        # deliberately twice it so an agent can hand over a diff. So an
        # oversized message was accepted, sealed, written, and dropped at every
        # far end while `link_send` reported `delivered: true`. Silent,
        # permanent, and the sender was the only party in a position to know.
        body = {"text": text, "meta": req.get("meta") or {}}
        size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        if size > MAX_MESSAGE_BYTES:
            return {"ok": False,
                    "error": f"message is {size // 1024} KiB and the limit is "
                             f"{MAX_MESSAGE_BYTES // 1024} KiB, so no peer would "
                             f"accept it",
                    "hint": "split it, or put the bulk somewhere both sides can "
                            "read and send the reference"}

        envelope = room.build(
            K_MSG,
            body,
            role=req.get("role") or ROLE_ORCHESTRATOR,
            agent=req.get("agent") or "main",
            channel=channel or req.get("channel"),
            to=req.get("to") or "*",
            reply_to=req.get("reply_to"),
            cwd=req.get("cwd"),
        )
        transport = await room.send(envelope)
        await self._conv_io(self.logger.log, envelope, "out", transport)
        return {
            "ok": True,
            "msg_id": envelope["msg_id"],
            "room": room.name,
            "channel": envelope.get("channel"),
            "transport": transport,
            "delivered": transport != "queued",
            "recipients": len(room.members),
            "note": None if transport != "queued" else
                    "nobody reachable right now; queued and sent on reconnect",
        }

    def _public(self, rec: dict[str, Any], full: bool = False) -> dict[str, Any]:
        env = rec["env"]
        origin = env.get("origin") or {}
        body = env.get("body") if isinstance(env.get("body"), dict) else {}
        # `body` is free-form JSON from a peer, so `text` need not be a string.
        # `truncate` used to call len() on whatever it was: an integer raised
        # TypeError, and that poisoned the inbox permanently -- the record stayed
        # unread, so every later link_inbox hit it and failed the same way, while
        # the clean messages ahead of it had already been marked read and were
        # returned to nobody.
        text = clean_text(body.get("text"))
        room = self.rooms.get(env.get("room_id") or "")
        out = {
            "seq": rec["seq"],
            "msg_id": env.get("msg_id"),
            "kind": env.get("kind"),
            "room": room.name if room else env.get("room_id"),
            "channel": env.get("channel"),
            "sent_at": env.get("ts"),
            "received_at": rec["received_at"],
            "transport": rec["transport"],
            "reply_to": env.get("reply_to"),
            # A label is self-declared and nothing binds it to the key that
            # signs the message, so it is cleaned before it is rendered and the
            # device id travels beside it. The compact renderers print both:
            # the name is what a human reads, the id is what cannot be chosen.
            "from": clean_label(origin.get("label")) or env.get("device_id"),
            "from_name": clean_label(origin.get("name") or "") or None,
            "from_device": env.get("device_id"),
            "from_agent_kind": clean_label(origin.get("agent_kind"), 24),
            "from_role": origin.get("role"),
            "from_agent": origin.get("agent"),
            "to": env.get("to"),
        }
        if full:
            out["text"] = text
            out["meta"] = body.get("meta") or {}
        else:
            out["text"] = truncate(text, PREVIEW_CHARS)
            out["truncated"] = len(text) > PREVIEW_CHARS
            meta = body.get("meta") or {}
            if meta:
                # Capped for the same reason `text` is, and it was not: a peer
                # chooses this, `link_inbox` renders it with `json.dumps` and no
                # limit, and one message with a 150 KB meta produced 151,327
                # characters from a single call advertised as cheap enough to
                # make between steps of your own work.
                blob = json.dumps(meta, ensure_ascii=False)
                if len(blob) > PREVIEW_CHARS:
                    out["meta"] = {"truncated": truncate(blob, PREVIEW_CHARS),
                                   "bytes": len(blob),
                                   "note": "full meta is in link_read"}
                else:
                    out["meta"] = meta
        return out

    def _matches(self, rec, room_id, channel, include_system) -> bool:
        env = rec["env"]
        if room_id and env.get("room_id") != room_id:
            return False
        if channel is not None and env.get("channel") != channel:
            return False
        # A knock is a question for the human, not housekeeping: it must reach
        # the default fetch (and so the hook) without include_system.
        if not include_system and env.get("kind") not in (K_MSG, K_KNOCK):
            return False
        return True

    async def _op_inbox(self, req) -> dict[str, Any]:
        room_id = None
        channel = None
        if req.get("room") or req.get("conv"):
            room, channel, err = self._resolve_room(req.get("room") or req.get("conv"))
            if err:
                return {"ok": False, "error": err}
            room_id = room.room_id

        limit = max(1, min(int(req.get("limit") or 20), 200))
        peek = bool(req.get("peek"))
        include_system = bool(req.get("include_system", False))

        out = []
        for rec in self.inbox:
            if rec["read"] or not self._matches(rec, room_id, channel, include_system):
                continue
            out.append(self._public(rec))
            if not peek:
                rec["read"] = True
            if len(out) >= limit:
                break

        # Counted with the same predicate as the fetch. Counting bare unread
        # records instead would advertise messages this caller can never see,
        # and would send it round the loop again for nothing.
        remaining = sum(
            1 for r in self.inbox
            if not r["read"] and self._matches(r, room_id, channel, include_system)
        )
        return {"ok": True, "messages": out, "count": len(out),
                "unread_remaining": remaining}

    async def _op_read(self, req) -> dict[str, Any]:
        """The full text of one message, by id. The other half of truncation."""
        msg_id = (req.get("msg_id") or "").strip()
        if not msg_id:
            return {"ok": False, "error": "msg_id is required"}
        for rec in self.inbox:
            if rec["env"].get("msg_id") == msg_id:
                return {"ok": True, "message": self._public(rec, full=True)}

        # Not in memory: fall back to the transcript, which outlives the daemon.
        for room in self.rooms.values():
            history = await asyncio.to_thread(
                self.logger.read_history, room.room_id, 2000)
            for record in history:
                if record.get("msg_id") == msg_id:
                    body = record.get("body") or {}
                    return {"ok": True, "message": {
                        "msg_id": msg_id,
                        "room": room.name,
                        "from": record.get("from_label"),
                        "sent_at": record.get("ts"),
                        "kind": record.get("kind"),
                        "text": body.get("text", ""),
                        "meta": body.get("meta") or {},
                        "from_transcript": True,
                    }}
        return {"ok": False, "error": f"no message with id {msg_id}"}

    async def _op_wait(self, req) -> dict[str, Any]:
        timeout = max(0.0, min(float(req.get("timeout_ms") or 30000) / 1000.0, 600.0))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            resp = await self._op_inbox(req)
            if not resp.get("ok") or resp["messages"]:
                resp["timed_out"] = False
                return resp
            remaining = deadline - loop.time()
            if remaining <= 0:
                return {"ok": True, "messages": [], "count": 0, "timed_out": True,
                        "waited_ms": int(timeout * 1000)}
            fut = loop.create_future()
            self._waiters.append(fut)
            try:
                await asyncio.wait_for(fut, timeout=remaining)
            except asyncio.TimeoutError:
                continue
            finally:
                if fut in self._waiters:
                    self._waiters.remove(fut)

    async def _op_channel_open(self, req) -> dict[str, Any]:
        room, channel, err = self._resolve_room(req.get("room"))
        if err:
            return {"ok": False, "error": err}
        if channel:
            return {"ok": False, "error": "channels do not nest"}

        channel_id = new_id("chan")
        topic = req.get("topic") or ""
        self.channels[channel_id] = {
            "channel_id": channel_id,
            "room_id": room.room_id,
            "topic": topic,
            "opened_by": self.identity.label,
            "opened_by_device": self.identity.device_id,
            "opened_by_role": req.get("role") or ROLE_ORCHESTRATOR,
            "opened_by_agent": req.get("agent") or "main",
            "created_at": now_iso(),
            "status": "open",
            "side": "local",
        }
        await self._persist_soon()
        await self._conv_io(self.logger.write_meta, channel_id, {
            "conv_id": channel_id, "kind": "channel", "parent": room.room_id,
            "topic": topic, "opened_by": self.identity.label,
        })

        envelope = room.build(
            K_CHANNEL_OPEN,
            {"channel_id": channel_id, "topic": topic,
             "text": f"opened side channel '{topic}'"},
            role=req.get("role") or ROLE_ORCHESTRATOR,
            agent=req.get("agent") or "main",
        )
        transport = await room.send(envelope)
        await self._conv_io(self.logger.log, envelope, "out", transport)
        return {"ok": True, "channel_id": channel_id, "room": room.name,
                "topic": topic, "transport": transport}

    async def _op_channel_close(self, req) -> dict[str, Any]:
        channel_id = req.get("channel_id") or req.get("conv") or req.get("channel")
        record = self.channels.get(channel_id or "")
        if not record:
            return {"ok": False, "error": f"unknown channel: {channel_id}"}
        record["status"] = "closed"
        record["closed_at"] = now_iso()
        await self._persist_soon()

        room = self.rooms.get(record.get("room_id") or "")
        if room:
            envelope = room.build(
                K_CHANNEL_CLOSE,
                {"channel_id": channel_id, "reason": req.get("reason") or "",
                 "text": f"closed side channel {channel_id}"},
                role=req.get("role") or ROLE_ORCHESTRATOR,
                agent=req.get("agent") or "main",
            )
            transport = await room.send(envelope)
            await self._conv_io(self.logger.log, envelope, "out", transport)
        return {"ok": True, "channel_id": channel_id, "status": "closed"}

    async def _op_channel_list(self, req) -> dict[str, Any]:
        items = list(self.channels.values())
        if not req.get("include_closed"):
            items = [c for c in items if c.get("status") == "open"]
        return {"ok": True, "channels": items, "count": len(items)}

    async def _op_history(self, req) -> dict[str, Any]:
        room, channel, err = self._resolve_room(req.get("room") or req.get("conv"))
        if err:
            return {"ok": False, "error": err}
        target = channel or room.room_id
        # Clamped exactly as `_op_inbox` is. Every record rendered here is
        # paid for in a model's context on this turn and every later one.
        limit = max(1, min(int(req.get("limit") or 30), 200))
        records = await asyncio.to_thread(self.logger.read_history, target, limit)
        return {"ok": True, "room": room.name, "records": records,
                "count": len(records)}

    async def _op_register_sink(self, req) -> dict[str, Any]:
        path = req.get("dir")
        # It creates the directory and seeds it with a README and a `.gitignore`
        # containing `*`. Pointed at a working tree that had no `.gitignore`,
        # that makes git ignore the whole repository. A sink is a `.conv`
        # directory; anything else is a mistake or an attempt.
        if not isinstance(path, str) or os.path.basename(
                os.path.normpath(os.path.expanduser(path))) != ".conv":
            return {"ok": False,
                    "error": "a log sink must be a directory named .conv"}
        added = await self._conv_io(self.logger.add_sink, path)
        if added:
            sinks = list(self.cfg.get("log_sinks") or [])
            resolved = os.path.abspath(os.path.expanduser(path))
            if resolved not in sinks:
                sinks.append(resolved)
                self.cfg["log_sinks"] = sinks
                save_config(self.cfg)
        return {"ok": True, "added": added, "sinks": self.logger.sinks}

    async def _op_config(self, req) -> dict[str, Any]:
        updates = req.get("set") or {}
        bad = [f"{k}={v!r}" for k, v in updates.items()
               if k in self.cfg and not config_value_ok(k, v)]
        if bad:
            # `inbox_max=-5` used to be accepted, written, and then kill every
            # later start at `deque(maxlen=-5)` -- before the control port
            # opens, so the only symptom is "daemon failed to start within 12s",
            # forever. A CLI typo reaches this; no attacker required.
            return {"ok": False,
                    "error": f"refusing to write: {', '.join(bad)}",
                    "hint": "the value has the wrong type or is out of range"}

        written = {k: v for k, v in updates.items() if k in self.cfg}
        if written and "display_name" in written:
            # display_name is not a transport key; a running daemon can act on
            # it immediately rather than reporting itself stale.
            self.cfg["display_name"] = written["display_name"]
            for room in self.rooms.values():
                room.display_name = clean_label(written["display_name"] or "", 40)
        if written:
            # The file, and deliberately **not** `self.cfg`. Updating both made
            # `_config_drift` compare the new value against itself and find
            # nothing, and `_setup_hint` see a configured transport, while the
            # transports themselves were built at startup and never rebuilt: a
            # room that reaches nobody with every diagnostic saying it is fine.
            # That drift report is the whole point of the machinery.
            await asyncio.to_thread(save_config, {**load_config(), **written})
        return {"ok": True, "config": {**self.cfg, **written},
                "note": "restart the daemon for transport changes to take effect"
                        if written else None}

    async def _op_shutdown(self, req) -> dict[str, Any]:
        self._stopping.set()
        return {"ok": True, "stopping": True}


def main() -> int:
    try:
        from . import crypto  # noqa: F401  -- fail here, with a readable message
    except ImportError as exc:
        print(f"agent-link cannot start: {exc}", file=sys.stderr, flush=True)
        return 2
    daemon = LinkDaemon(load_config())
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
