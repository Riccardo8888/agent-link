"""Persistence: config, pairing state, and the `.conv/` conversation logs.

Every message that crosses the link — in either direction, over either
transport — lands in `.conv/<conv_id>/<date>.jsonl` for every registered sink,
annotated with the remote IP and whether an orchestrator or a subagent sent it.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from typing import Any, Iterable

from .crypto import is_room_id
from .envelope import is_channel_id
from .util import (
    append_line,
    atomic_write_text,
    now_iso,
    read_json,
    today_str,
    write_json,
)

HOME = os.path.expanduser("~")
# Deliberately still `claude-link`, and renaming it would be a mistake: it
# holds the device identity, the room secrets and the transcripts, so a new
# name silently mints a new device and drops every joined room.
DEFAULT_ROOT = os.path.join(HOME, ".claude", "claude-link")

DEFAULT_WS_PORT = 45813
DEFAULT_CTRL_PORT = 45814


def root_dir() -> str:
    return os.environ.get("CLAUDE_LINK_HOME") or DEFAULT_ROOT


def _p(*parts: str) -> str:
    return os.path.join(root_dir(), *parts)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: dict[str, Any] = {
    "peer_id": "",
    "display_name": "",
    "ctrl_port": DEFAULT_CTRL_PORT,

    # No longer offered, and deliberately still honoured. A synced folder was
    # the headline setup and the least proven path here: every test drove it
    # against a local temp directory, which is instant, atomic and has no
    # opinions, while OneDrive and Dropbox bring sync latency in minutes,
    # conflict copies nothing parses, and partial writes the peer can see. It
    # never carried a real message between two machines. Nothing recommends it
    # now, `doctor` says so if it is set, and a config that already has one keeps
    # working rather than going quiet. `FileTransport` itself stays either way:
    # `GitTransport` is a subclass of it.
    "shared_dir": None,
    "file_poll_ms": 250,          # also the git transport's local poll

    # The carrier. Nothing to deploy, no inbound port, and it crosses any
    # network that can reach the host. Costs a commit per round instead of a
    # file write, which is why the heartbeat here is measured in tens of
    # seconds. The branch is created as a root commit, so a repo that also holds
    # code is safe to point at.
    "git_remote": None,
    # A wire constant, not a product name. Changing it orphans every live
    # channel, because the other members are still pushing to the old one.
    "git_branch": "claude-link",
    "git_sync_ms": 3000,
    "git_presence_s": 45,
    "git_depth": 0,              # >0 keeps the clone shallow; 0 fetches the branch whole
    "git_dir": None,             # override the clone location; default is under this home
    # How long a room may spend bringing the repo up before it starts without
    # it. Must stay under the 30 s the MCP server allows link_join, or an
    # unreachable remote turns a room that was created into a tool call that
    # reads as failed. Raise it if a first clone is genuinely slow.
    "git_start_timeout_s": 20,
    # The carrier learning the social graph is the documented cost of a
    # public repo; content stays sealed either way. Setting this records that
    # the human chose it, and turns doctor's failure into a note.
    "allow_public_carrier": False,

    # A relay is the optional upgrade: sub-second instead of a second or two,
    # at the cost of having something to deploy. Unset means "not used".
    "relay_url": None,
    "relay_insecure": False,     # skip TLS verification; only for a test relay

    # direct LAN websocket: off unless deliberately enabled, because
    # advertising a reachable inbound endpoint through the relay reintroduces
    # exactly the problem the relay exists to remove
    "direct_enabled": False,
    "ws_host": "0.0.0.0",
    "ws_port": DEFAULT_WS_PORT,

    "ping_interval_s": 20,
    "reconnect_min_s": 1,
    "reconnect_max_s": 30,
    "inbox_max": 500,
    "log_sinks": [],             # extra .conv directories (project cwds)
}


def config_path() -> str:
    return _p("config.json")


def load_config() -> dict[str, Any]:
    """Defaults, overlaid with config.json, overlaid with the environment.

    The environment layer has to be applied here rather than in the daemon: the
    control client, the CLI and the MCP server all need to resolve the same
    ports, and they never read config.json in the daemon's process.
    """
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(read_json(config_path(), {}) or {})

    for key, env_key in (
        ("ws_port", "CLAUDE_LINK_WS_PORT"),
        ("ctrl_port", "CLAUDE_LINK_CTRL_PORT"),
        ("file_poll_ms", "CLAUDE_LINK_POLL_MS"),
        ("git_sync_ms", "CLAUDE_LINK_GIT_SYNC_MS"),
        ("git_presence_s", "CLAUDE_LINK_GIT_PRESENCE_S"),
        ("git_depth", "CLAUDE_LINK_GIT_DEPTH"),
    ):
        raw = os.environ.get(env_key)
        if raw:
            try:
                cfg[key] = int(raw)
            except ValueError:
                pass
    for key, env_key in (
        ("peer_id", "CLAUDE_LINK_PEER_ID"),
        ("shared_dir", "CLAUDE_LINK_SHARED_DIR"),
        ("relay_url", "CLAUDE_LINK_RELAY_URL"),
        ("git_remote", "CLAUDE_LINK_GIT_REMOTE"),
        ("git_branch", "CLAUDE_LINK_GIT_BRANCH"),
        ("git_dir", "CLAUDE_LINK_GIT_DIR"),
    ):
        raw = os.environ.get(env_key)
        if raw:
            cfg[key] = raw.strip().lower() if key == "peer_id" else raw.strip()
    for key, env_key in (
        ("relay_insecure", "CLAUDE_LINK_RELAY_INSECURE"),
        ("direct_enabled", "CLAUDE_LINK_DIRECT"),
    ):
        raw = os.environ.get(env_key)
        if raw:
            cfg[key] = raw.strip().lower() in ("1", "true", "yes", "on")

    if not cfg.get("peer_id"):
        cfg["peer_id"] = _default_peer_id()
    if not cfg.get("display_name"):
        cfg["display_name"] = cfg["peer_id"]
    return cfg


def config_value_ok(key: str, value: Any) -> bool:
    """Is `value` something this key can actually be?

    Nothing checked, and the damage lands at the next startup rather than at the
    write: `inbox_max=-5` is accepted, saved, and then kills every later daemon
    at `deque(maxlen=-5)` *before* the control port opens, so the only symptom
    is "daemon failed to start within 12s" for ever. `ctrl_port="not-a-port"`
    does the same at `int()`. A plain CLI typo reaches both.

    Judged against the default's own type, so a new setting is covered the day
    it is added rather than the day somebody remembers this function.
    """
    if key not in DEFAULT_CONFIG:
        return False
    default = DEFAULT_CONFIG[key]
    if value is None:
        return default is None or not isinstance(default, (int, float, bool))
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        if key.endswith(("_port",)):
            return 0 <= value <= 65535
        return value > 0 if key in _MUST_BE_POSITIVE else value >= 0
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    if isinstance(default, list):
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if isinstance(default, str) or default is None:
        return isinstance(value, str)
    return True


# Settings where zero is not a degenerate case but a broken one: a zero-length
# inbox drops every message, a zero poll spins.
_MUST_BE_POSITIVE = {"inbox_max", "file_poll_ms", "git_sync_ms"}


def save_config(cfg: dict[str, Any]) -> None:
    write_json(config_path(), cfg)


def display_name_set() -> bool:
    """Whether a human has actually chosen a name. `load_config` backfills
    `display_name` from the OS username, so the file is the only witness."""
    return bool((read_json(config_path(), {}) or {}).get("display_name"))


# Settings the daemon acts on once, when it builds its transports at startup.
# Changing one of these in the file changes nothing about a daemon that is
# already running, which is the whole reason `config_changes` exists.
TRANSPORT_KEYS = frozenset({
    "shared_dir", "file_poll_ms",
    "git_remote", "git_branch", "git_dir", "git_sync_ms", "git_presence_s",
    "git_depth",
    "relay_url", "relay_insecure",
    "direct_enabled", "ws_host", "ws_port",
})


def config_changes(loaded: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Keys whose effective value now differs from what `loaded` holds.

    Effective configs are compared, not file bytes. A `config --set` that
    writes back the value already there must report nothing, or the warning
    built on this cries wolf on every no-op; and the environment layer is
    applied to both sides, so a value that only ever came from the environment
    is never blamed on the file.
    """
    current = load_config()
    return {
        key: (loaded.get(key), current.get(key))
        for key in set(current) | set(loaded)
        if loaded.get(key) != current.get(key)
    }


def _default_peer_id() -> str:
    """Derive a stable peer id from the OS username, falling back to hostname."""
    name = os.environ.get("USERNAME") or os.environ.get("USER") or socket.gethostname()
    name = name.strip().lower().replace(" ", "-")
    # Corporate usernames often look like "first.middle.last" — keep the first token.
    return name.split(".")[0] or "peer"


# --------------------------------------------------------------------------- #
# runtime state (pairings, daemon handle)
# --------------------------------------------------------------------------- #


def state_path() -> str:
    return _p("state.json")


def load_state() -> dict[str, Any]:
    """Load v2 state, retiring a v1 file if one is found.

    A v1 `state.json` holds `pairings` keyed by conversation id, and there is no
    way to convert them: a v2 room is derived from a secret this file never
    stored. Archiving beats both crashing on the old shape and silently
    discarding it, and the daemon logs a line telling the user to re-join.
    """
    st = read_json(state_path(), {}) or {}

    if "pairings" in st and "rooms" not in st:
        archive = state_path() + ".v1"
        try:
            os.replace(state_path(), archive)
        except OSError:
            pass
        st = {"migrated_from_v1": archive, "v1_pairings": len(st.get("pairings") or {})}

    st.setdefault("rooms", {})        # room_id -> {name, secret, joined_at, ...}
    st.setdefault("channels", {})     # channel_id -> record
    st.setdefault("pending_knocks", {})  # room_id -> {name, room_id, carrier...}
    st.setdefault("rooms_declined", {})  # room_id -> iso ts ("don't ask again")
    return st


def save_state(state: dict[str, Any]) -> None:
    write_json(state_path(), state)


def daemon_info_path() -> str:
    return _p("daemon.json")


def read_daemon_info() -> dict[str, Any] | None:
    return read_json(daemon_info_path(), None)


def write_daemon_info(info: dict[str, Any]) -> None:
    write_json(daemon_info_path(), info)


def clear_daemon_info() -> None:
    try:
        os.unlink(daemon_info_path())
    except OSError:
        pass


def daemon_log_path() -> str:
    return _p("logs", "daemon.log")


# --------------------------------------------------------------------------- #
# .conv/ logging
# --------------------------------------------------------------------------- #


def conv_dir_name(conv: Any) -> str:
    """The directory name for one conversation, under every sink.

    A room id or a channel id, and both are checked rather than trusted. The
    channel comes out of a peer's envelope, and `os.path.join(sink, "/etc/cron.d")`
    is `/etc/cron.d` -- the base is discarded outright when the second component
    is absolute. Anything that is not one of the two shapes this code mints goes
    to "unknown", which is a directory name and cannot be anything else.
    """
    if is_room_id(conv) or is_channel_id(conv):
        return conv
    return "unknown"


class ConvLogger:
    """Fan-out logger writing each event to every registered `.conv/` sink.

    Kept deliberately dumb and synchronous-under-a-lock: appends are a handful
    of microseconds and the daemon calls this off the hot send path.
    """

    def __init__(self, sinks: Iterable[str] | None = None) -> None:
        self._lock = threading.Lock()
        self._sinks: list[str] = []
        self.add_sink(home_conv_dir())
        for s in sinks or []:
            self.add_sink(s)

    # -- sinks ------------------------------------------------------------- #

    @property
    def sinks(self) -> list[str]:
        with self._lock:
            return list(self._sinks)

    def add_sink(self, path: str | None) -> bool:
        """Register a `.conv` directory. Returns True if it was newly added."""
        if not path:
            return False
        path = os.path.abspath(os.path.expanduser(path))
        with self._lock:
            if path in self._sinks:
                return False
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                return False
            self._sinks.append(path)
        _write_sink_readme(path)
        return True

    # -- writes ------------------------------------------------------------ #

    def log(
        self,
        env: dict[str, Any],
        direction: str,
        transport: str,
        remote_ip: str | None = None,
        remote_port: int | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Append one record to every sink. Returns the record written."""
        origin = env.get("origin") or {}
        record = {
            "ts": now_iso(),
            "dir": direction,                       # "in" | "out"
            "transport": transport,                 # relay | direct | file | local
            "room": env.get("room_id"),
            "channel": env.get("channel"),
            "kind": env.get("kind"),
            "msg_id": env.get("msg_id"),
            "seq": env.get("seq"),
            "reply_to": env.get("reply_to"),
            "from_device": env.get("device_id"),
            "from_label": origin.get("label"),
            "from_agent_kind": origin.get("agent_kind"),   # claude-code | codex | cli
            "from_role": origin.get("role"),               # orchestrator | subagent
            "from_agent": origin.get("agent"),
            "from_host": origin.get("host"),
            "remote_ip": remote_ip,                        # what we actually saw
            "remote_port": remote_port,
            # Default False, not True. This column is the transcript's record of
            # whether a signature checked out, and `_verified` is set only by
            # the path that checks one. Defaulting to True meant anything that
            # reached the logger by another route was filed as verified on the
            # strength of nobody having said otherwise.
            "verified": bool(env.get("_verified", False)),
            "to": env.get("to"),
            "body": env.get("body"),
        }
        if note:
            record["note"] = note

        line = json.dumps(record, ensure_ascii=False)
        conv = conv_dir_name(record["channel"] or record["room"])
        for sink in self.sinks:
            try:
                append_line(os.path.join(sink, conv, f"{today_str()}.jsonl"), line)
                self._append_markdown(sink, conv, record)
            except OSError:
                # A sink on a flaky network share must never break the link.
                continue
        return record

    def _append_markdown(self, sink: str, conv: str, rec: dict[str, Any]) -> None:
        """Human-readable mirror. Control chatter is skipped."""
        if rec["kind"] in ("ping", "pong", "hello"):
            return
        body = rec.get("body") or {}
        text = body.get("text") if isinstance(body, dict) else str(body)
        if not text:
            text = f"_{rec['kind']}_ " + json.dumps(body, ensure_ascii=False)[:200]
        arrow = "<-" if rec["dir"] == "in" else "->"
        role = rec.get("from_role") or "?"
        agent = rec.get("from_agent") or "?"
        kind = rec.get("from_agent_kind") or "?"
        who = rec.get("from_label") or rec.get("from_device") or "?"
        header = (
            f"**{rec['ts']}** `{arrow}` **{who}** "
            f"({kind} {role}/{agent} via {rec['transport']})"
        )
        append_line(
            os.path.join(sink, conv, "transcript.md"),
            f"{header}\n\n{text}\n\n---\n",
        )

    def write_meta(self, conv: str, meta: dict[str, Any]) -> None:
        conv = conv_dir_name(conv)
        for sink in self.sinks:
            try:
                path = os.path.join(sink, conv, "meta.json")
                existing = read_json(path, {}) or {}
                existing.update(meta)
                existing["updated_at"] = now_iso()
                write_json(path, existing)
            except OSError:
                continue

    def read_history(self, conv: str, limit: int = 50, sink: str | None = None) -> list[dict]:
        """Replay the most recent records for a conversation from one sink."""
        base = sink or home_conv_dir()
        conv_dir = os.path.join(base, conv)
        if not os.path.isdir(conv_dir):
            return []
        files = sorted(f for f in os.listdir(conv_dir) if f.endswith(".jsonl"))
        records: list[dict] = []
        for fname in reversed(files):
            try:
                lines = _tail_lines(os.path.join(conv_dir, fname), limit)
            except OSError:
                continue
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
                if len(records) >= limit:
                    return list(reversed(records))
        return list(reversed(records))


def _tail_lines(path: str, limit: int, block: int = 256 * 1024) -> list[str]:
    """The last lines of a file, without reading the whole thing.

    `readlines()` pulled an entire day's transcript into memory per file, and
    `_op_read`'s fallback does that for every room at limit=2000 whenever a
    msg_id misses -- which a model mistyping an id is enough to trigger. These
    files have no rotation, so they only get bigger.
    """
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        chunk = b""
        # A record is one JSON line; read backwards until there are enough of
        # them, or until the file runs out.
        while size > 0 and chunk.count(b"\n") <= limit:
            step = min(block, size)
            size -= step
            fh.seek(size)
            chunk = fh.read(step) + chunk
    text = chunk.decode("utf-8", errors="replace")
    return text.splitlines(keepends=True)[-(limit + 1):]


def home_conv_dir() -> str:
    return _p(".conv")


# --------------------------------------------------------------------------- #
# finding a shared folder
# --------------------------------------------------------------------------- #

_README = """# .conv/

Automatic transcripts written by **agent-link**.

Layout: `<conversation-id>/<YYYY-MM-DD>.jsonl` (machine readable, one envelope
per line) alongside `transcript.md` (human readable) and `meta.json`.

Every record carries `remote_ip` (the address the packet actually came from),
`from_ip`/`from_host` (what the sender claims), `from_role`
(`orchestrator` or `subagent`), `from_agent`, and `transport` (`relay`,
`direct`, `file` or `git`).

Sub-conversations opened by subagents appear as their own directory with
`parent` pointing at the main conversation.

Do not edit these files by hand; they are append-only.
"""


def _write_sink_readme(sink: str) -> None:
    path = os.path.join(sink, "README.md")
    if not os.path.exists(path):
        try:
            atomic_write_text(path, _README)
        except OSError:
            pass
    # A sink is the current project's directory, so these transcripts sit in
    # somebody's working tree in plain text -- one `git add -A` from being
    # committed and pushed. This repository's own .gitignore covers its own
    # .conv/; nothing covers everyone else's.
    ignore = os.path.join(sink, ".gitignore")
    if not os.path.exists(ignore):
        try:
            atomic_write_text(
                ignore,
                "# agent-link transcripts: plain text, keep them local.\n*\n",
            )
        except OSError:
            pass
