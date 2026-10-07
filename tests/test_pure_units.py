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
    # REGRESSION: ordinary remarks that START with a stop word are remarks.
    for t in ("Stop arguing about naming and look at the failing test.",
              "End users will see this, keep the old name.",
              "halt, wait - check the CHANGELOG first", "stop: use the v2 API"):
        check(chat.is_human_stop(t) is False, f"{t!r} is a remark, not a stop")
    for t in ("please stop", "Stop the chat", "end chat now!"):
        check(chat.is_human_stop(t) is True, f"{t!r} is still a stop")


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


def test_relay_chunks_lose_nothing() -> None:
    print("long relay messages: no blank line or space is lost where they're cut:")
    import random
    from discordinator.discord_client import split_chunks
    rnd = random.Random(11)
    words = ["a", "bb", " ", "  ", "\n", "\n\n", "\n\n\n", "y" * 30, "\t", "- item"]
    for n in range(6000):
        prefixed = n % 2 == 1  # sent after a "[label] " (shields the start)
        text = "".join(rnd.choice(words) for _ in range(rnd.randint(1, 80)))
        limit = rnd.randint(4, 40)
        pairs = split_chunks(text, limit, prefixed)
        chunks = [c for c, _ in pairs]
        if any(len(c) > limit for c in chunks):
            raise AssertionError(f"chunk over the limit: {text!r} {limit} -> {pairs!r}")
        if "".join(c + sep for c, sep in pairs) != text:
            raise AssertionError(f"lost text: {text!r} {limit} -> {pairs!r}")
        # Discord trims each message's ends; a cut must never leave whitespace
        # there (the text's own start and end are its sender's business) -
        # unless the window had no clean place to cut at all.
        pos = 0
        for i, (c, sep) in enumerate(pairs[:-1]):
            rest = text[pos:]
            clean_cut_exists = any(
                not rest[j - 1].isspace() and (prefixed or not rest[j].isspace())
                for j in range(1, min(limit, len(rest) - 1) + 1))
            nxt = pairs[i + 1][0]
            lead_lost = nxt[:1].isspace() and not prefixed
            if clean_cut_exists and (c[-1:].isspace() or lead_lost):
                raise AssertionError(f"trimmable cut: {text!r} {limit} {prefixed} -> {chunks!r}")
            pos += len(c) + len(sep)
    check(True, "every cut drops just one newline (or nothing) and leaves nothing to trim")
    t = "para one\n\n\npara two"
    pairs = split_chunks(t, 12, prefixed=True)
    check(pairs == [("para one", "\n"), ("\n\npara two", "")],
          f"a labeled message is cut at the paragraph, blank lines kept: {pairs}")
    pairs = split_chunks("one two three four", 10, prefixed=True)
    check(pairs[0][0] == "one two", f"...and otherwise between words, not mid-word: {pairs}")


def test_split_turn_survives_trimming() -> None:
    print("split turns come back exactly even if the transport trims each message:")
    import random
    rnd = random.Random(7)
    words = ["a", "bb", " ", "  ", "\n", "\n\n", "x" * 30, "\t", chat.GLUE]
    for _ in range(3000):
        text = "".join(rnd.choice(words) for _ in range(rnd.randint(1, 60)))
        limit = rnd.randint(4, 40)
        pieces = chat.split_turn(text, limit)
        if any(len(p) > limit for p in pieces):
            raise AssertionError(f"piece over the limit: {text!r} {limit} -> {pieces!r}")
        # Discord trims whitespace off the ends of a message. The header always
        # comes first, so only the end of a piece is at risk.
        sent = [chat.parse(("[A|say] " + p).strip()) for p in pieces]
        if chat.join_pieces([s["body"] for s in sent]) != text:
            raise AssertionError(f"lost text: {text!r} {limit} -> {pieces!r}")
    check(True, "no piece ends in whitespace that a trim could take off")
    t = "para one\n\npara two  "
    sent = [chat.parse(("[A|say] " + p).strip()) for p in chat.split_turn(t, 12)]
    check(chat.join_pieces([s["body"] for s in sent]) == t,
          "blank lines and trailing spaces survive")


def main() -> int:
    test_chunk_content()
    test_simplify_message()
    test_transport_normalization()
    test_sanitize_room()
    test_sanitize_handle()
    test_is_human_stop()
    test_parse_header_roundtrip()
    test_split_turn_survives_trimming()
    test_relay_chunks_lose_nothing()
    print(f"\nALL {_passed} PURE-UNIT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
