"""Command-line interface for Discordinator."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import __version__, chat, config, use_system_certs
from .client_factory import make_client
from .discord_client import DiscordClient, DiscordError, simplify_message
from .local_client import local_dir


def _err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def _read_stdin_if_needed(text: Optional[str]) -> str:
    if text is not None and text != "-":
        return text
    data = sys.stdin.read()
    return data.rstrip("\n")


def _parse_duration(text: str) -> int:
    """Parse '7d' / '24h' / '30m' / '90s' (bare number = days) into seconds."""
    m = re.fullmatch(r"\s*(\d+)\s*([dhms]?)\s*", text.lower())
    if not m:
        raise config.ConfigError(
            f"Invalid duration '{text}'. Use e.g. 7d, 24h, 30m, 90s."
        )
    n = int(m.group(1))
    mult = {"d": 86400, "h": 3600, "m": 60, "s": 1, "": 86400}[m.group(2)]
    return n * mult


def _resolve_ack(args: argparse.Namespace, cfg: dict[str, Any]) -> bool:
    """--ack / --no-ack win; otherwise fall back to the ack_on_read config."""
    val = getattr(args, "ack", None)
    return val if val is not None else bool(cfg.get("ack_on_read"))


def _ack_newest(
    client: DiscordClient, channel_id: str, messages: list[dict[str, Any]]
) -> None:
    """React ✅ to the newest of the given messages ('read up to here')."""
    if not messages:
        return
    newest = max(messages, key=lambda m: int(m["id"]))
    try:
        client.add_reaction(channel_id, newest["id"])
    except DiscordError as exc:
        print(f"warning: could not add ✅ reaction: {exc}", file=sys.stderr)


def _print_messages(messages: list[dict[str, Any]]) -> None:
    """Pretty-print simplified message objects (chronological order assumed)."""
    for m in messages:
        ts = (m["timestamp"] or "")[:19].replace("T", " ")
        header = f"[{ts}] {m['author']}"
        if m["bot"]:
            header += " (bot)"
        print(header)
        if m["content"]:
            for line in m["content"].splitlines() or [""]:
                print(f"    {line}")
        for a in m["attachments"]:
            kind = "image" if a.get("is_image") else "file"
            name = a.get("filename") or "(unnamed)"
            print(f"    <{kind}> {name}  {a.get('url')}")
        print(f"    #id={m['id']}")
        print()


# -- command handlers ------------------------------------------------------


def cmd_send(args: argparse.Namespace) -> int:
    cfg = config.load()
    channel_id = config.resolve_channel(cfg, args.channel)
    files = getattr(args, "file", None) or []
    if files:
        config.require_send_attachments(cfg)  # gate: raises if this machine hasn't opted in

    # Resolve the text body. With files present we don't block on stdin — a
    # files-only message is allowed (empty content).
    if args.text == "-":
        content = _read_stdin_if_needed(args.text)
    elif args.text is not None:
        content = args.text
    elif files:
        content = ""
    else:
        content = _read_stdin_if_needed(None)

    if not content and not files:
        return _err("nothing to send (empty message).")
    tag = args.label if args.label is not None else cfg.get("machine_label")

    with make_client(cfg, "relay") as client:
        if files:
            sent = client.send_files(channel_id, content, files, label=tag)
        else:
            sent = client.send_message(channel_id, content, label=tag)
    if args.json:
        print(json.dumps([simplify_message(m) for m in sent], indent=2))
    else:
        n = len(sent)
        plural = "s" if n != 1 else ""
        extra = f" with {len(files)} file(s)" if files else ""
        print(f"sent {n} message{plural}{extra} to channel {channel_id}")
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    cfg = config.load()
    channel_id = config.resolve_channel(cfg, args.channel)
    want_download = getattr(args, "download", False)
    if want_download:
        config.require_receive_attachments(cfg)  # gate: raises if disabled

    saved: list[str] = []
    with make_client(cfg, "relay") as client:
        raw = client.read_messages(
            channel_id, limit=args.limit, after=args.after, before=args.before
        )
        messages = [simplify_message(m) for m in raw]
        if _resolve_ack(args, cfg):
            _ack_newest(client, channel_id, messages)
        if want_download:
            dest_dir = Path(args.download_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            for m in messages:
                for a in m["attachments"]:
                    saved.append(str(client.download_attachment(a["url"], dest_dir)))
    if not args.newest_first:
        messages.reverse()  # default: oldest -> newest (chronological)

    if args.json:
        print(json.dumps(messages, indent=2))
        if saved:
            print(json.dumps({"downloaded": saved}, indent=2))
        return 0

    if not messages:
        print("(no messages)")
        return 0
    _print_messages(messages)
    if want_download:
        print(f"downloaded {len(saved)} attachment(s) to {args.download_dir}")
        for p in saved:
            print(f"    -> {p}")
    return 0


def _relay_poll(
    client: DiscordClient,
    channel_id: str,
    own_label: Optional[str],
    include_self: bool,
    backfill_limit: int,
) -> list[dict[str, Any]]:
    """Fetch messages newer than the stored cursor, advance the cursor, and
    return the ones worth showing (others' messages, unless include_self)."""
    cursor = config.get_cursor(channel_id)
    if cursor:
        raw = client.read_messages(channel_id, limit=100, after=cursor)
    else:
        raw = client.read_messages(channel_id, limit=backfill_limit)
    messages = [simplify_message(m) for m in raw]
    messages.reverse()  # chronological

    if messages:
        config.set_cursor(channel_id, messages[-1]["id"])  # advance past all seen

    if not include_self and own_label:
        prefix = f"[{own_label}]"
        messages = [m for m in messages if not m["content"].startswith(prefix)]
    return messages


def cmd_relay(args: argparse.Namespace) -> int:
    cfg = config.load()
    channel_id = config.resolve_channel(cfg, args.channel)
    own_label = cfg.get("machine_label")
    label_name = args.channel or cfg.get("default_channel") or channel_id
    do_ack = _resolve_ack(args, cfg)

    if args.reset:
        config.clear_cursor(channel_id)

    with make_client(cfg, "relay") as client:
        if not args.watch:
            messages = _relay_poll(client, channel_id, own_label, args.include_self, args.limit)
            if do_ack:
                _ack_newest(client, channel_id, messages)
            if args.json:
                print(json.dumps(messages, indent=2))
            elif not messages:
                print("(no new messages)")
            else:
                _print_messages(messages)
            return 0

        print(
            f"relay watching '{label_name}' every {args.interval}s "
            f"(filtering out [{own_label}] — Ctrl+C to stop)"
            if own_label and not args.include_self
            else f"relay watching '{label_name}' every {args.interval}s (Ctrl+C to stop)"
        )
        try:
            while True:
                messages = _relay_poll(client, channel_id, own_label, args.include_self, args.limit)
                if messages:
                    if do_ack:
                        _ack_newest(client, channel_id, messages)
                    if args.json:
                        print(json.dumps(messages, indent=2), flush=True)
                    else:
                        _print_messages(messages)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nstopped.")
            return 0


# -- local viewer ----------------------------------------------------------
# `watch` renders a room live (chat turns are parsed so floor/addressing/status
# show clearly); `interject`/`stop` write a human turn so a person can steer or
# halt a local chat that has no Discord UI to type into.

_PALETTE = (36, 32, 35, 33, 34, 31, 92, 95, 93, 94)  # cyan/green/magenta/...
_COLOR_CACHE: dict[str, str] = {}


def _color_for(name: str, enabled: bool) -> tuple[str, str]:
    if not enabled:
        return "", ""
    if name not in _COLOR_CACHE:
        _COLOR_CACHE[name] = f"\033[{_PALETTE[len(_COLOR_CACHE) % len(_PALETTE)]}m"
    return _COLOR_CACHE[name], "\033[0m"


def _fmt_watch(m: dict[str, Any], color: bool, room: Optional[str] = None) -> str:
    ts = (m.get("timestamp") or "")[11:19]  # HH:MM:SS
    tag = ""
    tag_w = 0
    if room is not None:  # --all mode: prefix each line with its room
        rc, rr = _color_for(f"#{room}", color)
        tag = f"{rc}{room[:10]:<11}{rr} "
        tag_w = 12
    parsed = chat.parse(m["content"])
    if parsed:  # a chat turn: [from>to|status] body
        handle, to = parsed["participant"], parsed["to"]
        status, body = parsed["status"], parsed["body"] or ""
        c, r = _color_for(handle, color)
        addr = f"{handle} ▸ {to}" if to else handle
        head = f"{tag}{ts}  {c}{addr:<16}{r} {status:<7}"
    else:  # relay / plain / human / nudge
        content = m["content"]
        if content.lstrip().startswith("⏳"):  # system waiting-nudge line
            base = f"{tag}{ts}  {content}"
            return "\n" + (f"\033[2m{base}\033[0m" if color else base)
        label = "human" if not m.get("bot") else (m.get("author") or "?")
        c, r = _color_for(label, color)
        head = f"{tag}{ts}  {c}{label:<16}{r} {'':<7}"
        body = content
    lines = body.splitlines() or [""]
    indent = " " * (tag_w + 35)  # align continuation lines under the body
    out = f"{head} {lines[0]}"
    for extra in lines[1:]:
        out += f"\n{indent}{extra}"
    return "\n" + out  # blank line before each timestamped message


def _state_footer(client: Any, room: str) -> Optional[str]:
    """One-line derived chat state (floor / others in rotation order / hands / suggestion)."""
    try:
        st = chat.compute_state(client, room, None)
    except Exception:
        return None
    if not st.get("participants"):
        return None
    # A human stop isn't a chat_say turn, so compute_state won't mark it ended;
    # detect it directly so the footer stays honest after `stop`/an interjection.
    human_stop = False
    try:
        newest = client.read_messages(room, limit=1)
        if newest:
            nm = simplify_message(newest[0])
            human_stop = (not nm.get("bot")) and chat.is_human_stop(nm["content"])
    except Exception:
        pass
    if st.get("ended") or human_stop:
        return "ENDED" + (" (human stop)" if human_stop else "")
    bits = [f"floor: {st.get('floor') or '-'}"]
    waiting = st.get("waiting") or []
    if waiting:
        bits.append("others: " + ", ".join(waiting))
    hands = [r["from"] for r in (st.get("floor_requests") or [])]
    if hands:
        bits.append("hands: " + ", ".join(hands))
    if st.get("suggest_next"):
        bits.append(f"suggest: {st['suggest_next']}")
    return " · ".join(bits)


def _dim(text: str, color: bool) -> str:
    return f"\033[2m{text}\033[0m" if color else text


def cmd_watch(args: argparse.Namespace) -> int:
    cfg = config.load()
    color = sys.stdout.isatty() and not args.no_color
    if args.all:
        return _watch_all(cfg, args, color)

    # Default to the chat room (the interesting one); any name/id also works.
    room = config.resolve_channel(cfg, args.room, mode="chat") if args.room else config.resolve_chat_channel(cfg, None)

    def show_state(client: Any) -> None:
        if args.state:
            footer = _state_footer(client, room)
            if footer:
                print(_dim(f"[{footer}]", color))

    with make_client(cfg, "chat") as client:
        raw = client.read_messages(room, limit=args.limit)
        msgs = [simplify_message(m) for m in raw]
        msgs.reverse()
        print(f"─ #{room} " + "─" * max(0, 40 - len(room)))
        for m in msgs:
            print(_fmt_watch(m, color))
        show_state(client)

        if not args.follow:
            if not msgs:
                print("(empty)")
            return 0

        # Seed to the last shown id (or "0" for an empty room) and always page
        # forward with after= — no backfill branch to accidentally re-print.
        cursor = msgs[-1]["id"] if msgs else "0"
        print(_dim("(following — Ctrl+C to stop)", color))
        try:
            while True:
                raw = client.read_messages(room, limit=100, after=cursor)
                new = [simplify_message(m) for m in raw]
                new.reverse()
                if new:
                    for m in new:
                        print(_fmt_watch(m, color))
                    cursor = new[-1]["id"]
                    show_state(client)
                    sys.stdout.flush()  # stream promptly even when piped
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nstopped.")
            return 0


def _watch_all(cfg: dict[str, Any], args: argparse.Namespace, color: bool) -> int:
    """Interleave every local room into one merged, time-ordered stream. Cross-
    room ordering works because ids are time-based, so sorting by id ≈ wall
    clock. Local-only (needs the room list on disk)."""
    if not config.is_local(cfg, "chat"):
        return _err("watch --all is only supported on the local transport "
                    "(in Discord mode, watch a specific channel).")

    def rooms_now(client: Any) -> list[str]:
        return [c["id"] for c in client.list_guild_channels()]

    def collect(client: Any, cursors: dict[str, str], initial: bool) -> list[tuple[int, str, dict]]:
        out: list[tuple[int, str, dict]] = []
        for room in rooms_now(client):
            cur = cursors.get(room)
            if initial and cur is None:
                raw = client.read_messages(room, limit=args.limit)
            else:
                raw = client.read_messages(room, limit=100, after=(cur or "0"))
            msgs = [simplify_message(m) for m in raw]
            msgs.reverse()
            for m in msgs:
                out.append((int(m["id"]), room, m))
            cursors[room] = msgs[-1]["id"] if msgs else (cur or "0")
        out.sort(key=lambda e: e[0])
        return out

    with make_client(cfg, "chat") as client:
        cursors: dict[str, str] = {}
        print("─ #(all local rooms) " + "─" * 24)
        entries = collect(client, cursors, initial=True)
        for _id, room, m in entries:
            print(_fmt_watch(m, color, room=room))
        if not args.follow:
            if not entries:
                print("(no rooms yet)")
            return 0
        print(_dim("(following all rooms — Ctrl+C to stop)", color))
        try:
            while True:
                batch = collect(client, cursors, initial=False)
                if batch:
                    for _id, room, m in batch:
                        print(_fmt_watch(m, color, room=room))
                    sys.stdout.flush()  # stream promptly even when piped
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nstopped.")
            return 0


def cmd_tui(args: argparse.Namespace) -> int:
    cfg = config.load()
    guard = _require_local(cfg, "tui")
    if guard is not None:
        return guard
    room = config.resolve_chat_channel(cfg, args.room)
    try:
        from .tui import run_tui
    except ImportError:
        return _err(
            "the TUI needs the optional 'textual' dependency. Install it with:\n"
            "  pip install -e .[tui]   (or: pip install textual)"
        )
    run_tui(room=room, poll=args.interval, limit=args.limit, label=cfg.get("machine_label"),
            retention_days=config.local_retention_days(cfg))
    return 0


def _require_local(cfg: dict[str, Any], action: str, mode: str = "chat") -> Optional[int]:
    if not config.is_local(cfg, mode):
        return _err(
            f"{action} only applies to the local transport. In Discord mode, "
            "just type in the channel yourself."
        )
    return None


def cmd_interject(args: argparse.Namespace) -> int:
    cfg = config.load()
    guard = _require_local(cfg, "interject")
    if guard is not None:
        return guard
    room = config.resolve_chat_channel(cfg, args.channel)
    text = _read_stdin_if_needed(args.text)
    if not text:
        return _err("nothing to interject (empty message).")
    with make_client(cfg, "chat") as client:
        client.post_human(room, text)
    print(f"interjected as human in #{room}. A waiting session will see it on its next chat_await.")
    return 0


def cmd_chat_guard(args: argparse.Namespace) -> int:
    """Claude Code Stop hook: block a stop that would strand a live chat."""
    from . import guard
    out = guard.main(sys.stdin.read())
    if out:
        print(out)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    cfg = config.load()
    guard = _require_local(cfg, "stop")
    if guard is not None:
        return guard
    room = config.resolve_chat_channel(cfg, args.channel)
    with make_client(cfg, "chat") as client:
        client.post_human(room, "[[STOP]]")
    print(f"sent stop to #{room}. Any session waiting there will end the chat.")
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    cfg = config.load()
    channel_id = config.resolve_channel(cfg, args.channel)

    cutoff = None
    if args.older_than:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=_parse_duration(args.older_than))

    with make_client(cfg, "relay") as client:
        my_id = client.whoami().get("id")

        # Page back through history up to --limit messages.
        collected: list[dict[str, Any]] = []
        before: Optional[str] = None
        while len(collected) < args.limit:
            batch = client.read_messages(
                channel_id, limit=min(100, args.limit - len(collected)), before=before
            )
            if not batch:
                break
            collected.extend(batch)
            before = batch[-1]["id"]
            if len(batch) < 100:
                break

        candidates: list[dict[str, Any]] = []
        for raw in collected:
            m = simplify_message(raw)
            if not args.all and m["author_id"] != my_id:
                continue  # default: only the bot's own messages
            if cutoff is not None:
                ts = datetime.fromisoformat(m["timestamp"]) if m["timestamp"] else None
                if ts is None or ts > cutoff:
                    continue  # not old enough
            candidates.append(m)

        if not candidates:
            print("nothing to delete.")
            return 0

        scope = "all users'" if args.all else "the bot's own"
        window = f" older than {args.older_than}" if args.older_than else ""
        if args.dry_run:
            print(f"[dry-run] would delete {len(candidates)} of {scope} message(s){window}:")
            _print_messages(candidates)
            return 0

        if not args.yes:
            if not sys.stdin.isatty():
                return _err(
                    f"{len(candidates)} {scope} message(s){window} match. "
                    "Re-run with --yes to delete, or --dry-run to preview."
                )
            confirm = input(f"Delete {len(candidates)} {scope} message(s){window}? [y/N] ").strip().lower()
            if confirm not in ("y", "yes"):
                print("aborted.")
                return 0

        deleted = 0
        for m in candidates:
            try:
                client.delete_message(channel_id, m["id"])
                deleted += 1
            except DiscordError as exc:
                print(f"warning: could not delete {m['id']}: {exc}", file=sys.stderr)
        print(f"deleted {deleted} message(s).")
    return 0


def cmd_channels(args: argparse.Namespace) -> int:
    cfg = config.load()
    channels = cfg.get("channels") or {}
    default = cfg.get("default_channel")

    if args.remote:
        if config.is_local(cfg, "relay"):
            # Local transport: list the room files that exist on disk.
            with make_client(cfg, "relay") as client:
                remote = client.list_guild_channels()
            if not remote:
                print("(no local rooms yet — they're created on first message)")
            for c in remote:
                print(f"{c['id']}  #{c.get('name')}")
            return 0
        if not args.guild:
            return _err("--remote requires --guild <guild_id>.")
        with make_client(cfg, "relay") as client:
            remote = client.list_guild_channels(args.guild)
        text_channels = [c for c in remote if c.get("type") in (0, 5)]
        for c in sorted(text_channels, key=lambda c: c.get("position", 0)):
            print(f"{c['id']}  #{c.get('name')}")
        return 0

    if not channels:
        print("(no channels configured — add one with: discordinator config add-channel <name> <id>)")
        return 0
    for name, cid in sorted(channels.items()):
        marker = " (default)" if name == default else ""
        print(f"{name}{marker}: {cid}")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    cfg = config.load()
    with make_client(cfg, "relay") as client:
        me = client.whoami()
    if args.json:
        print(json.dumps(me, indent=2))
    else:
        name = me.get("global_name") or me.get("username")
        if config.is_local(cfg, "relay"):
            print(f"Local relay transport — identity '{name}' (no Discord token in use)")
        else:
            print(f"Authenticated as {name} (id {me.get('id')})")
    return 0


def _fmt_days(days: float) -> str:
    if days <= 0:
        return "forever (retention off)"
    return f"{days:g} day{'' if days == 1 else 's'}"


def cmd_version(args: argparse.Namespace) -> int:
    print(f"discordinator {__version__}")
    try:
        cfg = config.load()
        any_local = False
        for m in ("relay", "chat"):
            try:
                t = config.transport(cfg, m)
                any_local = any_local or (t == "local")
            except config.ConfigError:
                t = "(unset)"
            print(f"{m} transport: {t}")
        if any_local:
            print(f"local dir:   {local_dir()}")
            days = config.local_retention_days(cfg)
            print(f"local keep:  {_fmt_days(days)}")
    except config.ConfigError:
        pass
    print(f"config file: {config.config_path()}")
    print(f"state file:  {config.state_path()}")
    # Best-effort git revision, so you can tell exactly what code is checked out.
    try:
        import subprocess

        here = str(config.Path(__file__).resolve().parent)
        rev = subprocess.run(
            ["git", "-C", here, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        if rev.returncode == 0 and rev.stdout.strip():
            dirty = subprocess.run(
                ["git", "-C", here, "status", "--porcelain"],
                capture_output=True, text=True, timeout=3,
            )
            suffix = " (modified)" if dirty.stdout.strip() else ""
            print(f"git commit:  {rev.stdout.strip()}{suffix}")
    except Exception:
        pass
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    action = args.config_action
    cfg = config.load()

    if action == "set-token":
        cfg["token"] = args.token
        path = config.save(cfg)
        print(f"token saved to {path}")
    elif action == "add-channel":
        cfg.setdefault("channels", {})[args.name] = str(args.channel_id)
        if cfg.get("default_channel") is None:
            cfg["default_channel"] = args.name
        config.save(cfg)
        print(f"channel '{args.name}' -> {args.channel_id}")
    elif action == "remove-channel":
        channels = cfg.get("channels") or {}
        if args.name not in channels:
            return _err(f"no channel named '{args.name}'.")
        del channels[args.name]
        if cfg.get("default_channel") == args.name:
            cfg["default_channel"] = next(iter(channels), None)
        config.save(cfg)
        print(f"removed channel '{args.name}'")
    elif action == "set-default":
        channels = cfg.get("channels") or {}
        if args.name not in channels:
            return _err(f"no channel named '{args.name}'. Add it first.")
        cfg["default_channel"] = args.name
        config.save(cfg)
        print(f"default channel set to '{args.name}'")
    elif action == "set-chat-channel":
        channels = cfg.get("channels") or {}
        if not args.name.isdigit() and args.name not in channels:
            return _err(f"no channel named '{args.name}'. Add it first.")
        cfg["chat_channel"] = args.name
        config.save(cfg)
        print(f"chat channel set to '{args.name}' (live chat_* default; relay unaffected)")
    elif action == "set-relay-transport":
        val = args.mode.strip().lower()
        cfg["relay_transport"] = val
        config.save(cfg)
        print(f"relay transport set to '{val}' (send/read/relay tools); chat is unaffected.")
    elif action == "set-chat-transport":
        val = args.mode.strip().lower()
        cfg["chat_transport"] = val
        config.save(cfg)
        print(f"chat transport set to '{val}' (live chat_* tools); relay is unaffected. "
              "This is how one session relays over Discord while chatting locally "
              "with a sibling session.")
    elif action == "set-label":
        cfg["machine_label"] = args.label
        config.save(cfg)
        print(f"machine label set to '{args.label}'")
    elif action == "set-ack":
        on = args.state.strip().lower() in ("on", "true", "1", "yes")
        cfg["ack_on_read"] = on
        config.save(cfg)
        print(f"ack_on_read set to {on} (✅ auto-reaction on reads {'enabled' if on else 'disabled'})")
    elif action == "set-attachments":
        on = args.state.strip().lower() in ("on", "true", "1", "yes")
        key = "allow_send_attachments" if args.direction == "send" else "allow_receive_attachments"
        cfg[key] = on
        config.save(cfg)
        verb = "upload files out" if args.direction == "send" else "download attachments in"
        print(f"{key} set to {on} (this machine {'may now' if on else 'will NOT'} {verb})")
    elif action == "set-local-retention":
        days = config.local_retention_days({"local_retention_days": args.days})  # validates
        cfg["local_retention_days"] = int(days) if days.is_integer() else days
        config.save(cfg)
        print(f"local_retention_days set to {cfg['local_retention_days']} "
              f"(local rooms keep {_fmt_days(days)}; older messages are dropped on the next write)")
    elif action == "path":
        print(config.config_path())
    elif action == "show":
        redacted = dict(cfg)
        if redacted.get("token"):
            tok = str(redacted["token"])
            redacted["token"] = f"...{tok[-4:]}" if len(tok) > 4 else "set"
        print(json.dumps(redacted, indent=2, sort_keys=True))
    else:  # pragma: no cover - argparse enforces choices
        return _err(f"unknown config action '{action}'")
    return 0


# -- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="discordinator",
        description="Send and read Discord channel messages from the command line.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("send", help="send a message to a channel")
    sp.add_argument("text", nargs="?", help="message text (use '-' or omit to read from stdin)")
    sp.add_argument("-c", "--channel", help="channel name (from config) or raw id")
    sp.add_argument("--label", help="tag to prefix onto this message (overrides config machine_label)")
    sp.add_argument("--file", action="append", metavar="PATH", help="attach a file (repeatable); needs send opt-in")
    sp.add_argument("--image", action="append", dest="file", metavar="PATH", help="attach an image (same as --file; images auto-embed in Discord)")
    sp.add_argument("--json", action="store_true", help="print created messages as JSON")
    sp.set_defaults(func=cmd_send)

    rp = sub.add_parser("read", help="read recent messages from a channel")
    rp.add_argument("-c", "--channel", help="channel name (from config) or raw id")
    rp.add_argument("-n", "--limit", type=int, default=20, help="how many messages (1-100, default 20)")
    rp.add_argument("--after", help="only messages after this message id")
    rp.add_argument("--before", help="only messages before this message id")
    rp.add_argument("--newest-first", action="store_true", help="show newest first (default is chronological)")
    rp.add_argument("--ack", dest="ack", action="store_const", const=True, default=None, help="react ✅ to the newest message read")
    rp.add_argument("--no-ack", dest="ack", action="store_const", const=False, help="do not react (overrides ack_on_read config)")
    rp.add_argument("--json", action="store_true", help="print messages as JSON")
    rp.add_argument("--download", action="store_true", help="download attachments from the read messages (needs receive opt-in)")
    rp.add_argument("--download-dir", default=".", help="where to save downloaded attachments (default: current dir)")
    rp.set_defaults(func=cmd_read)

    lp = sub.add_parser(
        "relay",
        help="show only NEW messages since last check (filters out your own)",
    )
    lp.add_argument("-c", "--channel", help="channel name (from config) or raw id")
    lp.add_argument("--watch", action="store_true", help="keep polling for new messages")
    lp.add_argument("--interval", type=float, default=5.0, help="seconds between polls in --watch (default 5)")
    lp.add_argument("--include-self", action="store_true", help="also show your own messages")
    lp.add_argument("--reset", action="store_true", help="forget the saved position and re-show recent messages")
    lp.add_argument("-n", "--limit", type=int, default=20, help="how many recent messages to show on first run (default 20)")
    lp.add_argument("--ack", dest="ack", action="store_const", const=True, default=None, help="react ✅ to the newest message from the other side")
    lp.add_argument("--no-ack", dest="ack", action="store_const", const=False, help="do not react (overrides ack_on_read config)")
    lp.add_argument("--json", action="store_true", help="print messages as JSON")
    lp.set_defaults(func=cmd_relay)

    wc = sub.add_parser("watch", help="live-view a room (parses chat turns; great for local mode)")
    wc.add_argument("room", nargs="?", help="room name/id (default: the chat room)")
    wc.add_argument("--all", action="store_true", help="interleave ALL local rooms into one stream (local only)")
    wc.add_argument("-f", "--follow", action="store_true", help="keep streaming new messages")
    wc.add_argument("--interval", type=float, default=1.5, help="seconds between polls in --follow (default 1.5)")
    wc.add_argument("-n", "--limit", type=int, default=30, help="how many recent messages to show first (default 30)")
    wc.add_argument("--state", action="store_true", help="also show derived chat state (floor/others/hands)")
    wc.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    wc.set_defaults(func=cmd_watch)

    ij = sub.add_parser("interject", help="post a HUMAN turn into a local chat room (steer the agents)")
    ij.add_argument("text", nargs="?", help="message text (use '-' or omit to read from stdin)")
    ij.add_argument("-c", "--channel", help="room name/id (default: the chat room)")
    ij.set_defaults(func=cmd_interject)

    st = sub.add_parser("stop", help="end a local chat (writes a human stop the awaiting session obeys)")
    st.add_argument("-c", "--channel", help="room name/id (default: the chat room)")
    st.set_defaults(func=cmd_stop)

    cg = sub.add_parser("chat-guard", help="Claude Code Stop hook: stop a session from dropping out of a live chat (reads the hook JSON on stdin)")
    cg.set_defaults(func=cmd_chat_guard)

    tp = sub.add_parser("tui", help="full-screen local chat viewer + input box (needs: pip install -e .[tui])")
    tp.add_argument("room", nargs="?", help="room name/id (default: the chat room)")
    tp.add_argument("--interval", type=float, default=1.0, help="seconds between polls (default 1.0)")
    tp.add_argument("-n", "--limit", type=int, default=200, help="how many recent messages to load first (default 200)")
    tp.set_defaults(func=cmd_tui)

    pp = sub.add_parser("purge", help="delete old messages (on request; safe defaults)")
    pp.add_argument("-c", "--channel", help="channel name (from config) or raw id")
    pp.add_argument("--older-than", help="only delete messages older than this (e.g. 7d, 24h, 30m)")
    pp.add_argument("--all", action="store_true", help="delete everyone's messages, not just the bot's (needs Manage Messages)")
    pp.add_argument("-n", "--limit", type=int, default=200, help="how many recent messages to scan (default 200)")
    pp.add_argument("--dry-run", action="store_true", help="show what would be deleted without deleting")
    pp.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    pp.set_defaults(func=cmd_purge)

    cp = sub.add_parser("channels", help="list configured channels")
    cp.add_argument("--remote", action="store_true", help="list text channels from a guild via the API")
    cp.add_argument("--guild", help="guild (server) id, required with --remote")
    cp.set_defaults(func=cmd_channels)

    wp = sub.add_parser("whoami", help="verify the token by fetching the bot identity")
    wp.add_argument("--json", action="store_true")
    wp.set_defaults(func=cmd_whoami)

    vp = sub.add_parser("version", help="show version, file locations, and git revision")
    vp.set_defaults(func=cmd_version)

    cfgp = sub.add_parser("config", help="manage configuration")
    csub = cfgp.add_subparsers(dest="config_action", required=True)
    x = csub.add_parser("set-token", help="store the bot token")
    x.add_argument("token")
    x = csub.add_parser("add-channel", help="map a friendly name to a channel id")
    x.add_argument("name")
    x.add_argument("channel_id")
    x = csub.add_parser("remove-channel", help="remove a channel mapping")
    x.add_argument("name")
    x = csub.add_parser("set-default", help="set the default channel name (relay tools)")
    x.add_argument("name")
    x = csub.add_parser("set-chat-channel", help="set the default channel for live chat_* tools (a shared room)")
    x.add_argument("name")
    x = csub.add_parser("set-relay-transport", help="transport for relay tools (send/read/relay): 'discord' or 'local'. Required — no default.")
    x.add_argument("mode", choices=["discord", "local"])
    x = csub.add_parser("set-chat-transport", help="transport for live chat_* tools: 'discord' or 'local'. Required — no default. (e.g. relay=discord, chat=local)")
    x.add_argument("mode", choices=["discord", "local"])
    x = csub.add_parser("set-label", help="set this machine's message label")
    x.add_argument("label")
    x = csub.add_parser("set-ack", help="auto-react ✅ to the newest message on every read (on/off)")
    x.add_argument("state", choices=["on", "off"])
    x = csub.add_parser("set-attachments", help="opt in to sending/receiving files (off by default, per machine)")
    x.add_argument("direction", choices=["send", "receive"], help="send = upload local files out; receive = download attachments in")
    x.add_argument("state", choices=["on", "off"])
    x = csub.add_parser("set-local-retention", help="local transport: days to keep messages in a room (default 7; 0 = forever)")
    x.add_argument("days")
    csub.add_parser("show", help="print config (token redacted)")
    csub.add_parser("path", help="print the config file path")
    cfgp.set_defaults(func=cmd_config)

    return p


def _configure_stdio() -> None:
    """Force UTF-8 on stdout/stderr so Unicode (emoji, non-latin text, code)
    in relayed messages doesn't crash on the Windows cp1252 console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv: Optional[list[str]] = None) -> int:
    use_system_certs()
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (config.ConfigError, DiscordError) as exc:
        return _err(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
