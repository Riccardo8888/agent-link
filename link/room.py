"""One room: its members, its transports, and the rules for what gets delivered.

A room is the v2 replacement for a two-peer conversation. It owns:

  * the keys, derived once from the invite and held in memory;
  * the roster -- who has been seen, over which transport, and how recently;
  * outbound sequencing, allocated in blocks so a crash can skip but never
    repeat;
  * inbound admission, which is `msg_id` and nothing else.

That last one is worth stating plainly, because the obvious alternative is a
trap. Dropping anything whose sequence number is not above a high-water mark
looks like replay protection, but a counter that rewinds -- an unsynced crash, a
restored backup, a config directory copied to a second machine -- then mutes
that device permanently and silently. Admission on a random `msg_id` cannot fail
that way. The sequence number survives as a diagnostic: gaps are reported, never
enforced.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from typing import Any, Awaitable, Callable

from . import crypto, gov
from .text import clean_label
from .envelope import (
    K_HELLO,
    ROLE_ORCHESTRATOR,
    make_envelope,
    make_origin,
    open_and_verify,
    seal_frame,
    validate_frame,
)
from .util import new_id, now_iso

SEQ_BLOCK = 1000          # sequence numbers reserved per persist
DEDUPE_MAX = 2000         # bounded, and well above the relay's own retention
MEMBER_STALE_S = 120.0    # no traffic for this long and a member reads as quiet


class Member:
    """What we know about another device in the room. All of it self-declared."""

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.label = device_id
        self.name = ""
        self.agent_kind = "unknown"
        self.public_key: str | None = None
        self.first_seen = time.time()
        self.last_heard: dict[str, float] = {}     # transport -> epoch
        self.highest_seq = -1
        self.received = 0
        self.gaps = 0
        # Which run of the sender `highest_seq` belongs to, and how many times
        # it has restarted. A restart resumes at a reserved ceiling, so the
        # numbering jumps; that is not loss and must not be counted as any.
        self.epoch: str | None = None
        self.resumes = 0

    @property
    def last_seen(self) -> float:
        return max(self.last_heard.values(), default=0.0)

    @property
    def online(self) -> bool:
        return (time.time() - self.last_seen) < MEMBER_STALE_S

    def note(self, transport: str, at: float | None = None) -> None:
        """Record when this member was last heard from over `transport`.

        `at` is for evidence that is older than the reading of it. A heartbeat
        file names the moment its owner wrote it, which may be hours before we
        listed the folder; stamping the reading instead of the writing is what
        turns a member who died overnight into one who answered a moment ago.
        Live transports omit it, because there the two moments are the same.
        """
        self.last_heard[transport] = time.time() if at is None else at

    def status(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "label": self.label,
            "name": self.name or None,
            "agent_kind": self.agent_kind,
            "online": self.online,
            "reachable_via": sorted(self.last_heard),
            "last_seen_s": round(time.time() - self.last_seen, 1) if self.last_seen else None,
            "received": self.received,
            "highest_seq": self.highest_seq,
            "gaps": self.gaps,
            # Beside `gaps` on purpose: the jump is real and worth seeing, it
            # simply is not loss. Hiding it would trade one lie for another.
            "resumes": self.resumes,
            "fingerprint": crypto.fingerprint(self.public_key) if self.public_key else None,
        }


class Room:
    """A joined room and everything needed to talk in it."""

    def __init__(
        self,
        keys: crypto.RoomKeys,
        identity,
        record: dict[str, Any],
        deliver: Callable[[dict[str, Any], str], Awaitable[None]],
        save_state: Callable[[], None],
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.keys = keys
        self.identity = identity
        self.record = record            # the persisted dict; mutated in place
        self.deliver = deliver
        self.save_state = save_state
        self.log = log or (lambda _m: None)
        # The human's chosen name, from config. Stamped on every envelope this
        # room builds; empty means nobody has chosen one yet.
        self.display_name: str = ""

        self.members: dict[str, Member] = {}
        # Knocks at this room's door, keyed by the knocker's device id, each
        # the dict `door.read_knock` returned. Fed by the daemon's scan;
        # cleared by a grant or a denial.
        self.pending_knocks: dict[str, dict[str, Any]] = {}
        # Governance: admins and removals, evaluated from the carrier's gov
        # records by the daemon's scan. Empty means a room from before roles
        # existed, and the role/remove ops say so.
        self.gov_state = gov.GovState()
        self.outbox: deque[dict[str, Any]] = deque(maxlen=500)
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._transports: dict[str, Any] = {}
        self.joined_at = now_iso()
        self.sent = 0
        self.dropped_duplicates = 0
        # The overflow branch in `_push` increments this and it was never
        # created here, so the 501st queued message raised `AttributeError`
        # instead of displacing the oldest -- the frame was destroyed rather
        # than queued, and the exception escaped `send` and `flush`. The comment
        # three lines above that branch says the whole point is that a message
        # must not go missing with every status line still green.
        self.dropped_overflow = 0
        # Set when a transport could not be brought up for a reason the user
        # has to fix, e.g. a shared folder that is not where it was said to be.
        self.setup_error: str | None = None

        # Sequence numbers come from a reserved block. Persisting the ceiling
        # once per thousand messages keeps the send path off the disk entirely,
        # and a crash loses the rest of the block rather than reusing it.
        self._seq = int(record.get("seq_reserved_to", 0))
        self._seq_ceiling = self._seq
        # Stamped on every envelope so a receiver can tell this run's numbering
        # from the last one's. A fresh Room is exactly a fresh run, which is
        # what makes the sequence jump above, so the two are minted together.
        self.epoch = new_id("run")
        for msg_id in record.get("seen", [])[-DEDUPE_MAX:]:
            self._seen[msg_id] = 0.0

    # -- identity ----------------------------------------------------------- #

    @property
    def room_id(self) -> str:
        return self.keys.room_id

    @property
    def name(self) -> str:
        return self.keys.room_name

    def invite(self) -> str:
        return f"{self.name}#{self.record.get('secret', '')}"

    # -- transports ---------------------------------------------------------- #

    def attach(self, name: str, transport: Any) -> None:
        self._transports[name] = transport

    def transport(self, name: str) -> Any:
        return self._transports.get(name)

    @property
    def live_transports(self) -> list[str]:
        """Transports that could carry a message right now, best first.

        `file` is listed whenever it is attached: the folder was probed at
        startup and a write into it is as good as this machine can tell. `git`
        has to prove itself instead, because a clone is always writable and
        whether anything leaves the machine is a question only the last push can
        answer -- reporting it live on the strength of a local directory would
        be the same false green light this project has been caught by before.
        """
        live = []
        for name in ("relay", "direct", "file", "git"):
            t = self._transports.get(name)
            if t is None:
                continue
            if name == "file" or getattr(t, "online", False):
                live.append(name)
        return live

    async def stop(self) -> None:
        # A pending git-channel retry must not outlive the room: on shutdown it
        # would otherwise sit out its whole backoff, and after `leave` it would
        # rebuild a transport for a room that is gone.
        task = getattr(self, "_git_retry_task", None)
        self._git_retry_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                # CancelledError is not an Exception, so it has to be named:
                # awaiting a task we just cancelled is expected to raise it.
                # Teardown then runs to completion either way -- a room left
                # half-stopped is worse than a teardown that ignores a late
                # cancel.
                pass
        for transport in self._transports.values():
            try:
                await transport.stop()
            except Exception:
                pass
        self._transports.clear()

    # -- sequencing ---------------------------------------------------------- #

    def next_seq(self) -> int:
        self._seq += 1
        if self._seq > self._seq_ceiling:
            self._seq_ceiling = self._seq + SEQ_BLOCK
            self.record["seq_reserved_to"] = self._seq_ceiling
            self.save_state()
        return self._seq

    # -- sending -------------------------------------------------------------- #

    def build(self, kind: str, body: Any, role: str = ROLE_ORCHESTRATOR,
              agent: str = "main", channel: str | None = None, to: str = "*",
              reply_to: str | None = None, cwd: str | None = None) -> dict[str, Any]:
        return make_envelope(
            kind, self.room_id, self.identity.device_id, self.next_seq(),
            make_origin(self.identity, role=role, agent=agent, cwd=cwd,
                        epoch=self.epoch, name=self.display_name or None),
            body=body, channel=channel, to=to, reply_to=reply_to,
        )

    async def send(self, envelope: dict[str, Any]) -> str:
        """Seal and push over the best transport available.

        Returns the transport used, or "queued" when none could take it. A
        queued message is not lost: it goes out on the next reconnect, which is
        why callers never have to decide whether it is safe to send.
        """
        frame = seal_frame(self.keys, self.identity, envelope)
        return await self._push(frame)

    async def _push(self, frame: dict[str, Any]) -> str:
        recipients = self._recipients()

        # Direct is tried first, but only when it can serve *everyone* we know
        # of. A transport that reaches some members is worse than one that
        # reaches none: the relay and the folder both fan out to the whole room,
        # so a partial direct send would look successful while leaving the
        # members who are not on this LAN with nothing.
        direct = self._transports.get("direct")
        if direct is not None and recipients and direct.covers(recipients):
            if await direct.send(frame, recipients):
                self.sent += 1
                return "direct"

        # Deliberately *not* given `direct`'s coverage test, though the second
        # audit asked for one. The two are not alike: direct is point-to-point,
        # so a member who is not on this LAN can never be reached by it, while
        # the relay is store-and-forward and accepts a frame for a member who is
        # not connected at all, holding it until they are. Requiring the roster
        # to cover the recipients would refuse to send to anybody currently
        # offline and queue locally instead -- swapping the relay's mailbox,
        # which is built for this, for a bounded in-memory outbox that is not.
        # Two e2e tests caught it saying `queued` where `relay` was correct.
        #
        # The worry underneath the finding is real and is a diagnostic, not a
        # routing rule: a relay only this member is configured for looks exactly
        # like a room where everyone else happens to be offline.
        relay = self._transports.get("relay")
        if relay is not None and relay.online and await relay.send(frame):
            self.sent += 1
            return "relay"

        shared = self._transports.get("file")
        if shared is not None and await shared.send(frame, recipients):
            self.sent += 1
            return "file"

        # Tried even when the last push failed, and deliberately so. A write
        # into the clone is durable and goes out on the next round that reaches
        # the remote, whereas the outbox below is in memory and does not survive
        # a restart. Being honest about reachability is `live_transports`' job,
        # not a reason to drop the frame somewhere worse.
        repo = self._transports.get("git")
        if repo is not None and await repo.send(frame, recipients):
            self.sent += 1
            return "git"

        # A bounded outbox has to drop something when it fills, and the oldest
        # is the right choice -- but dropping it silently is how a message goes
        # missing with every status line still green.
        if len(self.outbox) == self.outbox.maxlen:
            self.dropped_overflow += 1
            self.log(f"[{self.name}] outbox full ({self.outbox.maxlen}); "
                     f"dropped the oldest queued frame "
                     f"({self.dropped_overflow} lost so far)")
        self.outbox.append(frame)
        return "queued"

    def _recipients(self) -> list[str]:
        """Everyone we know of except ourselves; the fan-out set for a transport
        that has no server to do it for us.

        Includes every attached transport's own roster, not just `self.members`.
        `members` is fed from git presence by a 2 s poll in the daemon, so a
        member visible in the clone but not yet noted here was invisible to
        `direct.covers()` -- which then reported full coverage, sent over direct
        only, and left that member with nothing, permanently. The file and git
        transports already fan out to `recipients | roster()` for the same
        reason; the coverage test was the half that did not.
        """
        known = {d for d in self.members if d != self.identity.device_id}
        for transport in self._transports.values():
            try:
                known.update(d for d in (transport.roster() or {})
                             if d != self.identity.device_id)
            except Exception:                 # a roster is advisory, never fatal
                continue
        return sorted(known)

    async def flush(self) -> int:
        """Drain the outbox after a transport comes back. Returns how many went."""
        sent = 0
        while self.outbox:
            # Taken off the head *first*. Leaving it there and letting _push
            # append a second copy is what a full deque punishes: at maxlen the
            # append evicts the leftmost element, which is this very frame, and
            # the compensating pop() then removes the copy -- so the message
            # disappears from both ends of the queue. Popping first leaves room
            # for the retry, so nothing is evicted.
            frame = self.outbox.popleft()
            if await self._push(frame) == "queued":
                # _push re-queued it at the tail; move it back to the head so
                # the outbox keeps its order.
                self.outbox.pop()
                self.outbox.appendleft(frame)
                break
            sent += 1
        return sent

    # -- receiving ------------------------------------------------------------ #

    async def on_frame(self, frame: dict[str, Any], transport: str,
                       remote_ip: str | None = None) -> None:
        """Admit one sealed frame, or drop it with a reason in the log."""
        ok, why = validate_frame(frame)
        if not ok:
            self.log(f"[{self.name}] dropped malformed frame via {transport}: {why}")
            return
        if frame["room_id"] != self.room_id:
            self.log(f"[{self.name}] dropped frame for another room")
            return
        if frame["device_id"] == self.identity.device_id:
            return                                   # our own message, echoed back

        try:
            envelope = open_and_verify(self.keys, frame)
        except crypto.CryptoError as exc:
            self.log(f"[{self.name}] rejected frame from "
                     f"{frame.get('device_id')} via {transport}: {exc}")
            return

        msg_id = envelope.get("msg_id")
        if not isinstance(msg_id, str) or not msg_id:
            return
        if msg_id in self._seen:
            # Expected: the same message can legitimately arrive over two
            # transports, or be replayed from the relay's mailbox after a
            # reconnect. Silence is the right response.
            self.dropped_duplicates += 1
            return
        self._remember(msg_id)

        member = self._member(frame["device_id"])
        member.note(transport)
        member.received += 1
        self._update_member(member, envelope)

        self._note_seq(member, int(frame["seq"]),
                       (envelope.get("origin") or {}).get("epoch"))

        await self.deliver(envelope, transport)

    def _note_seq(self, member: Member, seq: int, epoch: str | None) -> None:
        """Advance a member's sequence, counting only what is really missing.

        A gap is a number this sender used and we never saw. A *resume* is a
        number it never used: sequence numbers are handed out from a reserved
        block and a restart continues from the ceiling, so coming back costs up
        to `SEQ_BLOCK` of them. Both look identical from here -- a jump forward
        -- and for a long time both were reported as loss, which is how a
        counter ends up reading `gaps=999` on a room that has lost nothing and
        stops being believed.

        `epoch` is what tells them apart, and a peer on older code sends none.
        Then this behaves exactly as it did before, which is the point: a stale
        counter against an old peer, and no wire break.
        """
        if epoch != member.epoch:
            if member.epoch is not None:
                member.resumes += 1
            member.epoch = epoch
            member.highest_seq = seq
            return

        if member.highest_seq >= 0 and seq > member.highest_seq + 1:
            member.gaps += seq - member.highest_seq - 1
        member.highest_seq = max(member.highest_seq, seq)

    def _remember(self, msg_id: str) -> None:
        self._seen[msg_id] = time.time()
        while len(self._seen) > DEDUPE_MAX:
            self._seen.popitem(last=False)
        # Persisted so a restart does not re-deliver everything the relay still
        # holds. Written in batches; losing the tail costs a duplicate line, not
        # a lost message.
        if len(self._seen) % 25 == 0:
            self.record["seen"] = list(self._seen)[-DEDUPE_MAX:]
            self.save_state()

    def _member(self, device_id: str) -> Member:
        """The one place a device id enters the roster, so the one place to check.

        Every id in `self.members` becomes a directory name -- the fan-out set
        for the folder and git transports is these keys. A frame cannot carry a
        bad one (its device id is checked against the hash of the announced
        key), but the relay's roster is not a frame: it arrives in the clear
        from a party this design treats as hostile, and it lands here.
        """
        if not crypto.is_device_id(device_id):
            raise ValueError(f"not a device id: {device_id!r}")
        member = self.members.get(device_id)
        if member is None:
            member = Member(device_id)
            self.members[device_id] = member
        return member

    def _update_member(self, member: Member, envelope: dict[str, Any]) -> None:
        origin = envelope.get("origin") or {}
        if origin.get("label"):
            member.label = clean_label(origin["label"])
        if origin.get("name"):
            member.name = clean_label(origin["name"], 40)
        if origin.get("agent_kind"):
            member.agent_kind = clean_label(origin["agent_kind"], limit=24)
        if origin.get("public_key"):
            member.public_key = origin["public_key"]

        # A hello may carry where its sender can be dialled. Acting on it is
        # what opens a direct link; the transport is absent unless the user
        # enabled it, so an address from a room that has it switched off is
        # simply ignored.
        direct = self._transports.get("direct")
        body = envelope.get("body")
        if direct is not None and isinstance(body, dict):
            advert = body.get("direct")
            if isinstance(advert, dict) and advert.get("host") and advert.get("port"):
                try:
                    direct.learn(member.device_id, str(advert["host"]), int(advert["port"]))
                except (TypeError, ValueError):
                    pass

    def note_seen(self, device_id: str, transport: str,
                  at: float | None = None) -> Member | None:
        """Record liveness learned from a transport rather than from a message.

        Pass `at` when the transport can date its evidence; see `Member.note`.

        Returns None for an id that is not one, which is the relay-roster case:
        nothing signed it, so it is refused rather than trusted.
        """
        try:
            member = self._member(device_id)
        except ValueError:
            self.log(f"[{self.name}] ignored a bogus device id from {transport}: "
                     f"{str(device_id)[:64]!r}")
            return None
        member.note(transport, at)
        return member

    def forget(self, device_id: str) -> None:
        self.members.pop(device_id, None)

    # -- announcements --------------------------------------------------------- #

    async def announce(self) -> str:
        """Introduce ourselves so the others can show a name instead of an id."""
        who = self.display_name or self.identity.label
        body = {
            "text": f"{who} joined ({self.identity.agent_kind})",
            "label": self.identity.label,
            "name": self.display_name or None,
            "agent_kind": self.identity.agent_kind,
            "fingerprint": self.identity.fingerprint(),
        }
        # Where we can be dialled, if direct links are enabled. This rides
        # inside the sealed envelope, so whatever carries the hello -- a relay,
        # a synced folder -- never learns anyone's address; only members do.
        direct = self._transports.get("direct")
        if direct is not None:
            advert = direct.advertisement()
            if advert:
                body["direct"] = advert
        return await self.send(self.build(K_HELLO, body))

    def local_event(self, kind: str, text: str, **extra: Any) -> dict[str, Any]:
        """A notice this machine generated: presence, a missed-mail warning.

        Shaped exactly like a received envelope so the inbox, the logger and the
        renderers need no special case for it.
        """
        envelope = make_envelope(
            kind, self.room_id, self.identity.device_id, 0,
            make_origin(self.identity, agent="link"),
            body={"text": text, **extra},
        )
        envelope["_verified"] = True
        envelope["_local"] = True
        return envelope

    # -- introspection ---------------------------------------------------------- #

    def status(self, verbose: bool = False) -> dict[str, Any]:
        members = [m.status() for m in self.members.values()]
        online = [m for m in members if m["online"]]
        transports = self.live_transports
        last = max((m["last_seen_s"] or 1e9) for m in members) if members else None
        status = {
            "room": self.name,
            "room_id": self.room_id,
            "transport": transports[0] if transports else "offline",
            "members": len(members),
            "online": len(online),
            "queued": len(self.outbox),
            "sent": self.sent,
            "quiet_for_s": None if last is None or last >= 1e9 else last,
            "setup_error": self.setup_error,
            "knocks": [
                {"device_id": d, "name": k.get("name"), "ts": k.get("ts")}
                for d, k in self.pending_knocks.items()
            ] or None,
        }
        if self.gov_state.order:
            status["admins"] = sorted(self.gov_state.admins)
        if verbose:
            status.update({
                # Deliberately not the invite. It is the room's long-lived
                # master secret, and `link_status(verbose=true)` writes its
                # result into a model's context, the session transcript on disk
                # and anything downstream of those. `link_join` returns it at
                # the one moment a human needs it, and `link.cli invite` prints
                # it on request; a diagnostic should not hand it out as a side
                # effect. Storing it plaintext in a 0600 state.json was the
                # accepted tradeoff -- echoing it was not part of that bargain.
                "joined_at": self.joined_at,
                "live_transports": transports,
                "duplicates_dropped": self.dropped_duplicates,
                "member_detail": members,
                "relay": self._transports["relay"].stats() if "relay" in self._transports else None,
                "direct": self._transports["direct"].stats() if "direct" in self._transports else None,
                "file": self._transports["file"].stats() if "file" in self._transports else None,
                "git": self._transports["git"].stats() if "git" in self._transports else None,
            })
        return status
