"""Unit tests for the pure, high-traffic helpers — no filesystem, no network.

These functions run on EVERY message (chunking on send, simplify on read) or
gate core behavior (transport normalization, handle/room sanitizing, stop-word
detection, header parsing), so their boundary and malformed-input behavior is
worth pinning down directly. Run:  python tests/test_pure_units.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# A config path is only needed so importing `config` never touches a real one.
os.environ["DISCORDINATOR_CONFIG"] = str(
    Path(tempfile.mkdtemp(prefix="discordinator-units-")) / "config.json")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discordinator import chat, config  # noqa: E402
from discordinator.discord_client import (  # noqa: E402
    MAX_MESSAGE_LEN,
    chunk_content,
    simplify_message,
)
from discordinator.local_client import sanitize_room  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def test_chunk_content() -> None:
    print("chunk_content (runs on every send):")
    # Empty content still yields exactly one (empty) message, never zero.
    check(chunk_content("") == [""], "empty content -> one empty chunk (not zero)")
    check(chunk_content("hello") == ["hello"], "short content -> single chunk")

    exactly = "x" * MAX_MESSAGE_LEN
    check(chunk_content(exactly) == [exactly],
          "content exactly at the limit stays one chunk (boundary)")

    over = "x" * (MAX_MESSAGE_LEN + 1)
    chunks = chunk_content(over)
    check(len(chunks) == 2 and all(len(c) <= MAX_MESSAGE_LEN for c in chunks),
          "one char over the limit splits into two within-limit chunks")
    check("".join(chunks) == over,
          "no-newline hard split loses no characters (reconstructs exactly)")

    # Prefer to break on a newline boundary rather than mid-word.
    body = ("a" * 1500) + "\n" + ("b" * 1500)  # 3001 chars, newline at 1500
    chunks = chunk_content(body, limit=2000)
    check(chunks[0] == "a" * 1500 and chunks[1] == "b" * 1500,
          "splits on the newline boundary, dropping the separator newline")
    check(all(len(c) <= 2000 for c in chunks), "every chunk respects the limit")

    # No newline available inside the window -> falls back to a hard cut.
    nolines = "a" * 2500
    chunks = chunk_content(nolines, limit=2000)
    check(chunks == ["a" * 2000, "a" * 500], "no newline in window -> hard cut at limit")

    # A leading newline (rfind would return 0) must not cause a zero-width split
    # or an infinite loop; the guard forces a hard cut.
    tricky = "\n" + ("a" * 2500)
    chunks = chunk_content(tricky, limit=2000)
    check(len(chunks) == 2 and all(len(c) <= 2000 for c in chunks),
          "newline at index 0 does not stall the splitter (progress guaranteed)")


def test_simplify_message() -> None:
    print("simplify_message (runs on every read):")
    raw = {
        "id": "42",
        "author": {"id": "7", "username": "user7", "global_name": "Seven", "bot": True},
        "timestamp": "2026-01-01T00:00:00+00:00",
        "content": "hi",
        "attachments": [
            {"url": "http://x/y.png", "filename": "y.png", "content_type": "image/png"},
            {"filename": "no-url"},  # dropped: no url
        ],
    }
    s = simplify_message(raw)
    check(s["author"] == "Seven", "global_name is preferred over username")
    check(s["bot"] is True and s["author_id"] == "7", "bot flag and author_id surfaced")
    check(len(s["attachments"]) == 1 and s["attachments"][0]["url"] == "http://x/y.png",
          "attachments without a url are dropped, urls kept")
    check(s["attachments"][0]["is_image"] is True,
          "each attachment is now a rich dict (is_image derived), not a bare url string")

    # Fallbacks: no global_name -> username; no author at all -> 'unknown'.
    check(simplify_message({"author": {"username": "bob"}})["author"] == "bob",
          "falls back to username when global_name is absent")
    check(simplify_message({})["author"] == "unknown",
          "missing author object -> 'unknown', not a crash")
    empty = simplify_message({})
    check(empty["content"] == "" and empty["bot"] is False and empty["attachments"] == [],
          "missing fields yield safe defaults (empty content, non-bot, no attachments)")


def test_transport_normalization() -> None:
    print("config.transport / is_local per-mode normalization:")
    for val in ("local", "LOCAL", " local ", "file", "offline"):
        check(config.transport({"relay_transport": val}, "relay") == "local",
              f"'{val}' normalizes to local")
    for val in ("discord", "DISCORD", "garbage"):
        check(config.transport({"chat_transport": val}, "chat") == "discord",
              f"{val!r} normalizes to discord (any non-local, non-empty value)")
    check(config.is_local({"relay_transport": "local"}, "relay") is True, "is_local True for local")
    # Unset / empty is NOT a silent default — it's an explicit error.
    for empty in ({}, {"relay_transport": ""}, {"relay_transport": None}):
        try:
            config.transport(empty, "relay")
            raise AssertionError(f"{empty!r} should raise, not default")
        except config.ConfigError:
            pass
    check(True, "an unset/empty transport raises ConfigError (no silent default)")


def test_sanitize_room() -> None:
    print("sanitize_room (room name -> safe filename stem):")
    check(sanitize_room("relay") == "relay", "plain name passes through")
    check(sanitize_room("Team Chat!") == "team-chat", "spaces/punct -> dash, lowercased")
    check(sanitize_room("--A--") == "a", "leading/trailing dashes stripped")
    check(sanitize_room("") == "room", "empty name -> 'room' fallback")
    check(sanitize_room("***") == "room", "all-invalid -> 'room' fallback (no empty stem)")
    check(sanitize_room("keep_this-1") == "keep_this-1", "underscore/dash/digits preserved")


def test_sanitize_handle() -> None:
    print("chat.sanitize_handle (participant identity):")
    check(chat.sanitize_handle("Alice") == "Alice", "plain handle passes through")
    check(chat.sanitize_handle("A|B]C[>") == "ABC",
          "wire-delimiter chars are stripped from handles")
    check(chat.sanitize_handle("x" * 50) == "x" * 32, "handle capped at 32 chars")
    try:
        chat.sanitize_handle("|][>")
        raise AssertionError("expected ValueError for an all-delimiter handle")
    except ValueError:
        check(True, "handle empty after stripping delimiters -> ValueError")


def test_is_human_stop() -> None:
    print("chat.is_human_stop (halts a running local chat):")
    for t in ("stop", "STOP now", "halt", "end chat please", "[[stop]]", "  Stop  "):
        check(chat.is_human_stop(t) is True, f"{t!r} recognized as a stop")
    # Multi-word stop phrases still end the chat.
    for t in ("stop now", "end this chat", "halt!", "end."):
        check(chat.is_human_stop(t) is True, f"{t!r} (leading whole stop word) is a stop")
    for t in ("keep going", "continue", "", "let's discuss endpoints later"):
        check(chat.is_human_stop(t) is False, f"{t!r} is not a stop")
    check(chat.is_human_stop(None) is False, "None is handled safely (not a stop)")

    # REGRESSION: a bare "end" prefix must NOT end a chat — a normal interjection
    # that merely starts with "end…" once silently terminated the conversation.
    for t in ("endpoint changes done", "ending the retry loop", "endeavor to simplify"):
        check(chat.is_human_stop(t) is False,
              f"{t!r} does NOT end the chat (bare 'end' prefix footgun fixed)")


def test_parse_header_roundtrip() -> None:
    print("chat.parse / chat.header (wire format):")
    # Unaddressed (2-party / broadcast).
    p = chat.parse(chat.header("A", "over") + "hello")
    check(p is not None and p["participant"] == "A" and p["to"] is None
          and p["status"] == "over" and p["body"] == "hello",
          "unaddressed header parses back to its parts")
    # Addressed (N-way floor token).
    p = chat.parse(chat.header("A", "over", to="B") + "hi B")
    check(p is not None and p["to"] == "B", "addressed header carries the target")
    # Not a chat message.
    check(chat.parse("just a plain relay message") is None,
          "non-chat content parses to None")
    check(chat.parse("") is None, "empty content parses to None")


def main() -> int:
    test_chunk_content()
    test_simplify_message()
    test_transport_normalization()
    test_sanitize_room()
    test_sanitize_handle()
    test_is_human_stop()
    test_parse_header_roundtrip()
    print(f"\nALL {_passed} PURE-UNIT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
