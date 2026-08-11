"""Wire format: the JSON object every transport carries, sealed and in clear.

Two layers travel together:

  * the **frame** -- `{room_id, device_id, seq, kind, nonce, ct}` -- which the
    relay reads to route and deduplicate, and nothing more;
  * the **envelope** inside the ciphertext, which carries the message itself
    and repeats every routing field so a receiver can catch a relay that lied.

Both transports move exactly this shape, so routing and logging never need to
know which one delivered a message.
"""

from __future__ import annotations

import os
import re
import secrets
import socket
from typing import Any

from . import PROTOCOL_VERSION
from .crypto import (
    CryptoError,
    b32,
    binding_for,
    canonical_json,
    is_device_id,
    is_room_id,
    key_matches_device,
    load_public,
    seal,
    unb64,
    unseal,
    verify_signature,
)
from .crypto import NONCE_LEN
from .util import now_iso

# --- message kinds ----------------------------------------------------------
# control plane
K_HELLO = "hello"                  # announces a device key into a room
K_PING = "ping"
K_PONG = "pong"
K_PRESENCE = "presence"            # a member joined or left
K_SYSTEM = "system"                # locally generated notice for the operator
# data plane
K_MSG = "msg"
K_CHANNEL_OPEN = "channel_open"
K_CHANNEL_CLOSE = "channel_close"

# Local-only: a knock arrives as a *file* on the carrier, never as a frame, so
# this kind is deliberately NOT in VALID_KINDS — nothing on the wire may
# carry it. It exists so the inbox, the hook and the transcripts can treat
# "someone is at the door" as a first-class event.
K_KNOCK = "knock"

# Exactly the keys a frame has on the wire. The relay rebuilds an incoming frame
# from this list rather than forwarding the client's object, so a member cannot
# smuggle in a field the relay itself uses for routing.
FRAME_FIELDS = ("room_id", "device_id", "seq", "kind", "nonce", "ct")

# One number, stated once, for how large a message may be. The relay already
# refused anything over 256 KiB; the folder and git transports refused nothing,
# so a hostile carrier could hand a member a multi-gigabyte file and have
# json.load raise MemoryError inside the poll loop. base64 costs a third, and
# the envelope adds a signature and headers on top of the caller's text.
MAX_MESSAGE_BYTES = 256 * 1024
MAX_CT_B64 = (MAX_MESSAGE_BYTES * 4) // 3 + 4096
# What a transport may read off the carrier before deciding it is not a frame.
MAX_FRAME_BYTES = MAX_CT_B64 + 8192

# Channel ids are minted by `util.new_id`, and they become directory names under
# `.conv/`. A peer chooses the one in an envelope, so it gets the same treatment
# as a device id.
CHANNEL_ID_RE = re.compile(r"\Achan_[0-9a-f]{16}\Z")


def is_channel_id(value: Any) -> bool:
    return isinstance(value, str) and CHANNEL_ID_RE.match(value) is not None


CONTROL_KINDS = {K_HELLO, K_PING, K_PONG, K_PRESENCE, K_SYSTEM}
DATA_KINDS = {K_MSG, K_CHANNEL_OPEN, K_CHANNEL_CLOSE}
VALID_KINDS = CONTROL_KINDS | DATA_KINDS

ROLE_ORCHESTRATOR = "orchestrator"
ROLE_SUBAGENT = "subagent"
VALID_ROLES = {ROLE_ORCHESTRATOR, ROLE_SUBAGENT}


def new_msg_id() -> str:
    """16 random bytes. Minted by the sender, carried inside the ciphertext.

    Dedupe keys on this and only this, so it must be unguessable and unique
    without coordination -- never a counter.
    """
    return "msg_" + b32(secrets.token_bytes(16))


def make_origin(identity, role: str = ROLE_ORCHESTRATOR, agent: str = "main",
                cwd: str | None = None, epoch: str | None = None,
                name: str | None = None) -> dict[str, Any]:
    """Identity block stamped on every outgoing envelope.

    The sender's public key rides along on every message, not just on a join.
    Forty-four bytes buys statelessness: a receiver can verify any frame the
    moment it arrives, with no key-exchange step to get wrong and no pinning
    store to keep in sync. It is safe because `device_id` *is* a hash of this
    key, so a liar can only present a key matching the id it is claiming.

    `epoch` names this run of this room. Sequence numbers come from a reserved
    block and a restart resumes at the ceiling, so a sender's numbering jumps by
    up to `SEQ_BLOCK` every time it comes back. Without a way to tell that from
    a hole in the stream, a receiver counts it as a thousand lost messages. It
    is omitted rather than empty when unknown, and its absence is what a peer on
    older code looks like, so `PROTOCOL_VERSION` is deliberately not bumped:
    that would reject every one of their frames outright, which is a far worse
    outcome than a stale counter.
    """
    origin = {
        "device": identity.device_id,
        "public_key": identity.public_key,
        "label": identity.label,
        "agent_kind": identity.agent_kind,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "role": role if role in VALID_ROLES else ROLE_ORCHESTRATOR,
        "agent": agent or "main",
        "cwd": cwd or os.getcwd(),
    }
    if epoch:
        origin["epoch"] = epoch
    # The human's chosen name, distinct from `label` (user@host): the label
    # disambiguates devices, the name is what a person answers to. Omitted
    # rather than empty when nobody has chosen one.
    if name:
        origin["name"] = name
    return origin


def make_envelope(kind: str, room_id: str, device_id: str, seq: int,
                  origin: dict[str, Any], body: Any = None,
                  channel: str | None = None, to: str = "*",
                  reply_to: str | None = None) -> dict[str, Any]:
    """Build an envelope. Every routing field appears here *and* in the frame.

    The duplication is deliberate: the receiver rejects any frame whose header
    disagrees with the signed body, which is what stops a relay relabelling a
    message it cannot read.
    """
    return {
        "v": PROTOCOL_VERSION,
        "msg_id": new_msg_id(),
        "room_id": room_id,
        "device_id": device_id,
        "seq": int(seq),
        "kind": kind,
        "ts": now_iso(),
        "channel": channel,
        "to": to or "*",
        "reply_to": reply_to,
        "origin": origin,
        "body": body if body is not None else {},
    }


def seal_frame(keys, identity, envelope: dict[str, Any]) -> dict[str, Any]:
    """Envelope -> the frame that goes on the wire."""
    nonce, ct = seal(
        keys.room_key, identity.sign_key(), envelope,
        envelope["room_id"], envelope["device_id"], envelope["seq"], envelope["kind"],
    )
    return {
        "room_id": envelope["room_id"],
        "device_id": envelope["device_id"],
        "seq": envelope["seq"],
        "kind": envelope["kind"],
        "nonce": nonce,
        "ct": ct,
    }


def open_frame(keys, frame: dict[str, Any], verify_key=None
               ) -> tuple[dict[str, Any], bytes, bytes]:
    """Frame -> (envelope, signature, plaintext). Raises CryptoError on anything suspect."""
    return unseal(
        keys.room_key, frame["nonce"], frame["ct"], frame["room_id"],
        frame["device_id"], frame["seq"], frame["kind"], verify_key,
    )


def open_and_verify(keys, frame: dict[str, Any]) -> dict[str, Any]:
    """Decrypt a frame and prove who sent it, in one step.

    Order matters and is fixed: decrypt, take the sender's key out of the
    envelope, check that key hashes to the device id the frame claims, and only
    then verify the signature. Checking the key against the id first is what
    makes carrying the key in-band safe -- otherwise anyone could sign as
    themselves while wearing someone else's device id.
    """
    # Before anything else: this frame has to belong to the room whose key
    # just opened it. Both the AEAD binding and the signed bytes are built from
    # the header the *sender* supplied, so neither binds the frame to `keys` on
    # its own -- leave room A's header in place, re-encrypt the identical signed
    # bytes under room B's key, and everything below here passes. `Room.on_frame`
    # happens to drop that first, but the claim is made about this function.
    if frame.get("room_id") != keys.room_id:
        raise CryptoError("frame is for another room")

    envelope, signature, plaintext = open_frame(keys, frame)

    # Underscore keys are this process's own annotations -- `_verified` says the
    # signature checked out, `_local` says we wrote it. Both are read after
    # decryption, and a member can put either in an envelope they seal legally.
    # `_local` in particular decides which direction a message is rendered in
    # `.conv/transcript.md`, so leaving it in lets a peer file their own message
    # as one this machine sent. They are ours; strip them on the way in.
    for key in [k for k in envelope if isinstance(k, str) and k.startswith("_")]:
        del envelope[key]

    origin = envelope.get("origin")
    if not isinstance(origin, dict):
        raise CryptoError("envelope has no origin block")
    public_key = origin.get("public_key")
    if not isinstance(public_key, str) or not public_key:
        raise CryptoError("envelope carries no sender public key")
    if not key_matches_device(public_key, frame["device_id"]):
        raise CryptoError("sender key does not match the device id it claims")

    verify_signature(
        load_public(public_key), signature, plaintext, frame["room_id"],
        frame["device_id"], frame["seq"], frame["kind"],
    )
    envelope["_verified"] = True

    ok, why = validate_envelope(envelope)
    if not ok:
        raise CryptoError(f"envelope rejected: {why}")
    return envelope


def validate_frame(frame: Any) -> tuple[bool, str]:
    """Cheap structural check on anything arriving from the network.

    Runs before any crypto, so a malformed frame costs a dictionary lookup
    rather than a decryption attempt.
    """
    if not isinstance(frame, dict):
        return False, "frame is not an object"
    for field in ("room_id", "device_id", "seq", "kind", "nonce", "ct"):
        if field not in frame:
            return False, f"missing field: {field}"
    # Shape, not just type. Both ids become path segments downstream, and an
    # absolute or traversing one escapes the share entirely -- see the note in
    # crypto.py. Checking here means nothing malformed reaches the roster, the
    # transports or `.conv/`.
    if not is_room_id(frame["room_id"]):
        return False, "room_id is not a room id"
    if not is_device_id(frame["device_id"]):
        return False, "device_id is not a device id"
    if not isinstance(frame["kind"], str) or frame["kind"] not in VALID_KINDS:
        return False, f"unknown kind: {frame.get('kind')!r}"
    # The upper bound is not pedantry. `binding_for` packs seq with `!Q`, and a
    # bigger integer raises struct.error -- which is not a CryptoError, so it
    # escapes the receive path and takes the rest of the batch with it. bool is
    # an int subclass and is not a sequence number.
    if not isinstance(frame["seq"], int) or isinstance(frame["seq"], bool):
        return False, "seq must be an integer"
    if not 0 <= frame["seq"] < 2 ** 64:
        return False, "seq out of range"
    if not isinstance(frame["nonce"], str) or not isinstance(frame["ct"], str):
        return False, "nonce and ct must be strings"
    # Same reasoning: AESGCM raises a plain ValueError for a nonce outside
    # 8..128 bytes, before the key is ever consulted. Decide here instead.
    try:
        if len(unb64(frame["nonce"])) != NONCE_LEN:
            return False, "nonce is not the right length"
    except CryptoError:
        return False, "nonce is not valid base64"
    if len(frame["ct"]) > MAX_CT_B64:
        return False, "ciphertext is larger than this protocol allows"
    return True, ""


def validate_envelope(env: Any) -> tuple[bool, str]:
    """Structural check on a decrypted envelope, after the signature verified."""
    if not isinstance(env, dict):
        return False, "envelope is not an object"
    for field in ("v", "msg_id", "room_id", "device_id", "seq", "kind", "ts", "origin"):
        if field not in env:
            return False, f"missing field: {field}"
    if env["v"] != PROTOCOL_VERSION:
        return False, (
            f"protocol version {env['v']} != {PROTOCOL_VERSION}; "
            "the other side is running a different agent-link"
        )
    # `ts` is peer-chosen and reaches a human and a model: `link_read` renders
    # it into the header line that sits *above* the untrusted-text fence, which
    # is the one part of that output the provenance sentence vouches for. A `ts`
    # carrying newlines can therefore write lines that read as not-peer-written.
    # Checked here so it holds for every consumer rather than one of them.
    if not isinstance(env["ts"], str) or len(env["ts"]) > 64:
        return False, "ts must be a short string"
    if any(c in env["ts"] for c in "\r\n"):
        return False, "ts must be one line"
    if not isinstance(env["origin"], dict) or "device" not in env["origin"]:
        return False, "origin block malformed"
    if env["origin"]["device"] != env["device_id"]:
        return False, "origin device disagrees with the envelope"
    # `channel` names a directory under `.conv/` on every member's machine, and
    # the peer chooses it. Unchecked, "/etc/cron.d" or "../.." writes outside
    # the sink -- os.path.join takes an absolute component as the whole path.
    if env.get("channel") is not None and not is_channel_id(env["channel"]):
        return False, "channel is not a channel id"
    if not isinstance(env.get("to", "*"), str):
        return False, "to must be a string"
    return True, ""


def summarize(env: dict[str, Any], limit: int = 140) -> str:
    """One-line human rendering, used in logs and status output."""
    body = env.get("body")
    text = body.get("text") or body.get("reason") or "" if isinstance(body, dict) else str(body or "")
    text = " ".join(str(text).split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    origin = env.get("origin", {})
    who = f"{origin.get('label', '?')}/{origin.get('agent', '?')}"
    return f"[{env.get('kind')}] {who}: {text}" if text else f"[{env.get('kind')}] {who}"


__all__ = [
    "K_HELLO", "K_PING", "K_PONG", "K_PRESENCE", "K_SYSTEM", "K_MSG",
    "K_CHANNEL_OPEN", "K_CHANNEL_CLOSE", "CONTROL_KINDS", "DATA_KINDS",
    "VALID_KINDS", "ROLE_ORCHESTRATOR", "ROLE_SUBAGENT", "VALID_ROLES",
    "new_msg_id", "make_origin", "make_envelope", "seal_frame", "open_frame",
    "open_and_verify", "validate_frame", "validate_envelope", "summarize",
    "binding_for", "canonical_json", "CryptoError",
]
