"""Small shared helpers: time, ids, atomic writes, local IP discovery, and how
to name this program in a command somebody is going to paste."""

from __future__ import annotations

import datetime as _dt
import json
import os
import shlex
import shutil
import socket
import sys
import tempfile
import threading
import uuid


def now_iso() -> str:
    """UTC timestamp, ISO-8601 with microseconds and a trailing Z."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now_ms() -> int:
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)


def today_str() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# --------------------------------------------------------------------------- #
# naming this program in a command somebody will paste
# --------------------------------------------------------------------------- #


def shell_quote(word: str) -> str:
    """Quote one argument for the shell that will parse the string we print.

    Two callers need this and both are producing a command that a shell will
    see: the notification hook, which is configured as a command *string*, and
    every line of advice that names an interpreter path. On POSIX a home
    directory containing a space, a quote or a `$` has to be quoted properly --
    `"$HOME/My Stuff/..."` would expand the variable, and shlex.quote would not.
    Windows has neither the expansion problem nor quotes in paths, but it does
    have spaces, and cmd.exe wants double quotes.
    """
    if os.name == "nt":
        return f'"{word}"' if (not word or any(ch in word for ch in ' \t&()[]{}^=;!+,`~')) else word
    return shlex.quote(word)


def cli_invocation(python: str | None = None) -> str:
    """How to tell somebody to run this, on the machine being told.

    `shutil.which` coming back empty is not the exotic case. A `pip install
    --user` on Windows puts the console script in
    `%APPDATA%\\Python\\PythonXXX\\Scripts`, and nothing puts that on PATH. So
    does a venv nobody has activated. Printing the bare name regardless hands
    over a command that does not exist on the machine that printed it.

    That is worst in `doctor`, which is what somebody runs when something is
    already wrong: every fix it offers would be a second dead end. The module
    form works from any directory once the package is installed, which is the
    whole reason for installing it rather than keeping the checkout.

    `which` searching PATH is exactly the question being asked, so when it
    answers, the bare name is the right thing to print: it resolves, and it is
    the form every document uses.
    """
    if shutil.which("agent-link"):
        return "agent-link"
    return f"{shell_quote(python or sys.executable)} -m link.cli"


HOSTNAME_LOOKUP_TIMEOUT = 1.0

# Set once a hostname lookup has blown its deadline. Whether resolving our own
# name is slow is a property of the machine's resolver, not of the moment, so
# there is nothing to gain by paying the timeout again on every later call.
_hostname_lookup_is_slow = False


def _hostname_ips(timeout: float) -> list[str]:
    """IPv4 addresses for this host's own name, or [] if that takes too long.

    `getaddrinfo` has no timeout parameter and ignores `setdefaulttimeout`, so
    bounding it means running it somewhere we can walk away from. This matters
    more than it looks: on macOS `gethostname()` is a `.local` name that
    resolves over mDNS, and on a machine with no responder -- a CI runner, a
    locked-down network -- that call blocks for minutes. It used to do so on the
    daemon's event loop, after the control port was already listening, which
    made a stalled daemon look like a running one that answered nothing.
    """
    global _hostname_lookup_is_slow
    if _hostname_lookup_is_slow:
        return []

    out: list[str] = []

    def resolve() -> None:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                out.append(info[4][0])
        except OSError:
            pass

    # Daemon thread: if the lookup is wedged in the resolver we still exit.
    worker = threading.Thread(target=resolve, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        _hostname_lookup_is_slow = True
        return []
    return out


def local_ips(timeout: float = HOSTNAME_LOOKUP_TIMEOUT) -> list[str]:
    """Best-effort list of this host's routable IPv4 addresses.

    Uses a connect() on a UDP socket (no packets sent) to find the address the
    OS would source from, then adds anything else its own hostname resolves to.
    The UDP probe sends nothing and cannot block; the hostname lookup can, so it
    is bounded by `timeout` and simply contributes nothing when it runs long.
    """
    found: list[str] = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        found.append(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    for ip in _hostname_ips(timeout):
        if ip not in found and not ip.startswith("127."):
            found.append(ip)
    return found or ["127.0.0.1"]


def primary_ip() -> str:
    return local_ips()[0]


def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    """Write a file atomically: temp file in the same dir, then os.replace.

    Needed for the file transport, where the peer may be polling the directory
    while we write.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_line(path: str, line: str) -> None:
    """Append one line to a file, creating parents. Opened per call so that
    external readers (tail, the peer's editor) always see a consistent file."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line.rstrip("\n") + "\n")


def read_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path: str, obj) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def free_port() -> int:
    """Ask the OS for an unused TCP port (then release it)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
