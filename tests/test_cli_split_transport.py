"""CLI glue for per-mode transport (explicit, no base, no fallback).

Drives `cli.main([...])` to cover the wiring unit tests skip: that each mode's
transport must be set explicitly (an unset mode errors), that
`config set-relay-transport` / `set-chat-transport` persist independently, that
the old `set-transport` command is gone, the per-mode `version` output, and that
a relay CLI command runs on the relay transport while a chat CLI command runs on
the chat transport (both over local, so no network/token). Isolated temp config.

Run:  python tests/test_cli_split_transport.py
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-cli-split-"))
os.chdir(_TMP)  # never the repo: a .env there would be loaded into the test
atexit.register(lambda: (os.chdir(tempfile.gettempdir()),
                         shutil.rmtree(_TMP, ignore_errors=True)))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
for _k in ("DISCORDINATOR_TRANSPORT", "DISCORDINATOR_RELAY_TRANSPORT",
           "DISCORDINATOR_CHAT_TRANSPORT", "DISCORD_BOT_TOKEN"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discordinator import cli, config  # noqa: E402
from discordinator.local_client import LocalClient  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def test_unset_mode_errors() -> None:
    print("a relay command refuses until relay_transport is set (no default):")
    check(cli.main(["config", "set-token", "bogus"]) == 0, "set-token ok")
    rc = cli.main(["send", "hi", "--channel", "x"])  # relay_transport still unset
    check(rc == 1, "send exits 1 when relay_transport is unset (fails fast, no guess)")


def test_set_subcommands_persist_independently() -> None:
    print("set-relay-transport / set-chat-transport persist independently:")
    check(cli.main(["config", "set-relay-transport", "local"]) == 0, "set-relay-transport local ok")
    check(cli.main(["config", "set-chat-transport", "discord"]) == 0, "set-chat-transport discord ok")
    cfg = config.load()
    check(config.transport(cfg, "relay") == "local", "relay persisted as local")
    check(config.transport(cfg, "chat") == "discord", "chat persisted as discord (independent)")
    check("transport" not in cfg or cfg.get("transport") is None,
          "no base 'transport' key is written")


def test_set_transport_is_gone() -> None:
    print("the old sets-both 'set-transport' command no longer exists:")
    try:
        cli.main(["config", "set-transport", "local"])
        raise AssertionError("set-transport should no longer be a valid subcommand")
    except SystemExit as exc:
        check(exc.code == 2, "config set-transport -> argparse error (removed)")


def test_version_shows_each_mode() -> None:
    print("version runs and reports each mode's transport (relay=local, chat=discord):")
    check(cli.main(["version"]) == 0, "version exits 0 with a mixed transport config")


def test_relay_cli_uses_relay_transport() -> None:
    print("a relay CLI command runs on the relay transport (local), no network:")
    rc = cli.main(["send", "yo relay", "--channel", "rlyroom", "--label", "T"])
    check(rc == 0, "send succeeded over the local relay transport")
    msgs = LocalClient("probe").read_messages("rlyroom", limit=5)
    check(any("yo relay" in (m.get("content") or "") for m in msgs),
          "the relay message landed in the LOCAL room")


def test_chat_cli_uses_chat_transport() -> None:
    print("a chat CLI command runs on the chat transport (local), no network:")
    check(cli.main(["config", "set-chat-transport", "local"]) == 0, "set-chat-transport local ok")
    from discordinator import chat
    chat.send_chat(LocalClient("S"), "chatroom", "A", "over", "a chat going on", to="B")
    rc = cli.main(["interject", "hiya chat", "--channel", "chatroom"])
    check(rc == 0, "interject succeeded over the local chat transport")
    msgs = LocalClient("probe").read_messages("chatroom", limit=5)
    check(any("hiya chat" in (m.get("content") or "") for m in msgs),
          "the human interjection landed in the LOCAL chat room")


def test_local_viewers_never_use_discord() -> None:
    print("watch/interject/stop act on local rooms even when this shell's chat is on Discord:")
    import contextlib
    import io
    from discordinator import chat
    from discordinator.discord_client import DiscordClient

    def no_discord(*_a, **_k):
        raise AssertionError("a local viewer talked to Discord")

    old = DiscordClient.read_messages, DiscordClient.send_message
    DiscordClient.read_messages = DiscordClient.send_message = no_discord  # type: ignore[assignment]
    try:
        check(cli.main(["config", "set-chat-transport", "discord"]) == 0, "chat on discord here")
        # A session chatting locally (transport set in its .mcp.json) and one
        # chatting on Discord: only the local room is remembered for the human.
        chat.send_chat(LocalClient("S"), "workroom", "A", "over", "local turn", to="B")
        chat.note_room("workroom", True)
        chat.note_room("1553829813624643634", False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["watch", "--no-events"])
        check(rc == 0, "watch with no room exits 0")
        check("#workroom" in out.getvalue() and "local turn" in out.getvalue(),
              "watch shows the local room the sessions last used, not the Discord one")
        check(cli.main(["interject", "steer"]) == 0, "interject works with chat on discord")
        check(cli.main(["stop"]) == 0, "stop works with chat on discord")
        msgs = [m.get("content") or "" for m in LocalClient("probe").read_messages("workroom", limit=5)]
        check("steer" in msgs and any(chat.is_human_stop(m) for m in msgs),
              "the interjection and the stop landed in that local room")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["watch", "--all", "--no-events"])
        check(rc == 0 and "local turn" in out.getvalue(), "watch --all works with chat on discord")
    finally:
        DiscordClient.read_messages, DiscordClient.send_message = old  # type: ignore[assignment]


def test_local_room_names_as_defaults() -> None:
    print("on local, set-default / set-chat-channel take any room name:")
    check(cli.main(["config", "set-relay-transport", "local"]) == 0, "relay on local")
    check(cli.main(["config", "set-chat-transport", "local"]) == 0, "chat on local")
    check(cli.main(["config", "set-chat-channel", "claudes-chatroom"]) == 0,
          "set-chat-channel accepts a plain room name")
    check(cli.main(["config", "set-default", "myroom"]) == 0, "set-default accepts a plain room name")
    cfg = config.load()
    check(config.resolve_chat_channel(cfg, None) == "claudes-chatroom"
          and config.resolve_channel(cfg, None) == "myroom", "and they're the defaults now")
    check(cli.main(["config", "set-relay-transport", "discord"]) == 0, "relay on discord")
    check(cli.main(["config", "set-default", "nosuch"]) == 1,
          "on Discord an unknown name still needs add-channel first")


def main() -> int:
    test_unset_mode_errors()
    test_set_subcommands_persist_independently()
    test_set_transport_is_gone()
    test_version_shows_each_mode()
    test_relay_cli_uses_relay_transport()
    test_chat_cli_uses_chat_transport()
    test_local_viewers_never_use_discord()
    test_local_room_names_as_defaults()
    print(f"\nALL {_passed} CLI-SPLIT-TRANSPORT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
