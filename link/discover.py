"""What rooms are already open on a carrier, before joining anything.

One depth-1 fetch into a throwaway directory: no clone is kept, nothing is
written to the remote, and everything read is the cleartext the carrier
already shows anyone with repo access — room ids, presence counts and
timestamps. Names are sealed and stay sealed; that is the deal the spec
struck, so a caller can say "3 people, active 5 min ago" and no more.

Returns None when the carrier cannot be read (unreachable, no credential):
"cannot tell" must never render as "nothing there".
"""

from __future__ import annotations

import json
import tempfile
import time
from typing import Any

from .crypto import is_room_id
from .transport_git import (
    GIT_NET_TIMEOUT_S,
    BadRemote,
    GitError,
    _rmtree,
    check_branch,
    check_remote,
    run_git,
)


def discover_rooms(remote: str, branch: str = "claude-link",
                   timeout: float = GIT_NET_TIMEOUT_S) -> list[dict[str, Any]] | None:
    try:
        remote = check_remote(remote)
        branch = check_branch(branch)
    except (BadRemote, ValueError):
        return None

    tmp = tempfile.mkdtemp(prefix="agent-link-peek-")
    try:
        run_git(["init", "-q"], cwd=tmp)
        rc, _out, err = run_git(["fetch", "--depth", "1", "--", remote, branch],
                                cwd=tmp, timeout=timeout, check=False)
        if rc != 0:
            low = (err or "").lower()
            if "couldn't find remote ref" in low or "remote ref" in low:
                return []                  # the repo answered: no channel yet
            return None                    # the repo did not answer: unknown
        rc, out, _err = run_git(
            ["ls-tree", "-r", "--name-only", "FETCH_HEAD", "--", "claude-link"],
            cwd=tmp, check=False)
        if rc != 0:
            return []

        rooms: dict[str, dict[str, Any]] = {}
        now = time.time()
        for path in out.splitlines():
            parts = path.strip().split("/")
            if len(parts) < 3 or parts[0] != "claude-link":
                continue
            room_id = parts[1]
            if not is_room_id(room_id):
                continue
            room = rooms.setdefault(room_id, {
                "room_id": room_id, "members": 0,
                "last_active_s": None, "has_door": False,
            })
            if parts[2] == "door":
                room["has_door"] = True
            elif parts[2] == "presence" and len(parts) == 4:
                room["members"] += 1
                rc, blob, _e = run_git(["show", f"FETCH_HEAD:{path}"],
                                       cwd=tmp, check=False)
                if rc != 0:
                    continue
                try:
                    age = max(0.0, now - float(json.loads(blob).get("epoch", 0)))
                except (ValueError, TypeError):
                    continue
                if room["last_active_s"] is None or age < room["last_active_s"]:
                    room["last_active_s"] = round(age, 1)
        return sorted(rooms.values(),
                      key=lambda r: (r["last_active_s"] is None,
                                     r["last_active_s"] or 0.0))
    except (GitError, OSError, ValueError):
        return None
    finally:
        _rmtree(tmp)
