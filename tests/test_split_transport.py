"""Tests for per-mode transport: relay and chat can use DIFFERENT backends.

The motivating case: a work session does RELAY over Discord (to reach another
machine) while CHATTING locally with a sibling session on the same box. Before
this, transport was a single global switch for the whole process. These tests
pin the mode-aware resolution (relay_transport / chat_transport overriding the
base transport), the env overrides, the client factory's per-mode + by-url
selection, and that channel resolution respects the per-mode transport.

Run:  python tests/test_split_transport.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-split-"))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
for _k in (
    "DISCORDINATOR_TRANSPORT", "DISCORDINATOR_RELAY_TRANSPORT",
    "DISCORDINATOR_CHAT_TRANSPORT", "DISCORD_BOT_TOKEN",
):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import json  # noqa: E402

from discordinator import chat, config  # noqa: E402
from discordinator.client_factory import make_client, make_client_for_url  # noqa: E402
from discordinator.discord_client import DiscordClient  # noqa: E402
from discordinator.local_client import LocalClient  # noqa: E402
import discordinator.mcp_server as mcp  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def _close(c) -> None:
    try:
        c.close()
    except Exception:
        pass


def test_mode_falls_back_to_base() -> None:
    print("with no per-mode override, both modes follow the base transport:")
    cfg = {"transport": "discord"}
    check(config.transport(cfg) == "discord", "base transport is discord")
    check(config.transport(cfg, "relay") == "discord", "relay falls back to base (discord)")
    check(config.transport(cfg, "chat") == "discord", "chat falls back to base (discord)")
    local = {"transport": "local"}
    check(config.transport(local, "relay") == "local", "relay follows base local")
    check(config.transport(local, "chat") == "local", "chat follows base local")


def test_per_mode_override() -> None:
    print("relay_transport / chat_transport override the base per mode:")
    cfg = {"transport": "discord", "chat_transport": "local"}
    check(config.transport(cfg, "relay") == "discord", "relay stays discord")
    check(config.transport(cfg, "chat") == "local", "chat overridden to local")
    check(config.is_local(cfg, "chat") is True, "is_local(chat) True")
    check(config.is_local(cfg, "relay") is False, "is_local(relay) False")
    # reverse: base local, relay overridden to discord
    rev = {"transport": "local", "relay_transport": "discord"}
    check(config.transport(rev, "relay") == "discord", "relay overridden to discord")
    check(config.transport(rev, "chat") == "local", "chat follows base local")
    # synonyms still normalize through the override
    syn = {"transport": "discord", "chat_transport": "offline"}
    check(config.transport(syn, "chat") == "local", "chat_transport synonym 'offline' -> local")


def test_make_client_per_mode() -> None:
    print("make_client builds the right backend for each mode, from ONE config:")
    cfg = {"transport": "discord", "chat_transport": "local", "token": "tok", "machine_label": "work"}
    relay = make_client(cfg, "relay")
    chat = make_client(cfg, "chat")
    check(isinstance(relay, DiscordClient), "relay mode -> DiscordClient")
    check(isinstance(chat, LocalClient), "chat mode -> LocalClient (needs no token)")
    _close(relay)
    _close(chat)
    # reverse combo
    rev = {"transport": "local", "relay_transport": "discord", "token": "tok", "machine_label": "w"}
    r2 = make_client(rev, "relay")
    c2 = make_client(rev, "chat")
    check(isinstance(r2, DiscordClient), "relay override -> DiscordClient")
    check(isinstance(c2, LocalClient), "chat follows base local -> LocalClient")
    _close(r2)
    _close(c2)
    # no mode -> base transport (backward compatible)
    base = make_client({"transport": "local", "machine_label": "m"})
    check(isinstance(base, LocalClient), "no mode -> base transport (local)")
    _close(base)


def test_make_client_for_url() -> None:
    print("download picks the backend by url shape (http -> Discord, path -> local):")
    cfg = {"transport": "discord", "token": "tok", "machine_label": "m"}
    c1 = make_client_for_url("https://cdn.discordapp.com/a/b.png?ex=1", cfg)
    check(isinstance(c1, DiscordClient), "https url -> DiscordClient")
    _close(c1)
    c2 = make_client_for_url(r"C:\Users\me\AppData\file.bin", cfg)
    check(isinstance(c2, LocalClient), "Windows path -> LocalClient")
    _close(c2)
    c3 = make_client_for_url("/home/me/.discordinator/local/files/1/x.txt", cfg)
    check(isinstance(c3, LocalClient), "unix path -> LocalClient")
    _close(c3)


def test_resolve_channel_respects_mode() -> None:
    print("channel resolution uses the per-mode transport (local allows free room names):")
    cfg = {"transport": "discord", "chat_transport": "local",
           "channels": {"relaych": "111"}, "default_channel": "relaych"}
    check(config.resolve_channel(cfg, None) == "111", "relay default -> its channel id")
    check(config.resolve_chat_channel(cfg, "brainstorm") == "brainstorm",
          "chat (local) accepts an arbitrary room name")
    try:
        config.resolve_channel(cfg, "nope")
        raise AssertionError("relay should reject an unknown channel name")
    except config.ConfigError:
        check(True, "relay (discord) still rejects an unknown channel name")
    # With NO default/chat channel, chat (local) uses the built-in 'chat' room,
    # while relay (discord) still errors for lack of a configured default.
    bare = {"transport": "discord", "chat_transport": "local"}
    check(config.resolve_chat_channel(bare, None) == "chat",
          "chat (local) default room is the built-in 'chat' with zero config")
    try:
        config.resolve_channel(bare, None)
        raise AssertionError("relay should require a default channel in discord mode")
    except config.ConfigError:
        check(True, "relay (discord) still requires a configured default channel")


def test_env_overrides() -> None:
    print("DISCORDINATOR_RELAY_TRANSPORT / CHAT_TRANSPORT override the file per mode:")
    (_TMP / "config.json").write_text('{"transport": "discord", "token": "t"}', encoding="utf-8")
    os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
    try:
        cfg = config.load()
        check(config.transport(cfg, "chat") == "local", "chat env override wins")
        check(config.transport(cfg, "relay") == "discord", "relay untouched by chat env")
        check(config.transport(cfg) == "discord", "base transport unchanged")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_TRANSPORT", None)


def _write_config(d: dict) -> None:
    (_TMP / "config.json").write_text(json.dumps(d), encoding="utf-8")
    for _k in ("DISCORDINATOR_TRANSPORT", "DISCORDINATOR_RELAY_TRANSPORT",
               "DISCORDINATOR_CHAT_TRANSPORT"):
        os.environ.pop(_k, None)


def test_mcp_chat_uses_chat_transport() -> None:
    print("MCP chat tools run on chat_transport even when relay/base is Discord:")
    # Base + relay = discord (a bogus token, never used); chat = local. If chat_say
    # touched the relay/base transport it would need the network — it must not.
    _write_config({"transport": "discord", "token": "bogus", "chat_transport": "local",
                   "machine_label": "work"})
    res = mcp.chat_say(text="hello over local", chatter="A", status="over", channel="wirechat")
    check(res["sent_messages"] >= 1, "chat_say succeeded with no Discord network (local chat)")
    stored = LocalClient("probe").read_messages("wirechat", limit=5)
    check(any("hello over local" in (m.get("content") or "") for m in stored),
          "the chat turn landed in the LOCAL room (chat transport was used)")


def test_mcp_relay_uses_relay_transport() -> None:
    print("MCP relay tools run on relay_transport even when chat/base is Discord:")
    # Base + chat = discord (bogus token); relay = local. send_message must go local.
    _write_config({"transport": "discord", "token": "bogus", "relay_transport": "local",
                   "machine_label": "work"})
    out = mcp.send_message(text="relayed locally", channel="wirerelay")
    check("channel wirerelay" in out, "send_message reported the local room")
    stored = LocalClient("probe").read_messages("wirerelay", limit=5)
    check(any("relayed locally" in (m.get("content") or "") for m in stored),
          "the relay message landed in the LOCAL room (relay transport was used)")


def main() -> int:
    test_mode_falls_back_to_base()
    test_per_mode_override()
    test_make_client_per_mode()
    test_make_client_for_url()
    test_resolve_channel_respects_mode()
    test_env_overrides()
    test_mcp_chat_uses_chat_transport()
    test_mcp_relay_uses_relay_transport()
    print(f"\nALL {_passed} SPLIT-TRANSPORT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
