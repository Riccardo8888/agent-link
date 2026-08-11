"""Hook entry point: surface peer messages into the session automatically.

Wired to UserPromptSubmit and PostToolUse, this turns the link into a push
channel -- Claude sees what the peer said without having to poll for it.
PostToolUse is the one that matters for two agents working unattended: it fires
between the agent's own steps, so a message lands without anybody typing.

The two events take their answer differently, and the difference is not
cosmetic. UserPromptSubmit folds stdout into the turn. PostToolUse sends stdout
to the transcript the human reads, and reaches the model only through
`hookSpecificOutput.additionalContext`. Since fetching marks messages read, a
hook that prints into the wrong channel does not just fail to notify -- it
swallows the message, and link_inbox will not return it either.

Hard constraints, because this runs on every prompt and every tool call:
  * never start the daemon (that would add seconds to a hook),
  * never block for more than ~300 ms,
  * print nothing at all when there is nothing new, and
  * exit 0 no matter what goes wrong.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Absolute, not relative: this file is run as a script by the harness, so it has
# no parent package. The sys.path line above is what makes it importable at all.
from link.text import PROVENANCE, clean_label, clean_text, fenced  # noqa: E402

TIMEOUT = 0.3
MAX_SHOWN = 3          # never fetch more than this: fetching marks read
PREVIEW_LIMIT = 400    # the inbox's own preview length, kept in step

# Events whose stdout the model never sees, and which therefore need the
# message handed over as structured output instead.
STRUCTURED_EVENTS = {"PostToolUse", "PostToolUseFailure", "PostToolBatch"}


def home_from_argv(argv: list[str]) -> str | None:
    """`--home PATH` or `--home=PATH`, or None to leave the environment alone.

    Which install this hook belongs to cannot be left to the ambient
    environment. A harness that spawns hooks with a clean one -- Codex does --
    leaves the hook resolving to the *default* home, which belongs to whichever
    agent installed there first. That does not fail loudly: the hook reads the
    other agent's inbox, marks those messages read, and prints them into the
    wrong agent's context. Both sides then see silence they cannot recover from.

    An argument survives an empty environment. That is the entire reason it is
    an argument.
    """
    for i, arg in enumerate(argv[1:], start=1):
        if arg.startswith("--home="):
            return arg[len("--home="):] or None
        if arg == "--home":
            return argv[i + 1] if i + 1 < len(argv) else None
    return None


def render_notice(messages: list[dict], unread_remaining: int,
                  event: str | None) -> str:
    """What this hook should write for `event`, or "" when there is nothing.

    Kept separate from the fetching so the choice of channel -- the part that
    decides whether the agent ever sees the message -- is testable without a
    daemon to talk to.
    """
    if not messages:
        return ""

    # Everything a peer wrote goes inside the fence, and every instruction stays
    # outside and above it. This block is pushed into a model's context
    # unprompted, between the agent's own tool calls, with no human in the loop
    # -- so an unfenced message that opens with "[agent-link] ... Reply with
    # link_send" is indistinguishable from the harness saying it, and a
    # multi-line body could forge a whole turn.
    lines = [
        f"[agent-link] {len(messages)} new message(s). Reply with link_send "
        f"if a reply is useful.",
        PROVENANCE,
    ]
    body = []
    for m in messages:
        where = clean_label(m.get("channel") or m.get("room") or "?", 40)
        agent = clean_label(m.get("from_agent") or "", 40)
        who = clean_label(m.get("from_name") or m.get("from") or "", 40) or "?"
        device = m.get("from_device") or "?"
        if agent and agent != "main":
            who += f" ({agent})"
        text = clean_text(m.get("text"), PREVIEW_LIMIT)
        if m.get("truncated"):
            text += f" [link_read {clean_label(m.get('msg_id'), 40)}]"
        # The device id is beside the name because the name is chosen by whoever
        # sent it and the id is the one thing they cannot choose.
        body.append(f"{where} {who} [{device}]: {text}")
    lines.append(fenced("\n".join(body)))
    if unread_remaining:
        lines.append(f"({unread_remaining} more — call link_inbox)")
    notice = "\n".join(lines)

    if event not in STRUCTURED_EVENTS:
        return notice
    return json.dumps({
        # Without a hookEventName that matches, the block is dropped and the
        # message is lost as surely as if it had gone to stdout.
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": notice,
        },
        # The agent gets the words; the person watching gets a one-liner, so a
        # message arriving mid-task is not invisible to them.
        "systemMessage": f"agent-link: {len(messages)} new message(s)",
    }, ensure_ascii=False)


def main() -> int:
    # Before anything imports the client, which resolves its daemon from here.
    home = home_from_argv(sys.argv)
    if home:
        os.environ["CLAUDE_LINK_HOME"] = home

    # The hook payload arrives on stdin. It names the event, which decides how
    # the answer has to be written -- but a malformed or absent payload must
    # never be worth failing over.
    event = None
    try:
        if not sys.stdin.isatty():
            payload = json.loads(sys.stdin.read() or "{}")
            if isinstance(payload, dict):
                event = payload.get("hook_event_name")
    except Exception:
        pass

    try:
        from link.client import ControlClient, LinkClientError
    except Exception:
        return 0

    client = ControlClient(timeout=TIMEOUT)
    try:
        # Fetch exactly what gets printed. Draining twenty and rendering three
        # would mark seventeen messages read that nobody ever saw -- and since
        # they are read, link_inbox would never return them either.
        resp = client.call("inbox", limit=MAX_SHOWN, include_system=False,
                           timeout=TIMEOUT)
    except LinkClientError:
        return 0  # daemon not running: stay silent and fast
    except Exception:
        return 0

    messages = (resp or {}).get("messages") or []
    notice = render_notice(messages, (resp or {}).get("unread_remaining") or 0, event)
    if notice:
        print(notice)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        raise SystemExit(0)
