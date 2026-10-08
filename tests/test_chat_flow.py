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


def test_non_holder_working_3way() -> None:
    print("3-way: a non-holder's `working` doesn't count as holding the floor:")
    r = "trio-work"
    for h in ("A", "B", "C"):
        mcp.chat_begin(chatter=h, channel=r)
    mcp.chat_say(text="A, your call", chatter="B", channel=r, to="A", wait=False)
    mcp.chat_say(text="meanwhile I'll check the schema", chatter="C", channel=r,
                 status="working", wait=False)
    for i in (1, 2):  # the 2nd call is past the fresh-message shortcut
        c = mcp.chat_await(chatter="C", channel=r, timeout=0.2, poll=0.02, nudge_after=0)
        check(not c.get("unfinished_turn") and c["timed_out"],
              f"C's chat_await #{i} keeps waiting (A holds the floor, not C)")
    b = mcp.chat_await(chatter="B", channel=r, timeout=0.2, poll=0.02, nudge_after=0)
    check("C is working" not in (b.get("note") or "") and b["progress"] == [],
          "B isn't told C holds the floor")
    st = chat.compute_state(LocalClient("B"), r, "B")
    check(st["floor"] == "A" and st["progress"] == [], "state: floor A, no turn in progress")
    mcp.chat_await(chatter="A", channel=r, timeout=1, poll=0.02, nudge_after=0)
    mcp.chat_say(text="on it", chatter="A", channel=r, status="working", wait=False)
    st = chat.compute_state(LocalClient("B"), r, "B")
    check([x["from"] for x in st["progress"]] == ["A"], "the holder's `working` does count")


def test_ask_doesnt_hide_unfinished_turn() -> None:
    print("3-way: a raised hand doesn't hide that the floor holder owes the turn:")
    r = "trio-ask"
    for h in ("A", "B", "C"):
        mcp.chat_begin(chatter=h, channel=r)
    mcp.chat_say(text="B, go", chatter="A", channel=r, to="B", wait=False)
    got = mcp.chat_await(chatter="B", channel=r, timeout=2, poll=0.02, nudge_after=0)
    check(got["your_turn"], "B has the floor")
    mcp.chat_say(text="hold on, running tests", chatter="B", channel=r, status="working", wait=False)
    mcp.chat_say(text="me next please", chatter="C", channel=r, status="ask", wait=False)
    b = mcp.chat_await(chatter="B", channel=r, timeout=0.3, poll=0.02, nudge_after=0)
    check(b.get("unfinished_turn") and not b.get("timed_out"),
          "B is told to finish its turn, not to keep waiting on itself")


LONG = "PART1 " + "x" * 2100 + " PART2-END"


def test_long_turn_recovered_whole() -> None:
    print("a turn split into pieces is recovered whole (chat_begin, already_received):")
    r = "long-recover"
    for h in ("A", "B"):
        mcp.chat_begin(chatter=h, channel=r)
    mcp.chat_say(text=LONG, chatter="A", channel=r, wait=False)
    mcp.chat_begin(chatter="B", channel=r)  # B (re)joins: the turn is owed
    got = mcp.chat_await(chatter="B", channel=r, timeout=2, poll=0.02, nudge_after=0)
    check(got["from"] == "A" and "PART1" in got["text"] and "PART2-END" in got["text"],
          "chat_begin recovery delivers every piece")
    again = mcp.chat_await(chatter="B", channel=r, timeout=2, poll=0.02, nudge_after=0)
    check(again.get("already_received") and "PART1" in again["text"]
          and "PART2-END" in again["text"], "already_received hands back the whole turn")
    st = chat.compute_state(LocalClient("B"), r, "B")
    check(st["_owed_text"].startswith("PART1"), "state's owed text is the whole turn")


def test_timeout_mid_turn_keeps_first_half() -> None:
    print("a timeout between someone's say and over doesn't lose the first half:")
    r = "half"
    for h in ("A", "B"):
        mcp.chat_begin(chatter=h, channel=r)
    mcp.chat_say(text="FIRST-HALF", chatter="A", channel=r, status="say", wait=False)
    t = mcp.chat_await(chatter="B", channel=r, timeout=0.2, poll=0.02, nudge_after=0)
    check(t["timed_out"], "B times out mid-turn")
    mcp.chat_say(text="SECOND-HALF", chatter="A", channel=r, wait=False)
    got = mcp.chat_await(chatter="B", channel=r, timeout=2, poll=0.02, nudge_after=0)
    check(got["text"] == "FIRST-HALF\nSECOND-HALF", f"whole turn arrives: {got['text']!r}")


def test_unknown_to_warns_now() -> None:
    print("`to` naming nobody known warns at once instead of silently waiting:")
    r = "typo"
    for h in ("Alpha", "Beta"):
        mcp.chat_begin(chatter=h, channel=r)
    mcp.chat_say(text="hi", chatter="Beta", channel=r, wait=False)
    mcp.chat_await(chatter="Alpha", channel=r, timeout=1, poll=0.02, nudge_after=0)
    t0 = time.monotonic()
    out = mcp.chat_say(text="hello", chatter="Alpha", channel=r, to="Bet")
    check(time.monotonic() - t0 < 5 and "reply" not in out, "doesn't wait for a reply")
    check("did you mean 'Beta'" in out["note"] and "Check `to`" in out["next"],
          "suggests the close match and says what to do")
    ok = mcp.chat_say(text="hello", chatter="Alpha", channel=r, to="Beta", wait=False)
    check("note" not in ok, "a known peer gets no warning")
    live = mcp.chat_say(text="x", chatter="Alpha", channel=r, to="Gamma", wait=False)
    check("note" in live, "an unknown handle warns")
    from discordinator import handles
    handles.resolve("Gamma", {})  # Gamma is now a live session on this machine
    quiet = mcp.chat_say(text="x", chatter="Alpha", channel=r, to="Gamma", wait=False)
    check("note" not in quiet, "a live session that hasn't posted yet is fine")
    bc = mcp.chat_say(text="x", chatter="Alpha", channel=r, to="all", wait=False)
    check("note" not in bc or "Nobody called" not in bc["note"], "a broadcast is never 'unknown'")


def test_human_stop_holds_on_rejoin() -> None:
    print("a human stop that is the last message ends the chat - a rejoin doesn't restart it:")
    r = "stopped"
    for h in ("A", "B"):
        mcp.chat_begin(chatter=h, channel=r)
    mcp.chat_say(text="hi", chatter="A", channel=r, wait=False)
    mcp.chat_await(chatter="B", channel=r, timeout=1, poll=0.02, nudge_after=0)
    mcp.chat_say(text="your turn A", chatter="B", channel=r, wait=False)
    LocalClient("human").post_human(r, "stop")
    st = mcp.chat_status(chatter="A", channel=r)
    check(st["ended"] and st["stop_reason"] == "human" and not st["your_turn"]
          and st["pending_turn"] is None, "state: ended by the human, nothing owed")
    b = mcp.chat_begin(chatter="A", channel=r)
    check(not b["recovered_pending_turn"] and "stopped by the human" in b["next"],
          "chat_begin says the chat was stopped instead of handing a turn back")
    mcp.chat_say(text="new topic", chatter="A", channel=r, wait=False)
    st2 = mcp.chat_status(chatter="B", channel=r)
    check(not st2["ended"] and st2["your_turn"], "a new chat after the stop works normally")


def test_interjection_mid_split_turn() -> None:
    print("a human remark in the middle of someone's long turn doesn't lose its first part:")
    r = "mid-human"
    for h in ("A", "B"):
        mcp.chat_begin(chatter=h, channel=r)
    mcp.chat_say(text="PART-ONE", chatter="B", channel=r, status="say", wait=False)
    LocalClient("human").post_human(r, "quick question")
    mcp.chat_say(text="PART-TWO", chatter="B", channel=r, wait=False)
    first = mcp.chat_await(chatter="A", channel=r, timeout=2, poll=0.02, nudge_after=0)
    check(first["from"] == "human", "the remark is delivered first")
    second = mcp.chat_await(chatter="A", channel=r, timeout=2, poll=0.02, nudge_after=0)
    check(second["text"] == "PART-ONE\nPART-TWO", f"then the whole turn: {second['text']!r}")
    st = chat.config.load_state()["chat"][r]["a"]
    check("partial" not in st, "nothing left buffered once the turn completed")


def test_peer_turn_mid_split_turn_3way() -> None:
    print("3-way: another peer's turn arriving mid-turn doesn't lose the first part:")
    r = "mid-peer"
    for h in ("A", "B", "C"):
        mcp.chat_begin(chatter=h, channel=r)
    mcp.chat_say(text="C-ONE", chatter="C", channel=r, status="say", wait=False)
    mcp.chat_say(text="for A", chatter="B", channel=r, to="A", wait=False)
    mcp.chat_say(text="C-TWO", chatter="C", channel=r, to="A", wait=False)
    got1 = mcp.chat_await(chatter="A", channel=r, timeout=2, poll=0.02, nudge_after=0)
    check(got1["from"] == "B", "B's turn wakes A first")
    got2 = mcp.chat_await(chatter="A", channel=r, timeout=2, poll=0.02, nudge_after=0)
    check(got2["from"] == "C" and got2["text"] == "C-ONE\nC-TWO", f"C's turn whole: {got2['text']!r}")


def test_begin_mid_split_turn() -> None:
    print("joining while someone is partway through a long turn still gets all of it:")
    r = "join-mid"
    mcp.chat_begin(chatter="B", channel=r)
    mcp.chat_say(text="EARLY", chatter="B", channel=r, status="say", wait=False)
    mcp.chat_begin(chatter="A", channel=r)
    mcp.chat_say(text="LATE", chatter="B", channel=r, wait=False)
    got = mcp.chat_await(chatter="A", channel=r, timeout=2, poll=0.02, nudge_after=0)
    check(got["text"] == "EARLY\nLATE", f"whole turn: {got['text']!r}")


def test_long_lines_exact() -> None:
    print("long turns come back byte-for-byte (no inserted or lost newlines):")
    import random
    rnd = random.Random(7)
    for limit in (50, 1990):
        for _ in range(200):
            n = rnd.randint(0, limit * 4)
            text = "".join(rnd.choice("ab \n{}\"") for _ in range(n))
            pieces = chat.split_turn(text, limit)
            if not (all(len(p) <= limit for p in pieces) and chat.join_pieces(pieces) == text):
                raise AssertionError(f"roundtrip failed (limit {limit}): {text!r}")
    check(True, "random texts round-trip exactly and every piece fits")
    r = "longline"
    for h in ("A", "B"):
        mcp.chat_begin(chatter=h, channel=r)
    blob = '{"k": "' + "v" * 4500 + '"}'
    para = "x" * 1980 + "\n\n" + "y" * 100
    for body in (blob, para):
        mcp.chat_say(text=body, chatter="A", channel=r, wait=False)
        got = mcp.chat_await(chatter="B", channel=r, timeout=2, poll=0.02, nudge_after=0)
        check(got["text"] == body, f"{len(body)}-char turn arrives exactly")
        mcp.chat_say(text="ok", chatter="B", channel=r, wait=False)
        mcp.chat_await(chatter="A", channel=r, timeout=2, poll=0.02, nudge_after=0)


def test_typo_leaves_no_phantom() -> None:
    print("a mistyped `to` that was re-sent doesn't leave a phantom participant:")
    r = "phantom"
    for h in ("A", "B"):
        mcp.chat_begin(chatter=h, channel=r)
    mcp.chat_say(text="hi", chatter="B", channel=r, wait=False)
    mcp.chat_await(chatter="A", channel=r, timeout=1, poll=0.02, nudge_after=0)
    mcp.chat_say(text="q", chatter="A", channel=r, to="Bobb", wait=False)
    mcp.chat_say(text="q", chatter="A", channel=r, to="B", wait=False)
    mcp.chat_await(chatter="B", channel=r, timeout=1, poll=0.02, nudge_after=0)
    mcp.chat_say(text="answer", chatter="B", channel=r, wait=False)
    st = chat.compute_state(LocalClient("A"), r, "A")
    check(st["participants"] == ["B", "A"] or sorted(st["participants"]) == ["A", "B"],
          f"just A and B: {st['participants']}")
    check(not st["multiparty"] and st["floor"] == "A", "still a 2-party chat; A's turn")


def test_interjection_only_wakes_holder() -> None:
    print("a human remark doesn't hand the turn to the side that isn't owed it:")
    r = "remark"
    for h in ("A", "B"):
        mcp.chat_begin(chatter=h, channel=r)
    mcp.chat_say(text="hi B", chatter="A", channel=r, wait=False)
    mcp.chat_await(chatter="B", channel=r, timeout=2, poll=0.02, nudge_after=0)
    mcp.chat_say(text="over to you A", chatter="B", channel=r, wait=False)
    LocalClient("human").post_human(r, "fyi: prefer small commits")
    b = mcp.chat_await(chatter="B", channel=r, timeout=2, poll=0.02, nudge_after=0)
    check(b["from"] == "human" and b["your_turn"] is False and b["floor"] == "A",
          "B (waiting) sees the remark but isn't given the turn")
    check("not your turn" in b["next"] and "chat_await" in b["next"], "B is told to keep waiting")
    a = mcp.chat_await(chatter="A", channel=r, timeout=2, poll=0.02, nudge_after=0)
    check(a["from"] in ("B", "human") and a["your_turn"], "A (owed the turn) still has it")


def test_error_after_post_is_not_a_failed_send() -> None:
    print("chat_say that fails AFTER posting says so (posted=True) instead of raising:")
    r = "post-then-fail"
    for h in ("A", "B"):
        mcp.chat_begin(chatter=h, channel=r)
    real_bump, real_await = chat.bump_turn, chat.await_turn

    def boom(*a, **k):
        raise PermissionError("[WinError 5] Access is denied")

    chat.bump_turn = boom
    try:
        out = mcp.chat_say(text="my only turn", chatter="A", channel=r, wait=False)
    finally:
        chat.bump_turn = real_bump
    check(out["posted"] and "PermissionError" in out["error"]
          and "don't send it again" in out["next"], "bookkeeping failure: posted, don't resend")
    mcp.chat_await(chatter="B", channel=r, timeout=1, poll=0.02, nudge_after=0)

    def lost(*a, **k):
        raise chat.DiscordError("503 Service Unavailable")

    chat.await_turn = lost
    try:
        out = mcp.chat_say(text="reply", chatter="B", channel=r)
    finally:
        chat.await_turn = real_await
    check(out["posted"] and "chat_await" in out["next"], "a failed wait for the reply: posted, call chat_await")
    msgs = [m["content"] for m in LocalClient("x").read_messages(r, limit=10)]
    check(sum("my only turn" in c for c in msgs) == 1 and sum("reply" in c for c in msgs) == 1,
          "each message is in the room exactly once")
    mcp.chat_await(chatter="A", channel=r, timeout=1, poll=0.02, nudge_after=0)
    chat.bump_turn = boom
    try:
        out = mcp.chat_say(text="bye", chatter="A", channel=r, status="end")
    finally:
        chat.bump_turn = real_bump
    check(out["posted"] and out["ended"] and "ended" in out["next"],
          "a posted `end` whose bookkeeping failed still reports the chat ended")


def test_shared_room_two_chats() -> None:
    print("two separate chats in one shared room don't cross:")
    r = "shared-room"
    for h in ("Alpha", "Beta"):
        mcp.chat_begin(chatter=h, channel=r)
    mcp.chat_say(text="Beta, what's the schema?", chatter="Alpha", channel=r, to="Beta", wait=False)
    mcp.chat_await(chatter="Beta", channel=r, timeout=1, poll=0.02, nudge_after=0)
    reply = mcp.chat_say(text="it's in schema.sql", chatter="Beta", channel=r, wait=False)
    check(reply["to"] == "Alpha", "an unaddressed reply goes back to who handed over the turn")
    last = LocalClient("x").read_messages(r, limit=1)[0]["content"]
    check(last.startswith("[Beta>Alpha|over]"), f"...and is addressed on the wire: {last[:20]}")
    mcp.chat_await(chatter="Alpha", channel=r, timeout=1, poll=0.02, nudge_after=0)
    mcp.chat_say(text="thanks - and table Y?", chatter="Alpha", channel=r, wait=False)
    e = mcp.chat_begin(chatter="Echo", channel=r)
    check(not e["recovered_pending_turn"] and "Another chat" in e.get("note", ""),
          "a newcomer isn't handed their turn, and is told to address its own")
    mcp.chat_say(text="anyone free for PR 12?", chatter="Echo", channel=r, wait=False)
    for h in ("Alpha", "Beta"):
        st = chat.compute_state(LocalClient("x"), r, h)
        check(st["floor"] == "Beta" and "Echo" not in st["participants"],
              f"an unaddressed opener from a newcomer stays out of {h}'s chat")
    mcp.chat_begin(chatter="Foxtrot", channel=r)
    mcp.chat_say(text="let's review PR 12", chatter="Echo", channel=r, to="Foxtrot", wait=False)
    a = mcp.chat_await(chatter="Alpha", channel=r, timeout=0.3, poll=0.02, nudge_after=0)
    check(a["timed_out"], "Echo's opener to Foxtrot doesn't wake Alpha")
    st = mcp.chat_status(chatter="Beta", channel=r)
    check(st["your_turn"], "Beta is still owed Alpha's question, though Echo spoke since")
    f = mcp.chat_await(chatter="Foxtrot", channel=r, timeout=1, poll=0.02, nudge_after=0)
    check(f["from"] == "Echo" and f["your_turn"], "Foxtrot gets Echo's opener")
    b = mcp.chat_await(chatter="Beta", channel=r, timeout=1, poll=0.02, nudge_after=0)
    check(b["from"] == "Alpha" and "table Y" in b["text"], "and Beta gets Alpha's")


def test_say_without_begin_ignores_old_chat() -> None:
    print("chat_say from a session that never called chat_begin doesn't get old turns:")
    r = "no-begin"
    c = LocalClient("x")
    chat.send_chat(c, r, "Old1", "over", "old question")
    chat.send_chat(c, r, "Old2", "wrap", "think we're done")
    out = mcp.chat_say(text="new topic", chatter="Fresh", channel=r, timeout=0.3)
    check(out["reply"]["timed_out"] and "think we're done" not in out["reply"]["text"],
          "the reply wait starts at its own message")


def test_failed_read_keeps_partial() -> None:
    print("a read error in the middle of someone's long turn doesn't lose its first part:")
    r = "read-fail"
    for h in ("A", "B"):
        mcp.chat_begin(chatter=h, channel=r)
    c = LocalClient("x")
    chat.send_chat(c, r, "B", "say", "PART ONE")
    first = mcp.chat_await(chatter="A", channel=r, timeout=0.2, poll=0.02, nudge_after=0)
    check(first["timed_out"], "only half the turn is in")
    chat.send_chat(c, r, "B", "say", "PART TWO")
    real = LocalClient.read_messages
    calls = {"n": 0}

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 4:  # the poll AFTER the piece was read and the cursor moved
            raise chat.DiscordError("502 transient")
        return real(self, *a, **k)

    LocalClient.read_messages = flaky
    try:
        for _ in range(5):
            try:
                mcp.chat_await(chatter="A", channel=r, timeout=0.2, poll=0.02, nudge_after=0)
            except chat.DiscordError:
                break
    finally:
        LocalClient.read_messages = real
    chat.send_chat(c, r, "B", "over", "PART THREE")
    got = mcp.chat_await(chatter="A", channel=r, timeout=1, poll=0.02, nudge_after=0)
    check(got["text"] == "PART ONE\nPART TWO\nPART THREE", f"whole turn: {got['text']!r}")


def test_plain_reply_goes_to_asker() -> None:
    print("a send_message reply goes to the side that asked, not back to its sender:")
    r = "plain-mcp"
    for h in ("A", "B"):
        mcp.chat_begin(chatter=h, channel=r)
    mcp.chat_say(text="which port?", chatter="A", channel=r, wait=False)
    mcp.chat_await(chatter="B", channel=r, timeout=1, poll=0.02, nudge_after=0)
    mcp.send_message(text="use 8080", channel=r)
    b = mcp.chat_await(chatter="B", channel=r, timeout=0.3, poll=0.02, nudge_after=0)
    check(b["timed_out"], "B isn't handed its own reply")
    a = mcp.chat_await(chatter="A", channel=r, timeout=1, poll=0.02, nudge_after=0)
    check(a["from"] == "B" and a["your_turn"] and "8080" in a["text"],
          "A gets it as B's turn (sent as a chat turn, since B owed A a reply)")
    check(not mcp.chat_status(chatter="B", channel=r)["your_turn"], "and B's state agrees")
    r2 = "plain-then-say"
    for h in ("A", "B"):
        mcp.chat_begin(chatter=h, channel=r2)
    mcp.chat_say(text="q?", chatter="A", channel=r2, wait=False)
    mcp.chat_await(chatter="B", channel=r2, timeout=1, poll=0.02, nudge_after=0)
    mcp.send_message(text="(oops, wrong tool)", channel=r2)
    out = mcp.chat_say(text="answer", chatter="B", channel=r2, wait=False)
    check(out["sent_messages"] == 1, "a session's own plain message doesn't block its next chat_say")


def test_first_reply_wins() -> None:
    print("when several sessions could answer (a human kickoff), only the first reply goes out:")
    r = "kickoff"
    for h in ("A", "B"):
        mcp.chat_begin(chatter=h, channel=r)
    LocalClient("human").post_human(r, "Please discuss dropping Python 3.9.")
    for h in ("A", "B"):
        got = mcp.chat_await(chatter=h, channel=r, timeout=1, poll=0.02, nudge_after=0)
        check(got["from"] == "human" and got["your_turn"], f"{h} sees the kickoff")
    mcp.chat_say(text="I say drop it", chatter="A", channel=r, wait=False)
    try:
        mcp.chat_say(text="me too", chatter="B", channel=r, wait=False)
        raise AssertionError("B's reply should be refused: A answered first")
    except mcp.ChatSendError as exc:
        check("A posted something you haven't read" in str(exc) and "chat_await" in str(exc),
              "B is told to read A's reply first; nothing posted")
    got = mcp.chat_await(chatter="B", channel=r, timeout=1, poll=0.02, nudge_after=0)
    check(got["from"] == "A" and got["your_turn"], "B then gets A's turn and answers that")
    mcp.chat_say(text="ok", chatter="B", channel=r, wait=False)


def test_finishing_own_turn_never_blocked() -> None:
    print("finishing my own long turn isn't refused because a remark came in meanwhile:")
    r = "own-turn"
    for h in ("A", "B"):
        mcp.chat_begin(chatter=h, channel=r)
    mcp.chat_say(text="go", chatter="A", channel=r, wait=False)
    mcp.chat_await(chatter="B", channel=r, timeout=1, poll=0.02, nudge_after=0)
    mcp.chat_say(text="running tests", chatter="B", channel=r, status="working", wait=False)
    LocalClient("human").post_human(r, "fyi CI is slow today")
    out = mcp.chat_say(text="tests pass", chatter="B", channel=r, wait=False)
    check(out["sent_messages"] == 1, "B's results went out")


def test_glue_in_user_text_exact() -> None:
    print("text that itself contains the split marker still comes back exactly:")
    import random
    rnd = random.Random(11)
    for limit in (5, 30):
        for _ in range(2000):
            text = "".join(rnd.choice("ab\n" + chat.GLUE) for _ in range(rnd.randint(0, limit * 4)))
            pieces = chat.split_turn(text, limit)
            if not (all(len(p) <= limit for p in pieces) and chat.join_pieces(pieces) == text):
                raise AssertionError(f"roundtrip failed (limit {limit}): {text!r} -> {pieces!r}")
    check(True, "random texts with the marker round-trip exactly")
    t = "ends with" + chat.GLUE
    check(chat.join_pieces(chat.split_turn(t, 100)) == t,
          "a one-piece turn ending in the marker keeps it")


def test_wrap_says_confirm_with_end() -> None:
    print("a peer's wrap comes with a next that says how to confirm it:")
    c = LocalClient()
    room = "wrapn"
    chat.reset(room, "B", _latest(c, room), 20)
    chat.send_chat(c, room, "A", "wrap", "all done?", to="B")
    r = mcp.chat_await(chatter="B", channel=room, timeout=2, poll=0.05)
    check(r["status"] == "wrap" and "status='end'" in r["next"], f"next: {r['next'][:50]}")


def test_left_out_waiter_is_told_how_to_join() -> None:
    print("a session waiting while others chat without it is told how to join:")
    c = LocalClient()
    room = "leftout"
    chat.send_chat(c, room, "P1", "over", "hi P2", to="P2")
    chat.send_chat(c, room, "P2", "over", "hi P1", to="P1")
    mcp.chat_begin(chatter="P3", channel=room)
    r = mcp.chat_await(chatter="P3", channel=room, timeout=0.3, poll=0.05, nudge_after=0)
    check(r["timed_out"] and "status='ask'" in r["next"], f"next: {r['next'][:60]}")


def test_relay_position_per_session_and_forward_only() -> None:
    print("each chatting session has its own relay position, and it never moves back:")
    from discordinator import config, handles
    cfg = {"machine_label": "laptop", "chat_handle": "ProjA"}
    check(config.relay_reader(cfg, "ProjA/ui") == "laptop|ProjA/ui", "keyed by the session's handle")
    config.set_cursor("relayp", "500", "laptop|ProjA")
    check(config.get_cursor("relayp", "laptop|ProjA/ui") == "500",
          "a session's first read starts where its project left off")
    config.set_cursor("relayp", "900", "laptop|ProjA/ui")
    config.set_cursor("relayp", "700", "laptop|ProjA/ui")  # a slower reader finishing late
    check(config.get_cursor("relayp", "laptop|ProjA/ui") == "900", "never moves backwards")
    check(config.get_cursor("relayp", "laptop|ProjA/api") == "500", "a sibling role is separate")
    del handles  # (imported for symmetry with the server's use)


def test_purge_floor() -> None:
    print("purge_messages refuses to touch messages under a day old:")
    try:
        mcp.purge_messages(channel="anything", older_than_days=0, dry_run=False)
        check(False, "older_than_days=0 must be refused")
    except ValueError as e:
        check("at least 1" in str(e), "refused with the reason")


def test_errors_reach_the_model() -> None:
    print("a tool's error text reaches the model through MCP, not just its name:")
    import asyncio

    async def call(name, args):
        try:
            res = await mcp.mcp.call_tool(name, args)
            return None, res
        except Exception as e:  # noqa: BLE001
            return str(e), None

    err, _ = asyncio.run(call("chat_say", {"text": "x", "chatter": "A", "status": "bogus",
                                           "channel": "errs"}))
    check(err and "status must be one of" in err and "Nothing was posted" in err,
          f"chat_say's refusal arrives in full: {(err or '')[:60]}")
    os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "discord"
    try:
        err, _ = asyncio.run(call("send_message", {"text": "x"}))
    finally:
        os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
    check(err and len(err) > len("Error executing tool send_message: "),
          f"a config error says what's wrong: {(err or '')[:70]}")
    err, res = asyncio.run(call("chat_status", {"chatter": "A", "channel": "errs"}))
    check(err is None and res is not None, "a working tool still returns its result")
    tools = asyncio.run(mcp.mcp.list_tools())
    say = next(t for t in tools if t.name == "chat_say")
    check(len(tools) == 12 and {"text", "chatter", "status", "to"} <= set(say.input_schema["properties"]),
          "every tool still registers with its parameters")


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
    test_non_holder_working_3way()
    test_ask_doesnt_hide_unfinished_turn()
    test_long_turn_recovered_whole()
    test_timeout_mid_turn_keeps_first_half()
    test_unknown_to_warns_now()
    test_human_stop_holds_on_rejoin()
    test_interjection_mid_split_turn()
    test_peer_turn_mid_split_turn_3way()
    test_begin_mid_split_turn()
    test_long_lines_exact()
    test_typo_leaves_no_phantom()
    test_interjection_only_wakes_holder()
    test_error_after_post_is_not_a_failed_send()
    test_shared_room_two_chats()
    test_say_without_begin_ignores_old_chat()
    test_failed_read_keeps_partial()
    test_plain_reply_goes_to_asker()
    test_first_reply_wins()
    test_finishing_own_turn_never_blocked()
    test_glue_in_user_text_exact()
    test_wrap_says_confirm_with_end()
    test_left_out_waiter_is_told_how_to_join()
    test_relay_position_per_session_and_forward_only()
    test_purge_floor()
    test_errors_reach_the_model()
    print(f"\nALL {_passed} CHAT-FLOW CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
