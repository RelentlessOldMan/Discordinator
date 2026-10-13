"""Sessions sharing one chat room, or one machine.

AGENTS.md has every project chat in the same room, so several conversations
run there at once. Each must behave as if it were alone: another pair's ending,
turns and members never reach it. Also covers what sessions on one machine
share: the relay read position, the home config, and the room the human's
commands act on.
Run:  python tests/test_shared_room.py
"""

from __future__ import annotations

import json
import atexit
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-shared-"))
os.chdir(_TMP)  # never the repo: a .env there would be loaded into the test
atexit.register(lambda: (os.chdir(tempfile.gettempdir()),
                         shutil.rmtree(_TMP, ignore_errors=True)))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
for _k in ("DISCORD_BOT_TOKEN", "DISCORDINATOR_CHAT_HANDLE", "DISCORDINATOR_CHAT_CHANNEL",
           "DISCORDINATOR_RELAY_CHANNEL", "DISCORDINATOR_LABEL", "DISCORDINATOR_ALLOW_SEND"):
    os.environ.pop(_k, None)

os.environ["DISCORDINATOR_LABEL"] = "M"  # this machine's name, in every full name

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import discordinator.mcp_server as mcp  # noqa: E402
from discordinator import chat, cli, config, handles  # noqa: E402
from discordinator.local_client import LocalClient  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def wait(h: str, room: str, t: float = 1.0) -> dict:
    return mcp.chat_await(chatter=h, channel=room, timeout=t, poll=0.02, nudge_after=0)


def two_pairs(room: str) -> LocalClient:
    """A<->B and C<->D chatting in one room, each pair's turns addressed."""
    c = LocalClient()
    for h in ("A", "B", "C", "D"):
        mcp.chat_begin(chatter=h, channel=room)
    chat.send_chat(c, room, "M:C", "over", "D, status?", to="M:D")
    chat.send_chat(c, room, "M:D", "over", "all good", to="M:C")
    chat.send_chat(c, room, "M:A", "over", "B, can you review my diff?", to="M:B")
    return c


def test_other_chats_end_isnt_mine() -> None:
    print("another conversation ending doesn't end mine:")
    r = "end-room"
    c = two_pairs(r)
    check(wait("B", r)["from"] == "M:A", "B gets A's question")
    mcp.chat_say(text="looks fine, one question", chatter="B", channel=r, wait=False)
    chat.send_chat(c, r, "M:D", "wrap", "wrap?", to="M:C")
    chat.send_chat(c, r, "M:C", "end", "bye", to="M:D")
    b = wait("B", r, 0.3)
    check(b["timed_out"] and not b["ended"], "B keeps waiting for A when C and D end theirs")
    a = wait("A", r)
    check(a["from"] == "M:B" and a["your_turn"] and not a["ended"], "A gets B's reply")
    st = mcp.chat_status(chatter="A", channel=r)
    check(not st["ended"] and st["session_active"], "A's chat is still going")
    d = mcp.chat_status(chatter="D", channel=r)
    check(d["ended"] and d["stop_reason"] == "agreed", "...while C and D's has ended")
    mcp.chat_say(text="thanks", chatter="A", channel=r, wait=False)
    wait("B", r)
    mcp.chat_say(text="done here", chatter="B", channel=r, status="end", wait=False)
    a = wait("A", r)
    check(a["ended"] and a["from"] == "M:B", "B's own `end` (addressed for it) ends A's chat")
    last = LocalClient().read_messages(r, limit=1)[0]["content"]
    check(last.startswith("[M:B>M:A|end]"), f"an unaddressed end goes to the peer: {last[:12]}")


def test_two_pairs_not_multiparty() -> None:
    print("two separate two-person chats aren't one group chat:")
    r = "pairs-room"
    two_pairs(r)
    b = wait("B", r)
    check(not b.get("multiparty") and "suggest_next" not in b,
          "B's turn from A isn't multiparty, and suggests nobody from the other chat")
    st = mcp.chat_status(chatter="B", channel=r)
    check(set(st["participants"]) == {"M:A", "M:B"} and st["floor"] == "M:B",
          f"B's chat is A and B: {st['participants']}")
    out = mcp.chat_say(text="sure", chatter="B", channel=r, wait=False)
    check("suggest_next" not in out and "waiting" not in out, "B's reply isn't told to rotate")


def test_opener_survives_other_traffic() -> None:
    print("an unaddressed opener reaches a responder who joins after other chats' turns:")
    r = "opener-room"
    c = LocalClient()
    mcp.chat_begin(chatter="A", channel=r)
    mcp.chat_say(text="Hi! Let's plan the migration.", chatter="A", channel=r, wait=False)
    chat.send_chat(c, r, "M:D", "over", "C: here are the logs", to="M:C")
    chat.send_chat(c, r, "M:C", "over", "thanks D", to="M:D")
    b = mcp.chat_begin(chatter="B", channel=r)
    check(b["recovered_pending_turn"], "B's chat_begin finds A's opener")
    got = wait("B", r)
    check(got["from"] == "M:A" and "migration" in got["text"], "and B receives it")
    d = wait("D", r, 0.3)
    check(d["from"] == "M:C", f"D, talking with C, gets C's turn - not A's opener ({d['from']})")
    d = wait("D", r, 0.3)
    check(d["timed_out"] or d.get("already_received"), "and is never handed A's opener")


def test_other_chats_broadcast_and_plain() -> None:
    print("another conversation's unaddressed turns and plain replies stay there:")
    r = "bcast-room"
    c = LocalClient()
    for h in ("A", "B", "C", "D", "E"):
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "M:A", "over", "B?", to="M:B")
    wait("B", r)
    mcp.chat_say(text="yes A", chatter="B", channel=r, wait=False)
    wait("A", r)  # A now holds the floor in A<->B
    chat.send_chat(c, r, "M:C", "over", "D, E: thoughts?", to="M:D")
    chat.send_chat(c, r, "M:D", "over", "E?", to="M:E")
    chat.send_chat(c, r, "M:E", "over", "anyone - here's mine", to="all")
    b = wait("B", r, 0.3)
    check(b["timed_out"], "a group's to='all' turn doesn't wake B, who's in another chat")
    c2 = wait("C", r)
    check(c2["from"] == "M:E", "...but does wake the group's own members")
    st = mcp.chat_status(chatter="A", channel=r)
    check(st["your_turn"] and st["floor"] == "M:A", "A is still owed B's reply")


def test_owed_turn_past_one_page() -> None:
    print("a turn owed to an idle session survives a busy room:")
    r = "busy-room"
    c = LocalClient()
    for h in ("A", "B"):
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "M:A", "over", "B: question about the schema", to="M:B")
    for i in range(70):
        chat.send_chat(c, r, "M:C", "over", f"c{i}", to="M:D")
        chat.send_chat(c, r, "M:D", "over", f"d{i}", to="M:C")
    st = mcp.chat_status(chatter="B", channel=r)
    check(st["your_turn"], "chat_status still sees the turn 140 messages back")
    begin = mcp.chat_begin(chatter="B", channel=r)
    check(begin["recovered_pending_turn"], "chat_begin recovers it")
    got = wait("B", r)
    check(got["from"] == "M:A" and "schema" in got["text"], "and chat_await delivers it")


def test_status_for_second_session() -> None:
    print("a second session's chat_status answers for itself, not its sibling:")
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "CodeCarver"
    handles._resolved.clear()  # a fresh session: no name used yet
    handles._since.clear()
    handles._last_chatter = None
    try:
        r = "sibling-room"
        other = os.getppid()  # a live process standing in for the sibling session
        reg = handles.registry_path()
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(json.dumps({"codecarver": {
            "handle": "CodeCarver", "pid": other, "ts": time.time(),
            "started": handles.process_started(other)}}), encoding="utf-8")
        chat.send_chat(LocalClient(), r, "Peer", "over", "CodeCarver: your turn", to="M:CodeCarver")
        st = mcp.chat_status(channel=r)
        check(not st["your_turn"], "the sibling's turn isn't reported as mine")
        b = mcp.chat_begin(channel=r)
        check(b["chatter"] == "M:CodeCarver-2", f"and chat_begin names me as status did: {b['chatter']}")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")
        handles._resolved.clear()
        handles._since.clear()
        handles._last_chatter = None


def test_cli_stop_finds_the_room() -> None:
    print("the human's `stop` reaches the room the sessions use:")
    os.environ["DISCORDINATOR_CHAT_CHANNEL"] = "claudes-chatroom"  # the sessions' .mcp.json
    mcp.chat_begin(chatter="A")
    mcp.chat_begin(chatter="B")
    mcp.chat_say(text="ping", chatter="A", to="B", wait=False)
    os.environ.pop("DISCORDINATOR_CHAT_CHANNEL")  # the human's shell doesn't have it
    cli.main(["stop"])
    os.environ["DISCORDINATOR_CHAT_CHANNEL"] = "claudes-chatroom"
    try:
        wait("B", None)
        b = wait("B", None)
        check(b["ended"] and b["stop_reason"] == "human", "B's chat is stopped")
        check(not (config.config_path().parent / "local" / "chat.jsonl").exists(),
              "nothing went to the default room")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_CHANNEL")


def test_relay_between_two_sessions() -> None:
    print("two sessions relaying on one machine don't consume each other's messages:")

    sent: dict[str, set] = {}
    current = {"label": None}
    handles._resolved.clear()  # sessions that haven't chatted: tagged by project
    handles._last_chatter = None

    def as_session(label: str) -> None:
        # Each session is its own MCP server process: its own label (set per
        # project in .mcp.json) and its own record of what it sent.
        if current["label"]:
            sent[current["label"]] = set(mcp._sent_ids)
        mcp._sent_ids.clear()
        mcp._sent_ids.update(sent.get(label, set()))
        current["label"] = label
        os.environ["DISCORDINATOR_LABEL"] = label
        handles._SESSION_KEY = "id:" + label  # its own Claude Code session

    try:
        as_session("sessA")
        mcp.send_message("handoff: please review PR 42", channel="relay-room")
        check(mcp.get_new_messages(channel="relay-room") == [], "A sees nothing new (its own post)")
        as_session("sessB")
        got = mcp.get_new_messages(channel="relay-room")
        check([m["content"] for m in got] == ["[sessA:DefaultChatName] handoff: please review PR 42"],
              "B still gets A's message after A checked its inbox")
        mcp.send_message("done", channel="relay-room")
        as_session("sessA")
        got = mcp.get_new_messages(channel="relay-room")
        check([m["content"] for m in got] == ["[sessB:DefaultChatName] done"], "and A gets B's answer")
        check(mcp.get_new_messages(channel="relay-room") == [], "each read moves only its own position")
    finally:
        os.environ["DISCORDINATOR_LABEL"] = "M"
        mcp._sent_ids.clear()
        handles._SESSION_KEY = None


def test_config_set_saves_only_the_setting() -> None:
    print("`config set-*` never saves this shell's env settings into the shared config:")
    env = {"DISCORDINATOR_ALLOW_SEND": "1", "DISCORDINATOR_CHAT_HANDLE": "CodeCarver",
           "DISCORD_BOT_TOKEN": "temporary-token-from-env"}
    os.environ.update(env)
    try:
        cli.main(["config", "set-label", "laptop"])
    finally:
        for k in env:
            os.environ.pop(k)
    saved = json.loads(config.config_path().read_text(encoding="utf-8"))
    check(saved.get("machine_label") == "laptop", "the label is saved")
    leaked = [k for k in ("allow_send_attachments", "chat_handle", "token", "relay_transport")
              if k in saved]
    check(not leaked, f"nothing from the environment is: {leaked}")
    cfg = config.load()
    check(not cfg["allow_send_attachments"], "the send opt-in is still off for other sessions")


def test_two_answers_at_once() -> None:
    print("two sessions answering the same human at the same moment: one gets through:")
    r = "race-room"
    for h in ("A", "B"):
        mcp.chat_begin(chatter=h, channel=r)
    LocalClient().post_human(r, "what do you both think?")
    for h in ("A", "B"):
        wait(h, r)
    real = chat.unread_for_me
    gate = threading.Barrier(2)

    def slow_unread(*a, **k):  # both pass the check before either posts
        out = real(*a, **k)
        try:
            gate.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return out

    chat.unread_for_me = slow_unread
    results: dict[str, str] = {}

    def answer(h: str) -> None:
        try:
            mcp.chat_say(text=f"{h}'s view", chatter=h, channel=r, wait=False)
            results[h] = "posted"
        except mcp.ChatSendError:
            results[h] = "refused"

    try:
        ts = [threading.Thread(target=answer, args=(h,)) for h in ("A", "B")]
        [t.start() for t in ts]
        [t.join() for t in ts]
    finally:
        chat.unread_for_me = real
    check(sorted(results.values()) == ["posted", "refused"], f"one posts, one is refused: {results}")


def test_relay_after_restart_and_transport_switch() -> None:
    print("a restarted server doesn't return its own messages; a position from the other transport heals:")
    os.environ["DISCORDINATOR_LABEL"] = "restarter"
    try:
        mcp.send_message("mine, before the restart", channel="restart-room")
        mcp._sent_ids.clear()  # a new server process after /mcp reconnect
        check(mcp.get_new_messages(channel="restart-room") == [],
              "its own message isn't handed back after a restart")
        # A read position far past anything in the room - e.g. kept from the
        # local transport (time_ns ids) after switching the channel to Discord.
        reader = mcp._reader(config.load())
        config.set_cursor("restart-room", str(10 ** 30), reader)
        LocalClient("other").send_message("restart-room", "from the other side", label="peer")
        got = mcp.get_new_messages(channel="restart-room")
        check([m["content"] for m in got] == ["[peer] from the other side"],
              "a position past the newest message starts over instead of hiding everything")
        LocalClient("other").send_message("restart-room", "and again", label="peer")
        got = mcp.get_new_messages(channel="restart-room")
        check([m["content"] for m in got] == ["[peer] and again"], "and reads go on from there")
    finally:
        os.environ["DISCORDINATOR_LABEL"] = "M"
        mcp._sent_ids.clear()


def test_same_label_sessions_relay() -> None:
    print("two sessions with the SAME label relay to each other:")
    a_sent = set()

    def as_session(handle: str, sent: set) -> None:
        # One MCP server process per session: its own sent-ids and project handle.
        os.environ["DISCORDINATOR_CHAT_HANDLE"] = handle
        mcp._sent_ids.clear()
        mcp._sent_ids.update(sent)
        handles._SESSION_KEY = "id:" + handle  # its own Claude Code session

    handles._resolved.clear()  # sessions that haven't chatted: tagged by project
    handles._last_chatter = None
    os.environ["DISCORDINATOR_LABEL"] = "laptop"  # shared, e.g. from `config set-label`
    try:
        as_session("ProjA", set())
        mcp.send_message("A: please review", channel="label-room")
        a_sent = set(mcp._sent_ids)
        check(mcp.get_new_messages(channel="label-room") == [], "A doesn't see its own message")
        as_session("ProjB", set())
        got = mcp.get_new_messages(channel="label-room")
        check([m["content"] for m in got] == ["[laptop:ProjA] A: please review"],
              "B sees A's message though they share a label")
        mcp.send_message("B: done", channel="label-room")
        b_sent = set(mcp._sent_ids)
        as_session("ProjA", a_sent)
        got = mcp.get_new_messages(channel="label-room")
        check([m["content"] for m in got] == ["[laptop:ProjB] B: done"], "and A sees B's answer")
        as_session("ProjB", b_sent)
        check(mcp.get_new_messages(channel="label-room") == [], "neither re-reads or loses anything")
    finally:
        os.environ["DISCORDINATOR_LABEL"] = "M"
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")
        mcp._sent_ids.clear()
        handles._SESSION_KEY = None


def test_plain_reply_reaches_its_peer() -> None:
    print("a chat reply sent with send_message reaches the right peer, not another chat:")
    r = "plain-room"
    c = LocalClient()
    for h in ("A", "B", "C", "D"):
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "M:A", "over", "B, can you review my diff?", to="M:B")
    wait("B", r)  # B now owes A a reply
    chat.send_chat(c, r, "M:C", "over", "D, status?", to="M:D")  # another pair goes on
    out = mcp.send_message("looks good to me", channel=r)  # B answers with the wrong tool
    check("chat turn to M:A" in out, f"it goes out as B's chat turn to A: {out[:60]}")
    a = wait("A", r)
    check(a["from"] == "M:B" and a["text"] == "looks good to me" and a["your_turn"],
          "A gets it as B's reply")
    d = wait("D", r)
    check(d["from"] == "M:C", "D still gets C's turn, not B's message")
    plain = mcp.send_message("an unrelated note", channel="not-a-chat-room")
    check(plain.startswith("Sent 1 message"), "elsewhere, send_message is a plain relay post")


def _backdate(room: str, minutes: float) -> None:
    from datetime import datetime, timedelta, timezone
    from discordinator.local_client import local_dir
    path = local_dir() / f"{room}.jsonl"
    when = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    recs = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    for rec in recs:
        rec["timestamp"] = when
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")


def test_long_work_reply_goes_to_its_asker() -> None:
    print("a reply after 30+ minutes of quiet work still goes back to its asker:")
    r = "long-work"
    c = LocalClient()
    mcp.chat_begin(chatter="B", channel=r)
    chat.send_chat(c, r, "M:A", "over", "please run the full suite and report", to="M:B")
    check(wait("B", r)["from"] == "M:A", "B gets A's request")
    _backdate(r, 40)  # B works for 40 minutes without posting
    chat.send_chat(c, r, "M:C", "over", "anyone around to look at my PR?")  # unaddressed
    out = mcp.chat_say(text="all tests pass", chatter="B", channel=r, wait=False)
    check(out.get("posted", True) and "error" not in out, "B's results aren't refused")
    last = c.read_messages(r, limit=1)[0]["content"]
    check(last.startswith("[M:B>M:A|over]"), f"they go to A: {last[:14]}")
    st = chat.compute_state(c, r, "M:A")
    check(st["your_turn"], "and it's A's turn")


def test_opener_after_a_dropped_chat() -> None:
    print("an opener from someone whose last chat was dropped (no end) is open to all:")
    r = "dropped"
    c = LocalClient()
    for h in ("A", "B", "C"):
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "M:A", "over", "hi B", to="M:B")
    chat.send_chat(c, r, "M:B", "over", "thanks, bye", to="M:A")
    _backdate(r, 60)  # nobody said `end`; an hour passes
    mcp.chat_begin(chatter="C", channel=r)
    chat.send_chat(c, r, "M:B", "over", "new topic, anyone?")
    got = wait("C", r)
    check(got["your_turn"] and got["from"] == "M:B", "C, in no conversation, gets B's opener")
    r = "not-dropped"
    for h in ("A", "B", "C"):
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "M:A", "over", "hi B", to="M:B")
    chat.send_chat(c, r, "M:B", "over", "one more thing", to="M:A")
    _backdate(r, 10)  # still a live conversation
    mcp.chat_begin(chatter="C", channel=r)
    chat.send_chat(c, r, "M:B", "over", "and for everyone in this chat")
    check(wait("C", r, 0.3)["timed_out"], "10 minutes later it's still A and B's chat")


def test_dead_role_not_taken_back() -> None:
    print("a new session takes back a role waiting on its turn - not one from a dead chat:")
    r = "role-back"
    c = LocalClient()
    chat.send_chat(c, r, "M:ProjectB/convex", "over", "question for X", to="X")
    check(mcp._left_waiting(c, r, "ProjectB", config.load()) == ["M:ProjectB/convex"],
          "a role waiting on its own recent turn is found")
    _backdate(r, 3 * 24 * 60)
    check(mcp._left_waiting(c, r, "ProjectB", config.load()) == [], "three days later it isn't")


def test_disconnect_lets_a_post_finish() -> None:
    print("a server whose client hangs up lets a call in progress finish before exiting:")
    exited = threading.Event()
    real_exit = os._exit
    os._exit = lambda code: exited.set()  # type: ignore[assignment]
    try:
        with mcp._running_lock:
            mcp._running += 1  # a long chat_say still posting its pieces
        mcp._disconnected()
        check(not exited.wait(2.0), "it doesn't exit while the call is still going")
        with mcp._running_lock:
            mcp._running -= 1
        check(exited.wait(3.0), "and exits once it's done")
    finally:
        os._exit = real_exit  # type: ignore[assignment]
        chat.SHUTDOWN.clear()


def test_held_turn_survives_chat_begin() -> None:
    print("a turn held while waiting for someone else survives a chat_begin:")
    r, c = "held-begin", LocalClient()
    for h in "ABC":
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "M:B", "over", "B asks A", to="M:A")
    chat.send_chat(c, r, "M:C", "over", "C asks A", to="M:A")
    got = mcp.chat_await(chatter="A", channel=r, timeout=1, poll=0.02, nudge_after=0, from_whom="C")
    check(got.get("from") == "M:C", f"it waits for C: {got.get('from')}")
    mcp.chat_say(text="to C", chatter="A", channel=r, wait=False)
    st = mcp.chat_status(chatter="A", channel=r)
    check(st["your_turn"] and st.get("held_from") == ["M:B"], f"chat_status says B's is owed: {st.get('held_from')}")
    b = mcp.chat_begin(chatter="A", channel=r)
    check(b["recovered_pending_turn"], "chat_begin says a turn is owed")
    got = wait("A", r)
    check(got.get("from") == "M:B" and got.get("text") == "B asks A", f"and it arrives: {got.get('text')}")
    check(got.get("multiparty") and "suggest_next" in got, "with the floor context of a multiparty room")


def test_no_unaddressed_reply_over_the_floor() -> None:
    print("an unaddressed turn doesn't go back to an old asker while someone else has the floor:")
    r = "old-asker"
    for h in "ABC":
        mcp.chat_begin(chatter=h, channel=r)
    mcp.chat_say(text="C?", chatter="A", channel=r, to="M:C", wait=False)
    wait("C", r)
    mcp.chat_say(text="I think X", chatter="C", channel=r, wait=False)
    wait("A", r)
    mcp.chat_say(text="B?", chatter="A", channel=r, to="M:B", wait=False)
    try:
        mcp.chat_say(text="also Y", chatter="C", channel=r, wait=False)
        check(False, "C's unaddressed turn went out over B's floor")
    except Exception as e:
        check("has the floor" in str(e), f"refused: {str(e)[:40]}")
    st = mcp.chat_status(chatter="B", channel=r)
    check(st["your_turn"] and st["floor"] == "M:B", "B still has it")


def test_a_first_word_end_ends_nobody_elses() -> None:
    print("a session whose first word is an unaddressed end doesn't close a waiting opener:")
    r = "first-word-end"
    mcp.chat_begin(chatter="Op", channel=r)
    mcp.chat_say(text="anyone up for planning?", chatter="Op", channel=r, wait=False)
    mcp.chat_begin(chatter="Nope", channel=r)
    mcp.chat_say(text="not me, bye", chatter="Nope", channel=r, status="end", wait=False)
    got = wait("Op", r, 0.3)
    check(not got.get("ended"), f"the opener's wait goes on: {got.get('stop_reason')}")
    b = mcp.chat_begin(chatter="Later", channel=r)
    check(b["recovered_pending_turn"], "and a session joining later is handed the opener")
    got = wait("Later", r)
    check(got.get("from") == "M:Op", f"from: {got.get('from')}")


def test_long_ask_stays_an_ask() -> None:
    print("a long hand-raise isn't stuck onto the front of its sender's next turn:")
    r = "long-ask"
    for h in "ABC":
        mcp.chat_begin(chatter=h, channel=r)
    mcp.chat_say(text="B?", chatter="A", channel=r, to="M:B", wait=False)
    mcp.chat_say(text="ASK " * 600, chatter="C", channel=r, status="ask", to="M:A", wait=False)
    wait("B", r)
    mcp.chat_say(text="B answers", chatter="B", channel=r, wait=False)
    check(wait("A", r).get("text") == "B answers", "A gets B's answer")
    mcp.chat_say(text="C, go", chatter="A", channel=r, to="M:C", wait=False)
    wait("C", r)
    mcp.chat_say(text="my point", chatter="C", channel=r, wait=False)
    got = wait("A", r)
    check(got.get("text") == "my point", f"and then C's turn, as sent: {got.get('text', '')[:20]}")


def test_a_turn_to_yourself_is_refused() -> None:
    print("a turn addressed to yourself is refused:")
    r = "to-self"
    mcp.chat_begin(chatter="Me", channel=r)
    try:
        mcp.chat_say(text="note to self", chatter="Me", channel=r, to="M:Me", wait=False)
        check(False, "it was posted")
    except Exception as e:
        check("is you" in str(e) and "Nothing was posted" in str(e), f"refused: {str(e)[:40]}")


def test_a_superseded_turn_isnt_handed_over() -> None:
    print("a turn whose sender has since handed the turn to someone else isn't handed over:")
    c, r = LocalClient(), "moved-on"
    for h in "ABC":
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "M:B", "over", "A, what do you think?", to="M:A")
    chat.send_chat(c, r, "M:B", "over", "C, and you?", to="M:C")
    got = wait("A", r, 0.3)
    st = mcp.chat_status(chatter="A", channel=r)
    check(not got.get("your_turn") and not st["your_turn"],
          f"chat_await and chat_status agree: not A's turn ({got.get('text')!r})")
    check(wait("C", r, 0.3).get("text") == "C, and you?", "C's is handed to C")


def test_suggest_next_is_never_me() -> None:
    print("after a turn to all, suggest_next is never the session it's suggested to:")
    c, r = LocalClient(), "suggest-me"
    for h in "ABC":
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "M:A", "over", "B?", to="M:B")
    chat.send_chat(c, r, "M:B", "over", "C?", to="M:C")
    chat.send_chat(c, r, "M:C", "over", "A?", to="M:A")
    chat.send_chat(c, r, "M:A", "over", "B first", to="M:B")
    chat.send_chat(c, r, "M:B", "over", "anyone?", to="all")
    for h in "CA":
        mcp.chat_begin(chatter=h, channel=r)
    got = wait("C", r)
    check(got.get("your_turn") and got.get("suggest_next") not in (None, "M:C"),
          f"C is told someone else: {got.get('suggest_next')}")
    out = mcp.chat_say(text="hi", chatter="C", channel=r, to=got["suggest_next"], wait=False)
    check(out.get("to") == got["suggest_next"] and out.get("message_ids"), f"and addressing it works: {out}")


def test_side_note_stays_out_of_the_next_turn() -> None:
    print("say pieces to one peer aren't part of the same sender's next turn to another:")
    c, r = LocalClient(), "side-note"
    for h in "ABC":
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "M:A", "over", "B?", to="M:B")
    chat.send_chat(c, r, "M:C", "say", "(side note while B thinks)", to="M:B")
    chat.send_chat(c, r, "M:B", "over", "C, yours", to="M:C")
    chat.send_chat(c, r, "M:C", "over", "A: real answer", to="M:A")
    check(mcp.chat_status(chatter="A", channel=r)["your_turn"], "A's turn")
    got = wait("A", r)
    check(got.get("text") == "A: real answer", f"chat_await: just the turn: {got.get('text')!r}")
    b = mcp.chat_begin(chatter="A", channel=r)
    got = wait("A", r)
    check(got.get("text") == "A: real answer", f"handed over again: {got.get('text')!r}")


def test_an_old_stop_isnt_a_newcomers() -> None:
    print("a stop that others have chatted since doesn't tell a newcomer the room is stopped:")
    c, r = LocalClient(), "old-stop"
    for h in "ABCD":
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "M:A", "over", "hi", to="M:B")
    c.post_human(r, "stop")
    chat.send_chat(c, r, "M:C", "over", "D?", to="M:D")
    chat.send_chat(c, r, "M:D", "over", "C!", to="M:C")
    b = mcp.chat_begin(chatter="E", channel=r)
    check(not b["state"]["ended"] and b["state"].get("stop_reason") is None,
          f"not stopped: {b['state'].get('stop_reason')}")
    r2 = "new-stop"
    for h in "AB":
        mcp.chat_begin(chatter=h, channel=r2)
    chat.send_chat(c, r2, "M:A", "over", "hi", to="M:B")
    c.post_human(r2, "stop")
    b = mcp.chat_begin(chatter="E", channel=r2)
    check(b["state"]["stop_reason"] == "human", "one with nothing since still is")


def main() -> int:
    test_held_turn_survives_chat_begin()
    test_no_unaddressed_reply_over_the_floor()
    test_a_first_word_end_ends_nobody_elses()
    test_long_ask_stays_an_ask()
    test_a_turn_to_yourself_is_refused()
    test_other_chats_end_isnt_mine()
    test_two_pairs_not_multiparty()
    test_opener_survives_other_traffic()
    test_other_chats_broadcast_and_plain()
    test_owed_turn_past_one_page()
    test_status_for_second_session()
    test_cli_stop_finds_the_room()
    test_relay_between_two_sessions()
    test_config_set_saves_only_the_setting()
    test_two_answers_at_once()
    test_relay_after_restart_and_transport_switch()
    test_same_label_sessions_relay()
    test_plain_reply_reaches_its_peer()
    test_long_work_reply_goes_to_its_asker()
    test_opener_after_a_dropped_chat()
    test_dead_role_not_taken_back()
    test_disconnect_lets_a_post_finish()
    test_a_superseded_turn_isnt_handed_over()
    test_suggest_next_is_never_me()
    test_side_note_stays_out_of_the_next_turn()
    test_an_old_stop_isnt_a_newcomers()
    print(f"\nALL {_passed} SHARED-ROOM CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
