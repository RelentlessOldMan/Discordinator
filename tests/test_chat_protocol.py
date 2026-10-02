"""Protocol-branch tests for chat.py — the highest-consequence module.

test_local_transport.py covers the happy 2-party and N-way handoffs. This file
targets the branches that keep a chat from silently stranding or starving a
participant: an out-of-band plain reply surfacing as a turn (the "stuck-chat
footgun" fix), from_whom narrowing, the anti-starvation nudge (both flavors),
the turn cap, and the derived-floor computation. Local transport, isolated tree.
Run:  python tests/test_chat_protocol.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-chatproto-"))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
os.environ.pop("DISCORD_BOT_TOKEN", None)
os.environ.pop("DISCORDINATOR_LABEL", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discordinator import chat  # noqa: E402
from discordinator.chat import NUDGE_MARK  # noqa: E402
from discordinator.local_client import LocalClient  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def _nudges(c: LocalClient, room: str) -> list[str]:
    return [m["content"] for m in c.read_messages(room, limit=100)
            if m["content"].lstrip().startswith(NUDGE_MARK)]


def test_out_of_band_plain_reply_surfaces() -> None:
    print("an out-of-band plain reply becomes a turn (stuck-chat footgun fix):")
    c = LocalClient()
    room = "oob"
    chat.send_chat(c, room, "A", "over", "your turn, B", to="B")
    r1 = chat.await_turn(c, room, "B", timeout=2.0, poll=0.05, nudge_after=0)
    check(r1["from"] == "A" and r1["status"] == "over" and r1["your_turn"],
          "B first receives A's proper chat turn")

    # A replies with send_message (a plain relay message) instead of chat_say.
    # Without the fix this would never wake B — the classic stranded-awaiter bug.
    c.send_message(room, "oops, I replied out of band")
    r2 = chat.await_turn(c, room, "B", timeout=2.0, poll=0.05, nudge_after=0)
    check(r2["from"] == "participant" and r2["status"] == "plain",
          "a plain (non-chat) bot reply surfaces as a 'plain' turn")
    check(r2["your_turn"] is True and r2["ended"] is False,
          "the plain turn hands B the floor and does not end the chat")


def test_from_whom_narrowing() -> None:
    print("from_whom makes await wait for one specific peer:")
    c = LocalClient()
    room = "narrow"
    # A yields to B, but B is specifically waiting on C -> A's turn is ignored.
    chat.send_chat(c, room, "A", "over", "from A", to="B")
    r = chat.await_turn(c, room, "B", timeout=1.0, poll=0.05, from_whom="C", nudge_after=0)
    check(r["timed_out"] is True,
          "a yield from the wrong peer does not satisfy a from_whom wait")
    # Now C yields to B -> the wait completes.
    chat.send_chat(c, room, "C", "over", "from C", to="B")
    r = chat.await_turn(c, room, "B", timeout=1.0, poll=0.05, from_whom="C", nudge_after=0)
    check(r["timed_out"] is False and r["from"] == "C",
          "a yield from the awaited peer completes the wait")


def test_turn_cap_reported() -> None:
    print("turn counting and cap are reported on results:")
    c = LocalClient()
    room = "turns"
    chat.reset(room, "A", cursor="0", cap=2)
    check(chat.get_meta(room, "A") == (0, 2), "reset seeds turns=0, cap=2")
    chat.bump_turn(room, "A")
    check(chat.bump_turn(room, "A") == 2, "bump_turn increments and returns the new count")
    r = chat.await_turn(c, room, "A", timeout=0.15, poll=0.05, nudge_after=0)
    check(r["my_turns"] == 2 and r["turn_cap"] == 2 and r["cap_reached"] is True,
          "a result reflects my_turns/turn_cap and cap_reached once the cap is hit")


def test_nudge_waiting_flavor() -> None:
    print("nudge (owed-the-floor flavor) names who should respond:")
    c = LocalClient()
    room = "nudge1"
    chat.send_chat(c, room, "B", "over", "over to you, A", to="A")  # floor -> A
    # Pretend B has already been waiting well past the nudge threshold.
    chat._set_wait(room, "B", since=time.time() - 600, nudged=False)
    r = chat.await_turn(c, room, "B", timeout=0.2, poll=0.05, nudge_after=1.0)
    check(r["timed_out"] and r["nudged"] is True, "past the threshold, the wait is marked nudged")
    ns = _nudges(c, room)
    check(len(ns) == 1 and "A: it's your turn" in ns[0] and "waiting" in ns[0].lower(),
          "exactly one nudge is posted, naming the peer who owes the turn")


def test_nudge_starvation_flavor() -> None:
    print("nudge (starvation flavor) asks the floor holder to yield:")
    c = LocalClient()
    room = "nudge2"
    chat.send_chat(c, room, "A", "over", "go ahead, C", to="C")  # floor -> C
    chat.send_chat(c, room, "B", "ask", "can I get a turn?")     # B raises a hand
    chat._set_wait(room, "B", since=time.time() - 600, nudged=False)
    r = chat.await_turn(c, room, "B", timeout=0.2, poll=0.05, nudge_after=1.0)
    check(r["timed_out"] and r["nudged"] is True, "starved hand-raiser's wait is marked nudged")
    ns = _nudges(c, room)
    check(len(ns) == 1 and "raised a hand" in ns[0] and "[C]" in ns[0] and "[B]" in ns[0],
          "the nudge names the floor holder (C) and asks them to yield to the starved B")


def test_nudge_fires_only_once() -> None:
    print("the nudge is posted once, not on every subsequent timeout:")
    c = LocalClient()
    room = "nudge3"
    chat.send_chat(c, room, "B", "over", "your turn A", to="A")
    chat._set_wait(room, "B", since=time.time() - 600, nudged=False)
    chat.await_turn(c, room, "B", timeout=0.2, poll=0.05, nudge_after=1.0)
    chat.await_turn(c, room, "B", timeout=0.2, poll=0.05, nudge_after=1.0)
    check(len(_nudges(c, room)) == 1, "a second timed-out await does not post a duplicate nudge")


def test_compute_state_floor_two_party() -> None:
    print("compute_state derives the floor in a 2-party chat (unaddressed):")
    c = LocalClient()
    room = "floor2"
    chat.send_chat(c, room, "A", "over", "no explicit address")  # unaddressed yield
    st = chat.compute_state(c, room, "B")
    check(st["floor"] == "B",
          "an unaddressed yield in a 2-party chat passes the floor to the other party")
    check(st["your_turn"] is True and st["multiparty"] is False,
          "B is told it's their turn; the chat is not multiparty")


def test_compute_state_open_floor_multiparty() -> None:
    print("compute_state leaves the floor open on a multiparty broadcast:")
    c = LocalClient()
    room = "floorN"
    chat.send_chat(c, room, "A", "over", "hi B", to="B")        # A,B
    chat.send_chat(c, room, "C", "over", "hello all", to="all")  # +C, broadcast pending
    st = chat.compute_state(c, room)
    check(st["multiparty"] is True and st["floor"] is None,
          "a broadcast yield in a multiparty room leaves an open floor (no single holder)")
    check(st["pending_turn"]["from"] == "C" and st["suggest_next"] == "B",
          "anti-starvation suggests the most-starved non-speaker (B) as the next addressee")


def main() -> int:
    test_out_of_band_plain_reply_surfaces()
    test_from_whom_narrowing()
    test_turn_cap_reported()
    test_nudge_waiting_flavor()
    test_nudge_starvation_flavor()
    test_nudge_fires_only_once()
    test_compute_state_floor_two_party()
    test_compute_state_open_floor_multiparty()
    print(f"\nALL {_passed} CHAT-PROTOCOL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
