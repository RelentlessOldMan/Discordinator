"""End-to-end tests for the local (no-Discord) transport.

Exercises the REAL LocalClient over temp JSONL files: id monotonicity + cursor
semantics, the relay round-trip (label self-filtering, after= paging), and the
full turn-based chat protocol (2-party handoff, N-way addressing/floor). Run:

    python tests/test_local_transport.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Isolate config/state/local dirs into a throwaway tree and force local mode.
_TMP = Path(tempfile.mkdtemp(prefix="discordinator-localtest-"))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_TRANSPORT"] = "local"
os.environ.pop("DISCORD_BOT_TOKEN", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discordinator import chat, config  # noqa: E402
from discordinator.discord_client import simplify_message  # noqa: E402
from discordinator.local_client import LocalClient  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def latest_id(client: LocalClient, room: str) -> str:
    r = client.read_messages(room, limit=1)
    return r[0]["id"] if r else "0"


def test_client_primitives() -> None:
    print("client primitives:")
    a = LocalClient(label="mA")
    b = LocalClient(label="mB")
    room = "unit"
    r1 = a.post(room, "one")
    r2 = b.post(room, "two")
    r3 = a.post(room, "three")
    check(int(r2["id"]) > int(r1["id"]) and int(r3["id"]) > int(r2["id"]),
          "ids strictly increase across separate clients")

    newest = a.read_messages(room, limit=2)
    check([m["content"] for m in newest] == ["three", "two"],
          "default read returns newest-first, limited")

    after = a.read_messages(room, limit=100, after=r1["id"])
    check([m["content"] for m in after] == ["three", "two"],
          "after= returns only messages past the cursor (newest-first)")

    before = a.read_messages(room, limit=100, before=r3["id"])
    check([m["content"] for m in before] == ["two", "one"],
          "before= pages backward")

    a.delete_message(room, r2["id"])
    remaining = [m["content"] for m in a.read_messages(room, limit=100)]
    check(remaining == ["three", "one"], "delete removes just that record")


def test_relay_roundtrip() -> None:
    print("relay round-trip:")
    room = config.resolve_channel(config.load(), None)
    check(room == "relay", "local relay default room resolves to 'relay'")

    a = LocalClient(label="mA")
    b = LocalClient(label="mB")
    own_b = "[mB]"

    a.send_message(room, "hello from A", label="mA")
    raw = b.read_messages(room, limit=50)
    msgs = [simplify_message(m) for m in raw]
    msgs.reverse()
    others = [m for m in msgs if not m["content"].startswith(own_b)]
    check(len(others) == 1 and others[0]["content"] == "[mA] hello from A",
          "B's first poll sees A's labeled message")
    cursor = msgs[-1]["id"]

    b.send_message(room, "reply from B", label="mB")
    a.send_message(room, "second from A", label="mA")
    raw2 = [simplify_message(m) for m in b.read_messages(room, limit=100, after=cursor)]
    raw2.reverse()
    others2 = [m for m in raw2 if not m["content"].startswith(own_b)]
    check(len(others2) == 1 and others2[0]["content"] == "[mA] second from A",
          "after cursor, B sees only A's new message (its own filtered out)")


def test_chat_two_party() -> None:
    print("chat (2-party):")
    c = LocalClient()
    room = config.resolve_chat_channel(config.load(), None)
    check(room == "chat", "local chat default room resolves to 'chat' (distinct from relay)")

    chat.reset(room, "A", latest_id(c, room), 20)
    chat.reset(room, "B", latest_id(c, room), 20)

    chat.send_chat(c, room, "A", "over", "hello B")
    res = chat.await_turn(c, room, "B", timeout=5, poll=0.1, nudge_after=0)
    check(res["from"] == "A" and res["your_turn"] and "hello B" in res["text"],
          "B receives A's over-turn")

    chat.send_chat(c, room, "B", "over", "hi A")
    res_a = chat.await_turn(c, room, "A", timeout=5, poll=0.1, nudge_after=0)
    check(res_a["from"] == "B" and res_a["your_turn"], "A receives B's over-turn")

    chat.send_chat(c, room, "A", "end", "bye")
    res_end = chat.await_turn(c, room, "B", timeout=5, poll=0.1, nudge_after=0)
    check(res_end["ended"], "B sees the chat ended")


def test_chat_nway_addressing() -> None:
    print("chat (N-way addressing + floor):")
    c = LocalClient()
    room = "trio"
    for h in ("A", "B", "C"):
        chat.reset(room, h, latest_id(c, room), 20)

    # A addresses C: only C should wake; B must keep holding (floor token).
    chat.send_chat(c, room, "A", "over", "over to you C", to="C")

    res_b = chat.await_turn(c, room, "B", timeout=2, poll=0.1, nudge_after=0)
    check(res_b.get("timed_out") and not res_b.get("your_turn"),
          "B is NOT woken by a turn addressed to C")

    res_c = chat.await_turn(c, room, "C", timeout=5, poll=0.1, nudge_after=0)
    check(res_c["from"] == "A" and res_c["to"] == "C" and res_c["your_turn"],
          "C is woken by the turn addressed to it")

    # B raises a hand (does not steal C's turn) — makes the room genuinely N-way
    # and should surface as an outstanding floor request.
    chat.send_chat(c, room, "B", "ask", "can I get in next?")
    st = chat.compute_state(c, room, "C")
    check(st.get("multiparty") is True, "state reports a multiparty room")
    check(set(st.get("participants", [])) >= {"A", "B", "C"},
          "participants include all three once B has spoken")
    check(any(r.get("from") == "B" for r in st.get("floor_requests", [])),
          "B's hand-raise shows up as an outstanding floor request")
    check(st.get("suggest_next") == "B",
          "suggest_next favors the hand-raiser (anti-starvation)")


def main() -> int:
    test_client_primitives()
    test_relay_roundtrip()
    test_chat_two_party()
    test_chat_nway_addressing()
    print(f"\nALL {_passed} CHECKS PASSED")
    print(f"(temp tree: {_TMP})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
