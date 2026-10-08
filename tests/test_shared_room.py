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
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-shared-"))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
for _k in ("DISCORD_BOT_TOKEN", "DISCORDINATOR_CHAT_HANDLE", "DISCORDINATOR_CHAT_CHANNEL",
           "DISCORDINATOR_RELAY_CHANNEL", "DISCORDINATOR_LABEL", "DISCORDINATOR_ALLOW_SEND"):
    os.environ.pop(_k, None)

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
    chat.send_chat(c, room, "C", "over", "D, status?", to="D")
    chat.send_chat(c, room, "D", "over", "all good", to="C")
    chat.send_chat(c, room, "A", "over", "B, can you review my diff?", to="B")
    return c


def test_other_chats_end_isnt_mine() -> None:
    print("another conversation ending doesn't end mine:")
    r = "end-room"
    c = two_pairs(r)
    check(wait("B", r)["from"] == "A", "B gets A's question")
    mcp.chat_say(text="looks fine, one question", chatter="B", channel=r, wait=False)
    chat.send_chat(c, r, "D", "wrap", "wrap?", to="C")
    chat.send_chat(c, r, "C", "end", "bye", to="D")
    b = wait("B", r, 0.3)
    check(b["timed_out"] and not b["ended"], "B keeps waiting for A when C and D end theirs")
    a = wait("A", r)
    check(a["from"] == "B" and a["your_turn"] and not a["ended"], "A gets B's reply")
    st = mcp.chat_status(chatter="A", channel=r)
    check(not st["ended"] and st["session_active"], "A's chat is still going")
    d = mcp.chat_status(chatter="D", channel=r)
    check(d["ended"] and d["stop_reason"] == "agreed", "...while C and D's has ended")
    mcp.chat_say(text="thanks", chatter="A", channel=r, wait=False)
    wait("B", r)
    mcp.chat_say(text="done here", chatter="B", channel=r, status="end", wait=False)
    a = wait("A", r)
    check(a["ended"] and a["from"] == "B", "B's own `end` (addressed for it) ends A's chat")
    last = LocalClient().read_messages(r, limit=1)[0]["content"]
    check(last.startswith("[B>A|end]"), f"an unaddressed end goes to the peer: {last[:12]}")


def test_two_pairs_not_multiparty() -> None:
    print("two separate two-person chats aren't one group chat:")
    r = "pairs-room"
    two_pairs(r)
    b = wait("B", r)
    check(not b.get("multiparty") and "suggest_next" not in b,
          "B's turn from A isn't multiparty, and suggests nobody from the other chat")
    st = mcp.chat_status(chatter="B", channel=r)
    check(set(st["participants"]) == {"A", "B"} and st["floor"] == "B",
          f"B's chat is A and B: {st['participants']}")
    out = mcp.chat_say(text="sure", chatter="B", channel=r, wait=False)
    check("suggest_next" not in out and "waiting" not in out, "B's reply isn't told to rotate")


def test_opener_survives_other_traffic() -> None:
    print("an unaddressed opener reaches a responder who joins after other chats' turns:")
    r = "opener-room"
    c = LocalClient()
    mcp.chat_begin(chatter="A", channel=r)
    mcp.chat_say(text="Hi! Let's plan the migration.", chatter="A", channel=r, wait=False)
    chat.send_chat(c, r, "D", "over", "C: here are the logs", to="C")
    chat.send_chat(c, r, "C", "over", "thanks D", to="D")
    b = mcp.chat_begin(chatter="B", channel=r)
    check(b["recovered_pending_turn"], "B's chat_begin finds A's opener")
    got = wait("B", r)
    check(got["from"] == "A" and "migration" in got["text"], "and B receives it")
    d = wait("D", r, 0.3)
    check(d["from"] == "C", f"D, talking with C, gets C's turn - not A's opener ({d['from']})")
    d = wait("D", r, 0.3)
    check(d["timed_out"] or d.get("already_received"), "and is never handed A's opener")


def test_other_chats_broadcast_and_plain() -> None:
    print("another conversation's unaddressed turns and plain replies stay there:")
    r = "bcast-room"
    c = LocalClient()
    for h in ("A", "B", "C", "D", "E"):
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "A", "over", "B?", to="B")
    wait("B", r)
    mcp.chat_say(text="yes A", chatter="B", channel=r, wait=False)
    wait("A", r)  # A now holds the floor in A<->B
    chat.send_chat(c, r, "C", "over", "D, E: thoughts?", to="D")
    chat.send_chat(c, r, "D", "over", "E?", to="E")
    chat.send_chat(c, r, "E", "over", "anyone - here's mine", to="all")
    b = wait("B", r, 0.3)
    check(b["timed_out"], "a group's to='all' turn doesn't wake B, who's in another chat")
    c2 = wait("C", r)
    check(c2["from"] == "E", "...but does wake the group's own members")
    st = mcp.chat_status(chatter="A", channel=r)
    check(st["your_turn"] and st["floor"] == "A", "A is still owed B's reply")


def test_owed_turn_past_one_page() -> None:
    print("a turn owed to an idle session survives a busy room:")
    r = "busy-room"
    c = LocalClient()
    for h in ("A", "B"):
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "A", "over", "B: question about the schema", to="B")
    for i in range(70):
        chat.send_chat(c, r, "C", "over", f"c{i}", to="D")
        chat.send_chat(c, r, "D", "over", f"d{i}", to="C")
    st = mcp.chat_status(chatter="B", channel=r)
    check(st["your_turn"], "chat_status still sees the turn 140 messages back")
    begin = mcp.chat_begin(chatter="B", channel=r)
    check(begin["recovered_pending_turn"], "chat_begin recovers it")
    got = wait("B", r)
    check(got["from"] == "A" and "schema" in got["text"], "and chat_await delivers it")


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
        chat.send_chat(LocalClient(), r, "Peer", "over", "CodeCarver: your turn", to="CodeCarver")
        st = mcp.chat_status(channel=r)
        check(not st["your_turn"], "the sibling's turn isn't reported as mine")
        b = mcp.chat_begin(channel=r)
        check(b["chatter"] == "CodeCarver-2", f"and chat_begin names me as status did: {b['chatter']}")
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

    def as_session(label: str) -> None:
        # Each session is its own MCP server process: its own label (set per
        # project in .mcp.json) and its own record of what it sent.
        if current["label"]:
            sent[current["label"]] = set(mcp._sent_ids)
        mcp._sent_ids.clear()
        mcp._sent_ids.update(sent.get(label, set()))
        current["label"] = label
        os.environ["DISCORDINATOR_LABEL"] = label

    try:
        as_session("sessA")
        mcp.send_message("handoff: please review PR 42", channel="relay-room")
        check(mcp.get_new_messages(channel="relay-room") == [], "A sees nothing new (its own post)")
        as_session("sessB")
        got = mcp.get_new_messages(channel="relay-room")
        check([m["content"] for m in got] == ["[sessA] handoff: please review PR 42"],
              "B still gets A's message after A checked its inbox")
        mcp.send_message("done", channel="relay-room")
        as_session("sessA")
        got = mcp.get_new_messages(channel="relay-room")
        check([m["content"] for m in got] == ["[sessB] done"], "and A gets B's answer")
        check(mcp.get_new_messages(channel="relay-room") == [], "each read moves only its own position")
    finally:
        os.environ.pop("DISCORDINATOR_LABEL", None)
        mcp._sent_ids.clear()


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


def test_same_label_sessions_relay() -> None:
    print("two sessions with the SAME label relay to each other:")
    a_sent = set()

    def as_session(handle: str, sent: set) -> None:
        # One MCP server process per session: its own sent-ids and project handle.
        os.environ["DISCORDINATOR_CHAT_HANDLE"] = handle
        mcp._sent_ids.clear()
        mcp._sent_ids.update(sent)

    os.environ["DISCORDINATOR_LABEL"] = "laptop"  # shared, e.g. from `config set-label`
    try:
        as_session("ProjA", set())
        mcp.send_message("A: please review", channel="label-room")
        a_sent = set(mcp._sent_ids)
        check(mcp.get_new_messages(channel="label-room") == [], "A doesn't see its own message")
        as_session("ProjB", set())
        got = mcp.get_new_messages(channel="label-room")
        check([m["content"] for m in got] == ["[laptop] A: please review"],
              "B sees A's message though they share a label")
        mcp.send_message("B: done", channel="label-room")
        b_sent = set(mcp._sent_ids)
        as_session("ProjA", a_sent)
        got = mcp.get_new_messages(channel="label-room")
        check([m["content"] for m in got] == ["[laptop] B: done"], "and A sees B's answer")
        as_session("ProjB", b_sent)
        check(mcp.get_new_messages(channel="label-room") == [], "neither re-reads or loses anything")
    finally:
        os.environ.pop("DISCORDINATOR_LABEL")
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")
        mcp._sent_ids.clear()


def test_plain_reply_reaches_its_peer() -> None:
    print("a chat reply sent with send_message reaches the right peer, not another chat:")
    r = "plain-room"
    c = LocalClient()
    for h in ("A", "B", "C", "D"):
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "A", "over", "B, can you review my diff?", to="B")
    wait("B", r)  # B now owes A a reply
    chat.send_chat(c, r, "C", "over", "D, status?", to="D")  # another pair goes on
    out = mcp.send_message("looks good to me", channel=r)  # B answers with the wrong tool
    check("chat turn to A" in out, f"it goes out as B's chat turn to A: {out[:60]}")
    a = wait("A", r)
    check(a["from"] == "B" and a["text"] == "looks good to me" and a["your_turn"],
          "A gets it as B's reply")
    d = wait("D", r)
    check(d["from"] == "C", "D still gets C's turn, not B's message")
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
    chat.send_chat(c, r, "A", "over", "please run the full suite and report", to="B")
    check(wait("B", r)["from"] == "A", "B gets A's request")
    _backdate(r, 40)  # B works for 40 minutes without posting
    chat.send_chat(c, r, "C", "over", "anyone around to look at my PR?")  # unaddressed
    out = mcp.chat_say(text="all tests pass", chatter="B", channel=r, wait=False)
    check(out.get("posted", True) and "error" not in out, "B's results aren't refused")
    last = c.read_messages(r, limit=1)[0]["content"]
    check(last.startswith("[B>A|over]"), f"they go to A: {last[:14]}")
    st = chat.compute_state(c, r, "A")
    check(st["your_turn"], "and it's A's turn")


def main() -> int:
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
    test_same_label_sessions_relay()
    test_plain_reply_reaches_its_peer()
    test_long_work_reply_goes_to_its_asker()
    print(f"\nALL {_passed} SHARED-ROOM CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
