"""This installation's device key and which agent is driving it.

The keypair is generated once and never leaves the machine. `device_id` is a
hash of the public half, so any member announcing a key can be checked against
the id it claims -- there is no separate trust-on-first-use store to keep.
"""

from __future__ import annotations

import os
import stat
import threading
from dataclasses import dataclass
from typing import Any

from . import crypto
from .store import root_dir
from .util import read_json, write_json

# Codex spawns stdio MCP servers with a cleared environment, so sniffing for
# CLAUDECODE / CODEX_* markers cannot work there. The installers declare this
# variable in the server's own env table instead, which is the only channel that
# survives on both hosts.
AGENT_KIND_ENV = "CLAUDE_LINK_AGENT_KIND"
VALID_AGENT_KINDS = {"claude-code", "codex", "cli"}

_lock = threading.Lock()
_cached: "Identity | None" = None


@dataclass(frozen=True)
class Identity:
    device_id: str
    public_key: str
    label: str
    agent_kind: str
    _private: Any

    def sign_key(self):
        return self._private

    def public(self) -> dict[str, str]:
        """What gets announced to a room. No secrets."""
        return {
            "device_id": self.device_id,
            "public_key": self.public_key,
            "label": self.label,
            "agent_kind": self.agent_kind,
        }

    def fingerprint(self) -> str:
        return crypto.fingerprint(self.public_key)


def identity_path() -> str:
    return os.path.join(root_dir(), "identity.json")


def agent_kind() -> str:
    kind = (os.environ.get(AGENT_KIND_ENV) or "").strip().lower()
    return kind if kind in VALID_AGENT_KINDS else "cli"


def default_label() -> str:
    """A human-readable name for this device: who, on what machine, via what."""
    import socket

    user = os.environ.get("USER") or os.environ.get("USERNAME") or "someone"
    return f"{user.strip().lower().split('.')[0]}@{socket.gethostname()}"


def load(refresh: bool = False) -> Identity:
    """Load the device identity, generating it on first run."""
    global _cached
    with _lock:
        if _cached is not None and not refresh:
            return _cached

        path = identity_path()
        data = read_json(path, None)
        if not isinstance(data, dict) or "private_key" not in data:
            data = _generate(path)

        private = crypto.load_private(data["private_key"])
        public = crypto.public_bytes(private)
        device_id = crypto.device_id_for(public)

        # A tampered or hand-edited file must not be able to claim a device_id
        # that does not belong to its key.
        if data.get("device_id") != device_id:
            data["device_id"] = device_id
            data["public_key"] = public
            _write(path, data)

        _cached = Identity(
            device_id=device_id,
            public_key=public,
            label=data.get("label") or default_label(),
            agent_kind=agent_kind(),
            _private=private,
        )
        return _cached


def set_label(label: str) -> Identity:
    path = identity_path()
    data = read_json(path, None) or {}
    data["label"] = (label or "").strip()[:60] or default_label()
    _write(path, data)
    return load(refresh=True)


def _generate(path: str) -> dict[str, Any]:
    key = crypto.generate_device_key()
    public = crypto.public_bytes(key)
    data = {
        "version": 2,
        "private_key": crypto.private_bytes(key),
        "public_key": public,
        "device_id": crypto.device_id_for(public),
        "label": default_label(),
    }
    _write(path, data)
    return data


def _write(path: str, data: dict[str, Any]) -> None:
    write_json(path, data)
    _restrict(path)


def _restrict(path: str) -> None:
    """Owner-only on the key file and the directory that holds it.

    POSIX modes are a no-op on Windows, where the file inherits the ACL of the
    user profile directory; that is the platform's own answer and there is no
    portable improvement worth the code.
    """
    for target, mode in ((os.path.dirname(path), 0o700), (path, 0o600)):
        try:
            os.chmod(target, mode)
        except (OSError, NotImplementedError):
            pass


def is_world_readable(path: str | None = None) -> bool:
    """True when the private key is readable by someone other than its owner.

    Windows has no POSIX mode bits, so `os.stat` synthesizes them: a readable
    file reports 0o666 whatever its ACL says, and `os.chmod(path, 0o600)` two
    functions above does not change that. The group and other bits were
    therefore set on every Windows install, which made `doctor` print a
    security warning unconditionally and exit 1 forever -- so a real
    permissions problem would have been indistinguishable from the noise, and
    anything gating on that exit code could never pass.

    Answering False there is not a claim the key is safe; it is a refusal to
    assert something this call cannot see. Reading the ACL needs pywin32 or
    parsing `icacls`, neither of which is worth a dependency for a key that
    lives under the user's own profile.
    """
    if os.name == "nt":
        return False
    try:
        mode = os.stat(path or identity_path()).st_mode
    except OSError:
        return False
    return bool(mode & (stat.S_IRGRP | stat.S_IROTH))
