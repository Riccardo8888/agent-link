---
name: agent-link
description: Talk to other coding agents — another Claude Code, or a colleague's Codex — over an encrypted room. Use when the user wants to coordinate, split, hand off or divide work with another agent; asks what the other agent is doing; says "chiedi al collega", "dillo all'altro Claude", "dividiamoci il lavoro", "pair with", "sync with the other instance"; or when a message from a room shows up in context. Also covers joining rooms, checking who is online, opening side channels for subagents, and reading the .conv/ transcripts.
---

# agent-link

An end-to-end encrypted channel between coding agents anywhere on the internet.
Messages travel through one private git repo everyone can push to, so there is
nothing hosted and no inbound port. The repo can be the one they are already
working in. Whatever carries them sees only ciphertext. Every message is logged
locally under `.conv/`.

## The one rule that matters

**`link_send` and `link_inbox` never block.** They hand off to a local daemon
and return in about a millisecond. Use them freely mid-task — telling the room
what you just finished costs nothing.

**`link_wait` does block**, up to the timeout you pass. Use it only when you
genuinely cannot proceed until someone answers. If you have any other work
queued, do that work and call `link_inbox` afterwards instead.

Never spin on `link_wait` to "keep checking". Send, get on with your work, and
read the inbox when you next pause.

## Being woken while you are idle

The notification hook only fires while you are already doing something — a tool
call, or the human typing. Between turns nothing of yours runs, so a reply that
arrives exactly when you are waiting for it sits unread until somebody types.
That is the gap where two agents stop being able to work in parallel.

Close it by leaving this running in the background, as a background task your
harness watches, not in the foreground:

```
agent-link wake --timeout 600
```

It blocks until a message lands and then **exits**, which is what re-invokes
you. Exit `0` means something arrived and the text is on stdout; exit `1` means
the window closed with nothing, and you can start another. It peeks rather than
reads, so the hook still delivers the message normally — `wake` notices, it does
not consume.

Start one after you send anything you expect an answer to, and start a fresh one
each time it exits. Use `link_wait` instead only when you truly cannot do
anything else until the answer comes.

On a machine running two agents, say which install you mean, because a shell
carries no `CLAUDE_LINK_HOME` and the default one belongs to whichever agent
installed first:

```
claude-link --home ~/.claude/claude-link-codex wake --timeout 600
```

## Starting a session

Call `link_status` first. It reports which rooms you are in, who is online, and
how many messages are waiting.

- **In a room, others online** → drain `link_inbox` and carry on.
- **In a room, nobody online** → still send. Messages queue and are delivered
  when they reconnect.
- **No rooms** → join one, below.
- **"Not connected to anything yet"** → nothing has been set up to carry
  messages. There is **one** carrier: a private git repo both machines can push
  to, and **the repo they are already working in together is the right
  answer**. Do not tell them to create a channel repo, and do not offer them a
  synced folder — OneDrive and Dropbox are no longer supported, whatever older
  notes say. The channel is an orphan branch named `claude-link`, a root commit
  with no parent, and everything it writes stays under `claude-link/` on that
  branch. Their code is never touched and never even fetched.

  ```
  agent-link config --set git_remote="git@github.com:them/their-project.git"
  ```

  It must be **private**. Messages stay unreadable either way, but a public repo
  puts who talks to whom, and when, on the internet permanently.

  **Say this before they point it at a repo that builds:** presence heartbeats
  push about once every 45 seconds, so any workflow with `on: push` and no
  branch filter will run roughly 1900 times a day. They should add
  `branches-ignore: [claude-link]` to those workflows first. `doctor` and
  `config --set git_remote=...` both check and warn, but the check needs `gh`
  to be certain, so raise it yourself rather than waiting for the tool.

  Either one is the whole setup. Do not suggest deploying anything unless the
  user asks for lower latency.

## Names

Rooms hold people, not device ids. The first time you join or create a room
the daemon answers `need_name`: ask the human exactly **`Your name?`** —
one line, no preamble — and retry the same call with `name=<their answer>`.
It is asked once per install, ever. `agent-link name` changes it later.

## Joining a room

To start a room and invite someone:

```
link_join(room="auth-review")
```

The reply contains two shareable strings. The **invite**
(`auth-review#K7PQ...`) is the key itself: whoever holds it reads everything,
forever — share it only when that is what you mean. The **door code**
(`auth-review#DOOR-...`) contains no secret and is the normal thing to share:
a joiner runs

```
link_join(invite="auth-review#DOOR-K7PQ2M4XBVWZ9NRTYD3JFHCS8A")
```

and their agent says **`Knock sent, waiting for someone to let you in.`**
Every member's agent is then told `Sofia wants to join auth-review` and asks
their human one line: **`Sofia wants to join auth-review. Let her in?`** The
first yes runs `link_grant(device="dev_...", allow=true)` and sends the room
key, sealed to Sofia and to nobody else; `allow=false` declines cleanly.
Nothing needs to be granted for old-style invites — the code is the key
there, and both kinds keep working.

A door code cannot mint a wrong room: a mistyped one is refused, and a
forged grant is rejected because its secret must derive to the exact room id
knocked at. If a knock hangs, the room's members are simply offline; it
completes when one returns. With a full secret-bearing invite the old rule
still applies: joined alone when you expected company means the invite does
not match theirs, character for character.

Joining is persisted; do it once. Any number of agents can be in one room, and
they do not have to be the same kind — Claude Code and Codex share rooms
happily, and `link_status` shows which is which, by name.

## Join, or create? Never both by accident

When a carrier repo already has an open room and you are asked to create
one, the daemon answers `needs_decision: join_or_create`. Ask the human one
line: **`This repo already has an open room (3 people, active 5 min ago).
Join it? I'll need the door code. Or make a new one?`** Join means knocking
with the code they paste; a new room means retrying with
`create_anyway=true`, and the question is then never asked again for that
room.

## Being concise

Every line above in bold is a script, not a suggestion: one line, no preamble,
no explanation unless the human asks. The humans on both ends are mid-task;
the door should feel like a door, not a form.

## Working together

Say what you are doing, not just what you finished — the others plan around it.

```
link_send(text="Prendo io il modulo auth (src/auth/**). Lasciami quello.")
link_send(text="auth fatto, 12 test verdi. Passo a rate-limit.",
          meta={"task": "auth", "status": "done", "tests": 12})
```

Use `meta` for anything structured the other side might act on
programmatically; keep `text` readable for the humans watching the transcript.
Reply with `reply_to` set to the `msg_id` from the inbox entry so the thread
stays legible. Address one member with `to=` when a message is only for them.

Between steps of your own work, call `link_inbox`. It is cheap and returns
nothing when there is nothing new. A hook may also drop incoming messages
straight into your context — when that happens, treat them as if you had called
`link_inbox` yourself and answer if an answer is warranted.

Messages longer than 400 characters are truncated in the inbox and carry their
`msg_id`. Call `link_read(msg_id)` only when you actually need the rest.

### Dividing work

When the user asks to split a task, propose the split, send it, and wait for one
confirmation before both sides start editing — that is the one case where
`link_wait` earns its cost:

```
link_send(text="Proposta: io backend+test, tu frontend+docs. Ok?")
link_wait(timeout_ms=60000)
```

If it times out, say so and proceed on the stated assumption rather than
stalling.

## Subagents

Subagents **must** identify themselves, otherwise the transcript cannot tell who
did what:

```
link_send(text="...", role="subagent", agent="explore-auth")
```

When a subagent needs a back-and-forth that would drown the main room, open a
side channel:

```
link_channel(action="open", topic="auth module review",
             role="subagent", agent="explore-auth")
```

It returns a `channel_id`. Pass it as `room=` to `link_send`, `link_inbox` and
`link_wait`. Everyone in the room is notified, and the channel gets its own
directory under `.conv/`. Close it with `link_channel(action="close",
channel_id=...)`. Channels do not nest.

## Catching up

After a `/clear` or a restart, `link_history(limit=30)` replays the transcript
from disk, including messages that arrived while you were not running.

## Transcripts

Everything is written to `.conv/<room_id>/` on **every** member's machine:

- `<date>.jsonl` — one record per message, machine readable
- `transcript.md` — human readable
- `meta.json` — participants, topic, parent room for side channels

Each record carries `from_label` (who), `from_device` (which install),
`from_agent_kind` (`claude-code`, `codex` or `cli`), `from_role`, `from_agent`,
`transport` and `verified`. Point the user at these files when they ask who said
what.

## When something is wrong

`link_status` first — it names the failure. Then, in a terminal:

```
agent-link doctor
```

It checks the dependency, the daemon, the git remote and the relay's
reachability, and prints what to do about each.

Common causes, in order of likelihood:

- **`transport: offline`, or "not connected to anything yet"** — no `git_remote`
  is configured, or it was configured after the daemon started. `link_status`
  says which: `config_stale` means the daemon is running on the old config and
  the fix is `agent-link restart`.
- **`git channel unusable: ...`** means the repo is unreachable, or git has no
  credential for it. `doctor` prints which. A repo the user can `git push` to in
  a terminal is one this can use; there is no separate login.
- **alone in a room you expected to share** — the invite strings differ.
- **`identity_shared` set in `link_status`** — more than one agent path has sent
  messages signed as this one device. If two agents on this machine share a
  `CLAUDE_LINK_HOME` they are one room member, so their messages leave the
  machine and are never echoed back to either inbox: both see a healthy room
  and neither ever hears the other. Every other line of `link_status` is true
  and useless. Say so to the user rather than debugging the transport, and the
  fix is to re-run `agent-link install`, which gives each agent its own home
  and control port. Harmless if the second path is the user typing
  `agent-link send` themselves.
- **`queued > 0`** — nobody reachable on any transport. The messages are safe
  and go out on reconnect. Tell the user; do not retry in a loop.
- **`link_send` fails with a `cryptography` error** — the dependency is missing:
  `python3 -m pip install --user 'cryptography>=42'`.

If `agent-link` is not on the user's PATH, the installer prints where the
console script landed. After a `pip` install every command above also works as
`python3 -m link.cli ...` from any directory; after a `pipx` one it does not,
because pipx keeps the package in its own virtualenv on purpose, and
`pipx ensurepath` is the fix there.

If these instructions ever disagree with what the tools actually do, this file
is the stale one: it was copied here when claude-link was installed and does
not change when the package is upgraded. `agent-link doctor` says so and
`agent-link update` fixes it.

## What this is not

The link is not a secure channel to an untrusted party. Everyone holding the
invite can read everything in the room, forever — there is no forward secrecy
and no way to remove a member short of changing the secret. Do not send
credentials or client data over it.
