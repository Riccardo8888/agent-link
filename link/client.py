"""Thin synchronous client for the daemon's control socket.

Deliberately blocking-with-a-short-timeout: every op except `wait` returns in
well under a millisecond because the daemon never does network I/O inline. If
the daemon is not running it is auto-started as a detached background process.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from typing import Any

from .store import DEFAULT_CTRL_PORT, load_config, read_daemon_info, root_dir
from .util import read_json

CONNECT_TIMEOUT = 1.5
DEFAULT_TIMEOUT = 3.0


class LinkClientError(Exception):
    pass


class ControlClient:
    """Reuses one socket across calls.

    Opening a fresh TCP connection per call costs ~13 ms on Windows loopback,
    which is the whole latency budget for a tool the model calls between steps
    of its own work. The daemon's control handler already loops over lines on a
    single connection, so we keep ours open and reconnect only when it breaks.
    """

    def __init__(self, port: int | None = None, timeout: float = DEFAULT_TIMEOUT,
                 home: str | None = None) -> None:
        self.timeout = timeout
        self._port = port
        # Which install's daemon.json holds the control token. Normally the
        # ambient one, but a caller that names a port is by definition talking
        # to a daemon it chose, and on a machine running two agents that is not
        # the one CLAUDE_LINK_HOME points at.
        self._home = home
        self._sock: socket.socket | None = None
        self._token: str | None = None
        self._lock = threading.Lock()

    # -- connection -------------------------------------------------------- #

    @property
    def port(self) -> int:
        if self._port:
            return self._port
        info = read_daemon_info() or {}
        if info.get("ctrl_port"):
            return int(info["ctrl_port"])
        return int(load_config().get("ctrl_port", DEFAULT_CTRL_PORT))

    @property
    def token(self) -> str:
        """The daemon's control token, from its 0600 daemon.json.

        Cached, because `link_inbox` on an empty inbox is 0.13 ms and a file
        read per call would be most of that. The daemon mints a new token every
        run, so the cache is dropped and re-read whenever a call comes back
        unauthorised -- which is exactly the daemon-restarted case.
        """
        # An empty answer is never cached. daemon.json does not exist until the
        # daemon has started, and the first calls of a session race exactly
        # that: caching "" there would leave the client permanently
        # unauthorised against a daemon that came up a moment later.
        if not self._token:
            info = (read_json(os.path.join(self._home, "daemon.json"), None)
                    if self._home else read_daemon_info()) or {}
            self._token = str(info.get("token") or "")
        return self._token

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
        try:
            sock.connect(("127.0.0.1", self.port))
        except OSError as exc:
            sock.close()
            raise LinkClientError(
                f"daemon not reachable on 127.0.0.1:{self.port} ({exc})"
            ) from exc
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        return sock

    def call(self, op: str, timeout: float | None = None, **kwargs: Any) -> dict[str, Any]:
        """One request/response round-trip.

        Raises LinkClientError on transport failure; protocol-level failures
        come back as {"ok": False, ...}. A stale reused socket is retried once
        on a fresh connection before giving up.
        """
        deadline = timeout if timeout is not None else self.timeout

        def wire_for(token: str) -> bytes:
            # Which agent path this process is, read from this process's own
            # environment. The daemon cannot work it out for itself: it knows
            # only who spawned it, and the failure in docs/postmortems.md is a
            # second agent arriving later, through a shell, with no environment
            # at all. Imported here rather than at the top so importing the
            # client does not pull in the key file.
            from .identity import agent_kind

            # Caller arguments first, reserved fields last, so the reserved
            # ones win. `call_tool` forwards every argument the model supplies
            # and nothing validates them against the tool schema, so a model
            # passing `token` locked itself out permanently, and one passing
            # `agent_kind` latched the shared-identity warning that exists to
            # diagnose something else entirely.
            payload = {k: v for k, v in kwargs.items() if v is not None}
            payload.update({"op": op, "token": token, "agent_kind": agent_kind()})
            return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

        with self._lock:
            # Two different failures, and each gets its own allowance. A daemon
            # restart produces both, always in this order: the pooled socket
            # dies, and then the token it was carrying is refused. Sharing one
            # budget meant the reconnect spent it and the token refresh below
            # could never run -- which locked every long-lived client out until
            # its process restarted, the MCP server above all, and made
            # `agent-link restart` a thing that breaks the agent that was told
            # to run it. Both flags are one-shot, so this loop runs at most
            # three times and cannot spin.
            may_reconnect = True
            may_refresh_token = True
            while True:
                # Rebuilt every time round. Building it once, outside the loop,
                # is how the retry came to resend the very token that had just
                # been refused.
                wire = wire_for(self.token)
                reused = self._sock is not None
                sock = self._sock or self._connect()
                try:
                    sock.settimeout(deadline)
                    sock.sendall(wire)
                    resp = self._read_response(sock, deadline)
                    if (not resp.get("ok")
                            and str(resp.get("error", "")).startswith("unauthorised")
                            and may_refresh_token):
                        # The daemon restarted and minted a new token, or this
                        # is the first call after one was written. Re-read and
                        # try again on a fresh socket -- it drops the connection
                        # on a refusal, so the current one is spent either way.
                        may_refresh_token = False
                        self._drop()
                        self._token = None
                        continue
                    return resp
                except socket.timeout as exc:
                    self._drop()
                    raise LinkClientError(
                        f"daemon did not answer within {deadline}s"
                    ) from exc
                except ValueError as exc:
                    self._drop()
                    raise LinkClientError(f"malformed response from daemon: {exc}") from exc
                except (OSError, LinkClientError) as exc:
                    self._drop()
                    # A socket we inherited from a previous call may have been
                    # closed by a daemon restart; that is worth one clean retry.
                    # A *fresh* connection failing is the daemon being gone, and
                    # retrying that would only be a slower way to say so.
                    if reused and may_reconnect:
                        may_reconnect = False
                        continue
                    raise LinkClientError(f"control call '{op}' failed: {exc}") from exc

    @staticmethod
    def _read_response(sock: socket.socket, deadline: float) -> dict[str, Any]:
        buf = bytearray()
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise LinkClientError("daemon closed the connection")
            buf.extend(chunk)
        return json.loads(bytes(buf).split(b"\n", 1)[0].decode("utf-8"))

    # -- lifecycle --------------------------------------------------------- #

    def is_running(self) -> bool:
        try:
            return bool(self.call("ping", timeout=1.0).get("ok"))
        except LinkClientError:
            return False

    def ensure_daemon(self, wait_s: float = 12.0) -> dict[str, Any]:
        """Start the daemon if needed and block until it answers `ping`.

        The version check is not ceremony. The daemon is long-lived and starts
        itself, so after an upgrade a new MCP server keeps talking to the old
        daemon still holding the control port -- and every call fails with
        `unknown op` until someone reboots. Restarting on a mismatch turns that
        into a one-second hiccup nobody notices.
        """
        from . import __version__

        try:
            resp = self.call("ping", timeout=1.0)
            if resp.get("ok"):
                if resp.get("version") == __version__:
                    return {"started": False, **resp}
                try:
                    self.call("shutdown", timeout=2.0)
                except LinkClientError:
                    pass
                self.close()
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and self.is_running():
                    time.sleep(0.05)
        except LinkClientError:
            pass

        spawn_daemon()
        deadline = time.monotonic() + wait_s
        last = "no response"
        while time.monotonic() < deadline:
            # Tight poll: this is the one-off cold start the user waits through,
            # so shave it rather than sleeping in lazy increments.
            time.sleep(0.05)
            try:
                resp = self.call("ping", timeout=1.0)
                if resp.get("ok"):
                    return {"started": True, **resp}
            except LinkClientError as exc:
                last = str(exc)
        raise LinkClientError(f"daemon failed to start within {wait_s}s: {last}")


def package_parent() -> str:
    """Directory that contains the `link` package (its import root)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def spawn_daemon() -> subprocess.Popen:
    """Launch the daemon fully detached so it outlives this process."""
    root = root_dir()
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    log_path = os.path.join(root, "logs", "daemon.out.log")

    kwargs: dict[str, Any] = {
        "cwd": package_parent(),
        "stdin": subprocess.DEVNULL,
        "env": {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    }
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW: no
        # console flash, and the daemon survives the parent MCP server exiting.
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True

    handle = open(log_path, "ab", buffering=0)
    try:
        return subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "link.daemon"],
            stdout=handle, stderr=handle, **kwargs,
        )
    finally:
        # Popen has already duplicated the handle for the child; holding our
        # copy open would pin the log file for the life of the caller.
        handle.close()
