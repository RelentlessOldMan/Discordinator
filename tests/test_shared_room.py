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
from datetime import datetime, timedelta, timezone
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _sessions  # noqa: E402

_sessions.install(mcp)  # each chatter a session of its own
from discordinator import chat, cli, config, handles  # noqa: E402
from discordinator.local_client import LocalClient, local_dir  # noqa: E402

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
        _sessions.pin(label)  # its own Claude Code session

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
        _sessions.unpin()
        _sessions.switch("")


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
        _sessions.pin(handle)  # its own Claude Code session

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
        _sessions.unpin()
        _sessions.switch("")


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
    check(st["your_turn"] and st.get("owed_from") == ["M:B"], f"chat_status says B's is owed: {st.get('owed_from')}")
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


def test_an_answered_broadcast_isnt_owed() -> None:
    print("a question to all that someone else already answered isn't handed to a third:")
    c, r = LocalClient(), "answered-all"
    for h in "ABC":
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "M:A", "over", "anyone know X?", to="all")
    chat.send_chat(c, r, "M:B", "over", "yes, X is Y", to="M:A")
    check(not mcp.chat_status(chatter="C", channel=r)["your_turn"], "chat_status: not C's turn")
    got = wait("C", r, 0.5)
    check(got.get("timed_out") and not got.get("your_turn"), f"chat_await: C keeps waiting: {got.get('text')}")


def test_a_turn_handed_on_doesnt_block_a_post() -> None:
    print("a turn its sender has since handed to someone else doesn't hold up my post:")
    c, r = LocalClient(), "handed-on"
    for h in "ABC":
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "M:B", "over", "A, what do you think?", to="M:A")
    chat.send_chat(c, r, "M:B", "over", "never mind, C?", to="M:C")
    check(not mcp.chat_status(chatter="A", channel=r)["your_turn"], "chat_status: not A's turn")
    try:
        out = mcp.chat_say(text="B, about that", chatter="A", channel=r, to="M:B", wait=False)
        check(bool(out.get("message_ids")), "A's turn to B goes out")
    except Exception as e:
        check(False, f"A's turn was refused: {str(e)[:80]}")
    got = wait("A", r, 0.5)
    check(got.get("timed_out"), "chat_await: nothing for A")


def test_answering_one_asker_leaves_the_other_owed() -> None:
    print("two peers ask me; answering one still leaves the other's turn owed:")
    c = LocalClient()
    for r, first, second in (("two-askers", "M:B", "M:C"), ("two-askers-2", "M:C", "M:B")):
        for h in "ABC":
            mcp.chat_begin(chatter=h, channel=r)
        chat.send_chat(c, r, first, "over", f"{first} asks A", to="M:A")
        chat.send_chat(c, r, second, "over", f"{second} asks A", to="M:A")
        got = [wait("A", r).get("from"), wait("A", r).get("from")]
        check(sorted(got) == ["M:B", "M:C"], f"A is handed both: {got}")
        mcp.chat_say(text="answer for B", chatter="A", channel=r, to="M:B", wait=False)
        st = mcp.chat_status(chatter="A", channel=r)
        check(st["your_turn"] and st.get("owed_from") == ["M:C"],
              f"chat_status: C's turn is still owed: {st.get('owed_from')}")
        check(mcp.chat_begin(chatter="A", channel=r)["recovered_pending_turn"], "chat_begin says so")
        got = wait("A", r)
        check(got.get("from") == "M:C" and got.get("your_turn"), f"chat_await hands it over: {got.get('from')}")
        mcp.chat_say(text="answer for C", chatter="A", channel=r, to="M:C", wait=False)
        check(not mcp.chat_status(chatter="A", channel=r)["your_turn"], "answered both: nothing owed")


def test_working_seen_by_its_asker() -> None:
    print("my 'working' to the peer I owe is seen by them, though the room's floor is elsewhere:")
    c, r = LocalClient(), "working-asker"
    for h in "ABCD":
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "M:A", "over", "C, D: join us", to="M:C")
    chat.send_chat(c, r, "M:C", "over", "D?", to="M:D")
    chat.send_chat(c, r, "M:B", "over", "A, please run the tests", to="M:A")
    chat.send_chat(c, r, "M:D", "over", "C, sure", to="M:C")
    got = wait("A", r)
    check(got.get("from") == "M:B" and got.get("your_turn"), f"A has B's turn: {got.get('from')}")
    mcp.chat_say(text="running the tests", chatter="A", channel=r, status="working", wait=False)
    st = chat.compute_state(c, r, "M:B")
    check(chat._recently_working(st, "M:B"), f"B sees A working: {[x['from'] for x in st['progress']]}")
    a = wait("A", r)
    check(a.get("unfinished_turn") and not a.get("already_received"), "A is told to finish its turn")



def test_an_empty_room_points_at_the_one_in_use() -> None:
    print("joining a local room nobody has chatted in names the one in use:")
    c = LocalClient()
    mcp.chat_begin(chatter="A", channel="busy-room")
    chat.send_chat(c, "busy-room", "M:A", "over", "anyone?", to=None)
    mcp.send_message(text="a relay note", channel="busy-room-relay")
    out = mcp.chat_begin(chatter="B", channel="busy-room-2")
    check("busy-room-relay" not in (out.get("note") or ""),
          f"a relay room isn't a chat room in use: {out.get('note')!r}")
    check("did you mean 'busy-room'" in (out.get("note") or ""),
          f"a misnamed room says which one is in use: {out.get('note')!r}")
    out = mcp.chat_begin(chatter="B", channel="busy-room")
    check("did you mean" not in (out.get("note") or ""), "the room in use says nothing of it")
    out = mcp.chat_begin(chatter="B", channel="planning")
    check("did you mean" not in (out.get("note") or ""),
          f"a new room with a name of its own says nothing: {out.get('note')!r}")
    path = local_dir() / "busy-room.jsonl"
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    path.write_text("".join(json.dumps({**json.loads(x), "timestamp": old}) + "\n"
                            for x in path.read_text(encoding="utf-8").splitlines()),
                    encoding="utf-8")
    out = mcp.chat_begin(chatter="B", channel="busy-room-3")
    check("did you mean" not in (out.get("note") or ""),
          f"a room nobody has used for days isn't suggested: {out.get('note')!r}")


def test_an_opener_to_my_last_peer_stays_mine() -> None:
    print("an opener unaddressed after an end stays its sender's last peer's, however old:")
    r, c = "reopen-room", LocalClient()
    for h in ("A", "B"):
        mcp.chat_begin(chatter=h, channel=r)
    # (B's turns are another session's - sent as they'd arrive - and A's
    # its own, so it was in that chat)
    chat.send_chat(c, r, "M:B", "over", "build it?", to="M:A")
    wait("A", r)
    mcp.chat_say(text="will do, nothing else", chatter="A", status="wrap", channel=r, wait=False)
    chat.send_chat(c, r, "M:B", "end", "ending", to="M:A")
    opener = chat.send_chat(c, r, "M:B", "over", "carve done, go ahead and build", to=None)
    _backdate(r, 13 * 60)
    # An older version's chat_begin moved A past the turn and dropped whom
    # it replies to - all the state that said the turn was A's.
    chat.reset(r, "M:A", str(opener[-1]["id"]), 20)
    st = mcp.chat_status(chatter="A", channel=r)
    check(st["your_turn"] and st["floor"] == "M:A",
          f"13h on, A is still owed B's opener, as the floor says: {st['your_turn']}, {st['floor']}")
    check(mcp.chat_begin(chatter="A", channel=r)["recovered_pending_turn"],
          "and chat_begin hands it back")
    check(not mcp.chat_status(chatter="C", channel=r)["your_turn"],
          "a session that wasn't in that chat isn't handed the old opener")


def test_my_own_turn_isnt_owed_to_my_old_name() -> None:
    print("my own unaddressed turn isn't pointed out as owed to my old name:")
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "Carver9"
    handles._resolved.clear()
    handles._since.clear()
    handles._last_chatter = None
    try:
        r, c = "own-old-name", LocalClient()
        # An older version's turns: names without a machine.
        chat.send_chat(c, r, "Convex9", "over", "hi", to="Carver9")
        chat.send_chat(c, r, "Carver9", "over", "hello back", to="Convex9")
        _backdate(r, 120)  # (a chat long over)
        mcp.chat_begin(chatter="carver", channel=r)
        mcp.chat_say(text="re-carve done", chatter="carver", channel=r, wait=False)
        st = mcp.chat_status(chatter="carver", channel=r)
        note = (st.get("note") or "") + (st.get("handle_note") or "")
        check("re-carve done" not in note,
              f"it isn't told it owes its own turn an answer: {note!r}")
        os.environ["DISCORDINATOR_LABEL"] = "N"  # the machine renamed
        st = mcp.chat_status(chatter="carver", channel=r)
        note = (st.get("note") or "") + (st.get("handle_note") or "")
        check("re-carve done" not in note,
              f"nor once its machine is renamed (it sent it as M:...): {note!r}")
    finally:
        os.environ["DISCORDINATOR_LABEL"] = "M"
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")
        handles._resolved.clear()
        handles._since.clear()
        handles._last_chatter = None


def test_an_old_opener_is_only_its_latest_peers() -> None:
    print("an old opener is for the peer its sender last chatted with, not an earlier one:")
    r, c = "stale-peer", LocalClient()
    for h in ("A", "C"):
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "M:B", "over", "you there?", to="M:C")
    wait("C", r)
    mcp.chat_say(text="bye", chatter="C", status="end", channel=r, wait=False)
    chat.send_chat(c, r, "M:B", "over", "build it?", to="M:A")
    wait("A", r)
    mcp.chat_say(text="will do", chatter="A", status="wrap", channel=r, wait=False)
    chat.send_chat(c, r, "M:B", "end", "ending", to="M:A")
    opener = chat.send_chat(c, r, "M:B", "over", "carve done, build it", to=None)
    _backdate(r, 5 * 60)
    for h in ("M:A", "M:C"):
        chat.reset(r, h, str(opener[-1]["id"]), 20)
    check(mcp.chat_status(chatter="A", channel=r)["your_turn"], "A, B's latest peer, owes it")
    st = mcp.chat_status(chatter="C", channel=r)
    check(not st["your_turn"], f"C, from B's chat before, doesn't: {st['your_turn']}")


def test_an_old_turn_to_my_name_from_before_me_isnt_mine() -> None:
    print("an old turn to a name I only go by now (another session had it) isn't mine:")
    r, c = "name-reused", LocalClient()
    # A session that went by M:A2 two days ago, gone since.
    chat.send_chat(c, r, "M:A2", "over", "build it?", to="W:Peer")
    chat.send_chat(c, r, "W:Peer", "over", "built", to="M:A2")
    _backdate(r, 2 * 24 * 60)
    out = mcp.chat_begin(chatter="A2", channel=r)
    check(not out.get("recovered_pending_turn"), "a new session by that name isn't handed it")
    check(not mcp.chat_status(chatter="A2", channel=r)["your_turn"], "nor is it its turn")
    r = "name-reused-all"
    chat.send_chat(c, r, "M:A3", "over", "anyone around?", to="all")
    chat.send_chat(c, r, "W:Peer", "over", "here", to="all")
    _backdate(r, 2 * 24 * 60)
    check(not mcp.chat_status(chatter="A3", channel=r)["your_turn"],
          "nor an old broadcast answering that name")


def test_an_old_turn_to_me_after_a_third_spoke_is_mine() -> None:
    print("an old turn addressed to me is mine with someone else speaking before it:")
    r, c = "three-way-old", LocalClient()
    mcp.chat_begin(chatter="A", channel=r)
    mcp.chat_say(text="C, take a look", chatter="A", to="M:C", channel=r, wait=False)
    chat.send_chat(c, r, "M:C", "over", "B, yours", to="M:B")
    last = chat.send_chat(c, r, "M:B", "over", "A, done", to="M:A")
    _backdate(r, 5 * 60)
    chat.reset(r, "M:A", str(last[-1]["id"]), 20)
    st = mcp.chat_status(chatter="A", channel=r)
    check(st["your_turn"] and st["floor"] == "M:A",
          f"A owes B's turn, as the floor says: {st['your_turn']}, {st['floor']}")


def test_my_turn_as_my_bare_name_isnt_mine_as_a_role() -> None:
    print("a turn I sent under my project's bare name isn't my turn once I take a role:")
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "Carver8"
    handles._resolved.clear()
    handles._since.clear()
    handles._last_chatter = None
    _sessions.switch("")  # (one session, going by other names)
    try:
        with _sessions.one("carver", "Carver8"):
            r = "bare-then-role"
            mcp.chat_begin(channel=r)
            mcp.chat_say(text="bare done", channel=r, to="all", wait=False)
            mcp.chat_begin(chatter="carver", channel=r)
            st = mcp.chat_status(chatter="carver", channel=r)
            check(not st["your_turn"], f"it isn't handed its own turn: {st.get('last_turn')!r}")
            r = "bare-then-role-peer"
            chat.send_chat(LocalClient(), r, "M:Peer8", "over", "anyone?", to="all")
            mcp.chat_say(text="Peer8, yours", chatter="Carver8", channel=r, to="M:Peer8", wait=False)
            mcp.chat_begin(chatter="carver", channel=r)
            st = chat.compute_state(LocalClient(), r, "M:Peer8")
            check(st["your_turn"], "for its peer, a turn it sent under its bare name is still owed")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")
        handles._resolved.clear()
        handles._since.clear()
        handles._last_chatter = None


def test_the_room_hint_holds_up() -> None:
    print("the empty-room hint: unreadable rooms, buried turns, ended chats, idle files:")
    c = LocalClient()
    (local_dir() / "team-roon.jsonl").mkdir(parents=True)  # (a room it can't read)
    try:
        chat.send_chat(c, "team-room", "M:A", "over", "anyone?", to=None)
        out = mcp.chat_begin(chatter="B", channel="team-room2")
        check("did you mean 'team-room'" in (out.get("note") or ""),
              f"a room it can't read doesn't hide the others: {out.get('note')!r}")
    finally:
        (local_dir() / "team-roon.jsonl").rmdir()
    chat.send_chat(c, "notes-room", "M:A", "over", "anyone?", to=None)
    for n in range(21):
        mcp.send_message(text=f"note {n}", channel="notes-room")
    out = mcp.chat_begin(chatter="B", channel="notes-room2")
    check("did you mean 'notes-room'" in (out.get("note") or ""),
          f"a chat turn under relay notes still counts: {out.get('note')!r}")
    chat.send_chat(c, "done-room", "M:A", "end", "that's all", to="M:B")
    out = mcp.chat_begin(chatter="B", channel="done-room2")
    check("did you mean" not in (out.get("note") or ""),
          f"a chat that ended isn't one going on: {out.get('note')!r}")
    chat.send_chat(c, "stale-room", "M:A", "over", "anyone?", to=None)
    old = time.time() - 2 * 24 * 3600
    os.utime(local_dir() / "stale-room.jsonl", (old, old))
    out = mcp.chat_begin(chatter="B", channel="stale-room2")
    check("did you mean" not in (out.get("note") or ""),
          f"a room file untouched for days isn't read: {out.get('note')!r}")
    os.environ["DISCORDINATOR_CHAT_CHANNEL"] = "hub"
    try:
        chat.send_chat(c, "hub", "M:A", "over", "anyone?", to=None)
        out = mcp.chat_begin(chatter="B", channel="side-project")
        check("did you mean" not in (out.get("note") or ""),
              f"a room of its own isn't taken for the busy configured one: {out.get('note')!r}")
        out = mcp.chat_begin(chatter="B", channel="hub2")
        check("leave channel out" in (out.get("note") or ""),
              f"a slip for the configured room says to leave channel out: {out.get('note')!r}")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_CHANNEL")



def _long_ago(r: str, last: list) -> None:
    """Make the room's turns hours old, with nothing kept that says whose they
    are (an older version's chat_begin moved A past them): only the room can."""
    _backdate(r, 5 * 60)
    chat.reset(r, "M:A", str(last[-1]["id"]), 20)


def test_whom_an_old_turn_answers() -> None:
    print("an old turn is mine only when it answers me, read from the room:")
    c = LocalClient()
    for r, to in (("old-to-c", None), ("old-to-c-all", "all")):
        mcp.chat_begin(chatter="A", channel=r)
        mcp.chat_say(text="C, review?", chatter="A", to="M:C", channel=r, wait=False)
        chat.send_chat(c, r, "M:C", "over", "B, your take?", to="M:B")
        _long_ago(r, chat.send_chat(c, r, "M:B", "over", "looks fine", to=to))
        st = mcp.chat_status(chatter="A", channel=r)
        check(not st["your_turn"], f"B's reply to C ({to}) isn't A's: {st.get('owed_from')}")
    r = "old-answer"
    mcp.chat_begin(chatter="A", channel=r)
    chat.send_chat(c, r, "M:C", "over", "A, start?", to="M:A")
    wait("A", r)
    mcp.chat_say(text="B, done?", chatter="A", to="M:B", channel=r, wait=False)
    _long_ago(r, chat.send_chat(c, r, "M:B", "over", "yes done", to=None))
    st = mcp.chat_status(chatter="A", channel=r)
    check(st["your_turn"], f"B's unaddressed answer to A is A's: {st.get('owed_from')}")
    r = "old-after-third"
    mcp.chat_begin(chatter="A", channel=r)
    chat.send_chat(c, r, "M:B", "over", "A, plan?", to="M:A")
    wait("A", r)
    mcp.chat_say(text="C, check this", chatter="A", to="M:C", channel=r, wait=False)
    chat.send_chat(c, r, "M:C", "over", "noted", to=None)
    _long_ago(r, chat.send_chat(c, r, "M:B", "over", "ok", to=None))
    st = mcp.chat_status(chatter="A", channel=r)
    check(not st["your_turn"], f"B's turn after C spoke answers C, not A: {st.get('owed_from')}")


def test_an_old_opener_after_an_end_is_its_askers() -> None:
    print("an old opener after its sender's end is for whom it ended with:")
    c = LocalClient()
    r = "old-end-plain"
    mcp.chat_begin(chatter="A", channel=r)
    mcp.chat_say(text="B, build it?", chatter="A", to="M:B", channel=r, wait=False)
    chat.send_chat(c, r, "M:B", "end", "ending", to=None)
    _long_ago(r, chat.send_chat(c, r, "M:B", "over", "built, go ahead", to=None))
    st = mcp.chat_status(chatter="A", channel=r)
    check(st["your_turn"], f"after an unaddressed end, to its asker: {st.get('owed_from')}")
    r = "old-end-to-me"
    mcp.chat_begin(chatter="A", channel=r)
    mcp.chat_say(text="C, review?", chatter="A", to="M:C", channel=r, wait=False)
    chat.send_chat(c, r, "M:B", "end", "nothing more from me", to="M:A")
    _long_ago(r, chat.send_chat(c, r, "M:B", "over", "actually, one more thing", to=None))
    st = mcp.chat_status(chatter="A", channel=r)
    check(st["your_turn"], f"after an end to A, to A: {st.get('owed_from')}")


def test_posting_under_a_reused_name_doesnt_take_its_old_turns() -> None:
    print("posting under a name another session had doesn't make its old turns mine:")
    r, c = "reused-then-post", LocalClient()
    chat.send_chat(c, r, "M:A6", "over", "build it?", to="W:Peer")
    chat.send_chat(c, r, "W:Peer", "over", "built", to="M:A6")
    _backdate(r, 2 * 24 * 60)
    mcp.chat_begin(chatter="A6", channel=r)
    mcp.chat_say(text="hello", chatter="A6", channel=r, to="W:Peer2", wait=False)
    st = mcp.chat_status(chatter="A6", channel=r)
    check(not st["your_turn"], f"the old turn to M:A6 stays the old session's: {st.get('owed_from')}")


def test_other_names_of_a_session() -> None:
    print("which names a session posted under are other names of it:")
    f = mcp._other_name_of
    check(f("M:Carver-2", "M:Carver"), "it with -2 on the end")
    check(f("M:Carver/ui", "M:Carver"), "a role of its project's bare name")
    check(f("M:Carver", "M:Carver/ui"), "its project's bare name, for a role")
    check(f("N:Carver", "M:Carver"), "the same name on a machine named otherwise")
    check(not f("M:Other", "M:Carver"), "not another name")
    check(not f("M:Carver", "M:Carver"), "not itself")


def test_a_turn_to_my_old_name_is_pointed_out_however_old() -> None:
    print("a reply to the name I went by before (I asked it) is pointed out, however old:")
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "Carver7"
    handles._resolved.clear()
    handles._since.clear()
    handles._last_chatter = None
    _sessions.switch("")  # (one session, going by other names)
    try:
        with _sessions.one("ui", "Carver7"):
            r, c = "old-name-owed", LocalClient()
            mcp.chat_begin(channel=r)
            mcp.chat_say(text="peer, build it?", channel=r, to="W:Peer", wait=False)
            last = chat.send_chat(c, r, "W:Peer", "over", "built long ago", to="M:Carver7")
            _backdate(r, 2 * 24 * 60)
            chat.reset(r, "M:Carver7", str(last[-1]["id"]), 20)
            mcp.chat_begin(chatter="ui", channel=r)
            st = mcp.chat_status(chatter="ui", channel=r)
            note = (st.get("note") or "") + (st.get("handle_note") or "")
            check("owed to 'M:Carver7'" in note, f"2 days on, it's still pointed out: {note!r}")
            r = "old-name-two"
            mcp.chat_begin(chatter="Carver7", channel=r)  # (the bare name again)
            mcp.chat_say(text="anyone?", chatter="Carver7", channel=r, to="all", wait=False)
            chat.send_chat(c, r, "W:P1", "over", "first answer", to="M:Carver7")
            last = chat.send_chat(c, r, "W:P2", "over", "second answer", to="M:Carver7")
            _backdate(r, 2 * 24 * 60)
            for h in ("M:Carver7", "M:Carver7/ui"):  # (both read past them long ago)
                chat.reset(r, h, str(last[-1]["id"]), 20)
            mcp.chat_begin(chatter="ui", channel=r)
            st = mcp.chat_status(chatter="ui", channel=r)
            note = (st.get("note") or "") + (st.get("handle_note") or "")
            check("owed to 'M:Carver7'" in note, f"two turns owed to it: {note!r}")
            check(mcp._lost_turn.get("m:carver7") == ("W:P1", "first answer"),
                  f"the oldest is the one named: {mcp._lost_turn.get('m:carver7')}")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")
        handles._resolved.clear()
        handles._since.clear()
        handles._last_chatter = None


def test_the_room_hint_picks_the_right_room() -> None:
    print("the empty-room hint: only for an empty room, only a chat going on, the latest:")
    c = LocalClient()
    chat.send_chat(c, "proj-chat", "M:A", "over", "anyone?", to=None)
    chat.send_chat(c, "proj-chat2", "M:C", "over", "anyone?", to=None)
    out = mcp.chat_begin(chatter="B", channel="proj-chat2")
    check("did you mean" not in (out.get("note") or ""),
          f"a room with a chat in it says nothing: {out.get('note')!r}")
    chat.send_chat(c, "halt-room", "M:A", "over", "anyone?", to=None)
    c.post_human("halt-room", "stop")
    out = mcp.chat_begin(chatter="B", channel="halt-room2")
    check("did you mean" not in (out.get("note") or ""),
          f"a chat a human stopped isn't going on: {out.get('note')!r}")
    chat.send_chat(c, "fin-room", "M:A", "over", "anyone?", to="M:B")
    chat.send_chat(c, "fin-room", "M:B", "end", "bye", to="M:A")
    out = mcp.chat_begin(chatter="B", channel="fin-room2")
    check("did you mean" not in (out.get("note") or ""),
          f"a chat that ended after some turns isn't going on: {out.get('note')!r}")
    chat.send_chat(c, "alpha-room", "M:A", "over", "anyone?", to=None)
    _backdate("alpha-room", 60)
    chat.send_chat(c, "alpha-rooms", "M:C", "over", "anyone?", to=None)
    out = mcp.chat_begin(chatter="B", channel="alpha-room2")
    check("did you mean 'alpha-rooms'" in (out.get("note") or ""),
          f"of two in use, the one used last: {out.get('note')!r}")
    chat.send_chat(c, "beta-room-2", "M:A", "end", "done", to="M:B")
    chat.send_chat(c, "beta-room", "M:C", "over", "anyone?", to=None)
    out = mcp.chat_begin(chatter="B", channel="beta-room-3")
    check("did you mean 'beta-room'" in (out.get("note") or ""),
          f"a closer room nobody chats in doesn't hide the busy one: {out.get('note')!r}")


def test_one_session_one_name() -> None:
    print("a session goes by one name at a time: what it posted under another is its own:")
    r, c = "one-name", LocalClient()
    _sessions.switch("Solo9")
    mcp.chat_begin(chatter="Solo9", channel=r)
    mcp.chat_say(text="Duo9, take this", chatter="Solo9", channel=r, to="all", wait=False)
    with _sessions.one("Duo9"):
        mcp.chat_begin(chatter="Duo9", channel=r)
        st = mcp.chat_status(chatter="Duo9", channel=r)
    check(not st["your_turn"], f"its own turn under another name isn't owed to it: {st.get('owed_from')}")
    held = [h for h, _ in handles.live_full_claims()]
    check(not any(chat.same_handle(h, "M:Solo9") for h in held),
          f"and it no longer holds the name it left: {held}")
    chat.send_chat(c, r, "W:Peer", "over", "Solo9, a question", to="M:Solo9")
    _sessions.switch("Other9")
    out = mcp.chat_begin(chatter="Other9", channel=r)
    st = mcp.chat_status(chatter="Other9", channel=r)
    note = (out.get("note") or "") + (st.get("note") or "") + (st.get("handle_note") or "")
    check("Solo9, a question" not in note, f"another session isn't told of it: {note!r}")


def test_a_siblings_old_name_isnt_mine() -> None:
    print("a turn to the name a sibling session left is pointed out to it, not to me:")
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "Sib9"
    r, c = "sib-old-name", LocalClient()
    try:
        _sessions.pin("sib-api")
        mcp.chat_begin(channel=r)
        mcp.chat_say(text="peer, schema?", channel=r, to="W:Peer", wait=False)
        mcp.chat_begin(chatter="api", channel=r)  # (it takes a role)
        chat.send_chat(c, r, "W:Peer", "over", "schema ok, migrate it", to="M:Sib9")
        _sessions.pin("sib-ui")
        mcp.chat_begin(chatter="ui", channel=r)
        st = mcp.chat_status(chatter="ui", channel=r)
        note = (st.get("note") or "") + (st.get("handle_note") or "")
        check("'M:Sib9'" not in note, f"the sibling isn't told: {note!r}")
        _sessions.pin("sib-api")
        st = mcp.chat_status(chatter="api", channel=r)
        note = (st.get("note") or "") + (st.get("handle_note") or "")
        check("'M:Sib9'" in note, f"the session that left it is: {note!r}")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")
        _sessions.unpin()
        _sessions.switch("")


def test_a_dash2_name_back_once_free() -> None:
    print("a session renamed -2 takes its name back once free, and says why when it can't:")
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "Dash9"
    r, c = "dash-back", LocalClient()
    try:
        _sessions.pin("dash-1")
        mcp.chat_begin(channel=r)
        _sessions.pin("dash-2")
        check(mcp.chat_begin(channel=r)["chatter"] == "M:Dash9-2", "the second is -2")
        mcp.chat_say(text="peer, review?", channel=r, to="W:Peer", wait=False)
        _sessions.end("dash-1")
        out = mcp.chat_begin(channel=r)
        check(out["chatter"] == "M:Dash9-2" and "still going" in (out.get("note") or ""),
              f"with its chat under -2 going, it keeps -2 and says why: {out}")
        check("another live session" not in (out.get("note") or ""), "not that it's in use")
        chat.send_chat(c, r, "W:Peer", "end", "fine, bye", to="M:Dash9-2")
        out = mcp.chat_begin(channel=r)
        check(out["chatter"] == "M:Dash9", f"once that chat ends, it takes the name back: {out}")
        chat.send_chat(c, r, "W:Peer", "over", "wait, one more: bump version", to="M:Dash9-2")
        st = mcp.chat_status(channel=r)
        note = (st.get("note") or "") + (st.get("handle_note") or "")
        check("bump version" in note, f"a turn to its -2 is pointed out: {note!r}")
        mcp.chat_say(text="bumped", channel=r, to="W:Peer", wait=False)
        st = mcp.chat_status(channel=r)
        note = (st.get("note") or "") + (st.get("handle_note") or "")
        check("bump version" not in note, f"and once answered, no longer: {note!r}")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")
        _sessions.unpin()
        _sessions.switch("")



def test_old_turns_read_further() -> None:
    print("whom an old turn is for: asks, ends to all, a reused name, another's asker:")
    c = LocalClient()
    r = "ask-then-end"
    mcp.chat_begin(chatter="A", channel=r)
    mcp.chat_say(text="B, build it?", chatter="A", to="M:B", channel=r, wait=False)
    chat.send_chat(c, r, "M:B", "ask", "C, a word after?", to="M:C")
    chat.send_chat(c, r, "M:B", "end", "ending", to=None)
    _long_ago(r, chat.send_chat(c, r, "M:B", "over", "built, go ahead", to=None))
    st = mcp.chat_status(chatter="A", channel=r)
    check(st["your_turn"], f"a hand-raise to C doesn't say whom B last chatted with: {st.get('owed_from')}")
    r = "end-to-all"
    mcp.chat_begin(chatter="A", channel=r)
    mcp.chat_say(text="B, build it?", chatter="A", to="M:B", channel=r, wait=False)
    chat.send_chat(c, r, "M:B", "end", "ending, bye all", to="all")
    _long_ago(r, chat.send_chat(c, r, "M:B", "over", "built, go ahead", to=None))
    st = mcp.chat_status(chatter="A", channel=r)
    check(st["your_turn"], f"an end to all says nothing of whom; A asked B before: {st.get('owed_from')}")
    r = "opener-for-c"
    mcp.chat_begin(chatter="A", channel=r)
    mcp.chat_say(text="C, hi", chatter="A", to="M:C", channel=r, wait=False)
    chat.send_chat(c, r, "M:C", "over", "B, build it?", to="M:B")
    chat.send_chat(c, r, "M:B", "end", "ending", to=None)
    _long_ago(r, chat.send_chat(c, r, "M:B", "over", "built, go ahead", to=None))
    st = mcp.chat_status(chatter="A", channel=r)
    check(not st["your_turn"], f"B's old opener is for C, who asked B: {st.get('owed_from')}")
    r = "reused-then-asked"
    chat.send_chat(c, r, "M:A", "over", "old session's turn", to="W:Old")  # (another session, as M:A)
    chat.send_chat(c, r, "W:Old", "end", "bye", to="M:A")
    mcp.chat_begin(chatter="A", channel=r)
    mcp.chat_say(text="B, build it?", chatter="A", to="M:B", channel=r, wait=False)
    _long_ago(r, chat.send_chat(c, r, "M:B", "over", "built", to="M:A"))
    st = mcp.chat_status(chatter="A", channel=r)
    check(st["your_turn"], f"my latest turn before it is mine, not the first by my name: {st.get('owed_from')}")


def test_the_room_hint_counts_right() -> None:
    print("the empty-room hint: with a rename, past emptied rooms, past a bot's 'stop':")
    import subprocess
    c = LocalClient()
    chat.send_chat(c, "proj-room1", "M:A", "over", "anyone?", to=None)
    for n in range(2, 6):
        mcp.send_message(text="a note", channel=f"proj-room{n}")
    (local_dir() / "proj-room9.jsonl").write_text("", encoding="utf-8")
    out = mcp.chat_begin(chatter="B", channel="proj-room9")
    check("did you mean 'proj-room1'" in (out.get("note") or ""),
          f"the empty room itself takes no close-match slot: {out.get('note')!r}")
    chat.send_chat(c, "stopnote-room", "M:A", "over", "anyone?", to=None)
    c.send_message("stopnote-room", "stop")  # (a bot post: the CLI's `send stop`)
    out = mcp.chat_begin(chatter="B", channel="stopnote-room2")
    check("did you mean 'stopnote-room'" in (out.get("note") or ""),
          f"a bot's 'stop' isn't a human's: {out.get('note')!r}")
    chat.send_chat(c, "twin-room", "M:A", "over", "anyone?", to=None)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "Twin"
    path = handles.registry_path()
    saved = path.read_text(encoding="utf-8") if path.exists() else None
    try:
        reg = json.loads(saved) if saved else {}
        reg["twin"] = {"handle": "Twin", "pid": other.pid, "ts": time.time()}
        path.write_text(json.dumps(reg), encoding="utf-8")
        _sessions.pin("twin")  # (a new session, giving no chatter)
        out = mcp.chat_begin(channel="twin-room2")
        note = out.get("note") or ""
        check(out["chatter"] == "M:Twin-2", f"renamed: {out['chatter']}")
        check("Twin-2" in note and "did you mean 'twin-room'" in note,
              f"told both the rename and the room hint: {note!r}")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")
        other.kill()
        other.wait()
        _sessions.unpin()
        _sessions.end("twin")
        reg = json.loads(path.read_text(encoding="utf-8"))
        reg.pop("twin", None)
        path.write_text(json.dumps(reg), encoding="utf-8")


def test_another_name_of_mine_is_spelled_exactly() -> None:
    print("another name of a session: its -N, not a name it merely starts:")
    f = mcp._other_name_of
    check(f("M:Carver-12", "M:Carver"), "with -12 on the end (a 12th session by the name)")
    check(not f("M:Car", "M:Carver"), "a name its own starts with isn't another name of it")
    check(not f("M:Carver", "M:Car"), "nor one that starts with its name")


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
    test_an_empty_room_points_at_the_one_in_use()
    test_an_opener_to_my_last_peer_stays_mine()
    test_my_own_turn_isnt_owed_to_my_old_name()
    test_an_old_opener_is_only_its_latest_peers()
    test_an_old_turn_to_my_name_from_before_me_isnt_mine()
    test_an_old_turn_to_me_after_a_third_spoke_is_mine()
    test_my_turn_as_my_bare_name_isnt_mine_as_a_role()
    test_the_room_hint_holds_up()
    test_whom_an_old_turn_answers()
    test_an_old_opener_after_an_end_is_its_askers()
    test_posting_under_a_reused_name_doesnt_take_its_old_turns()
    test_other_names_of_a_session()
    test_a_turn_to_my_old_name_is_pointed_out_however_old()
    test_one_session_one_name()
    test_a_siblings_old_name_isnt_mine()
    test_a_dash2_name_back_once_free()
    test_the_room_hint_picks_the_right_room()
    test_an_answered_broadcast_isnt_owed()
    test_a_turn_handed_on_doesnt_block_a_post()
    test_answering_one_asker_leaves_the_other_owed()
    test_working_seen_by_its_asker()
    test_old_turns_read_further()
    test_the_room_hint_counts_right()
    test_another_name_of_mine_is_spelled_exactly()
    print(f"\nALL {_passed} SHARED-ROOM CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
