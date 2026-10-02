"""CLI glue for per-mode transport: relay and chat can use different backends.

Drives `cli.main([...])` to cover the new wiring the unit tests skip: the
`config set-relay-transport` / `set-chat-transport` persistence, the per-mode
`version` display, and that a relay CLI command runs on the relay transport
while a chat CLI command runs on the chat transport (both exercised over local,
so no network/token is needed even with a Discord base). Isolated temp config.

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


def test_set_transport_subcommands_persist() -> None:
    print("config set-relay-transport / set-chat-transport persist independently:")
    check(cli.main(["config", "set-token", "bogus"]) == 0, "set-token ok (base stays discord)")
    check(cli.main(["config", "set-relay-transport", "local"]) == 0, "set-relay-transport local ok")
    cfg = config.load()
    check(config.transport(cfg, "relay") == "local", "relay transport persisted as local")
    check(config.transport(cfg, "chat") == "discord", "chat still falls back to base discord")
    check(config.transport(cfg) == "discord", "base transport untouched")


def test_version_shows_per_mode() -> None:
    print("version runs and shows the split when relay/chat transports differ:")
    # relay=local (from the previous test), chat=discord -> they differ.
    check(cli.main(["version"]) == 0, "version exits 0 with a mixed transport config")


def test_relay_cli_uses_relay_transport() -> None:
    print("a relay CLI command runs on the relay transport (local), no network:")
    rc = cli.main(["send", "yo relay", "--channel", "rlyroom", "--label", "T"])
    check(rc == 0, "send succeeded over the local relay transport (base is discord)")
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
    cfg = config.load()
    check(config.transport(cfg, "chat") == "local" and config.transport(cfg, "relay") == "local",
          "both per-mode transports now persisted as local")


def main() -> int:
    test_set_transport_subcommands_persist()
    test_version_shows_per_mode()
    test_relay_cli_uses_relay_transport()
    test_chat_cli_uses_chat_transport()
    print(f"\nALL {_passed} CLI-SPLIT-TRANSPORT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
