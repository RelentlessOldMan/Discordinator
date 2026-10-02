"""Tests for per-mode transport: relay and chat are configured independently.

Each mode's transport (relay_transport / chat_transport) is set EXPLICITLY —
there is no shared base transport and no fallback, so an unset mode fails fast
with a clear message instead of guessing. The motivating case: a work session
relays over Discord (to reach another machine) while chatting locally with a
sibling session on the same box. These tests pin the explicit resolution, the
unset-is-an-error contract, the env overrides, the client factory's per-mode +
by-url selection, and mode-aware channel resolution.

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


def test_unset_mode_is_an_error() -> None:
    print("an unset mode raises a clear ConfigError — no default, no fallback:")
    for mode in ("relay", "chat"):
        try:
            config.transport({}, mode)
            raise AssertionError(f"{mode} with nothing configured should raise")
        except config.ConfigError as exc:
            check(f"set-{mode}-transport" in str(exc), f"{mode} unset -> ConfigError naming the exact fix")
    # Setting ONE mode does not satisfy the other (no cross-fallback).
    half = {"relay_transport": "discord"}
    check(config.transport(half, "relay") == "discord", "the set mode resolves")
    try:
        config.transport(half, "chat")
        raise AssertionError("chat should still raise with only relay set")
    except config.ConfigError:
        check(True, "the other mode still errors (relay does NOT fall through to chat)")
    # A bogus mode is a programming error, not a config error.
    try:
        config.transport({"relay_transport": "discord"}, "nope")
        raise AssertionError("bogus mode should ValueError")
    except ValueError:
        check(True, "an invalid mode -> ValueError")


def test_explicit_per_mode() -> None:
    print("each mode takes its own explicit transport (any crossed combo works):")
    cfg = {"relay_transport": "discord", "chat_transport": "local"}
    check(config.transport(cfg, "relay") == "discord", "relay explicitly discord")
    check(config.transport(cfg, "chat") == "local", "chat explicitly local")
    check(config.is_local(cfg, "chat") is True, "is_local(chat) True")
    check(config.is_local(cfg, "relay") is False, "is_local(relay) False")
    rev = {"relay_transport": "local", "chat_transport": "discord"}
    check(config.transport(rev, "relay") == "local", "relay explicitly local")
    check(config.transport(rev, "chat") == "discord", "chat explicitly discord")
    syn = {"relay_transport": "offline", "chat_transport": "file"}
    check(config.transport(syn, "relay") == "local" and config.transport(syn, "chat") == "local",
          "synonyms (offline/file) normalize to local")


def test_make_client_per_mode() -> None:
    print("make_client builds the right backend for each mode, from ONE config:")
    cfg = {"relay_transport": "discord", "chat_transport": "local", "token": "tok", "machine_label": "work"}
    relay = make_client(cfg, "relay")
    chat_c = make_client(cfg, "chat")
    check(isinstance(relay, DiscordClient), "relay mode -> DiscordClient")
    check(isinstance(chat_c, LocalClient), "chat mode -> LocalClient (needs no token)")
    _close(relay)
    _close(chat_c)
    rev = {"relay_transport": "local", "chat_transport": "discord", "token": "tok", "machine_label": "w"}
    r2 = make_client(rev, "relay")
    c2 = make_client(rev, "chat")
    check(isinstance(r2, LocalClient), "relay explicitly local -> LocalClient")
    check(isinstance(c2, DiscordClient), "chat explicitly discord -> DiscordClient")
    _close(r2)
    _close(c2)
    # No mode (or an unset one) refuses to guess.
    try:
        make_client({"relay_transport": "local"})  # mode omitted
        raise AssertionError("make_client without a mode should refuse")
    except (ValueError, config.ConfigError):
        check(True, "make_client without a mode refuses (no base transport to guess)")


def test_make_client_for_url() -> None:
    print("download picks the backend by url shape (http -> Discord, path -> local):")
    cfg = {"token": "tok", "machine_label": "m"}
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
    cfg = {"relay_transport": "discord", "chat_transport": "local",
           "channels": {"relaych": "111"}, "default_channel": "relaych"}
    check(config.resolve_channel(cfg, None) == "111", "relay default -> its channel id")
    check(config.resolve_chat_channel(cfg, "brainstorm") == "brainstorm",
          "chat (local) accepts an arbitrary room name")
    try:
        config.resolve_channel(cfg, "nope")
        raise AssertionError("relay should reject an unknown channel name")
    except config.ConfigError:
        check(True, "relay (discord) still rejects an unknown channel name")
    bare = {"relay_transport": "discord", "chat_transport": "local"}
    check(config.resolve_chat_channel(bare, None) == "chat",
          "chat (local) default room is the built-in 'chat' with zero channel config")
    try:
        config.resolve_channel(bare, None)
        raise AssertionError("relay should require a default channel in discord mode")
    except config.ConfigError:
        check(True, "relay (discord) still requires a configured default channel")


def test_env_overrides() -> None:
    print("per-mode env vars set each transport; the removed base env is ignored:")
    (_TMP / "config.json").write_text('{"relay_transport": "discord", "token": "t"}', encoding="utf-8")
    os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
    try:
        cfg = config.load()
        check(config.transport(cfg, "chat") == "local", "DISCORDINATOR_CHAT_TRANSPORT sets chat")
        check(config.transport(cfg, "relay") == "discord", "relay still from the file")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_TRANSPORT", None)
    # The old base env no longer does anything: with only relay in the file and a
    # base env set, chat remains unset (an error), not silently 'local'.
    os.environ["DISCORDINATOR_TRANSPORT"] = "local"
    try:
        cfg2 = config.load()
        check(config.transport(cfg2, "relay") == "discord", "relay unaffected by removed base env")
        try:
            config.transport(cfg2, "chat")
            raise AssertionError("chat should still be unset (base env is ignored)")
        except config.ConfigError:
            check(True, "DISCORDINATOR_TRANSPORT is ignored — no base transport anymore")
    finally:
        os.environ.pop("DISCORDINATOR_TRANSPORT", None)


def _write_config(d: dict) -> None:
    (_TMP / "config.json").write_text(json.dumps(d), encoding="utf-8")
    for _k in ("DISCORDINATOR_TRANSPORT", "DISCORDINATOR_RELAY_TRANSPORT",
               "DISCORDINATOR_CHAT_TRANSPORT"):
        os.environ.pop(_k, None)


def test_mcp_chat_uses_chat_transport() -> None:
    print("MCP chat tools run on chat_transport even when relay is Discord:")
    _write_config({"relay_transport": "discord", "token": "bogus", "chat_transport": "local",
                   "machine_label": "work"})
    res = mcp.chat_say(text="hello over local", chatter="A", status="over", channel="wirechat")
    check(res["sent_messages"] >= 1, "chat_say succeeded with no Discord network (local chat)")
    stored = LocalClient("probe").read_messages("wirechat", limit=5)
    check(any("hello over local" in (m.get("content") or "") for m in stored),
          "the chat turn landed in the LOCAL room (chat transport was used)")


def test_mcp_relay_uses_relay_transport() -> None:
    print("MCP relay tools run on relay_transport even when chat is Discord:")
    _write_config({"relay_transport": "local", "token": "bogus", "chat_transport": "discord",
                   "machine_label": "work"})
    out = mcp.send_message(text="relayed locally", channel="wirerelay")
    check("channel wirerelay" in out, "send_message reported the local room")
    stored = LocalClient("probe").read_messages("wirerelay", limit=5)
    check(any("relayed locally" in (m.get("content") or "") for m in stored),
          "the relay message landed in the LOCAL room (relay transport was used)")


def main() -> int:
    test_unset_mode_is_an_error()
    test_explicit_per_mode()
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
