"""Command line for humans: join rooms, send, watch, diagnose.

    python -m link.cli status
    python -m link.cli join --room auth-review          # creates it, prints the invite
    python -m link.cli join --invite 'auth-review#K7PQ...'
    python -m link.cli send "ho finito il modulo auth"
    python -m link.cli watch
    python -m link.cli doctor
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from link import __version__                             # noqa: E402
from link import install                                 # noqa: E402
from link.client import ControlClient, LinkClientError   # noqa: E402
from link.store import (                                 # noqa: E402
    config_value_ok,
    daemon_log_path,
    home_conv_dir,
    load_config,
    read_daemon_info,
    root_dir,
    save_config,
)
from link.util import cli_invocation, local_ips, shell_quote   # noqa: E402

client = ControlClient()


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def cmd_start(args) -> int:
    info = client.ensure_daemon()
    print("daemon started" if info.get("started") else "daemon already running")
    _dump(read_daemon_info())
    return 0


def cmd_stop(args) -> int:
    try:
        client.call("shutdown", timeout=2.0)
        print("shutdown requested")
    except LinkClientError as exc:
        print(f"not running ({exc})")
    return 0


def cmd_restart(args) -> int:
    cmd_stop(args)
    time.sleep(1.0)
    return cmd_start(args)


def cmd_status(args) -> int:
    client.ensure_daemon()
    _dump(client.call("status", verbose=True))
    return 0


def cmd_whoami(args) -> int:
    from link import identity

    me = identity.load()
    cfg = load_config()
    print("This device:\n")
    print(f"  label       : {me.label}")
    print(f"  device_id   : {me.device_id}")
    print(f"  agent kind  : {me.agent_kind}")
    print(f"  fingerprint : {me.fingerprint()}")
    print(f"  relay       : {cfg.get('relay_url')}")
    print(f"  hostname    : {socket.gethostname()}")
    print(f"  local IPs   : {', '.join(local_ips())}")
    print("\nTo talk to someone, share a room invite, not these details:")
    print(f"  {cli_invocation()} join --room our-room")
    return 0


def cmd_join(args) -> int:
    client.ensure_daemon()
    resp = client.call("join", room=args.room, invite=args.invite,
                       name=args.name, create_anyway=args.create_anyway,
                       passphrase=args.passphrase, shared_dir=args.shared_dir,
                       git_remote=args.git_remote, git_branch=args.git_branch,
                       relay_url=args.relay, timeout=60.0)
    if resp.get("need_name"):
        print("Your name? Rooms hold people, not device ids. Retry with "
              f"--name <your-name>, or set it once: {cli_invocation()} name "
              "<your-name>", file=sys.stderr)
        return 1
    if resp.get("needs_decision") == "join_or_create":
        room = (resp.get("open_rooms") or [{}])[0]
        print(f"This repo already has an open room "
              f"({room.get('members', '?')} member(s)). Join it with its door "
              f"code ({cli_invocation()} join --invite <code>; a member "
              f"prints it with `invite --door`), or re-run with "
              f"--create-anyway for a separate room.", file=sys.stderr)
        return 1
    if not resp.get("ok"):
        print(f"error: {resp.get('error')}", file=sys.stderr)
        return 1
    if resp.get("knocked"):
        print(f"knock sent at {resp.get('room')} — waiting for a member to "
              f"let you in. You'll see it in status and the inbox when they "
              f"answer.")
        return 0
    print(f"room    : {resp['room']}")
    print(f"invite  : {resp['invite']}")
    print(f"online  : {resp.get('members_online', 0)} other member(s)")
    print(f"transport: {resp.get('transport')}")
    if resp.get("warning"):
        print(f"note    : {resp['warning']}")
    if resp.get("relay_error"):
        print(f"relay   : not connected — {resp['relay_error']}")
    if not resp.get("members_online"):
        print("\nShare the invite line above. Whoever pastes it into their own "
              "`join` lands in the same room.")
    return 0


def cmd_invite(args) -> int:
    """Print a room's invite string, or its door code with --door.

    Its own command because `status` no longer carries it: the invite is the
    room's master secret, and a diagnostic that returns it puts that secret into
    every transcript that ever recorded a status call. The door code is the
    opposite kind of string — no secret at all — and is the normal one to share.
    """
    client.ensure_daemon()
    resp = client.call("invite", room=args.room)
    if not resp.get("ok"):
        print(f"error: {resp.get('error')}", file=sys.stderr)
        return 1
    if args.door:
        print(resp["door"])
        print("share this door code — it contains no secret; you approve "
              "each join", file=sys.stderr)
    else:
        print(resp["invite"])
    return 0


def cmd_name(args) -> int:
    """Show or set the display name rooms will ask for."""
    from link.store import display_name_set

    if not args.value:
        if display_name_set():
            print(load_config().get("display_name"))
        else:
            print("no name set — rooms will ask for one. Set it: "
                  f"{cli_invocation()} name <your-name>")
        return 0
    chosen = args.value.strip()[:40]
    client.ensure_daemon()
    resp = client.call("config", set={"display_name": chosen})
    if not resp.get("ok"):
        print(f"error: {resp.get('error')}", file=sys.stderr)
        return 1
    print(f"you are {chosen}")
    return 0


def cmd_knocks(args) -> int:
    client.ensure_daemon()
    resp = client.call("knocks")
    if not resp.get("ok"):
        print(f"error: {resp.get('error')}", file=sys.stderr)
        return 1
    if not resp.get("knocks"):
        print("nobody is at the door")
        return 0
    for k in resp["knocks"]:
        print(f"{k.get('name') or '?'} [{k['device_id']}] wants to join "
              f"{k['room']} — {cli_invocation()} grant {k['device_id']}")
    return 0


def cmd_grant(args) -> int:
    client.ensure_daemon()
    resp = client.call("grant", device=args.who, allow=not args.deny,
                       room=args.room)
    if not resp.get("ok"):
        print(f"error: {resp.get('error')}", file=sys.stderr)
        return 1
    who = resp.get("name") or resp.get("device")
    print(f"{who} is in." if resp.get("granted") else f"Told {who} no.")
    return 0


def cmd_role(args) -> int:
    client.ensure_daemon()
    resp = client.call("role", device=args.who, role=args.role, room=args.room)
    if not resp.get("ok"):
        print(f"error: {resp.get('error')}", file=sys.stderr)
        return 1
    print(f"{args.who} is now {resp['role']}.")
    return 0


def cmd_remove(args) -> int:
    client.ensure_daemon()
    resp = client.call("remove", device=args.who, room=args.room, timeout=30.0)
    if not resp.get("ok"):
        print(f"error: {resp.get('error')}", file=sys.stderr)
        return 1
    note = resp.get("note") or ""
    print(f"Removed {args.who}. {note}".strip())
    return 0


def cmd_leave(args) -> int:
    client.ensure_daemon()
    _dump(client.call("leave", room=args.room))
    return 0


def _timeout_arg(text: str) -> float:
    """Seconds to wait for the daemon: a positive, finite number.

    argparse would otherwise hand `inf` (or `1e400`) straight to
    socket.settimeout, which raises OverflowError from deep inside the client
    and escapes as a traceback, and would let `0` through as "give up
    immediately", which puts the socket in non-blocking mode and reports a
    confusing errno rather than a wait.
    """
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number")
    if value != value or value in (float("inf"), float("-inf")):
        raise argparse.ArgumentTypeError("timeout must be a finite number")
    if value <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return value


def cmd_send(args) -> int:
    client.ensure_daemon()
    # A git-backed send pushes to a remote before the daemon can answer, which
    # routinely exceeds the 3 s control default: the CLI reported "daemon did
    # not answer within 3.0s" for sends that had in fact gone out.
    _dump(client.call("send", text=args.text, room=args.room,
                      role=args.role, agent=args.agent,
                      timeout=float(getattr(args, "timeout", 30.0) or 30.0)))
    return 0


def cmd_read(args) -> int:
    """The full text of one message by id -- the CLI half of `link_read`.

    `inbox` truncates each entry at PREVIEW_CHARS. Without this command someone
    driving agent-link from a shell can see the first 400 characters of a
    message and has no supported way to read the rest.
    """
    client.ensure_daemon()
    resp = client.call("read", msg_id=args.msg_id, timeout=30.0)
    if not resp.get("ok"):
        print(f"error: {resp.get('error')}", file=sys.stderr)
        return 1
    message = resp.get("message") or {}
    if getattr(args, "json", False):
        _dump(resp)
    else:
        print(message.get("text", ""))
    return 0


def cmd_inbox(args) -> int:
    client.ensure_daemon()
    _dump(client.call("inbox", room=args.room, limit=args.limit, peek=args.peek,
                      include_system=True))
    return 0


def cmd_watch(args) -> int:
    """Live tail: long-poll the daemon and print each message as it lands."""
    client.ensure_daemon()
    print("watching for messages (Ctrl+C to stop)...")
    try:
        while True:
            resp = client.call("wait", timeout_ms=25000, room=args.room,
                               include_system=True, timeout=30.0)
            for m in resp.get("messages", []):
                where = m.get("channel") or m.get("room")
                print(f"[{m.get('received_at', '')[11:19]}] {where} "
                      f"{m.get('from')} ({m.get('from_agent_kind')}/"
                      f"{m.get('from_agent')}) via {m.get('transport')}: "
                      f"{m.get('text')}")
    except KeyboardInterrupt:
        print("\nstopped")
    except LinkClientError as exc:
        print(f"error: {exc}")
        return 1
    return 0


def render_wake(resp: dict) -> tuple[str, int]:
    """What `wake` prints and exits with, given one `wait` response.

    The exit code is the signal the harness acts on, so the two outcomes have
    to be distinguishable: 0 means a message is waiting and the agent should
    look, 1 means the window closed with nothing. Both re-invoke the agent;
    only one of them is news. Neither is silent -- an exit with no output is
    indistinguishable from a crash.
    """
    messages = resp.get("messages") or []
    if not messages:
        return ("agent-link: no message arrived within the watch window", 1)

    lines = [f"agent-link: {len(messages)} new message(s) waiting"]
    for m in messages:
        where = m.get("channel") or m.get("room")
        who = m.get("from") or m.get("from_device") or "?"
        kind = m.get("from_agent_kind")
        lines.append(f"  {where} {who}"
                     + (f" ({kind})" if kind else "")
                     + f": {m.get('text', '')}")
    return ("\n".join(lines), 0)


def cmd_wake(args) -> int:
    """Block until a message lands, then exit so the harness wakes the agent.

    `watch` tails forever, which is right for a human at a terminal and useless
    to an agent: nothing re-invokes a model while a process keeps running. This
    is the same long poll, ended at the first message.

    The wait peeks. The notification hook fetches the same inbox moments later
    and fetching marks messages read, so consuming here would wake the agent to
    an inbox that no longer holds what woke it.
    """
    client.ensure_daemon()
    resp = client.call("wait", timeout_ms=int(args.timeout * 1000), room=args.room,
                       peek=True, include_system=False,
                       timeout=args.timeout + 30.0)
    text, code = render_wake(resp)
    print(text)
    return code


def stale_lines(drift: dict | None) -> list[str]:
    """What `doctor` prints about a daemon running on an out-of-date config.

    Nothing at all in the ordinary case. `doctor` is read when something is
    wrong, and a line it prints every single time is a line nobody reads by the
    third run.
    """
    if not drift:
        return []
    changed = ", ".join(drift.get("affects_transport")
                        or drift.get("changed") or [])
    lines = [f"config      : STALE — the daemon started before the current "
             f"config.json ({changed})"]
    if drift.get("affects_transport"):
        lines.append("              it is still using the old transport "
                     "settings, so what")
        lines.append("              you configured is not what is running.")
    lines.append(f"              {drift.get('fix', 'restart the daemon')}")
    return lines


DOCTOR_LOAD_WAIT_S = 12.0     # above a cold start, below anybody's patience


def status_when_loaded(timeout: float = DOCTOR_LOAD_WAIT_S,
                       poll: float = 0.25) -> tuple[dict, bool]:
    """`status`, waited out until the daemon has finished loading its rooms.

    `doctor` starts a daemon when none is running, which is the ordinary case,
    and for a second or two after the control port opens the rooms exist but
    their transports have not attached. `_op_status`'s own readiness gate is
    deliberately bounded, so it answers anyway and sets `loading` to say the
    answer is partial.

    Waiting is the right trade here and nowhere else: `doctor` is not on a
    latency budget, and a correct line beats a fast one. Bounded, because a
    wedged transport must not be able to make the diagnostic hang, which is the
    same reason the gate underneath it is bounded. Returns whether it cleared,
    because "still loading after twelve seconds" is itself a symptom and has to
    be said rather than dressed up as a result.
    """
    deadline = time.monotonic() + timeout
    status = client.call("status", verbose=True)
    while status.get("loading") and time.monotonic() < deadline:
        time.sleep(poll)
        status = client.call("status", verbose=True)
    return status, not status.get("loading")


def room_lines(status: dict, loaded: bool) -> list[str]:
    """What `doctor` prints about the rooms, given whether it can be sure.

    An empty list from a daemon that has finished loading means no rooms. From
    one that has not, it means nothing at all, and the old text told you to
    `join --room <name>` -- which, run against a machine that is already in a
    room, is advice to create a second one.
    """
    rooms = status.get("rooms") or []
    if not loaded:
        lines = [f"rooms       : STILL LOADING after {DOCTOR_LOAD_WAIT_S:.0f}s, "
                 f"so the lines below are provisional"]
        lines.append("              a room shows `offline` until its transport "
                     "attaches, and")
        lines.append("              key derivation is slow on purpose. If this "
                     "does not clear,")
        lines.append(f"              the transport is wedged: {cli_invocation()} logs")
        for room in rooms:
            lines.append(f"room        : {room['room']} "
                         f"transport={room['transport']} (still loading)")
        return lines

    if not rooms:
        return ["rooms       : none — run `join --room <name>`"]
    return [f"room        : {room['room']} transport={room['transport']} "
            f"{room['online']}/{room['members']} online queued={room['queued']}"
            for room in rooms]


def identity_lines(shared: dict | None) -> list[str]:
    """What `doctor` prints when two agent paths have signed as one device.

    Nothing in the ordinary case, for the same reason as above. When it does
    print, it prints without calling it a fault: one person with one agent who
    also types `agent-link send` produces exactly this, and so does the hour
    of silence in docs/postmortems.md. The difference is something only the
    reader can see, so the reader gets the fact and the check.
    """
    if not shared:
        return []
    indent = " " * 14
    return [
        f"identity    : shared by {', '.join(shared['kinds'])}",
        indent + _wrap(shared["problem"] + ".", indent),
        indent + _wrap(shared["fix"], indent),
    ]


def config_saved_message(drift: dict | None) -> str:
    """What to say after a `config --set`, given the running daemon's own view.

    This used to read "saved; restart the daemon for transport changes to take
    effect", printed unconditionally -- including when no daemon was running and
    there was nothing to restart. Printed every time, it reads as boilerplate,
    and it is the load-bearing sentence in the one case that matters.
    """
    if not drift:
        return "saved."
    changed = ", ".join(drift.get("affects_transport") or drift.get("changed") or [])
    # `cli_invocation` rather than the bare name: this is the load-bearing
    # sentence under a room that has gone quiet, and the reader is about to
    # paste the command in it.
    restart = f"`{cli_invocation()} restart`"
    if drift.get("affects_transport"):
        return (f"saved, but the running daemon is still using the old {changed}. "
                f"Until you run {restart}, nothing you just "
                f"configured is in effect.")
    return (f"saved. The running daemon still holds the previous {changed}; "
            f"{restart} picks it up.")


def cmd_config(args) -> int:
    cfg = load_config()
    if args.set:
        for pair in args.set:
            key, _, value = pair.partition("=")
            key = key.strip()
            if key not in cfg:
                print(f"unknown key: {key}")
                return 1
            if isinstance(cfg[key], bool):
                cfg[key] = value.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(cfg[key], int):
                try:
                    cfg[key] = int(value)
                except ValueError:
                    print(f"{key} takes a whole number, not {value.strip()!r}")
                    return 1
            elif isinstance(cfg[key], list):
                # A list setting used to fall through to the string branch, so
                # `log_sinks=myproj` stored the string, and `ConvLogger` then
                # iterated it and fanned out to one directory per character.
                cfg[key] = [p for p in value.split(os.pathsep) if p.strip()]
            else:
                cfg[key] = value.strip() or None
            if not config_value_ok(key, cfg[key]):
                print(f"{key} cannot be {cfg[key]!r}")
                return 1
        save_config(cfg)
        # Ask the daemon rather than guess, and do not start one: a config write
        # is not a reason to launch a process, and "no daemon answered" is
        # exactly the case where there is nothing to restart.
        try:
            status = client.call("status")
        except LinkClientError:
            status = {}
        drift = status.get("config_stale")
        print(config_saved_message(drift))
        # The moment somebody points this at a repository is the moment to say
        # what it will do to that repository's CI. Afterwards it is their
        # colleague's Actions bill that reports it.
        if any(pair.partition("=")[0].strip() == "git_remote" for pair in args.set):
            from link.transport_git import DEFAULT_BRANCH, shared_repo_warning

            note = shared_repo_warning(
                cfg.get("git_remote") or "",
                cfg.get("git_branch") or DEFAULT_BRANCH,
                presence_s=float(cfg.get("git_presence_s", 45)),
            )
            if note:
                print("\nNOTE: " + _wrap(note, "      "))
            # The other thing worth saying at this exact moment: somebody may
            # already be talking on that repo. One advisory line, never an
            # error, and only when this install is in no room of its own.
            if cfg.get("git_remote") and not status.get("rooms"):
                from link.discover import discover_rooms

                found = discover_rooms(
                    cfg["git_remote"],
                    cfg.get("git_branch") or DEFAULT_BRANCH) or []
                if found:
                    room = found[0]
                    print(f"\nThis repo already has an open room "
                          f"({room['members']} member(s)). Join it with a door "
                          f"code ({cli_invocation()} join <code>), or create "
                          f"your own room.")
    _dump(cfg)
    return 0


def cmd_logs(args) -> int:
    path = daemon_log_path()
    if not os.path.exists(path):
        print(f"no log yet at {path}")
        return 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    for line in lines[-args.lines:]:
        print(line.rstrip())
    return 0


def _doctor_skill() -> tuple[list[str], bool]:
    """Is the SKILL.md each agent reads the one this version ships?

    No agent reads the file inside the package. The installer copies it into
    `~/.claude/skills/claude-link/` and `~/.codex/skills/claude-link/`, and from
    that moment those copies are snapshots: upgrading rewrites the original and
    leaves them saying whatever they said.

    Nothing checked, so the only symptom was an agent confidently recommending
    something this version had removed, which is close to untraceable from the
    outside. `doctor` is the right place for it, because it is what people run
    when the thing is behaving oddly.
    """
    stale = install.stale_skills()
    if not stale:
        return [], True

    lines = [f"skill       : OUT OF DATE ({len(stale)} "
             f"cop{'y' if len(stale) == 1 else 'ies'})"]
    for path in stale:
        lines.append(f"              {path}")
    lines += [
        "              An agent reads its own copy, not the one inside",
        f"              agent-link {__version__}, and these no longer match. It was",
        "              copied when you installed and nothing has updated it since,",
        "              so the agent may be acting on instructions this version no",
        "              longer supports. Refresh them with:",
        f"                {cli_invocation()} update",
    ]
    return lines, False


def cmd_install(args) -> int:
    """Run the installer through the console script.

    The README leads with `pipx install ...` followed by
    `python3 -m link.install`, and that second line has never worked: pipx puts
    the package in an isolated virtualenv by design, so no system interpreter
    can import `link`. Checked on 2026-08-09 with a real pipx and a throwaway
    PIPX_HOME -- the install succeeds, `agent-link` appears in the bin
    directory, and the next line dies on `No module named 'link'`.

    The console script is the only thing pipx exposes, so this is the way in.
    Every flag belongs to the installer's own parser and is handed over
    untouched, so the two cannot drift.
    """
    return install.main(list(args.installer_args))


def cmd_update(args) -> int:
    """Refresh what the installer copied, and be clear about what it cannot.

    Two things go out of date here and only one of them belongs to this
    program. The package is whatever pip or pipx put on the machine and
    upgrading it is theirs; the SKILL.md copies are ours, and nothing was
    keeping them current.
    """
    was_stale = set(install.stale_skills())
    refreshed = install.refresh_skills()

    if not refreshed:
        print("No agent-link skill is installed for any agent on this machine,")
        print("so there is nothing here to refresh. That is what the installer")
        print("is for, and it wires up the rest as well:")
        print(f"    {shell_quote(sys.executable)} -m link.install")
        return 1

    for path in refreshed:
        print(f"  {'updated' if path in was_stale else 'current'}  {path}")

    print()
    print("That is the copy each agent reads. The package itself came from your")
    print("package manager and this command does not run it for you:")
    print("    pipx upgrade agent-link")
    print("    pip install --upgrade "
          "git+https://github.com/Riccardo8888/agent-link.git")
    print("Run this again afterwards, so the agents pick up the new instructions.")
    return 0


def cmd_doctor(args) -> int:
    """Pre-flight checks, in the order things actually go wrong."""
    cfg = load_config()
    ok = True
    print(f"agent-link {__version__}")
    print(f"python      : {sys.version.split()[0]} ({sys.executable})")
    print(f"home        : {root_dir()}")
    print(f"conv logs   : {home_conv_dir()}")

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        import cryptography

        print(f"cryptography: OK ({cryptography.__version__})")
    except ImportError:
        print("cryptography: MISSING — the link cannot encrypt without it")
        print(f"              {sys.executable} -m pip install --user 'cryptography>=42'")
        return 1

    from link import identity

    me = identity.load()
    print(f"device      : {me.label} ({me.device_id}) as {me.agent_kind}")
    print(f"fingerprint : {me.fingerprint()}")
    if identity.is_world_readable():
        print("key perms   : WARNING — identity.json is readable by other users")
        ok = False

    # Before the daemon, because this one is about what the *agent* is being
    # told, and an agent acting on last month's instructions will not look like
    # a transport problem to anybody.
    skill_lines, skill_ok = _doctor_skill()
    for line in skill_lines:
        print(line)
    ok = skill_ok and ok

    # Whether the agent can still see agent-link at all. Everything below this
    # line describes a daemon and a transport that work perfectly well without
    # anybody being able to use them.
    problems = install.registration_problems()
    for problem in problems:
        print(f"registration: MISSING — {problem}")
    if problems:
        print(f"              re-run: {cli_invocation()} install")
        ok = False

    try:
        info = client.ensure_daemon()
        status, loaded = status_when_loaded()
        print(f"daemon      : OK (pid {(read_daemon_info() or {}).get('pid')}, "
              f"{'just started' if info.get('started') else 'already running'})")
    except LinkClientError as exc:
        print(f"daemon      : FAILED — {exc}")
        return 1

    # Immediately under the daemon line, because this is a fact about the daemon
    # rather than about any transport, and every transport line below it is
    # describing the configuration the daemon is *not* using.
    stale = stale_lines(status.get("config_stale"))
    for line in stale:
        print(line)
    if stale:
        ok = False

    # Also a fact about the daemon rather than about any transport, and the one
    # that makes every transport line below it misleading: two agents sharing an
    # identity are one room member, so the roster is right and useless.
    for line in identity_lines(status.get("identity_shared")):
        print(line)

    # The git channel comes first because it is now the only carrier offered.
    ok = _doctor_git(cfg) and ok

    # Only reachable from a config written before the shared folder stopped
    # being offered. Ignoring it silently would be the same class of bug as the
    # stale daemon config above: a setting that looks live and explains nothing.
    if cfg.get("shared_dir"):
        from link.transport_file import probe_shared_dir

        good, why = probe_shared_dir(cfg["shared_dir"])
        print(f"shared dir  : set, and no longer offered ({cfg['shared_dir']})")
        print(f"              the folder itself is {'usable' if good else 'NOT usable'}: {why}")
        print("              a synced folder is not a supported carrier. It was")
        print("              the least proven path here and never carried a real")
        print("              message between two machines. Use the git channel.")
        print("              Nothing clears this for you, and while it is set it")
        print("              still runs, so clear it once git is working:")
        print(f"                {cli_invocation()} config --set shared_dir=")

    relay_url = cfg.get("relay_url")
    if relay_url:
        reachable = _relay_reachable(relay_url)
        print(f"relay       : {relay_url} {'OK' if reachable else 'UNREACHABLE'}")
        if not reachable:
            ok = False
            print("              outbound 443 may be blocked, or the relay is down.")
    else:
        print("relay       : not configured (optional — the git channel is the default)")

    if not cfg.get("git_remote") and not relay_url and not cfg.get("shared_dir"):
        ok = False
        print("\nNothing is configured to carry messages, so no room can reach "
              "anyone.\nSet the git remote above. That is the whole setup.")

    for line in room_lines(status, loaded):
        print(line)
    # A daemon that never finishes loading is a fault in its own right, and the
    # transport lines above it describe a configuration nothing has acted on yet.
    ok = loaded and ok

    return 0 if ok else 1


def visibility_verdict(visibility: str | None,
                       cfg: dict) -> tuple[bool, list[str]]:
    """What doctor says about a PUBLIC carrier repo, and whether it passes.
    Pure so the test can hold it still. A public repo is a documented cost
    (the social graph, world-readable, forever) that `allow_public_carrier`
    accepts on the record; without that, it is a failure with the fix named."""
    if visibility != "PUBLIC":
        return True, []
    if cfg.get("allow_public_carrier"):
        return True, [
            "visibility: PUBLIC, by choice (allow_public_carrier).",
            "Content stays sealed; who talks to whom, and when, is on the",
            "internet permanently.",
        ]
    return False, [
        "WARNING: this repo is PUBLIC. Messages stay unreadable,",
        "but every device id, who talks to whom and when is on",
        "the internet permanently. Make it private, or set",
        "allow_public_carrier=true to accept that on the record.",
    ]


def _doctor_git(cfg: dict) -> bool:
    """Report on the git channel. False only for a fault the user has to fix.

    The visibility warning is the part worth having. A repository holds
    ciphertext, so a public one leaks no message -- but it publishes the routing
    permanently, in a history that is awkward to erase, to anyone who looks.
    That is not obvious from anywhere else, and it is easy to do by accident.
    """
    from link.store import root_dir as _root
    from link.transport_git import (
        DEFAULT_BRANCH,
        clone_dir,
        git_version,
        github_visibility,
        probe_git_remote,
        redact_remote,
        shared_repo_warning,
    )

    remote = cfg.get("git_remote")
    branch = cfg.get("git_branch") or DEFAULT_BRANCH
    if not remote:
        version = git_version()
        print("git channel : not configured"
              + (f" (git {version} is available)" if version else " (git not installed)"))
        if version:
            # The repository they are already working in is the answer here, and
            # it used to be the one thing this never said. Nothing has to be
            # created: the channel is an orphan branch, scoped to `claude-link/`,
            # and the transport fetches only that branch, so their code is never
            # cloned and never touched.
            print("              the PRIVATE repo you and your colleague already share")
            print("              works; nothing new to create. On BOTH machines:")
            # One line, however long. It was wrapped with a trailing `\`, which
            # is bash continuation: pasting it into PowerShell or cmd -- on the
            # machine that had just printed it, since this is where Windows
            # users land -- runs `config --set` with no value and then a stray
            # second command. A command printed to be pasted does not get to be
            # pretty at the cost of being wrong.
            print(f"                {cli_invocation()} config --set "
                  f"git_remote=https://github.com/you/your-project.git")
        return True

    # Shorter than the transport's own 60 s. Doctor is what somebody runs when
    # nothing is working, and a diagnostic that itself sits there for a minute
    # is the thing they will kill before it tells them anything.
    good, why = probe_git_remote(remote, branch, timeout=25.0)
    print(f"git channel : {'OK' if good else 'FAILED'} "
          f"({redact_remote(remote)} @ {branch}) {why}")
    if not good:
        print("              nothing can be sent over the repo until this is fixed")
        return False

    print(f"              clone: {cfg.get('git_dir') or clone_dir(_root(), remote, branch)}")
    visibility = github_visibility(remote)
    if visibility == "PUBLIC":
        ok, lines = visibility_verdict(visibility, cfg)
        for line in lines:
            print(f"              {line}")
        if not ok:
            return False
    elif visibility:
        print(f"              visibility: {visibility.lower()}")
    else:
        # Said out loud, because silence here read as a pass. `github_visibility`
        # returns None off GitHub, without `gh`, and when `gh` is not logged in,
        # and printing nothing in those cases is indistinguishable from printing
        # "private". The README says unqualified that doctor "checks it is
        # private", and the install instructions are what people follow.
        print("              visibility: CANNOT TELL from here (no gh, not "
              "logged in,")
        print("              or not GitHub). Confirm by hand that this repo is "
              "private:")
        print("              the host learns every device id, who talks to whom, "
              "and when.")

    # Attaching to a repository that already holds code is the cheapest setup
    # there is and the orphan branch keeps it safe for the code. Their CI is the
    # part the branch does not protect, and nothing else here would notice.
    ci = shared_repo_warning(remote, branch,
                             presence_s=float(cfg.get("git_presence_s", 45)))
    if ci:
        print("              NOTE: " + _wrap(ci, "                    "))
    return True


def _wrap(text: str, indent: str, width: int = 62) -> str:
    """Fill `text` to `width`, continuing lines under `indent`.

    Never across a hyphen. The advice this renders contains
    `branches-ignore: [claude-link]`, which somebody is meant to copy, and the
    default would break it over two lines in the middle of a word.
    """
    import textwrap

    lines = textwrap.wrap(text, width, break_on_hyphens=False,
                          break_long_words=False)
    return ("\n" + indent).join(lines or [""])


def cmd_git_prune(args) -> int:
    """Squash the channel branch to one commit, on the remote.

    History here is append-only and nothing trims it on its own: rewriting a
    branch other people are pushing to is not something to do behind their
    backs. Asked for explicitly, it is safe -- the push carries a lease, and the
    other members treat a rewritten history as a rebuild rather than a conflict.
    """
    from link.transport_git import DEFAULT_BRANCH, GitError, prune_history

    cfg = load_config()
    remote = args.remote or cfg.get("git_remote")
    branch = args.branch or cfg.get("git_branch") or DEFAULT_BRANCH
    if not remote:
        print("no git_remote configured, and none given with --remote", file=sys.stderr)
        return 1
    try:
        print(prune_history(remote, branch))
    except GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("the other members will notice and rebuild their clones automatically")
    return 0


def _relay_reachable(url: str | None, timeout: float = 3.0) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    except ValueError:
        return False
    if not host:
        return False
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def apply_home(home: str | None) -> None:
    """Point every later lookup at one install, or leave the environment alone.

    A machine running two agents has two homes, and the CLI is how an agent
    reaches the link when it has no MCP server of its own. A shell carries no
    `CLAUDE_LINK_HOME`, so without this the caller silently adopts whichever
    identity owns the default home -- in practice the other agent's, which is
    the failure this project spent an afternoon on.
    """
    if home:
        # Expanded and absolute, because a shell may not have done either.
        # `SKILL.md` tells the agent to run `--home ~/.claude/claude-link-codex`,
        # and Windows PowerShell does not expand `~` for a native command's
        # arguments: the literal string arrived here, `os.path.abspath` turned
        # it into a directory called `~` inside whatever the cwd was, and the
        # caller got a brand-new identity, no rooms, and a control port that
        # falls back to the other agent's. The two-agents-one-identity family
        # again, arriving through the one file that ages independently.
        os.environ["CLAUDE_LINK_HOME"] = os.path.abspath(os.path.expanduser(home))


def build_parser() -> argparse.ArgumentParser:
    # Not the literal "link", which is not a command on any machine this has
    # ever run on. `prog` is printed in every usage line and every argparse
    # error, which is to say in front of somebody who has just typed something
    # wrong, and naming a command that does not exist there is the same defect
    # the installer's next-steps block and `doctor`'s config line both had.
    p = argparse.ArgumentParser(prog=cli_invocation(),
                                description="agent-link control CLI")
    p.add_argument("--home", help="which install to talk to, when this machine "
                                  "runs more than one agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("start").set_defaults(func=cmd_start)
    sub.add_parser("stop").set_defaults(func=cmd_stop)
    sub.add_parser("restart").set_defaults(func=cmd_restart)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("whoami").set_defaults(func=cmd_whoami)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    sub.add_parser("update",
                   help="refresh the SKILL.md each agent reads, after an upgrade"
                   ).set_defaults(func=cmd_update)
    si = sub.add_parser("install",
                        help="wire up the agents on this machine; takes the "
                             "same flags as `python -m link.install`")
    si.add_argument("installer_args", nargs="*",
                    help="passed through to the installer unchanged")
    si.set_defaults(func=cmd_install)

    sj = sub.add_parser("join", help="create or join a room")
    sj.add_argument("--room", help="name of a room to create")
    sj.add_argument("--invite", help="invite string 'name#SECRET', or a door "
                                     "code 'name#DOOR-...' to knock")
    sj.add_argument("--name", help="your display name, asked once per install")
    sj.add_argument("--passphrase", help="shared secret instead of an invite (weaker)")
    sj.add_argument("--shared-dir", help="folder all members can see, used as a fallback")
    sj.add_argument("--git-remote", help="repo all members can push to, used as a fallback")
    sj.add_argument("--git-branch", help="branch inside that repo (default claude-link)")
    sj.add_argument("--relay", help="relay URL, overriding the configured one")
    sj.add_argument("--create-anyway", action="store_true",
                    help="create a new room even though this repo already has "
                         "an open one")
    sj.set_defaults(func=cmd_join)

    si = sub.add_parser("invite", help="print a room's invite string, to re-share it")
    si.add_argument("--room", help="which room, when you are in more than one")
    si.add_argument("--door", action="store_true",
                    help="print the door code instead: no secret, joins need "
                         "a member's yes")
    si.set_defaults(func=cmd_invite)

    sn = sub.add_parser("name", help="show or set your display name")
    sn.add_argument("value", nargs="?", default=None)
    sn.set_defaults(func=cmd_name)

    skn = sub.add_parser("knocks", help="who is waiting at your rooms' doors")
    skn.set_defaults(func=cmd_knocks)

    sg = sub.add_parser("grant", help="let a knocker in")
    sg.add_argument("who", help="device id, or name if unambiguous")
    sg.add_argument("--room", default=None)
    sg.set_defaults(func=cmd_grant, deny=False)

    sd = sub.add_parser("deny", help="decline a knocker")
    sd.add_argument("who", help="device id, or name if unambiguous")
    sd.add_argument("--room", default=None)
    sd.set_defaults(func=cmd_grant, deny=True)

    sr = sub.add_parser("role", help="make a member admin, or member again")
    sr.add_argument("who", help="device id, or name if unambiguous")
    sr.add_argument("role", choices=["admin", "member"])
    sr.add_argument("--room", default=None)
    sr.set_defaults(func=cmd_role)

    sx = sub.add_parser("remove",
                        help="remove a member: rekey the room without them")
    sx.add_argument("who", help="device id, or name if unambiguous")
    sx.add_argument("--room", default=None)
    sx.set_defaults(func=cmd_remove)

    sv = sub.add_parser("leave")
    sv.add_argument("room")
    sv.set_defaults(func=cmd_leave)

    ss = sub.add_parser("send")
    ss.add_argument("text")
    ss.add_argument("--timeout", type=_timeout_arg, default=30.0,
                    help="seconds to wait for the daemon (git sends are slow)")
    ss.add_argument("--room")
    ss.add_argument("--role", default="orchestrator", choices=["orchestrator", "subagent"])
    ss.add_argument("--agent", default="cli")
    ss.set_defaults(func=cmd_send)

    si = sub.add_parser("inbox")
    si.add_argument("--room")
    si.add_argument("--limit", type=int, default=50)
    si.add_argument("--peek", action="store_true")
    si.set_defaults(func=cmd_inbox)

    sr = sub.add_parser("read",
                        help="one message in full, past the inbox preview")
    sr.add_argument("msg_id")
    sr.add_argument("--json", action="store_true",
                    help="emit the whole record rather than just the text")
    sr.set_defaults(func=cmd_read)

    sw = sub.add_parser("watch")
    sw.add_argument("--room")
    sw.set_defaults(func=cmd_watch)

    sk = sub.add_parser("wake", help="block until a message arrives, then exit "
                                     "(exit 0 = message, 1 = window expired)")
    sk.add_argument("--room")
    sk.add_argument("--timeout", type=float, default=600.0,
                    help="seconds to wait before giving up (default 600)")
    sk.set_defaults(func=cmd_wake)

    sc = sub.add_parser("config")
    sc.add_argument("--set", action="append", metavar="KEY=VALUE")
    sc.set_defaults(func=cmd_config)

    sl = sub.add_parser("logs")
    sl.add_argument("--lines", type=int, default=60)
    sl.set_defaults(func=cmd_logs)

    sg = sub.add_parser("git-prune",
                        help="squash the git channel branch to a single commit")
    sg.add_argument("--remote", help="overriding the configured git_remote")
    sg.add_argument("--branch", help="overriding the configured git_branch")
    sg.set_defaults(func=cmd_git_prune)

    return p


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse a command line, handing `install` its own flags untouched.

    `install` wraps a second parser with its own options (`--agent`, `--dev`,
    `--self-test`), and this one must not need to know what they are: the two
    would drift, and the wrapper would start rejecting flags the thing it wraps
    accepts. Everything argparse does not recognise is forwarded there and
    nowhere else, so every other subcommand stays as strict as it was.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    head, tail = argv, []
    if "install" in argv:
        cut = argv.index("install") + 1
        head, tail = argv[:cut], argv[cut:]

    parser = build_parser()
    args = parser.parse_args(head)
    if getattr(args, "func", None) is cmd_install:
        # In order and untouched. Splitting the line rather than letting
        # argparse.REMAINDER at it, because that reorders: `--agent codex`
        # came back as `codex ... --agent`, which is a different command.
        args.installer_args = tail
    elif tail:
        # "install" was a value rather than the subcommand -- a room called
        # install, a message that is the word. Parse the whole line as given.
        args = parser.parse_args(argv)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    apply_home(getattr(args, "home", None))
    try:
        return args.func(args)
    except LinkClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
