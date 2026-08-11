"""Room secrets, authenticated encryption and device signatures.

Everything a room needs is derived from one `(name, secret)` pair, split into
three values with distinct powers and no path back from one to another:

    room_id       what the relay routes on, and all it can correlate
    room_auth_sk  Ed25519 key proving membership to the relay
    room_key      AES-256-GCM key; never leaves the machine

The relay stores only `room_auth_pk`, the *public* half. That asymmetry is the
point: it can check that a joiner belongs to a room without gaining the ability
to join one itself. A shared HMAC token would have handed it both.

Secrets are generated, not typed. `room_id` is visible to whoever runs the
relay, so a human-typed passphrase sitting behind it is offline-crackable no
matter how slow the KDF; 128 random bits are not. `new_invite()` mints those
bits and `parse_invite()` reads them back, which is the path the tools use.
A typed passphrase still works, and still warns.

Confidentiality comes from AES-256-GCM under the room key. Authenticity does
not: every member holds that key, so any member could encrypt a message claiming
to be another. Ed25519 signatures over the sealed payload close that, which is
what makes a room with more than two members mean anything.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import unicodedata
from dataclasses import dataclass
from typing import Any

try:
    from cryptography.exceptions import InvalidSignature, InvalidTag
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError as exc:  # pragma: no cover - the installer checks for this
    raise ImportError(
        "agent-link needs the 'cryptography' package for end-to-end encryption.\n"
        "Install it with:  python3 -m pip install --user 'cryptography>=42'"
    ) from exc


# --------------------------------------------------------------------------- #
# parameters
# --------------------------------------------------------------------------- #

# ~0.5 s and 128 MiB. Paid once when a room is joined, never on the message
# path. Sized against offline attack on the typed-passphrase escape hatch, not
# against interactive login.
SCRYPT_N = 2 ** 17
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 256 * 1024 * 1024

KEY_LEN = 32
NONCE_LEN = 12
SIG_LEN = 64
INVITE_BYTES = 16          # 128 bits, base32 -> 26 characters

DERIVATION_SALT = b"claude-link/v2/room|"
SIGNING_CONTEXT = b"claude-link/v2/frame\x00"


class CryptoError(Exception):
    """Anything that fails to decrypt, verify or parse as expected."""


# --------------------------------------------------------------------------- #
# encoding
# --------------------------------------------------------------------------- #


def b32(data: bytes) -> str:
    """Lowercase unpadded base32: case-insensitive and safe in a path segment."""
    return base64.b32encode(data).decode("ascii").rstrip("=").lower()


def unb32(text: str) -> bytes:
    raw = text.strip().upper().replace("-", "").replace(" ", "")
    pad = (-len(raw)) % 8
    try:
        return base64.b32decode(raw + "=" * pad)
    except (ValueError, TypeError) as exc:
        raise CryptoError(f"not valid base32: {exc}") from exc


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise CryptoError(f"not valid base64: {exc}") from exc


def canonical_json(obj: Any) -> bytes:
    """Byte-for-byte reproducible JSON, so signatures verify across machines."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def framed(*parts: bytes) -> bytes:
    """Length-prefixed concatenation.

    Plain concatenation is ambiguous: ("ab", "c") and ("a", "bc") would produce
    the same associated data, letting an attacker shift a byte out of the device
    id and into the sequence number without the tag noticing. A four-byte length
    in front of each field makes the encoding injective.
    """
    return b"".join(struct.pack("!I", len(p)) + p for p in parts)


def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _hkdf(master: bytes, label: bytes, length: int = KEY_LEN) -> bytes:
    """HKDF-Expand (RFC 5869 section 2.3) over an already-uniform master key.

    scrypt output is uniformly random, so the extract step buys nothing here;
    expand alone is the correct half of the construction.
    """
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = _hmac_sha256(master, block + label + bytes([counter]))
        out += block
        counter += 1
    return out[:length]


# --------------------------------------------------------------------------- #
# invites
# --------------------------------------------------------------------------- #


def new_invite(room_name: str) -> str:
    """Mint `name#SECRET` with 128 bits of entropy. The thing you paste to a colleague."""
    return f"{normalize_name(room_name)}#{b32(secrets.token_bytes(INVITE_BYTES)).upper()}"


def parse_invite(invite: str) -> tuple[str, str]:
    """Split `name#SECRET` back into its parts. Raises if it is not an invite."""
    text = (invite or "").strip()
    if "#" not in text:
        raise CryptoError(
            "not an invite string: expected 'room-name#SECRET' as printed by link_join"
        )
    name, _, secret = text.partition("#")
    if not name.strip() or not secret.strip():
        raise CryptoError("invite is missing the room name or the secret")
    return normalize_name(name), secret.strip()


def normalize_name(room_name: str) -> str:
    """Canonical room name. Both sides must derive the same bytes from it.

    Whitespace collapses to a hyphen so an invite stays one pasteable token, and
    so "Auth Review" and "auth-review" are the same room rather than two rooms
    that look identical in a status line.
    """
    text = unicodedata.normalize("NFKC", room_name or "").strip().casefold()
    # '#' is the invite separator. A name containing one makes `parse_invite`
    # split in the wrong place, so the person pasting the invite derives a
    # different room from the person who printed it -- and lands alone in a
    # valid, empty room with no error anywhere. Refuse the name instead.
    if "#" in text:
        raise CryptoError("room name must not contain '#' (it separates name from secret)")
    return "-".join(text.split())


def normalize_secret(secret: str) -> str:
    """Normalise a secret without casefolding it — that would discard entropy."""
    return unicodedata.normalize("NFKC", secret or "").strip()


# --------------------------------------------------------------------------- #
# room derivation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RoomKeys:
    """Everything derived from one (name, secret) pair. Memory only."""

    room_name: str
    room_id: str
    room_auth_sk: Ed25519PrivateKey
    room_key: bytes

    @property
    def room_auth_pk(self) -> str:
        return public_bytes(self.room_auth_sk)

    def public(self) -> dict[str, Any]:
        """The half that is safe to show a user or write to a log."""
        return {"room_name": self.room_name, "room_id": self.room_id}


def derive_room(room_name: str, secret: str) -> RoomKeys:
    """Derive the room id, the relay auth key and the message key.

    Identical on every member's machine, with no negotiation. Costs ~0.5 s.
    """
    name = normalize_name(room_name)
    sec = normalize_secret(secret)
    if not name:
        raise CryptoError("room name must not be empty")
    if not sec:
        raise CryptoError("room secret must not be empty")

    master = hashlib.scrypt(
        sec.encode("utf-8"),
        salt=DERIVATION_SALT + name.encode("utf-8"),
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_LEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return RoomKeys(
        room_name=name,
        room_id="room_" + b32(_hkdf(master, b"room-id", 16)),
        room_auth_sk=Ed25519PrivateKey.from_private_bytes(_hkdf(master, b"relay-auth")),
        room_key=_hkdf(master, b"message-key"),
    )


def derive_room_from_invite(invite: str) -> RoomKeys:
    name, secret = parse_invite(invite)
    return derive_room(name, secret)


# --------------------------------------------------------------------------- #
# relay membership proof
# --------------------------------------------------------------------------- #


CHALLENGE_CONTEXT = b"claude-link/challenge/v2"


def challenge_bytes(challenge: str, room_id: str, device_id: str,
                    audience: str) -> bytes:
    """The challenge is signed as the opaque string it arrived as.

    Decoding it first would mean both sides had to agree on an encoding to
    re-derive the same bytes; treating it as text removes that coupling.

    `audience` names **who the signature is for**, and its absence was a real
    break. Without it the signed statement is only "I saw challenge C for room
    R as device D", which is a bearer credential: a relay the victim was once
    pointed at could take an honest relay's challenge, hand it over as its own,
    and replay the victim's signed join upstream. It reads nothing -- it has no
    room key -- but the honest relay seats it as that device, and its cursor
    advances for every frame it swallows, so the real member gets none of them.
    Claim (c) in the threat model says that cannot happen.

    For the relay it is the `wss://` origin the client dialled; for the direct
    transport it is the `host:port`. A context string is prefixed for the same
    reason frames have `SIGNING_CONTEXT`: the separation between the two kinds
    of signature this key makes should be by design, not an accident of how
    long the framing prefix happens to be.
    """
    return framed(
        CHALLENGE_CONTEXT,
        challenge.encode("utf-8"),
        room_id.encode("utf-8"),
        device_id.encode("utf-8"),
        audience.encode("utf-8"),
    )


def sign_challenge(keys: RoomKeys, challenge: str, device_id: str,
                   audience: str) -> str:
    """Prove membership to one verifier. Bound to one challenge and one audience.

    `audience` is what stops the proof being forwarded: see `challenge_bytes`.
    """
    return b64(keys.room_auth_sk.sign(
        challenge_bytes(challenge, keys.room_id, device_id, audience)))


def verify_challenge(room_auth_pk: str, challenge: str, room_id: str,
                     device_id: str, signature: str, audience: str) -> bool:
    """Relay side. Verifies with a public key it can check but never use to join.

    `audience` must be this verifier's own name. A signature made for somebody
    else will not verify here, which is the point.
    """
    try:
        load_public(room_auth_pk).verify(
            unb64(signature),
            challenge_bytes(challenge, room_id, device_id, audience),
        )
        return True
    except (InvalidSignature, CryptoError):
        return False


# --------------------------------------------------------------------------- #
# sealing
# --------------------------------------------------------------------------- #


def binding_for(room_id: str, device_id: str, seq: int, kind: str) -> bytes:
    """The routing fields the relay may read, bound into the tag and the signature.

    These travel in clear so the relay can fan out and deduplicate. Feeding them
    as associated data means it cannot rewrite them; covering them with the
    signature means no member can either -- including by lifting a signed
    payload out of one room and resealing it into another.
    """
    return framed(
        room_id.encode("utf-8"),
        device_id.encode("utf-8"),
        struct.pack("!Q", int(seq)),
        kind.encode("utf-8"),
    )


def seal(room_key: bytes, signing_key: Ed25519PrivateKey, envelope: dict[str, Any],
         room_id: str, device_id: str, seq: int, kind: str) -> tuple[str, str]:
    """Sign an envelope, then encrypt it. Returns (nonce_b64, ciphertext_b64)."""
    plaintext = canonical_json(envelope)
    binding = binding_for(room_id, device_id, seq, kind)
    signature = signing_key.sign(SIGNING_CONTEXT + binding + plaintext)

    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(room_key).encrypt(nonce, signature + plaintext, binding)
    return b64(nonce), b64(ct)


def unseal(room_key: bytes, nonce_b64: str, ct_b64: str, room_id: str,
           device_id: str, seq: int, kind: str,
           verify_key: Ed25519PublicKey | None = None
           ) -> tuple[dict[str, Any], bytes, bytes]:
    """Decrypt, verify, and cross-check the header against the signed body.

    Returns `(envelope, signature, plaintext)`. The plaintext comes back because
    it is what was actually signed: verifying against a re-serialisation of the
    parsed object instead would mean the guarantee is "the signature covers a
    canonical rendering of what we think we read", which is a different and
    weaker claim.

    `verify_key` may be None only when the sender's key is not yet known -- the
    caller must then take the key out of the envelope, check it hashes to
    `device_id`, and verify before trusting anything.
    """
    # Every failure below has to leave as a CryptoError. Callers catch that and
    # drop the frame; anything else escapes into a transport's poll loop, which
    # has already deleted the batch it was draining. `binding_for` in particular
    # runs *before* decryption, so an out-of-range seq is reachable by anyone
    # who can write to the carrier, with no key at all.
    try:
        binding = binding_for(room_id, device_id, seq, kind)
    except (struct.error, ValueError, TypeError, OverflowError) as exc:
        raise CryptoError(f"unusable frame header: {exc}") from exc
    try:
        opened = AESGCM(room_key).decrypt(unb64(nonce_b64), unb64(ct_b64), binding)
    except InvalidTag as exc:
        raise CryptoError("failed to decrypt (wrong room secret, or tampered with)") from exc
    except ValueError as exc:              # e.g. a nonce of an impossible length
        raise CryptoError(f"unusable frame: {exc}") from exc

    if len(opened) <= SIG_LEN:
        raise CryptoError("payload too short to contain a signature")
    signature, plaintext = opened[:SIG_LEN], opened[SIG_LEN:]

    try:
        envelope = json.loads(plaintext.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CryptoError(f"decrypted payload is not valid JSON: {exc}") from exc
    except RecursionError as exc:
        # 34 KB of nested arrays is under every size cap on the path here, and
        # json's C parser recurses. RecursionError is not a ValueError, so it
        # used to leave this function as itself and kill the transport task
        # that called it.
        raise CryptoError("decrypted payload is nested too deeply") from exc
    if not isinstance(envelope, dict):
        raise CryptoError("decrypted payload is not an object")

    if verify_key is not None:
        _verify(verify_key, signature, binding, plaintext)

    # The header is what routing, dedupe and filtering act on. If it disagrees
    # with the bytes that were actually signed, drop the frame rather than
    # deciding which of the two to believe.
    for field, expected in (
        ("room_id", room_id), ("device_id", device_id), ("kind", kind),
    ):
        if envelope.get(field) != expected:
            raise CryptoError(f"header {field} disagrees with the signed envelope")
    # A non-numeric seq inside the envelope is a malformed frame, not a crash:
    # int("abc") raises ValueError and int({}) raises TypeError, and this runs
    # before the signature is checked, so the sender is not yet trusted.
    # OverflowError is in there because `json.dumps` emits a bare `Infinity`
    # and `json.loads` accepts it, so a member can put one on the wire, and
    # `int(float("inf"))` raises an ArithmeticError rather than a ValueError.
    try:
        same_seq = int(envelope.get("seq", -1)) == int(seq)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CryptoError(f"envelope seq is not a number: {exc}") from exc
    if not same_seq:
        raise CryptoError("header seq disagrees with the signed envelope")

    return envelope, signature, plaintext


def verify_signature(verify_key: Ed25519PublicKey, signature: bytes,
                     plaintext: bytes, room_id: str, device_id: str,
                     seq: int, kind: str) -> None:
    """Verify a signature kept aside while the sender's key was still unknown.

    Takes the plaintext bytes `unseal` returned, not the parsed envelope. It
    used to re-serialise the object, which meant a sender could put a lone
    surrogate in a string and have the re-encode raise UnicodeEncodeError --
    out of the receive path, past every CryptoError handler, taking the rest of
    the drained batch with it.
    """
    _verify(verify_key, signature, binding_for(room_id, device_id, seq, kind),
            plaintext)


def _verify(verify_key: Ed25519PublicKey, signature: bytes, binding: bytes,
            plaintext: bytes) -> None:
    try:
        verify_key.verify(signature, SIGNING_CONTEXT + binding + plaintext)
    except InvalidSignature as exc:
        raise CryptoError("signature does not match the sending device") from exc


# --------------------------------------------------------------------------- #
# device keys
# --------------------------------------------------------------------------- #


def generate_device_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def private_bytes(key: Ed25519PrivateKey) -> str:
    return b64(key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    ))


def public_bytes(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return b64(pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ))


def load_private(raw_b64: str) -> Ed25519PrivateKey:
    try:
        return Ed25519PrivateKey.from_private_bytes(unb64(raw_b64))
    except (ValueError, CryptoError) as exc:
        raise CryptoError(f"malformed device private key: {exc}") from exc


def load_public(raw_b64: str) -> Ed25519PublicKey:
    try:
        return Ed25519PublicKey.from_public_bytes(unb64(raw_b64))
    except (ValueError, CryptoError) as exc:
        raise CryptoError(f"malformed public key: {exc}") from exc


def device_id_for(public_key_b64: str) -> str:
    """Stable short id for a device key.

    The id *is* a hash of the key, so a member announcing a key can be checked
    against the id it claims. That removes any need for trust-on-first-use
    pinning: forging another member's identity would take an 80-bit second
    preimage, not a race to be seen first.
    """
    return "dev_" + b32(hashlib.sha256(unb64(public_key_b64)).digest()[:10])


def key_matches_device(public_key_b64: str, device_id: str) -> bool:
    try:
        return hmac.compare_digest(device_id_for(public_key_b64), device_id)
    except CryptoError:
        return False


# --------------------------------------------------------------------------- #
# id shapes
# --------------------------------------------------------------------------- #
# These are the only two ids this code mints, and both end up as **filesystem
# path segments** and as git paths: `<share>/claude-link/<room_id>/out/<sender>/
# <recipient>/`. os.path.join takes an absolute second component as the whole
# path, discarding the base -- so a single unvalidated id turns "write into the
# share" into "write anywhere this process can write", including a Windows UNC
# path that makes the machine authenticate to a host of the attacker's choosing.
#
# Frames cannot carry a bad id: `device_id` is checked against the hash of the
# announced key before anything is believed. But the relay's roster is not a
# frame. It arrives in the clear, from a party the threat model calls hostile,
# and it reaches the same code. Hence a shape check, applied at every boundary
# where an id enters rather than at the one place it is used.

DEVICE_ID_RE = re.compile(r"\Adev_[a-z2-7]{16}\Z")
ROOM_ID_RE = re.compile(r"\Aroom_[a-z2-7]{26}\Z")


def is_device_id(value: Any) -> bool:
    """True for exactly what `device_id_for` emits, and nothing else."""
    return isinstance(value, str) and DEVICE_ID_RE.match(value) is not None


def is_room_id(value: Any) -> bool:
    """True for exactly what `derive_room` emits, and nothing else."""
    return isinstance(value, str) and ROOM_ID_RE.match(value) is not None


def fingerprint(public_key_b64: str) -> str:
    """Six groups a human can read aloud to confirm a key out of band."""
    digest = hashlib.sha256(unb64(public_key_b64)).hexdigest()[:24].upper()
    return "-".join(digest[i:i + 4] for i in range(0, 24, 4))
