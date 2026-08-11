"""Room governance: who is an admin, and removing a member.

Three signed record kinds live in <room_root>/gov/ beside the door files:
genesis (the creating device is the first admin), role (an admin grants or
revokes admin), removal (an admin removes a member and rekeys the room to a
successor sealed per remaining member). Records order themselves: `prev`
names the predecessor's hash, siblings order by ascending record hash, so
the total order is a pure function of the record set -- git commit order is
rebased on sync and cannot be trusted for this.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

from .crypto import (
    CryptoError,
    b64,
    canonical_json,
    is_device_id,
)
from .door import _verify_signed, open_box, seal_box

GOV_CONTEXT = b"claude-link/v2/gov\x00"
KINDS = ("genesis", "role", "removal")
ROLES = ("admin", "member")
MAX_GOV_FILE_BYTES = 256 * 1024


def record_hash(rec: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(rec)).hexdigest()[:32]


def _base(identity, room_id: str, kind: str, prev: str) -> dict[str, Any]:
    return {
        "v": 1,
        "room_id": room_id,
        "kind": kind,
        "device_id": identity.device_id,
        "public_key": identity.public_key,
        "prev": prev,
        "ts": int(time.time()),
    }


def _sign(identity, rec: dict[str, Any]) -> dict[str, Any]:
    payload = GOV_CONTEXT + canonical_json(rec)
    rec["sig"] = b64(identity.sign_key().sign(payload))
    return rec


def build_genesis(identity, room_id: str) -> dict[str, Any]:
    return _sign(identity, _base(identity, room_id, "genesis", prev=""))


def build_role(identity, room_id: str, prev: str, target: str,
               role: str) -> dict[str, Any]:
    if role not in ROLES:
        raise CryptoError(f"not a role: {role!r}")
    if not is_device_id(target):
        raise CryptoError(f"not a device id: {target!r}")
    rec = _base(identity, room_id, "role", prev)
    rec["target"] = target
    rec["role"] = role
    return _sign(identity, rec)


def verify_record(rec: Any, room_id: str) -> str:
    """Shape + signature. Returns the signer's device id, raises CryptoError."""
    kind = rec.get("kind") if isinstance(rec, dict) else None
    blob = _verify_signed(rec, room_id, GOV_CONTEXT, f"gov {kind}")
    if blob.get("kind") not in KINDS:
        raise CryptoError(f"unknown gov kind: {blob.get('kind')!r}")
    if blob["kind"] == "genesis" and blob["prev"] != "":
        raise CryptoError("genesis must not have a predecessor")
    if blob["kind"] != "genesis" and not blob.get("prev"):
        raise CryptoError(f"{blob['kind']} record without a predecessor")
    if blob["kind"] == "role":
        if not is_device_id(blob.get("target")):
            raise CryptoError("role record without a target device")
        if blob.get("role") not in ROLES:
            raise CryptoError(f"not a role: {blob.get('role')!r}")
    if blob["kind"] == "removal":
        if not is_device_id(blob.get("target")):
            raise CryptoError("removal record without a target device")
        successor = blob.get("successor")
        if not isinstance(successor, dict) or not successor.get("boxes"):
            raise CryptoError("removal record without successor boxes")
    return blob["device_id"]


REKEY_PURPOSE = "gov-rekey"
NOTICE_PURPOSE = "gov-removed-notice"


def build_removal(identity, room_id: str, prev: str, target: str,
                  successor_name: str, successor_secret: str,
                  admins: list[str], box_keys: dict[str, str],
                  removed_by_name: str) -> dict[str, Any]:
    """box_keys: device_id -> door box key (from door entries), for every
    current member INCLUDING the target. The target's key seals only the
    notice; everyone else's seals the successor secret."""
    if not is_device_id(target):
        raise CryptoError(f"not a device id: {target!r}")
    payload = {"name": successor_name, "secret": successor_secret}
    boxes = {
        dev: seal_box(key, payload, room_id, REKEY_PURPOSE)
        for dev, key in box_keys.items() if dev != target
    }
    if not boxes:
        raise CryptoError("a removal that leaves nobody is a leave, not a removal")
    rec = _base(identity, room_id, "removal", prev)
    rec["target"] = target
    rec["successor"] = {
        "boxes": boxes,
        "admins": sorted(set(admins) - {target}),
    }
    if target in box_keys:
        notice = {"text": f"You were removed from this room by {removed_by_name}."}
        rec["successor"]["notice_box"] = seal_box(
            box_keys[target], notice, room_id, NOTICE_PURPOSE)
    return _sign(identity, rec)


def open_rekey_box(identity, rec: dict[str, Any]) -> dict[str, Any]:
    box = (rec.get("successor") or {}).get("boxes", {}).get(identity.device_id)
    if box is None:
        raise CryptoError("no rekey box for this device")
    return open_box(identity, box, rec["room_id"], REKEY_PURPOSE)


def open_notice_box(identity, rec: dict[str, Any]) -> dict[str, Any]:
    box = (rec.get("successor") or {}).get("notice_box")
    if box is None:
        raise CryptoError("no notice box on this removal")
    return open_box(identity, box, rec["room_id"], NOTICE_PURPOSE)


@dataclass
class GovState:
    admins: set[str] = field(default_factory=set)
    removed: set[str] = field(default_factory=set)
    order: list[str] = field(default_factory=list)   # record hashes, applied order
    void: list[str] = field(default_factory=list)    # record hashes, refused
    head: str = ""                                   # hash of last applied record
    removal: dict[str, Any] | None = None            # the winning removal, if any


def total_order(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic total order over any subset of a room's records.

    Depth-first from genesis; among siblings, ascending record hash. A pure
    function of the set: every member computes the same order however the
    carrier delivered the files. Records whose predecessor is absent are
    orphans and sort after everything reachable, by hash -- they become
    evaluable when the gap fills in.
    """
    by_hash = {record_hash(r): r for r in records}
    children: dict[str, list[str]] = {}
    for h, r in by_hash.items():
        children.setdefault(r["prev"], []).append(h)
    for sibs in children.values():
        sibs.sort()
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    stack = list(reversed(children.get("", [])))
    while stack:
        h = stack.pop()
        if h in seen:
            continue
        seen.add(h)
        ordered.append(by_hash[h])
        stack.extend(reversed(children.get(h, [])))
    orphans = sorted(set(by_hash) - seen)
    ordered.extend(by_hash[h] for h in orphans)
    return ordered


def evaluate(records: list[dict[str, Any]], room_id: str) -> GovState:
    """Verify, order, and apply every record. Invalid or unauthorized
    records land in .void and change nothing. After a winning removal the
    room is closed: every later record is void."""
    verified = []
    for rec in records:
        try:
            verify_record(rec, room_id)
        except CryptoError:
            continue
        verified.append(rec)
    state = GovState()
    for rec in total_order(verified):
        h = record_hash(rec)
        signer = rec["device_id"]
        if state.removal is not None:
            state.void.append(h)
            continue
        if rec["kind"] == "genesis":
            if state.order:
                state.void.append(h)      # a second genesis is a forgery
                continue
            state.admins.add(signer)
        elif signer not in state.admins or signer in state.removed:
            state.void.append(h)
            continue
        elif rec["kind"] == "role":
            if rec["role"] == "admin":
                state.admins.add(rec["target"])
            else:
                state.admins.discard(rec["target"])
        elif rec["kind"] == "removal":
            state.removed.add(rec["target"])
            state.admins.discard(rec["target"])
            state.removal = rec
        state.order.append(h)
        state.head = h
    return state


# --------------------------------------------------------------------------- #
# carrier files
# --------------------------------------------------------------------------- #

_HASH_RE = re.compile(r"\A[0-9a-f]{32}\Z")


def _gov_dir(shared_dir: str, room_id: str) -> str:
    # Restates transport_file.room_root's layout, like door._room_root does,
    # so gov.py never imports a transport.
    return os.path.join(shared_dir, "claude-link", room_id, "gov")


def write_gov_record(shared_dir: str, room_id: str,
                     rec: dict[str, Any]) -> bool:
    """Blocking; callers hand it to transport.publish_files(). Returns False
    when the record is already there byte-identically (no git commit)."""
    path = os.path.join(_gov_dir(shared_dir, room_id),
                        record_hash(rec) + ".json")
    data = canonical_json(rec)
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return False
    except OSError:
        pass
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows: a virus scanner holds the just-created file for a
                # moment and os.replace loses. Seen for real (one run in
                # eight on this machine); a short retry outlasts it.
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def read_gov_records(shared_dir: str, room_id: str) -> list[dict[str, Any]]:
    """Every parseable, verified record whose filename matches its hash.
    Blocking; call off the loop."""
    d = _gov_dir(shared_dir, room_id)
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    out = []
    for name in names:
        stem, ext = os.path.splitext(name)
        if ext != ".json" or not _HASH_RE.match(stem):
            continue
        path = os.path.join(d, name)
        try:
            if os.path.getsize(path) > MAX_GOV_FILE_BYTES:
                continue
            with open(path, "rb") as f:
                rec = json.loads(f.read().decode("utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or record_hash(rec) != stem:
            continue
        try:
            verify_record(rec, room_id)
        except CryptoError:
            continue
        out.append(rec)
    return out
