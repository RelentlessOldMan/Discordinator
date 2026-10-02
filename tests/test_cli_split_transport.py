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

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-cli-split-"))
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
    rc = cli.main(["interject", "hiya chat", "--channel", "chatroom"])
    check(rc == 0, "interject succeeded over the local chat transport")
    msgs = LocalClient("probe").read_messages("chatroom", limit=5)
    check(any("hiya chat" in (m.get("content") or "") for m in msgs),
          "the human interjection landed in the LOCAL chat room")


def main() -> int:
    test_unset_mode_errors()
    test_set_subcommands_persist_independently()
    test_set_transport_is_gone()
    test_version_shows_each_mode()
    test_relay_cli_uses_relay_transport()
    test_chat_cli_uses_chat_transport()
    print(f"\nALL {_passed} CLI-SPLIT-TRANSPORT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
