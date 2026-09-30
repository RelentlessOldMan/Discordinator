"""Tests for configuration resolution, precedence, and malformed input.

Config handling gates which channel every command talks to and which transport
it uses, so its precedence rules (arg > config > default), its error messages,
and its tolerance of broken files matter. Filesystem-isolated; each test writes
its own config into a throwaway tree. Run:  python tests/test_config.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-cfgtest-"))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discordinator import config  # noqa: E402
from discordinator.config import ConfigError  # noqa: E402

_passed = 0

# Env vars config.load() consults — cleared before each load-based test so a
# stray value in the real environment can't make a test pass or fail spuriously.
_ENV_KEYS = (
    "DISCORD_BOT_TOKEN", "DISCORDINATOR_TRANSPORT", "DISCORDINATOR_LABEL",
    "DISCORDINATOR_RELAY_CHANNEL", "DISCORDINATOR_CHAT_CHANNEL", "DISCORDINATOR_ACK",
)


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def _clear_env() -> None:
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


def _write_config(obj) -> None:
    path = Path(os.environ["DISCORDINATOR_CONFIG"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj) if not isinstance(obj, str) else obj, encoding="utf-8")


def _rm_config() -> None:
    Path(os.environ["DISCORDINATOR_CONFIG"]).unlink(missing_ok=True)


def test_resolve_channel() -> None:
    print("resolve_channel (arg > default; name/id/room):")
    cfg = {"transport": "discord", "channels": {"relay": "111", "proj": "222"},
           "default_channel": "relay"}
    check(config.resolve_channel(cfg, "proj") == "222", "explicit name -> its id")
    check(config.resolve_channel(cfg, None) == "111", "no arg -> default_channel's id")
    check(config.resolve_channel(cfg, "999888777") == "999888777",
          "a raw numeric id passes through even if not configured")
    try:
        config.resolve_channel(cfg, "nope")
        raise AssertionError("expected ConfigError for unknown name")
    except ConfigError as e:
        check("Unknown channel" in str(e), "unknown name (discord) -> helpful ConfigError")

    nodef = {"transport": "discord", "channels": {}}
    try:
        config.resolve_channel(nodef, None)
        raise AssertionError("expected ConfigError when no channel and no default")
    except ConfigError:
        check(True, "no channel + no default (discord) -> ConfigError")


def test_resolve_channel_local() -> None:
    print("resolve_channel (local: any string is a room):")
    cfg = {"transport": "local", "channels": {}}
    check(config.resolve_channel(cfg, "brainstorm") == "brainstorm",
          "arbitrary room name is accepted verbatim in local mode")
    check(config.resolve_channel(cfg, None) == "relay",
          "local default relay room is 'relay' (zero-config)")
    named = {"transport": "local", "channels": {"a": "aaa"}}
    check(config.resolve_channel(named, "a") == "aaa",
          "a configured name still resolves in local mode")


def test_resolve_chat_channel() -> None:
    print("resolve_chat_channel (own default, separate from relay):")
    cfg = {"transport": "discord", "channels": {"c": "333"}, "chat_channel": "c",
           "default_channel": "relay"}
    check(config.resolve_chat_channel(cfg, None) == "333",
          "chat uses chat_channel, not default_channel")
    fallback = {"transport": "discord", "channels": {"d": "444"}, "default_channel": "d"}
    check(config.resolve_chat_channel(fallback, None) == "444",
          "chat falls back to default_channel when no chat_channel set")
    check(config.resolve_chat_channel({"transport": "local"}, None) == "chat",
          "local chat default room is 'chat' (distinct from relay)")
    try:
        config.resolve_chat_channel({"transport": "discord", "channels": {}}, None)
        raise AssertionError("expected ConfigError with no chat channel (discord)")
    except ConfigError:
        check(True, "no chat channel configured (discord) -> ConfigError")


def test_load_env_precedence() -> None:
    print("load() env overrides win over the file:")
    _clear_env()
    _write_config({"token": "file-token", "transport": "discord",
                   "machine_label": "file-label", "ack_on_read": True})
    os.environ["DISCORD_BOT_TOKEN"] = "env-token"
    os.environ["DISCORDINATOR_TRANSPORT"] = "local"
    os.environ["DISCORDINATOR_LABEL"] = "env-label"
    os.environ["DISCORDINATOR_ACK"] = "false"
    cfg = config.load()
    check(cfg["token"] == "env-token", "DISCORD_BOT_TOKEN overrides file token")
    check(config.is_local(cfg), "DISCORDINATOR_TRANSPORT overrides file transport")
    check(cfg["machine_label"] == "env-label", "DISCORDINATOR_LABEL overrides file label")
    check(cfg["ack_on_read"] is False, "DISCORDINATOR_ACK='false' parses to False")
    _clear_env()
    os.environ["DISCORDINATOR_ACK"] = "yes"
    check(config.load()["ack_on_read"] is True, "DISCORDINATOR_ACK='yes' parses to True")
    _clear_env()


def test_load_malformed() -> None:
    print("load() surfaces broken config clearly (no silent wrong defaults):")
    _clear_env()
    _write_config("{ this is not json ")
    try:
        config.load()
        raise AssertionError("expected ConfigError on invalid JSON")
    except ConfigError as e:
        check("not valid JSON" in str(e), "invalid JSON -> ConfigError naming the problem")

    _write_config("[1, 2, 3]")  # valid JSON, wrong shape
    try:
        config.load()
        raise AssertionError("expected ConfigError on non-object JSON")
    except ConfigError as e:
        check("must contain a JSON object" in str(e), "non-object JSON -> ConfigError")

    # A missing file is fine: pure defaults, no crash.
    _rm_config()
    cfg = config.load()
    check(cfg["transport"] == "discord" and cfg["channels"] == {},
          "missing config file -> safe defaults (discord, no channels)")


def test_dotenv_parsing() -> None:
    print("_parse_env_file (project-local .env, no clobber):")
    _clear_env()
    env_path = _TMP / "dotenv-sample.env"
    env_path.write_text(
        "# a comment\n"
        "\n"
        "export DISCORDINATOR_LABEL=from-dotenv\n"
        'DISCORD_BOT_TOKEN="quoted-token"\n'
        "NOT_A_PAIR\n",
        encoding="utf-8",
    )
    os.environ["DISCORDINATOR_LABEL"] = "already-set"
    config._parse_env_file(env_path)
    check(os.environ["DISCORDINATOR_LABEL"] == "already-set",
          "does not clobber a variable already present in the environment")
    check(os.environ.get("DISCORD_BOT_TOKEN") == "quoted-token",
          "sets a new var and strips surrounding quotes; 'export ' prefix handled")
    _clear_env()


def test_cursor_state() -> None:
    print("relay cursor state (get/set/clear roundtrip):")
    config.set_cursor("chan-1", "1000")
    check(config.get_cursor("chan-1") == "1000", "set then get returns the stored cursor")
    check(config.get_cursor("never-seen") is None, "unknown channel cursor is None")
    config.set_cursor("chan-1", "2000")
    check(config.get_cursor("chan-1") == "2000", "set overwrites the previous cursor")
    config.clear_cursor("chan-1")
    check(config.get_cursor("chan-1") is None, "clear removes the cursor")
    config.clear_cursor("chan-1")  # clearing a missing cursor must not raise
    check(True, "clearing an already-absent cursor is a safe no-op")


def test_client_factory() -> None:
    print("client_factory.make_client (backend selection seam):")
    from discordinator.client_factory import make_client
    from discordinator.discord_client import DiscordClient
    from discordinator.local_client import LocalClient
    _clear_env()
    lc = make_client({"transport": "local", "machine_label": "m"})
    check(isinstance(lc, LocalClient), "local transport builds a LocalClient (no token needed)")
    dcl = make_client({"transport": "discord", "token": "a-token"})
    check(isinstance(dcl, DiscordClient), "discord transport + token builds a DiscordClient")
    dcl.close()
    try:
        make_client({"transport": "discord", "token": None})
        raise AssertionError("expected ConfigError building a Discord client with no token")
    except ConfigError:
        check(True, "discord transport without a token -> ConfigError (fails fast)")


def main() -> int:
    test_resolve_channel()
    test_resolve_channel_local()
    test_resolve_chat_channel()
    test_load_env_precedence()
    test_load_malformed()
    test_dotenv_parsing()
    test_cursor_state()
    test_client_factory()
    print(f"\nALL {_passed} CONFIG CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
