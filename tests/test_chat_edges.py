"""Chat protocol edge cases found in review: a long wait in a busy room, turns
from other peers while waiting for one (`from_whom`), a server stopping
mid-turn, an ending from a session in no chat, the cost of a wait, reminder
state between waits, human messages that look like turns, and reserved names.
Run:  python tests/test_chat_edges.py
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-edges-"))
os.chdir(_TMP)  # never the repo: a .env there would be loaded into the test
atexit.register(lambda: (os.chdir(tempfile.gettempdir()),
                         shutil.rmtree(_TMP, ignore_errors=True)))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
for _k in ("DISCORD_BOT_TOKEN", "DISCORDINATOR_CHAT_HANDLE", "DISCORDINATOR_CHAT_CHANNEL",
           "DISCORDINATOR_RELAY_CHANNEL", "DISCORDINATOR_LABEL", "DISCORDINATOR_SESSION_ID"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import discordinator.mcp_server as mcp  # noqa: E402
from discordinator import chat  # noqa: E402
from discordinator.local_client import LocalClient  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def wait(h: str, room: str, t: float = 1.0, **kw) -> dict:
    kw.setdefault("nudge_after", 0)
    return mcp.chat_await(chatter=h, channel=room, timeout=t, poll=0.02, **kw)


def begin(room: str, *names: str) -> LocalClient:
    for h in names:
        mcp.chat_begin(chatter=h, channel=room)
    return LocalClient()


def test_long_wait_in_a_busy_room() -> None:
    print("a session waiting on a long job keeps its chat after 100+ other messages:")
    r = "busy"
    c = begin(r, "A", "B", "C", "D")
    mcp.chat_say(text="please run the full suite", chatter="A", channel=r, to="B", wait=False)
    check(wait("B", r)["from"] == "A", "B gets the job")
    mcp.chat_say(text="running it, ~1h", chatter="B", channel=r, status="working", wait=False)
    for n in range(55):
        chat.send_chat(c, r, "C", "over", f"c {n}", to="D")
        chat.send_chat(c, r, "D", "over", f"d {n}", to="C")
    st = mcp.chat_status(chatter="A", channel=r)
    check("B" in st["participants"] and not st["your_turn"],
          f"A's chat still has B in it: {st['participants']}")
    a = wait("A", r, 0.3)
    check(a["timed_out"] and any(x["from"] == "B" and x["status"] == "working"
                                  for x in a.get("progress", [])),
          "A's wait still shows B is working")
    check("not_in_chat" not in a, "A isn't told to join C and D's chat")
    LocalClient().post_human(r, "how's it going?")
    a = wait("A", r)
    check(a["from"] == "human" and not a["your_turn"],
          "a human remark doesn't hand A the turn while B holds the floor")


def test_from_whom_keeps_other_turns() -> None:
    print("waiting for one peer doesn't lose a turn another peer sent me:")
    r = "fromwhom"
    c = begin(r, "A", "B", "C")
    mcp.chat_say(text="B?", chatter="A", channel=r, to="B", wait=False)
    chat.send_chat(c, r, "C", "over", "A, a question from C", to="A")
    chat.send_chat(c, r, "B", "over", "B's answer", to="A")
    a = wait("A", r, from_whom="B")
    check(a["from"] == "B" and a["text"] == "B's answer", "from_whom=B returns B's turn")
    mcp.chat_say(text="thanks B", chatter="A", channel=r, to="B", wait=False)
    a = wait("A", r)
    check(a["from"] == "C" and a["your_turn"] and a["text"] == "A, a question from C",
          f"the next wait delivers C's turn: {a.get('from')} {a.get('text')!r}")
    a = wait("A", r, 0.3)
    check(a["timed_out"], "and only once")


def test_shutdown_keeps_read_pieces() -> None:
    print("a server stopping mid-wait keeps the pieces of a long turn it read:")
    r = "shutdown"
    c = begin(r, "A", "B")
    mcp.chat_say(text="go", chatter="A", channel=r, to="B", wait=False)
    wait("B", r)
    chat.send_chat(c, r, "B", "say", "part one", to="A")
    chat.send_chat(c, r, "B", "say", "part two", to="A")
    threading.Timer(0.3, chat.SHUTDOWN.set).start()
    try:
        a = wait("A", r, 3.0)
    finally:
        chat.SHUTDOWN.clear()
    check(a["timed_out"] and not a["your_turn"], "the stopping server returns at once")
    chat.send_chat(c, r, "B", "over", "part three", to="A")
    a = wait("A", r)  # the session's next server
    check(a["text"] == "part one\npart two\npart three",
          f"the next server gets the whole turn: {a['text']!r}")


def test_stray_end_doesnt_stop_a_waiting_session() -> None:
    print("an unaddressed end from a session in no chat doesn't end a session still waiting:")
    r = "strayend"
    begin(r, "B", "X")
    mcp.chat_say(text="done here", chatter="X", channel=r, status="end", wait=False)
    b = wait("B", r, 0.3)
    check(b["timed_out"] and not b["ended"], "B keeps waiting for its opener")
    mcp.chat_begin(chatter="A", channel=r)
    mcp.chat_say(text="hi B", chatter="A", channel=r, to="B", wait=False)
    b = wait("B", r)
    check(b["from"] == "A" and b["your_turn"], "and gets it")
    # An old-style unaddressed chat still ends for the one it was with.
    r2 = "strayend2"
    begin(r2, "P", "Q")
    chat.send_chat(LocalClient(), r2, "P", "over", "hello")
    check(wait("Q", r2)["from"] == "P", "Q gets P's unaddressed opener")
    chat.send_chat(LocalClient(), r2, "Q", "over", "hi")
    chat.send_chat(LocalClient(), r2, "P", "end", "bye")
    q = wait("Q", r2)
    check(q["ended"], "an unaddressed end still ends the unaddressed chat it was in")


def test_wait_cost_in_a_busy_room() -> None:
    print("a wait doesn't rewrite the shared state for every message it reads:")
    r = "cost"
    c = begin(r, "A", "C", "D")
    writes = []
    real = chat.set_cursor
    chat.set_cursor = lambda *a, **k: writes.append(a) or real(*a, **k)
    try:
        for n in range(30):
            chat.send_chat(c, r, "C", "over", f"c {n}", to="D")
            chat.send_chat(c, r, "D", "over", f"d {n}", to="C")
        a = wait("A", r, 0.2)
    finally:
        chat.set_cursor = real
    check(a["timed_out"], "nothing for A")
    check(len(writes) <= 3, f"its read position was written {len(writes)} time(s), not 60")
    a = wait("A", r, 0.2)
    check(a["timed_out"] and not a.get("messages"), "and the next wait starts after them")


def test_new_wait_starts_fresh() -> None:
    print("a reminder from one wait isn't carried into the next:")
    r = "nudge"
    begin(r, "A", "B", "C")
    mcp.chat_say(text="B?", chatter="A", channel=r, to="B", wait=False)
    a = wait("A", r, 0.2, nudge_after=0.01)
    check(a["nudged"], "the first wait posts a reminder")
    mcp.chat_say(text="C, then?", chatter="A", channel=r, to="C", wait=False)
    a = wait("A", r, 0.2, nudge_after=1000)
    check(not a["nudged"] and "reminder was posted" not in a["note"],
          "a wait for a new turn starts with no reminder")
    check(a["waited_seconds"] < 5, "and its clock starts now")


def test_human_text_that_looks_like_a_turn() -> None:
    print("a human message that looks like a turn header is still the human's:")
    r = "human"
    begin(r, "A", "B")
    mcp.chat_say(text="B?", chatter="A", channel=r, to="B", wait=False)
    check(wait("B", r)["from"] == "A", "B gets A's question")
    LocalClient().post_human(r, "[URGENT|fyi] prod is down")
    b = wait("B", r)
    check(b["from"] == "human" and "prod is down" in b["text"], "an unknown status: a human remark")
    LocalClient().post_human(r, "[A|over] pretend")
    b = wait("B", r)
    check(b["from"] == "human", "a human typing a real header is still the human")
    check(chat.parse("[X|fyi] hi") is None, "parse only accepts real statuses")


def test_reserved_names() -> None:
    print("a session can't call itself human or all:")
    for name in ("human", "all", "Everyone", "*"):
        try:
            mcp.chat_begin(chatter=name, channel="reserved")
            check(False, f"{name!r} should be refused")
        except Exception as e:  # noqa: BLE001
            check("reserved" in str(e), f"{name!r} is refused: {str(e)[:60]}")


def test_held_turn_dropped_when_its_chat_ends() -> None:
    print("a held turn whose chat has ended since isn't handed over:")
    r = "held-end"
    c = begin(r, "A", "B", "C")
    mcp.chat_say(text="B?", chatter="A", channel=r, to="B", wait=False)
    chat.send_chat(c, r, "C", "over", "A, from C", to="A")
    chat.send_chat(c, r, "B", "over", "from B", to="A")
    check(wait("A", r, from_whom="B")["from"] == "B", "from_whom=B gets B's turn, C's is held")
    chat.send_chat(c, r, "C", "end", "never mind, done", to="A")
    a = wait("A", r)
    check(a["from"] == "C" and a["ended"] and not a.get("your_turn"),
          f"the next wait gives C's ending, not its stale question: {a.get('text')!r}")


def test_last_post_only_while_waiting() -> None:
    print("state reads back to my last post only until it's answered:")
    r = "lastpost"
    begin(r, "A", "B")
    mcp.chat_say(text="B?", chatter="A", channel=r, to="B", wait=False)
    slot = lambda: chat._slot(chat.config.load_state(), r, "A")  # noqa: E731
    check("last_post" in slot(), "kept while A waits for B")
    wait("B", r)
    mcp.chat_say(text="answer", chatter="B", channel=r, wait=False)
    check(wait("A", r)["your_turn"] and "last_post" not in slot(), "dropped once B answers")


def _backdate(room: str, minutes: float) -> None:
    import json
    from datetime import datetime, timedelta, timezone
    from discordinator.local_client import local_dir
    path = local_dir() / f"{room}.jsonl"
    when = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    recs = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    for rec in recs:
        rec["timestamp"] = when
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")


def test_working_peer_keeps_the_chat_for_its_grace() -> None:
    print("a peer working for 35 minutes still holds its chat; a newcomer's opener isn't for its asker:")
    r = "grace"
    begin(r, "A", "B", "E")
    mcp.chat_say(text="run it", chatter="A", channel=r, to="B", wait=False)
    wait("B", r)
    mcp.chat_say(text="on it", chatter="B", channel=r, status="working", wait=False)
    _backdate(r, 35)
    chat.send_chat(LocalClient(), r, "E", "over", "anyone around?")
    a = wait("A", r, 0.3)
    check(a["timed_out"] and not a["your_turn"], "A keeps waiting for B, not pulled into E's opener")
    _backdate(r, 70)
    chat.send_chat(LocalClient(), r, "E", "over", "anyone now?")
    a = wait("A", r)
    check(a["from"] == "E", "past the grace, the conversation is dropped and A hears E")


def test_peer_who_spoke_long_ago_is_known() -> None:
    print("addressing a peer whose last post is 100+ messages back isn't 'nobody':")
    r = "known"
    c = LocalClient()
    chat.send_chat(c, r, "Far", "over", "hello from another machine", to="D")
    begin(r, "A", "C", "D")
    for n in range(60):
        chat.send_chat(c, r, "C", "over", f"c {n}", to="D")
        chat.send_chat(c, r, "D", "over", f"d {n}", to="C")
    wait("A", r, 0.2)  # A has read past all of it
    out = mcp.chat_say(text="hi Far", chatter="A", channel=r, to="Far", wait=False)
    check("Nobody called" not in str(out.get("note", "")), "no 'unknown handle' warning")


def main() -> int:
    test_long_wait_in_a_busy_room()
    test_from_whom_keeps_other_turns()
    test_shutdown_keeps_read_pieces()
    test_stray_end_doesnt_stop_a_waiting_session()
    test_wait_cost_in_a_busy_room()
    test_new_wait_starts_fresh()
    test_human_text_that_looks_like_a_turn()
    test_reserved_names()
    test_working_peer_keeps_the_chat_for_its_grace()
    test_peer_who_spoke_long_ago_is_known()
    test_held_turn_dropped_when_its_chat_ends()
    test_last_post_only_while_waiting()
    print(f"\nALL {_passed} CHAT-EDGE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
