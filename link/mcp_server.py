"""MCP stdio server exposing the link to Claude Code and to Codex.

Raw JSON-RPC 2.0 over stdin/stdout -- no SDK, so startup is one Python import
and a socket connect. All real work lives in the daemon; these handlers are
~1 ms wrappers, and only `link_wait` blocks.

Output here is the product. Every byte a tool returns lands in a model's context
and is paid for again on every subsequent turn, so the default rendering is one
compact line per fact, long messages are truncated with `link_read` to fetch the
rest, and the raw JSON is behind `verbose`. The tool schemas below are in
context for the whole session, which is why their descriptions are terse.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from link import __version__                            # noqa: E402
from link.client import ControlClient, LinkClientError  # noqa: E402
from link.store import load_config                      # noqa: E402
from link.text import PROVENANCE, clean_label, clean_text, fenced  # noqa: E402

# Versions we know how to speak. The client's request is echoed when we know it,
# because Claude Code and Codex do not always ask for the same one and a server
# that answers with its own regardless is a server that fails on one of them.
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = SUPPORTED_PROTOCOLS[0]
SERVER_NAME = "agent-link"

# Tools whose result can contain something a peer wrote. Their `verbose` dump is
# fenced, like their compact rendering already is.
_CARRIES_PEER_TEXT = frozenset({
    "link_inbox", "link_wait", "link_read", "link_history",
    "link_channel", "link_status", "link_join",
})

_client = ControlClient()
_sink_registered = False


def _err(msg: str) -> None:
    print(f"[agent-link] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# tool schemas
# --------------------------------------------------------------------------- #

_ROOM = {"type": "string", "description": "Room name or id. Omit if you are in one room."}
_ROLE = {"type": "string", "enum": ["orchestrator", "subagent"],
         "description": "Subagents MUST pass 'subagent'."}
_AGENT = {"type": "string", "description": "Speaking agent's name, e.g. 'explore-auth'."}
_VERBOSE = {"type": "boolean", "description": "Return full JSON instead of a summary."}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "link_status",
        "description": "Rooms you are in, who is online, unread count, live transport. "
                       "Starts the daemon if needed. Call first in a session.",
        "inputSchema": {"type": "object", "properties": {"verbose": _VERBOSE}},
    },
    {
        "name": "link_join",
        "description": "Create or join a room. room= alone creates one; invite= joins with "
                       "a 'name#SECRET' invite or knocks with a 'name#DOOR-...' door code "
                       "(a member must let you in). If it answers need_name, ask the user "
                       "\"Your name?\" and retry with name=. Persists across restarts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room": {"type": "string", "description": "Name for a new room."},
                "invite": {"type": "string",
                           "description": "'name#SECRET' invite, or 'name#DOOR-...' door code."},
                "name": {"type": "string",
                         "description": "The human's name, asked once: \"Your name?\""},
                "create_anyway": {"type": "boolean",
                                  "description": "Make a new room even though this repo already has an open one."},
                "passphrase": {"type": "string",
                               "description": "Shared secret instead of an invite. Weaker; prefer invite."},
                "shared_dir": {"type": "string",
                               "description": "Optional folder all members can see, used if the relay is blocked."},
                "git_remote": {"type": "string",
                               "description": "Optional private git repo URL all members can push to, instead of a folder."},
            },
        },
    },
    {
        "name": "link_grant",
        "description": "Answer a knock: allow=true lets them in (sends the room "
                       "key sealed to them), allow=false declines. device= from "
                       "the knock notification.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "Knocker's device id, or their name if unambiguous."},
                "allow": {"type": "boolean", "description": "Default true."},
                "room": _ROOM,
            },
            "required": ["device"],
        },
    },
    {
        "name": "link_send",
        "description": "Send a message to a room. NON-BLOCKING: returns immediately, and "
                       "queues if nobody is reachable, so it is always safe to call. Use it "
                       "to split work, report progress, or hand off a task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The message."},
                "room": _ROOM,
                "to": {"type": "string", "description": "Address one member by device id or label. Default everyone."},
                "reply_to": {"type": "string", "description": "msg_id this answers."},
                "meta": {"type": "object", "description": "Structured payload alongside the text."},
                "role": _ROLE,
                "agent": _AGENT,
            },
            "required": ["text"],
        },
    },
    {
        "name": "link_inbox",
        "description": "Drain new messages. NON-BLOCKING, returns [] when nothing is new. "
                       "Cheap — call between steps of your own work. Long messages are cut "
                       "at 400 chars; use link_read for the rest.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room": _ROOM,
                "limit": {"type": "integer", "description": "Max messages (default 20)."},
                "peek": {"type": "boolean", "description": "Do not mark as read."},
                "include_system": {"type": "boolean",
                                   "description": "Include join/leave notices. Default false."},
                "verbose": _VERBOSE,
            },
        },
    },
    {
        "name": "link_read",
        "description": "Full text of one message by msg_id, when link_inbox truncated it.",
        "inputSchema": {
            "type": "object",
            "properties": {"msg_id": {"type": "string"}},
            "required": ["msg_id"],
        },
    },
    {
        "name": "link_wait",
        "description": "BLOCKING: wait for a message or the timeout. Use only when you "
                       "cannot proceed without an answer — otherwise send, keep working, "
                       "and call link_inbox later. Always returns by timeout_ms.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_ms": {"type": "integer", "description": "Capped at 600000. Default 30000."},
                "room": _ROOM,
                "include_system": {"type": "boolean"},
            },
        },
    },
    {
        "name": "link_channel",
        "description": "Side channels for subagents, so a long exchange does not flood the "
                       "main room. action=open|close|list. Pass the returned channel_id as "
                       "room= to link_send. Channels do not nest.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["open", "close", "list"]},
                "topic": {"type": "string", "description": "For open: what it is for."},
                "channel_id": {"type": "string", "description": "For close."},
                "room": _ROOM,
                "role": _ROLE,
                "agent": _AGENT,
                "include_closed": {"type": "boolean"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "link_history",
        "description": "Replay a room from the on-disk transcript, including messages from "
                       "before this session. Use after a restart or /clear.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room": _ROOM,
                "limit": {"type": "integer", "description": "Most recent N (default 30)."},
            },
        },
    },
    {
        "name": "link_leave",
        "description": "Leave a room and stop syncing it. The transcript stays on disk.",
        "inputSchema": {
            "type": "object",
            "properties": {"room": _ROOM},
            "required": ["room"],
        },
    },
]

_TOOL_OPS = {
    "link_status": "status",
    "link_join": "join",
    "link_grant": "grant",
    "link_send": "send",
    "link_inbox": "inbox",
    "link_read": "read",
    "link_wait": "wait",
    "link_history": "history",
    "link_leave": "leave",
}

_CHANNEL_OPS = {"open": "channel_open", "close": "channel_close", "list": "channel_list"}


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #


def _register_project_sink() -> None:
    """Mirror transcripts into this project's .conv/ as well as the global one.

    Attempted once per server process rather than only on a cold daemon start:
    the daemon usually outlives any one project, so tying this to its startup
    means the second project you open never gets its own transcript.
    """
    global _sink_registered
    if _sink_registered:
        return
    _sink_registered = True
    try:
        _client.call("register_sink", dir=os.path.join(os.getcwd(), ".conv"), timeout=2.0)
    except LinkClientError:
        pass


def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    args = dict(args or {})
    if name == "link_channel":
        op = _CHANNEL_OPS.get(str(args.pop("action", "")).lower())
        if op is None:
            return {"ok": False, "error": "action must be open, close or list"}
    else:
        op = _TOOL_OPS.get(name)
        if op is None:
            return {"ok": False, "error": f"unknown tool: {name}"}

    try:
        _client.ensure_daemon()
    except LinkClientError as exc:
        return {"ok": False, "error": str(exc),
                "hint": "Start it by hand to see why: python3 -m link.daemon"}
    _register_project_sink()

    args.setdefault("cwd", os.getcwd())
    # `wait` may legitimately block; give the socket room beyond the daemon's own
    # deadline so we read its answer rather than tripping our own timeout first.
    if op == "wait":
        timeout = min(float(args.get("timeout_ms") or 30000) / 1000.0, 600.0) + 5.0
    elif op == "join":
        timeout = 30.0            # key derivation is deliberately slow
    else:
        timeout = 5.0

    try:
        return _client.call(op, timeout=timeout, **args)
    except LinkClientError as exc:
        return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def _setup_lines(hint: dict[str, Any] | None) -> str:
    """Turn "you cannot reach anyone" into the one command that fixes it."""
    if not hint:
        return ""
    out = f"\n\nNot connected to anything yet: {hint['problem']}."
    if hint.get("command"):
        out += f"\n{hint['fix']}:\n  {hint['command']}"
    else:
        # The stale-config case, which has a fix and nothing to type.
        out += f"\n{hint['fix']}"
    note = hint.get("note_on_that_repo")
    if note:
        out += f"\n{note}"
    return out


def _identity_lines(shared: dict | None) -> str:
    """More than one agent path has signed as this device.

    Said to the agent, not just left in the JSON for a human to find. Under a
    shared identity every other line of `link_status` reads healthy, and every
    one of them is true: the room is fine, the transport is fine, the roster is
    fine. The two agents are simply one member, so neither ever hears the
    other. That is what cost an hour in docs/postmortems.md.
    """
    if not shared:
        return ""
    return (f"\n\nCheck this before trusting the roster: {shared['problem']}."
            f"\n{shared['fix']}")


def _render_messages(messages: list[dict], empty: str) -> str:
    if not messages:
        return empty
    lines = []
    for m in messages:
        where = f"/{clean_label(m['channel'], 40)}" if m.get("channel") else ""
        who = clean_label(m.get("from_name") or m.get("from"), 40) or "?"
        agent = clean_label(m.get("from_agent"), 40)
        # Name *and* device id. The name is chosen by the sender and two members
        # may legally claim the same one; the id is a hash of their signing key,
        # which is the identity nobody can pick for themselves.
        tag = f"{who} [{m.get('from_device') or '?'}]" + (
            f" ({agent})" if agent and agent != "main" else "")
        more = f"  [truncated — link_read {m.get('msg_id')}]" if m.get("truncated") else ""
        kind = "" if m.get("kind") == "msg" else f"{clean_label(m.get('kind'), 24)}: "
        lines.append(f"{clean_label(m.get('room'), 40)}{where} {tag}: "
                     f"{kind}{clean_text(m.get('text'))}{more}")
        if m.get("meta"):
            lines.append(f"    meta {json.dumps(m['meta'], ensure_ascii=False)}")
    # Everything above this line was written by somebody else. The fence says so
    # to the model that is about to read it alongside the user's own words.
    return PROVENANCE + "\n" + fenced("\n".join(lines))


def render(name: str, result: dict[str, Any], verbose: bool = False) -> str:
    """Compact by default. The full result is one `verbose: true` away."""
    # Questions, not failures: they arrive with ok=False because nothing was
    # done yet, and each one is a script — one line to the human, no preamble.
    if name == "link_join" and result.get("need_name"):
        return ('Ask the user: "Your name?" — one line, no preamble — then '
                'retry this link_join with name=<their answer>.')

    if name == "link_join" and result.get("needs_decision") == "join_or_create":
        room = (result.get("open_rooms") or [{}])[0]
        age = room.get("last_active_s")
        ago = ("just now" if age is not None and age < 90
               else f"active {int(age // 60)} min ago" if age is not None
               else "activity unknown")
        return (f"This repo already has an open room ({room.get('members', '?')} "
                f"people, {ago}). Join it? I'll need the door code. Or make a "
                f"new one? (join: link_join(invite=<door code>) — new: retry "
                f"with create_anyway=true)")

    if not result.get("ok"):
        text = f"FAILED: {result.get('error', 'unknown error')}"
        if result.get("hint"):
            text += f"\n{result['hint']}"
        return text

    if verbose:
        blob = json.dumps(result, indent=2, ensure_ascii=False)
        # Fenced like everything else. This branch returns before every
        # PROVENANCE and `fenced()` call below it, and `verbose` is a declared
        # parameter on the message-bearing tools, so a peer asking the agent to
        # "retry with verbose" was one sentence away from an unfenced dump.
        if name in _CARRIES_PEER_TEXT:
            return PROVENANCE + "\n" + fenced(blob)
        return blob

    if name == "link_status":
        rooms = result.get("rooms") or []
        if not rooms:
            if result.get("loading"):
                return ("Still loading persisted rooms. Retry link_status shortly; "
                        "do not join another room.")
            head = (f"No rooms yet. link_join(room=\"a-name\") creates one and prints "
                    f"an invite to share.\nYou are {result.get('label')} "
                    f"({result.get('device_id')}).")
            for k in (result.get("knocking") or []):
                head += (f"\nknocking at {clean_label(k.get('room'), 40)} — "
                         f"waiting to be let in")
            if result.get("open_rooms"):
                room = result["open_rooms"][0]
                head += (f"\nThis repo already has an open room "
                         f"({room.get('members', '?')} people). Join it? "
                         f"I'll need the door code. Or make a new one?")
            return (head + _setup_lines(result.get("setup_needed"))
                    + _identity_lines(result.get("identity_shared")))
        lines = []
        if result.get("loading"):
            lines.append(
                "Still loading persisted rooms; showing rooms loaded so far. "
                "Retry link_status shortly."
            )
        for r in rooms:
            quiet = r.get("quiet_for_s")
            quiet_txt = "" if quiet is None else f", quiet {int(quiet // 60)}m" \
                if quiet >= 60 else f", quiet {int(quiet)}s"
            lines.append(
                f"{r['room']}: {r['online']}/{r['members']} online via {r['transport']}"
                + (f", {r['queued']} queued" if r.get("queued") else "") + quiet_txt
            )
            if r.get("setup_error"):
                lines.append(f"  ! {r['setup_error']}")
            for k in (r.get("knocks") or []):
                lines.append(
                    f"  {clean_label(k.get('name'), 40) or '?'} "
                    f"[{k.get('device_id')}] wants to join {r['room']} — "
                    f"link_grant(device=\"{k.get('device_id')}\", allow=true|false)")
        for k in (result.get("knocking") or []):
            lines.append(f"knocking at {k.get('room')} — waiting to be let in")
        lines.append(f"unread {result.get('unread', 0)}")
        channels = result.get("channels") or []
        if channels:
            lines.append(f"channels open: {', '.join(c['channel_id'] for c in channels)}")
        return ("\n".join(lines) + _setup_lines(result.get("setup_needed"))
                + _identity_lines(result.get("identity_shared")))

    if name == "link_join":
        if result.get("knocked"):
            return ("Knock sent, waiting for someone to let you in. "
                    "You'll be notified when a member answers.")
        head = (f"{'joined' if result.get('members_online') else 'created'} "
                f"{result.get('room')} — ")
        setup_error = result.get("setup_error")
        if result.get("members_online"):
            head += f"{result['members_online']} other member(s) online."
        elif setup_error:
            # The daemon knew why all along. Sending somebody to re-read a
            # 26-character string that was never the problem is worse than
            # saying nothing, and this is the moment people give up.
            head += ("nobody can reach you: this room has no working transport, "
                     "so the invite is not the thing to check.")
        else:
            head += ("you are the only one here. If you expected company, check the "
                     "invite matches theirs exactly.")
        if setup_error:
            head += f"\ntransport: offline — {clean_text(setup_error, 300)}"
        head += f"\ninvite: {result.get('invite')}"
        if result.get("warning"):
            head += f"\nnote: {result['warning']}"
        if result.get("relay_error"):
            head += f"\nrelay not connected: {result['relay_error']}"
        return head + _setup_lines(result.get("setup_needed"))

    if name == "link_grant":
        who = clean_label(result.get("name"), 40) or result.get("device")
        return f"{who} is in." if result.get("granted") else f"Told {who} no."

    if name == "link_send":
        if result.get("delivered"):
            return (f"sent to {result.get('room')} via {result.get('transport')} "
                    f"({result.get('msg_id')})")
        return (f"queued for {result.get('room')} — nobody reachable; goes out on "
                f"reconnect ({result.get('msg_id')})")

    if name in ("link_inbox", "link_wait"):
        msgs = result.get("messages") or []
        empty = "no new messages" if name == "link_inbox" else "timed out, no messages"
        text = _render_messages(msgs, empty)
        if result.get("unread_remaining"):
            text += f"\n({result['unread_remaining']} more — call link_inbox again)"
        return text

    if name == "link_read":
        m = result.get("message") or {}
        # Every field on this line is cleaned, `sent_at` included. It sits
        # between the provenance sentence and the opening marker, which is the
        # one part of this output that sentence vouches for, so a `ts` carrying
        # newlines wrote lines that read as not-peer-written.
        head = (f"{clean_label(m.get('from'), 40)} [{m.get('from_device') or '?'}] "
                f"in {clean_label(m.get('room'), 40)} "
                f"at {clean_label(m.get('sent_at'), 40)}")
        return (PROVENANCE + "\n" + head + "\n"
                + fenced(clean_text(m.get("text")), sender=m.get("from_device") or ""))

    if name == "link_channel":
        if "channels" in result:
            items = result.get("channels") or []
            if not items:
                return "no open side channels"
            # A peer names the topic, and `clean_text` keeps newlines, so one
            # of them made the next line read as a separate record. `clean_label`
            # is the one that strips them, and the list is fenced: this was the
            # only branch here that carried peer text outside one.
            return PROVENANCE + "\n" + fenced("\n".join(
                f"{c['channel_id']} — {clean_label(c.get('topic'), 120) or 'no topic'} "
                f"(opened by {clean_label(c.get('opened_by'), 40)})" for c in items
            ))
        if result.get("status") == "closed":
            return f"closed {result.get('channel_id')}"
        return (f"channel {result.get('channel_id')} open in {result.get('room')} "
                f"— pass it as room= to link_send")

    if name == "link_history":
        records = result.get("records") or []
        if not records:
            return "no transcript yet for this room"
        lines = []
        for r in records:
            body = r.get("body") or {}
            text = clean_text(body.get("text")) or f"({clean_label(r.get('kind'), 24)})"
            arrow = "<-" if r.get("dir") == "in" else "->"
            lines.append(f"{str(r.get('ts', ''))[11:19]} {arrow} "
                         f"{clean_label(r.get('from_label'), 40) or '?'}: {text[:200]}")
        return PROVENANCE + "\n" + fenced("\n".join(lines))

    if name == "link_leave":
        return f"left {result.get('left')}"

    return json.dumps(result, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# JSON-RPC
# --------------------------------------------------------------------------- #


def handle(req: dict[str, Any]) -> dict[str, Any] | None:
    method = req.get("method")
    req_id = req.get("id")

    if method == "initialize":
        asked = ((req.get("params") or {}).get("protocolVersion") or "").strip()
        protocol = asked if asked in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": (
                    "Encrypted channel to other coding agents. Call link_status first. "
                    "link_send and link_inbox are non-blocking; only link_wait blocks. "
                    "Subagents must pass role='subagent' and their agent name."
                ),
            },
        }

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        verbose = bool(args.get("verbose"))
        try:
            result = call_tool(name, args)
        except Exception as exc:            # a tool crash must not kill the server
            _err(f"tool {name} raised: {type(exc).__name__}: {exc}")
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "content": [{"type": "text", "text": render(name, result, verbose)}],
                "isError": not result.get("ok", False),
            },
        }

    if req_id is None:
        return None
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> int:
    load_config()                            # materialises defaults on first run
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        try:
            resp = handle(req)
        except Exception as exc:
            _err(f"handler crashed: {type(exc).__name__}: {exc}")
            resp = {"jsonrpc": "2.0", "id": req.get("id"),
                    "error": {"code": -32603, "message": str(exc)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
