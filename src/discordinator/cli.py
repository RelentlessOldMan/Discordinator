"""Command-line interface for Discordinator."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Optional

from . import __version__, config
from .discord_client import DiscordClient, DiscordError, simplify_message


def _err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def _read_stdin_if_needed(text: Optional[str]) -> str:
    if text is not None and text != "-":
        return text
    data = sys.stdin.read()
    return data.rstrip("\n")


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
        for url in m["attachments"]:
            print(f"    <attachment> {url}")
        print(f"    #id={m['id']}")
        print()


# -- command handlers ------------------------------------------------------


def cmd_send(args: argparse.Namespace) -> int:
    cfg = config.load()
    token = config.require_token(cfg)
    channel_id = config.resolve_channel(cfg, args.channel)
    content = _read_stdin_if_needed(args.text)
    if not content:
        return _err("nothing to send (empty message).")
    tag = args.label if args.label is not None else cfg.get("machine_label")

    with DiscordClient(token) as client:
        sent = client.send_message(channel_id, content, label=tag)
    if args.json:
        print(json.dumps([simplify_message(m) for m in sent], indent=2))
    else:
        n = len(sent)
        plural = "s" if n != 1 else ""
        print(f"sent {n} message{plural} to channel {channel_id}")
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    cfg = config.load()
    token = config.require_token(cfg)
    channel_id = config.resolve_channel(cfg, args.channel)

    with DiscordClient(token) as client:
        raw = client.read_messages(
            channel_id, limit=args.limit, after=args.after, before=args.before
        )
    messages = [simplify_message(m) for m in raw]
    if not args.newest_first:
        messages.reverse()  # default: oldest -> newest (chronological)

    if args.json:
        print(json.dumps(messages, indent=2))
        return 0

    if not messages:
        print("(no messages)")
        return 0
    _print_messages(messages)
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
    token = config.require_token(cfg)
    channel_id = config.resolve_channel(cfg, args.channel)
    own_label = cfg.get("machine_label")
    label_name = args.channel or cfg.get("default_channel")

    if args.reset:
        config.clear_cursor(channel_id)

    with DiscordClient(token) as client:
        if not args.watch:
            messages = _relay_poll(client, channel_id, own_label, args.include_self, args.limit)
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
                    if args.json:
                        print(json.dumps(messages, indent=2), flush=True)
                    else:
                        _print_messages(messages)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nstopped.")
            return 0


def cmd_channels(args: argparse.Namespace) -> int:
    cfg = config.load()
    channels = cfg.get("channels") or {}
    default = cfg.get("default_channel")

    if args.remote:
        token = config.require_token(cfg)
        if not args.guild:
            return _err("--remote requires --guild <guild_id>.")
        with DiscordClient(token) as client:
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
    token = config.require_token(cfg)
    with DiscordClient(token) as client:
        me = client.whoami()
    if args.json:
        print(json.dumps(me, indent=2))
    else:
        name = me.get("global_name") or me.get("username")
        print(f"Authenticated as {name} (id {me.get('id')})")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"discordinator {__version__}")
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
    elif action == "set-label":
        cfg["machine_label"] = args.label
        config.save(cfg)
        print(f"machine label set to '{args.label}'")
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
    sp.add_argument("--json", action="store_true", help="print created messages as JSON")
    sp.set_defaults(func=cmd_send)

    rp = sub.add_parser("read", help="read recent messages from a channel")
    rp.add_argument("-c", "--channel", help="channel name (from config) or raw id")
    rp.add_argument("-n", "--limit", type=int, default=20, help="how many messages (1-100, default 20)")
    rp.add_argument("--after", help="only messages after this message id")
    rp.add_argument("--before", help="only messages before this message id")
    rp.add_argument("--newest-first", action="store_true", help="show newest first (default is chronological)")
    rp.add_argument("--json", action="store_true", help="print messages as JSON")
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
    lp.add_argument("--json", action="store_true", help="print messages as JSON")
    lp.set_defaults(func=cmd_relay)

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
    x = csub.add_parser("set-default", help="set the default channel name")
    x.add_argument("name")
    x = csub.add_parser("set-label", help="set this machine's message label")
    x.add_argument("label")
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
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (config.ConfigError, DiscordError) as exc:
        return _err(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
