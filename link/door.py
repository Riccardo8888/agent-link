"""The door: knocks and grants, so a shareable code stops being the key.

A door code (`team-x#DOOR-<ROOMID>`) carries no secret. A joiner writes a
sealed *knock* at the room's door on the carrier; any member opens it, asks
their human, and answers with a *grant*: the room secret sealed to the key
inside the signed knock. Someone who can write to the carrier can deny
service or learn a knocker's chosen name; they can never reach room content,
and a forged grant fails closed because the secret it carries must derive to
the very room id the joiner knocked at.

Everything here is a *file* on the carrier, never a frame: old peers ignore
these paths entirely, so there is no protocol-version question to answer.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .crypto import (
    CryptoError,
    NONCE_LEN,
    _hkdf,
    b64,
    canonical_json,
    derive_room,
    is_device_id,
    is_room_id,
    key_matches_device,
    load_public,
    normalize_name,
    unb64,
)
from .util import atomic_write_text, now_iso

DOOR_KEY_LABEL = b"claude-link/v2/door-x25519"
DOOR_CONTEXT = b"claude-link/v2/door\x00"
KNOCK_CONTEXT = b"claude-link/v2/knock\x00"
GRANT_CONTEXT = b"claude-link/v2/grant\x00"
BOX_INFO = b"claude-link/v2/doorbox|"

KNOCK_TTL_S = 7 * 24 * 3600.0        # matches the transports' retention
MAX_DOOR_FILE_BYTES = 64 * 1024      # a door file is a few hundred bytes; cap reads

_RAW = dict(encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw)


def _door_secret(identity) -> X25519PrivateKey:
    """This device's X25519 door key, derived from its Ed25519 signing key.

    The Ed25519 raw private bytes are a uniformly random 32-byte seed, so
    HKDF with a distinct label yields an independent key. Derived on demand:
    nothing new lands in identity.json and nothing has to migrate.
    """
    raw = identity.sign_key().private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return X25519PrivateKey.from_private_bytes(_hkdf(raw, DOOR_KEY_LABEL))


def door_box_key_b64(identity) -> str:
    return b64(_door_secret(identity).public_key().public_bytes(**_RAW))


def _load_box_key(raw_b64: str) -> X25519PublicKey:
    try:
        return X25519PublicKey.from_public_bytes(unb64(raw_b64))
    except (ValueError, CryptoError) as exc:
        raise CryptoError(f"malformed door key: {exc}") from exc


def _box_key_for(shared: bytes, room_id: str, purpose: str) -> bytes:
    return _hkdf(shared, BOX_INFO + purpose.encode("utf-8") + b"|"
                 + room_id.encode("utf-8"))


def seal_box(recipient_b64: str, payload: dict[str, Any], room_id: str,
             purpose: str) -> dict[str, str]:
    """Seal `payload` so only the holder of `recipient_b64`'s key opens it.

    Ephemeral X25519 ECDH, HKDF bound to the room and the purpose, AES-GCM.
    Binding room and purpose into the key means a box lifted out of a knock
    cannot be replayed as a grant, or into another room.
    """
    ephemeral = X25519PrivateKey.generate()
    shared = ephemeral.exchange(_load_box_key(recipient_b64))
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(_box_key_for(shared, room_id, purpose)).encrypt(
        nonce, canonical_json(payload), None)
    return {
        "epk": b64(ephemeral.public_key().public_bytes(**_RAW)),
        "nonce": b64(nonce),
        "ct": b64(ct),
    }


def open_box(identity, box: Any, room_id: str, purpose: str) -> dict[str, Any]:
    """Open a sealed box addressed to this device. CryptoError on anything."""
    if not isinstance(box, dict):
        raise CryptoError("box is not an object")
    try:
        epk = X25519PublicKey.from_public_bytes(unb64(box.get("epk") or ""))
        shared = _door_secret(identity).exchange(epk)
        plaintext = AESGCM(_box_key_for(shared, room_id, purpose)).decrypt(
            unb64(box.get("nonce") or ""), unb64(box.get("ct") or ""), None)
    except CryptoError:
        raise
    except Exception as exc:   # InvalidTag, ValueError, TypeError
        raise CryptoError(f"could not open box: {type(exc).__name__}") from exc
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CryptoError(f"box payload is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CryptoError("box payload is not an object")
    return payload


# --------------------------------------------------------------------------- #
# door entries
# --------------------------------------------------------------------------- #


def door_entry(identity, room_id: str) -> dict[str, Any]:
    """This device's public door key, signed. Deterministic on purpose:
    a byte-identical file is a file the git channel never re-commits."""
    entry = {
        "v": 1,
        "room_id": room_id,
        "device_id": identity.device_id,
        "public_key": identity.public_key,
        "box_key": door_box_key_b64(identity),
    }
    entry["sig"] = b64(identity.sign_key().sign(
        DOOR_CONTEXT + canonical_json(entry)))
    return entry


def _verify_signed(blob: Any, room_id: str, context: bytes, what: str) -> dict:
    """Shared shape + signature check for door entries, knocks and grants."""
    if not isinstance(blob, dict):
        raise CryptoError(f"{what} is not an object")
    if blob.get("room_id") != room_id:
        raise CryptoError(f"{what} is for another room")
    if not is_device_id(blob.get("device_id")):
        raise CryptoError(f"{what} carries no device id")
    public_key = blob.get("public_key")
    if not isinstance(public_key, str) or not key_matches_device(
            public_key, blob["device_id"]):
        raise CryptoError(f"{what} key does not match the device id it claims")
    signed = {k: v for k, v in blob.items() if k != "sig"}
    try:
        load_public(public_key).verify(unb64(blob.get("sig") or ""),
                                       context + canonical_json(signed))
    except CryptoError:
        raise
    except Exception as exc:
        raise CryptoError(f"{what} signature does not verify") from exc
    return blob


def verify_door_entry(entry: Any, room_id: str) -> str:
    """Returns the entry's box key once everything about it checks out."""
    _verify_signed(entry, room_id, DOOR_CONTEXT, "door entry")
    box_key = entry.get("box_key")
    if not isinstance(box_key, str) or not box_key:
        raise CryptoError("door entry carries no box key")
    _load_box_key(box_key)                     # shape check, fail here not later
    return box_key


# --------------------------------------------------------------------------- #
# door codes
# --------------------------------------------------------------------------- #

_DOOR_RE = re.compile(r"\ADOOR-([A-Za-z2-7]{26})\Z")


def door_code(room_name: str, room_id: str) -> str:
    """`team-x#DOOR-<26 chars>`: the shareable, secretless half of an invite."""
    return f"{normalize_name(room_name)}#DOOR-{room_id[len('room_'):].upper()}"


def parse_door_code(invite: str) -> tuple[str, str] | None:
    """(room_name, room_id) for a door code; None for anything that is not one.

    A string that *starts* like a door code but is mangled raises instead of
    returning None: falling through would hand `DOOR-...` to `derive_room` as
    a secret, which mints a valid, empty, different room — the exact typo trap
    door codes exist to close.
    """
    text = (invite or "").strip()
    if "#" not in text:
        return None
    name, _, rest = text.partition("#")
    rest = rest.strip()
    if not rest.upper().startswith("DOOR-"):
        return None
    match = _DOOR_RE.match(rest.upper())
    if not match:
        raise CryptoError(
            "that looks like a door code but is damaged; check it "
            "character for character against the one you were given")
    room_id = "room_" + match.group(1).lower()
    if not is_room_id(room_id):
        raise CryptoError("door code does not name a valid room id")
    return normalize_name(name), room_id


# --------------------------------------------------------------------------- #
# knocks and grants
# --------------------------------------------------------------------------- #


def build_knock(identity, room_id: str, name: str,
                door_entries: list[Any]) -> dict[str, Any]:
    """A signed knock, sealed to every verifiable door key at this room."""
    payload = {"name": str(name or "")[:60]}
    boxes: dict[str, dict[str, str]] = {}
    for entry in door_entries:
        try:
            box_key = verify_door_entry(entry, room_id)
        except CryptoError:
            continue                       # one bad entry must not stop a knock
        boxes[entry["device_id"]] = seal_box(box_key, payload, room_id, "knock")
    if not boxes:
        raise CryptoError("no usable door keys at this room")
    knock = {
        "v": 1,
        "room_id": room_id,
        "device_id": identity.device_id,
        "public_key": identity.public_key,
        "box_key": door_box_key_b64(identity),
        "ts": now_iso(),
        "boxes": boxes,
    }
    knock["sig"] = b64(identity.sign_key().sign(
        KNOCK_CONTEXT + canonical_json(knock)))
    return knock


def read_knock(identity, room_id: str, knock: Any) -> dict[str, Any]:
    """Member side: who is knocking, and the key a grant must seal to.

    `box_key` comes out of the *signed* knock, which is what makes the grant
    immune to the knock file being overwritten after the human said yes.
    """
    _verify_signed(knock, room_id, KNOCK_CONTEXT, "knock")
    boxes = knock.get("boxes")
    if not isinstance(boxes, dict):
        raise CryptoError("knock carries no boxes")
    box = boxes.get(identity.device_id)
    if box is None:
        raise CryptoError("knock is not sealed to this device")
    payload = open_box(identity, box, room_id, "knock")
    box_key = knock.get("box_key")
    if not isinstance(box_key, str) or not box_key:
        raise CryptoError("knock carries no reply key")
    _load_box_key(box_key)
    return {
        "device_id": knock["device_id"],
        "name": str(payload.get("name") or "")[:60],
        "box_key": box_key,
        "ts": str(knock.get("ts") or ""),
    }


def build_grant(identity, room_id: str, for_device: str, box_key: str,
                room_name: str | None = None, secret: str | None = None,
                denied: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = ({"denied": True} if denied
                               else {"name": room_name, "secret": secret})
    grant = {
        "v": 1,
        "room_id": room_id,
        "for": for_device,
        "device_id": identity.device_id,
        "public_key": identity.public_key,
        "ts": now_iso(),
        "box": seal_box(box_key, payload, room_id, "grant"),
    }
    grant["sig"] = b64(identity.sign_key().sign(
        GRANT_CONTEXT + canonical_json(grant)))
    return grant


def read_grant(identity, room_id: str, grant: Any) -> dict[str, Any]:
    """Joiner side. The caller must still run `grant_matches_room` before
    believing the secret — that check needs ~0.5 s of scrypt and belongs off
    the event loop, so it is separate on purpose."""
    _verify_signed(grant, room_id, GRANT_CONTEXT, "grant")
    if grant.get("for") != identity.device_id:
        raise CryptoError("grant is for another device")
    payload = open_box(identity, grant["box"], room_id, "grant")
    if payload.get("denied"):
        return {"denied": True, "granter": grant["device_id"]}
    name, secret = payload.get("name"), payload.get("secret")
    if not isinstance(name, str) or not isinstance(secret, str) or not secret:
        raise CryptoError("grant carries no usable secret")
    return {"name": name, "secret": secret, "granter": grant["device_id"]}


def grant_matches_room(room_name: str, secret: str, room_id: str) -> bool:
    """The check that closes the forged-grant hole: the secret must derive to
    the very room id the joiner knocked at. Blocking (~0.5 s)."""
    try:
        return derive_room(room_name, secret).room_id == room_id
    except CryptoError:
        return False


# --------------------------------------------------------------------------- #
# files on the carrier
# --------------------------------------------------------------------------- #
# Layout, beside `out/` and `presence/` under the room root:
#
#     door/<device_id>.json     one per member, cleartext, signed, no names
#     knock/<device_id>.json    one per joiner, name sealed to the door keys
#     grant/<device_id>.json    the answer, sealed to the knocker
#
# All blocking; callers hand them to a worker thread, and on the git carrier
# to `publish_files()` so the write happens under the repo lock.


def _room_root(shared_dir: str, room_id: str) -> str:
    # Same layout constant as transport_file.room_root, restated here so this
    # module never imports the transport (the daemon imports both).
    return os.path.join(shared_dir, "claude-link", room_id)


def _subfile(shared_dir: str, room_id: str, kind: str, device_id: str) -> str:
    if not is_device_id(device_id):
        raise CryptoError(f"not a device id: {device_id!r}")
    return os.path.join(_room_root(shared_dir, room_id), kind,
                        f"{device_id}.json")


def _read_signed_dir(shared_dir: str, room_id: str, kind: str) -> list[dict]:
    directory = os.path.join(_room_root(shared_dir, room_id), kind)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    out: list[dict] = []
    for name in names:
        if not name.endswith(".json") or name.startswith(".tmp-"):
            continue
        if not is_device_id(name[:-len(".json")]):
            continue
        path = os.path.join(directory, name)
        try:
            if os.stat(path).st_size > MAX_DOOR_FILE_BYTES:
                continue
            with open(path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(blob, dict) and blob.get("device_id") == name[:-len(".json")]:
            out.append(blob)
    return out


def write_door_entry(shared_dir: str, room_id: str, entry: dict) -> bool:
    """Write this device's door entry. Returns False when it is already
    byte-identical on disk — which, on the git carrier, is what keeps a
    deterministic file from costing a commit per presence refresh."""
    path = _subfile(shared_dir, room_id, "door", entry["device_id"])
    text = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            if fh.read() == text:
                return False
    except OSError:
        pass
    atomic_write_text(path, text)
    return True


def read_door_entries(shared_dir: str, room_id: str) -> list[dict]:
    return _read_signed_dir(shared_dir, room_id, "door")


def write_knock_file(shared_dir: str, room_id: str, knock: dict) -> None:
    atomic_write_text(_subfile(shared_dir, room_id, "knock", knock["device_id"]),
                      json.dumps(knock, ensure_ascii=False, sort_keys=True))


def read_knock_files(shared_dir: str, room_id: str) -> list[dict]:
    return _read_signed_dir(shared_dir, room_id, "knock")


def write_grant_file(shared_dir: str, room_id: str, grant: dict) -> None:
    atomic_write_text(_subfile(shared_dir, room_id, "grant", grant["for"]),
                      json.dumps(grant, ensure_ascii=False, sort_keys=True))


def read_grant_file(shared_dir: str, room_id: str,
                    device_id: str) -> dict | None:
    path = _subfile(shared_dir, room_id, "grant", device_id)
    try:
        if os.stat(path).st_size > MAX_DOOR_FILE_BYTES:
            return None
        with open(path, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        return None
    return blob if isinstance(blob, dict) else None


def remove_join_files(shared_dir: str, room_id: str, device_id: str) -> None:
    """A joiner cleaning up after itself: its knock and its grant."""
    for kind in ("knock", "grant"):
        try:
            os.unlink(_subfile(shared_dir, room_id, kind, device_id))
        except OSError:
            pass


def gc_stale_knocks(shared_dir: str, room_id: str,
                    now: float | None = None) -> int:
    """Unlink knocks past KNOCK_TTL_S. Any member may sweep; returns count."""
    import datetime
    now = time.time() if now is None else now
    removed = 0
    for knock in read_knock_files(shared_dir, room_id):
        ts = str(knock.get("ts") or "")
        try:
            written = datetime.datetime.fromisoformat(
                ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            written = 0.0
        if now - written > KNOCK_TTL_S:
            try:
                os.unlink(_subfile(shared_dir, room_id, "knock",
                                   knock["device_id"]))
                removed += 1
            except (OSError, CryptoError):
                pass
    return removed
