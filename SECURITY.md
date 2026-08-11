# Reporting a vulnerability

**Do not open a public issue for a security problem.** Use GitHub's private
reporting: the **Security** tab, then **Report a vulnerability**. That opens a
thread only the maintainers can see.

Tell us what you can reach and how you reached it. A rough sketch is worth
sending; it does not have to be a working exploit.

This is a side project, not a funded one. You will get an acknowledgement, and
an honest answer about whether and when it will be fixed. If a report goes
unanswered for two weeks, assume it was missed rather than ignored, and say so
in the same thread.

## What the project claims

The claims worth attacking, stated as things to falsify. In short:

- Whatever carries the messages is assumed hostile. It sees ciphertext, room and
  device ids, sizes and timing, and nothing else.
- A room member cannot forge another member, and cannot reseal a message into a
  different room.
- The control socket on 127.0.0.1 is authenticated. Anything else on the machine
  that can reach it should still be refused.
- A device id supplied by a relay never becomes a filesystem path.
- Text a peer wrote reaches a model's context as fenced, marked data.

Something that breaks one of those is a vulnerability. So is anything that gets
code execution out of a config value, a frame, or a remote URL.

## What is a known limit rather than a bug

These are design decisions, documented in the README, and reports about them
will be closed as such:

- **No forward secrecy.** One long-lived room key. Anyone holding the invite can
  read that room's past and future until the secret changes.
- **No way to remove a member** except changing the secret and re-inviting.
- **The carrier learns the social graph**: which devices talk to which rooms,
  when, and how much. A git host keeps it. This is why the repo must be private.
- **Transcripts are plain text** in `.conv/` on every member's machine.
- **A room is for people who already trust each other.** Every member can read
  everything in it. Credentials and client data do not belong in one.
- **A hostile carrier can replay old messages.** Duplicates are dropped
  through a bounded window (a 2000-entry `msg_id` LRU), so a carrier that
  re-delivers something older can make it read again as it did the first
  time. The obvious fix — rejecting frames whose timestamp is far from local
  time — silently drops all traffic between machines whose clocks disagree,
  which is worse, so it is deliberately absent until there is an answer for
  what happens when the check fires. Audit item C7, open on purpose.

### The door (knock/grant)

A door code contains no key material. Anyone with write access to the
carrier repo can deny service (delete knocks or grants) and can plant a fake
door entry to learn a knocker's chosen display name. They cannot read room
content, impersonate a member to a verifying reader, or admit anyone: a
grant's secret must derive to the exact room id knocked at, which requires
already holding the room secret. Names travel only sealed; presence files
and door entries on the carrier stay pseudonymous by design.

## Versions

`main` only. Tags mark installable states; the newest one is the only
version that gets fixes, and there is nothing to backport to.

## Where things have gone wrong before

The defects that cost real time include the five an independent audit found
on 2026-08-07: an unauthenticated
control socket, a remote URL that was a code-execution primitive, relay-supplied
ids used as paths, a relay cursor any member could poison, and remote text
reaching the model unfenced. They are fixed. They are listed because the shape
of a past mistake is the best guide to where the next one is.
