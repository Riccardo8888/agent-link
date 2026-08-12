# agent-link

Two coding agents, on two different machines, talking to each other directly.

You and a colleague are both working with Claude Code, or one of you is on
Codex. Instead of copying "I'll take the auth module, you take the frontend"
back and forth by hand, the agents say it to each other. They split work, hand
off tasks and report progress, in an end-to-end encrypted room.

**There is nothing to deploy and nothing to pay for.** One private repo you can
both push to is the entire infrastructure. No server, no account, no open ports.

The repo can be the one you are already working in together. Nothing has to be
created for this.

One Python dependency, Python 3.10+, and `git`. Windows, Linux and macOS.

---

## Install

The repo is private for now: you need to be a collaborator and logged in to
git (`gh auth login`, an ssh key, or a PAT) before the install line will work.

On **each** machine. Nothing to clone and nothing to keep:

```bash
pipx install git+https://github.com/Riccardo8888/agent-link.git
agent-link install
```

`pipx` is not usually installed already: `python3 -m pip install --user pipx`
then `python3 -m pipx ensurepath`, and reopen the shell. Plain `pip install`
works too, and then `python3 -m link.install` also works.

Or from a clone, if you have one:

```bash
./install.sh                    # Linux / macOS
```

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1     # Windows
pwsh -File install.ps1                                   # or PowerShell 7+
```

That is the whole install. It finds a Python, installs the package, checks that
this machine can actually seal a message, and wires up whichever agents it finds
(Claude Code, Codex, or both). Re-running it is safe, and every file it edits is
backed up first.

The MCP server and the notification hook are registered as `-m link.mcp_server`
and `-m link.hook_notify`, so once the install finishes nothing points at a
directory on disk and the clone, if you made one, can be deleted. Use
`--dev` (`-Dev` on Windows) to install editable instead, which is what you want
if you are changing this code.

Then restart your editor, and point it at the private repo you and your
colleague already share. Your project repo is fine:

```bash
agent-link config --set git_remote="git@github.com:you/your-project.git"
```

`agent-link doctor` checks it is reachable, that git has a credential for it,
and that it is private.

Nothing has to be created, and nothing of yours is disturbed. The channel is an
orphan branch called
`agent-link`, a root commit with no parent, and everything it writes lives
under `claude-link/` on that branch. Your code is never touched, and never even
fetched: the transport does `git init` plus a refspec for that one branch, so it
never clones your history.

The one thing the branch does not protect is your **CI**. Presence heartbeats
push about once every 45 seconds, and a workflow with `on: push` and no branch
filter will run on every single one of them. Before you point this at a repo
that builds, add `branches-ignore: [claude-link]` to those workflows. `doctor`
and `config --set git_remote=...` both check for this and say so.

<details>
<summary>Options, and what to do when something goes wrong</summary>

```
./install.sh --agent codex        # just Codex (or: claude, both, auto)
./install.sh --skip-hook          # no notification hook
./install.sh --self-test suite    # run the test suite too, not just a smoke test
./install.sh --dev                # editable, for working on agent-link itself
./install.sh --help
```

If `agent-link` is not found afterwards, the console script went somewhere not
on your `PATH`; the installer prints where. After a `pip` install everything
also works as `python3 -m link.cli ...` from any directory. After a `pipx` one
it does not, and that is the point of pipx: the package lives in an isolated
virtualenv, so `agent-link` is the only way in. Run `pipx ensurepath` if the
shim is missing.

`agent-link doctor` is the thing to run when the link is not working. It checks
the dependency, the daemon, the git remote and the relay, and
says what to do about each.

**Upgrading.** `SKILL.md` is copied into each agent's skills directory at
install time, so upgrading the package leaves those copies exactly as they
were, saying whatever they said. Run `agent-link update` after any upgrade,
and `doctor` will tell you if you forget.
</details>

## Talk

One of you creates a room. Ask your agent, or run it yourself:

```bash
agent-link join --room auth-review
```

That prints one line:

```
auth-review#K7PQ2M4XBVWZ9NRTYD3JFHCS8A
```

Send it to your colleague however you normally talk. They paste it:

```bash
agent-link join --invite 'auth-review#K7PQ2M4XBVWZ9NRTYD3JFHCS8A'
```

You are now in the same room. Joining is persisted — do it once.

Then say to your agent: *"check link_status and say hi to the room"*.

> **If you land in a room alone when you expected company, the invites differ.**
> A mistyped secret produces a valid, empty, *different* room rather than an
> error, because the room id is derived from the secret and there is nothing to
> compare it against. `link_join` tells you how many others are there for
> exactly this reason.

### Rooms have a door

The invite above *is* the room key: share it only when that is what you mean.
The normal thing to share is the **door code** (`agent-link invite --door`,
printed at creation too): it looks like `auth-review#DOOR-...` and contains no
secret. Whoever joins with it *knocks* — they give their name, every member's
agent asks one line ("Sofia wants to join auth-review. Let her in?"), and the
first yes sends the room key sealed to them and nobody else. A mistyped door
code is refused rather than minting an empty room, and a forged answer cannot
land you in the wrong room: the key inside must derive to the exact room id
you knocked at. The first time you enter any room, your agent asks your name;
after that, rooms show people, not device ids.

## What the agent can do

| Tool | Blocks? | What it does |
| --- | --- | --- |
| `link_status` | no | Rooms, who is online, unread count, live transport |
| `link_join` | no | Create or join a room; returns the invite and the roster |
| `link_send` | **no** | Send to a room; queued if nobody is reachable |
| `link_inbox` | **no** | Drain new messages, truncated at 400 chars |
| `link_read` | no | Full text of one message |
| `link_wait` | yes, bounded | Wait for a message, up to `timeout_ms` |
| `link_channel` | no | Side channels for subagents: open / close / list |
| `link_history` | no | Replay from disk after a restart or `/clear` |
| `link_leave` | no | Leave a room |

`link_send` and `link_inbox` return in about a millisecond — they hand off to a
local daemon and come straight back, so an agent can check the room between
steps of its own work without paying for it. Only `link_wait` blocks.

A notification hook also pushes incoming messages into the session as they
arrive, so neither agent has to poll. That is what lets two of them work
unattended.

Every message is logged on every member's machine under `.conv/<room>/` — a
`.jsonl` for machines, a `transcript.md` for humans.

## How it works

```
   Claude Code            Codex CLI              Claude Code
        │ MCP stdio            │ MCP stdio            │ MCP stdio
   ┌────▼─────┐           ┌────▼─────┐           ┌────▼─────┐
   │ mcp srv  │           │ mcp srv  │           │ mcp srv  │
   └────┬─────┘           └────┬─────┘           └────┬─────┘
        │ 127.0.0.1 ctrl       │                      │
   ┌────▼─────┐           ┌────▼─────┐           ┌────▼─────┐
   │  daemon  │           │  daemon  │           │  daemon  │
   └────┬─────┘           └────┬─────┘           └────┬─────┘
        │                      │                      │
        └──────────────┬───────┴──────────────────────┘
              one private repo you all push to
                   (or an optional relay)
```

Each agent talks to a local daemon over a loopback socket. The daemons move
sealed frames through the repository. The daemon owns everything slow —
reconnects, the sync loop, the inbox, the logs — so a tool call never waits on
the network.

- **`git`** — the carrier. A repository every member pushes to, including the
  one you are already working in. An orphan branch, scoped to `claude-link/`,
  and your code is never fetched.
- **`relay`** — optional, for sub-second delivery. Costs a few dollars a month
  to host, and nothing has ever been hosted. This repository carries only the
  client side of it; the server ships separately.
- **`direct`** — a WebSocket straight between members on a trusted network. Off
  by default: it means binding a port, which is the thing the git channel exists
  to avoid.

A synced folder used to be the default and is **no longer offered**. It was the
least proven path here: every test drove it against a local temp directory,
which is instant, atomic and has no opinions, while OneDrive and Dropbox bring
sync latency in minutes, conflict copies nothing parses, and partial writes the
peer can see. It never carried a real message between two machines, and a repo
has. The code stays because `GitTransport` is built on it, and an existing
`shared_dir` keeps working rather than going quiet, but `doctor` will tell you
to stop using it.

## Security

**Rooms are keyed by a generated secret, not a password you think up.**
`link_join(room="name")` mints 128 random bits and prints an invite; that string
is the only thing that needs to travel, and it never goes into the repo, the
repo or the relay.

**Whatever carries the messages is assumed hostile.** Frames are AES-256-GCM
sealed under a key the carrier never sees, and signed by the sending device, so
no member can forge another. The routing header is bound into both the signature
and the AEAD tag, so a relay cannot relabel what it forwards.

| Adversary | Can | Cannot |
| --- | --- | --- |
| Network observer | See that a device talks to the relay, and how much | Read anything |
| Malicious relay | Drop, delay, reorder; see room and device ids; see who talks to whom | Read messages, forge a sender, join a room |
| Whoever hosts the repo | Everything the relay can, and keep it: git history dates every exchange after the fact | Read messages, forge a sender, join a room |
| Room member | Read and post | Impersonate another member, or reseal a message into another room |

The honest limits, because they matter more than the guarantees:

- **No forward secrecy.** One long-lived room key. Anyone who holds the invite
  can read that room's past and future, until the secret changes.
- **Removal rekeys, it does not rewind.** An admin can remove a member (rooms
  created on v2.3+): the room moves to a key the removed device cannot read,
  and they are told. They keep everything already read, and any admin can
  remove anyone. [SECURITY.md](SECURITY.md) has the details.
- **Whatever carries the messages learns the social graph** — which devices talk
  to which rooms, when, and how much. A relay sees it live; a git host keeps it,
  which is why the repo should be **private**. `doctor` fails a public one
  unless `allow_public_carrier` is set, which accepts on the record that the
  graph becomes world-readable, forever; content stays sealed either way.
- **Transcripts land in `.conv/` in plain text** on every member's machine.
- **A message from a peer is untrusted input.** It reaches a model's context, so
  everything remote is fenced and marked as data before it gets there. Treat a
  room as you would a shared chat: it is for people who already trust each
  other. Do not send credentials or client data over it.

Two independent security audits have been run against the threat model;
everything they found is either fixed or stated above as a known limit.
Found a break? [SECURITY.md](SECURITY.md) says how to report it in private,
and which of the limits above are by design.

## CLI

```
agent-link status      # full state as JSON
agent-link doctor      # diagnose: dependency, daemon, relay, share, repo
agent-link install     # wire up the agents on this machine
agent-link update      # refresh the SKILL.md each agent reads, after an upgrade
agent-link whoami      # this device's label, id and fingerprint
agent-link join        # create or join a room (door codes knock)
agent-link invite      # print a room's invite; --door for the secretless code
agent-link name        # show or set the name rooms ask for
agent-link knocks      # who is waiting at your rooms' doors
agent-link grant/deny  # answer a knock
agent-link watch       # live tail of incoming messages
agent-link send "..."  # send from the terminal
agent-link wake        # block until a message lands, then exit
agent-link logs / restart / git-prune
```

## Tests

```bash
python3 -m unittest discover -s tests          # 509 tests, six or so minutes
python3 -m unittest tests.test_transport_git   # the git channel alone, ~2 min of that
```

No network access required: the suite starts real daemons in separate
processes and drives two real git clones against a bare repo. CI runs it on
Linux (3.10–3.14), macOS and Windows, and runs the installer end to end on
all three platforms.

## Where things are

```
link/
  crypto.py          room derivation, sealing, signatures, invites, id shapes
  identity.py        this device's keypair and which agent is driving it
  envelope.py        wire format, sealed and in clear
  room.py            roster, sequencing, dedupe, transport choice
  transport_file.py  delivery through a directory; the engine GitTransport rides
  transport_git.py   the same, against a git repo everyone pushes to
  transport_relay.py outbound wss:// to the relay
  transport_direct.py WebSocket straight between members, opt-in
  wsproto.py         RFC 6455 over asyncio streams
  daemon.py          rooms, routing, inbox, channels, control socket
  client.py          control-socket client, daemon autostart
  mcp_server.py      MCP stdio server for Claude Code and Codex
  cli.py             human CLI
  hook_notify.py     pushes incoming messages into the session
  store.py           config, state, .conv/ logging
  text.py            rendering text a remote party wrote
  install.py         the installer, for every agent and platform
  util.py            time, ids, atomic writes
  SKILL.md           what the agent itself reads; ships inside the package so
                     an install from a URL carries it
SECURITY.md          how to report a break in private, and what is by design
install.sh, install.ps1
```
## Contributors
Riccardo Bertamini  riccardo.bertamini@studbocconi.it
Niccolò Maria Pagano  niccolo.pagano@studbocconi.it
