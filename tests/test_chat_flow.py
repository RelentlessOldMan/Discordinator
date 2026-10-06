"""Chat flow robustness: keeping both sides engaged without a human kicking them.

Real failure: a (cheaper) model posted its turn, said "I'll wait for their
reply", and ended its turn instead of calling chat_await — stranding the chat.
Covers the fixes: chat_say(over) waits for and returns the reply; every result
carries an imperative `next`; the `working` status ("hold on, doing a 20-minute
task") keeps the floor, shows the waiting side what's happening, and doesn't
trigger a false "it's your turn" reminder; and chat_await hands back an
already-delivered turn instead of blocking on yourself.
Run:  python tests/test_chat_flow.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-flow-"))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
for _k in ("DISCORD_BOT_TOKEN", "DISCORDINATOR_CHAT_HANDLE", "DISCORDINATOR_CHAT_CHANNEL"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import discordinator.mcp_server as mcp  # noqa: E402
from discordinator import chat  # noqa: E402
from discordinator.discord_client import simplify_message  # noqa: E402
from discordinator.local_client import LocalClient  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def _latest(c: LocalClient, room: str) -> str:
    msgs = c.read_messages(room, limit=1)
    return msgs[0]["id"] if msgs else "0"


def _later(delay: float, fn) -> threading.Thread:
    t = threading.Thread(target=lambda: (time.sleep(delay), fn()))
    t.start()
    return t


def test_say_waits_for_reply() -> None:
    print("chat_say(over) waits for and returns the reply in one call:")
    c = LocalClient()
    room = "sayw"
    mcp.chat_begin(chatter="A", channel=room)
    mcp.chat_begin(chatter="B", channel=room)
    t = _later(0.4, lambda: chat.send_chat(c, room, "B", "over", "here's my answer"))
    out = mcp.chat_say(text="question?", chatter="A", channel=room, timeout=5)
    t.join()
    check(out["reply"]["from"] == "B" and out["reply"]["text"] == "here's my answer",
          "reply returned inside chat_say")
    check("YOUR turn" in out["next"], f"next tells it to respond: {out['next'][:40]}")


def test_say_wait_timeout_says_keep_waiting() -> None:
    print("no reply yet -> next says call chat_await again, never stop:")
    out = mcp.chat_say(text="anyone?", chatter="A", channel="lonely", timeout=0.3)
    check(out["reply"]["timed_out"] is True, "reply timed out")
    check("chat_await again" in out["next"] and "do NOT" in out["next"], "next: keep waiting")
    check("nothing can wake you" in out["reply"]["note"], "note explains why stopping is fatal")


def test_say_next_for_non_yields() -> None:
    print("non-yield statuses don't wait and say what to do next:")
    room = "nonyield"
    w = mcp.chat_say(text="hold on, running tests", chatter="B", status="working", channel=room)
    check("reply" not in w and "results" in w["next"], "working -> go do the work, then post results")
    s = mcp.chat_say(text="part one", chatter="B", status="say", channel=room)
    check("reply" not in s and "status='over'" in s["next"], "say -> send the rest")
    a = mcp.chat_say(text="me next?", chatter="C", status="ask", channel=room)
    check("chat_await" in a["next"], "ask -> wait for the floor")
    e = mcp.chat_say(text="bye", chatter="B", status="end", channel=room)
    check("ended" in e["next"] and e["ended"], "end -> you may stop")
    check(w["my_turns"] == 0, "working doesn't count against the turn cap")


def test_working_hold_on_flow() -> None:
    print("'hold on, I'll go do the work' (20 minutes, scaled to seconds):")
    c = LocalClient()
    room = "holdon"
    chat.reset(room, "A", _latest(c, room), 20)
    chat.send_chat(c, room, "B", "working", "hold on, running the full test suite")
    r1 = chat.await_turn(c, room, "A", timeout=0.3, poll=0.05, nudge_after=0.01)
    check(r1["timed_out"] and not r1["your_turn"], "A keeps waiting (working never wakes it)")
    check("B is working on something" in r1["note"] and "full test suite" in r1["note"],
          f"A sees what B is doing: {r1['note'][:70]}")
    check(r1["progress"] and r1["progress"][0]["status"] == "working", "progress listed")
    check(r1["nudged"] is False, "no false 'your turn' reminder while B is working")
    raw = [simplify_message(m)["content"] for m in c.read_messages(room, limit=20)]
    check(not any(x.startswith(chat.NUDGE_MARK) for x in raw), "nothing posted to the channel")
    st = chat.compute_state(c, room, "A")
    check(st["floor"] is None and st["progress"][0]["from"] == "B", "B still holds things up")
    r2 = chat.await_turn(c, room, "A", timeout=0.2, poll=0.05, nudge_after=0.01)
    check(r2["timed_out"] and "full test suite" in r2["note"],
          "a LATER await still shows the working note (state-derived, not buffer)")
    _later(0.2, lambda: chat.send_chat(c, room, "B", "over", "all 17 suites pass"))
    r3 = chat.await_turn(c, room, "A", timeout=5, poll=0.05, nudge_after=0)
    check(r3["from"] == "B" and r3["your_turn"] and "all 17 suites pass" in r3["text"],
          "B's results wake A")
    st = chat.compute_state(c, room, "A")
    check(st["progress"] == [], "progress cleared once B yields")


def test_stale_working_still_flagged() -> None:
    print("'working' from over an hour ago no longer holds back the reminder:")
    import json
    from datetime import datetime, timedelta, timezone
    c = LocalClient()
    room = "deadworker"
    chat.reset(room, "A", _latest(c, room), 20)
    chat.send_chat(c, room, "B", "working", "brb, big refactor")
    path = c._room_path(room)
    recs = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
    recs[-1]["timestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    r = chat.await_turn(c, room, "A", timeout=0.2, poll=0.05, nudge_after=0.01)
    check(r["nudged"] is True, "reminder posted: B may have died mid-task")
    check("90m ago" in r["note"], "note still shows what B was doing and when")


def test_nudge_still_fires_without_working() -> None:
    print("control: with no working note, the reminder still posts:")
    c = LocalClient()
    room = "silent"
    chat.send_chat(c, room, "A", "over", "your turn B")
    chat.reset(room, "A", _latest(c, room), 20)
    r = chat.await_turn(c, room, "A", timeout=0.2, poll=0.05, nudge_after=0.01)
    check(r["nudged"] is True, "reminder posted when the other side is just silent")
    check("still thinking" in r["note"], "generic note when nobody said they're working")


def test_already_received_turn_handed_back() -> None:
    print("calling chat_await when it's already your turn returns that turn:")
    c = LocalClient()
    room = "owed"
    chat.reset(room, "B", _latest(c, room), 20)
    chat.send_chat(c, room, "A", "over", "what do you think?")
    r1 = chat.await_turn(c, room, "B", timeout=2, poll=0.05, nudge_after=0)
    check(r1["from"] == "A", "B receives A's turn")
    t0 = time.monotonic()
    r2 = chat.await_turn(c, room, "B", timeout=5, poll=0.05, nudge_after=0)
    check(time.monotonic() - t0 < 2, "returned immediately instead of blocking on itself")
    check(r2.get("already_received") and r2["your_turn"] and r2["text"] == "what do you think?",
          "same turn handed back with its text")
    check("YOUR turn" in chat.next_step(r2), "next: reply with chat_say")
    r3 = chat.await_turn(c, room, "B", timeout=5, poll=0.05, nudge_after=0, from_whom="Z")
    check(r3["timed_out"], "from_whom for someone else doesn't trigger the hand-back")
    chat.send_chat(c, room, "B", "over", "my reply")
    chat.reset(room, "B", _latest(c, room), 20)
    r4 = chat.await_turn(c, room, "B", timeout=0.2, poll=0.05, nudge_after=0)
    check(r4["timed_out"], "after replying, the guard no longer fires (normal wait)")


def test_say_then_await_is_caught() -> None:
    print("ended its turn on 'say' then called chat_await -> told to finish, no deadlock:")
    c = LocalClient()
    room = "saystall"
    chat.reset(room, "B", _latest(c, room), 20)
    chat.send_chat(c, room, "A", "over", "your turn B")
    chat.await_turn(c, room, "B", timeout=2, poll=0.05, nudge_after=0)
    chat.send_chat(c, room, "B", "say", "here's my answer")  # the mistake: say, not over
    t0 = time.monotonic()
    r = chat.await_turn(c, room, "B", timeout=5, poll=0.05, nudge_after=0)
    check(time.monotonic() - t0 < 2, "returned immediately instead of deadlocking")
    check(r.get("unfinished_turn") and r["your_turn"] and "status='over'" in r["note"],
          "note explains 'say' kept the floor and to send 'over'")
    check("Finish YOUR turn" in chat.next_step(r), "next: finish the turn")
    chat.send_chat(c, room, "B", "working", "running tests first")
    r2 = chat.await_turn(c, room, "B", timeout=5, poll=0.05, nudge_after=0)
    check(r2.get("unfinished_turn") and "Do the work" in r2["note"], "'working' then await -> go do the work")
    chat.send_chat(c, room, "B", "over", "done: all green")
    chat.reset(room, "B", _latest(c, room), 20)
    r3 = chat.await_turn(c, room, "B", timeout=0.2, poll=0.05, nudge_after=0)
    check(r3["timed_out"] and not r3.get("unfinished_turn"), "after 'over', waiting is normal again")


def test_newer_message_beats_unfinished_check() -> None:
    print("if the other side already replied anyway, that reply is delivered:")
    c = LocalClient()
    room = "sayreply"
    chat.reset(room, "B", _latest(c, room), 20)
    chat.send_chat(c, room, "B", "say", "thinking out loud")
    chat.reset(room, "B", _latest(c, room), 20)
    chat.send_chat(c, room, "A", "over", "I'll jump in anyway")
    r = chat.await_turn(c, room, "B", timeout=2, poll=0.05, nudge_after=0)
    check(r["from"] == "A" and not r.get("unfinished_turn"), "A's newer turn delivered first")


def test_nudge_names_stalled_say() -> None:
    print("the waiting side's reminder names a stalled 'say':")
    c = LocalClient()
    room = "saynudge"
    chat.send_chat(c, room, "A", "over", "go B")
    chat.reset(room, "A", _latest(c, room), 20)
    chat.send_chat(c, room, "B", "say", "partial answer")
    r = chat.await_turn(c, room, "A", timeout=0.2, poll=0.05, nudge_after=0.01)
    check(r["nudged"] is True, "reminder posted (a stalled 'say' isn't 'working')")
    posted = [simplify_message(m)["content"] for m in c.read_messages(room, limit=5)]
    nudge = next(x for x in posted if x.startswith(chat.NUDGE_MARK))
    check("[B] sent status 'say'" in nudge and 'status="over"' in nudge,
          f"reminder says what B did wrong: {nudge[2:60]!a}")
    check("B is mid-turn" in r["note"], "A's note shows B is mid-turn")


def test_mcp_await_has_next() -> None:
    print("chat_await results carry `next` too:")
    out = mcp.chat_await(chatter="Q", channel="nextroom", timeout=0.2, nudge_after=0)
    check("chat_await again" in out["next"], "timeout -> keep waiting")
    check(chat.next_step({"ended": True}).startswith("The chat has ended"), "ended -> stop")
    check("human" in chat.next_step({"from": "human"}), "human -> follow them")
    check("chat_await to wait" in chat.next_step({}), "fallback -> wait")


def main() -> int:
    test_say_waits_for_reply()
    test_say_wait_timeout_says_keep_waiting()
    test_say_next_for_non_yields()
    test_working_hold_on_flow()
    test_stale_working_still_flagged()
    test_nudge_still_fires_without_working()
    test_already_received_turn_handed_back()
    test_say_then_await_is_caught()
    test_newer_message_beats_unfinished_check()
    test_nudge_names_stalled_say()
    test_mcp_await_has_next()
    print(f"\nALL {_passed} CHAT-FLOW CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
