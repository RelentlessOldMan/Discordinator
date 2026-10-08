"""A peer addressing a project's bare handle reaches its role-named session.

Work-machine report: one side kept addressing "ProjectB" while the other
session was "ProjectB/convex" (a role of that project), so its chat_await never
woke and chat_status never said it was its turn. A turn to a bare project handle
stands for the one session of that project in the room - unless somebody posts
under the bare name itself, or two roles of it are around (then it's ambiguous
and wakes neither, rather than both).
Run:  python tests/test_bare_handle.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-bare-"))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
for _k in ("DISCORD_BOT_TOKEN", "DISCORDINATOR_CHAT_HANDLE", "DISCORDINATOR_CHAT_CHANNEL",
           "DISCORDINATOR_RELAY_CHANNEL", "DISCORDINATOR_LABEL", "DISCORDINATOR_SESSION_ID"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import discordinator.mcp_server as mcp  # noqa: E402
from discordinator import chat, handles  # noqa: E402
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


def _fresh() -> None:
    handles.registry_path().unlink(missing_ok=True)
    handles.sessions_path().unlink(missing_ok=True)
    handles._resolved.clear()
    handles._since.clear()
    handles._last_chatter = None
    handles._restored = False


def test_aliases_unit() -> None:
    print("bare_aliases: which bare names stand for a role:")
    P = lambda h, to=None: {"participant": h, "to": to, "status": "over", "body": ""}  # noqa: E731
    check(chat.bare_aliases([P("A"), P("ProjectB/convex")]) == {"projectb": "ProjectB/convex"},
          "one role of the project -> the bare name means it")
    check(chat.bare_aliases([P("A")], me="ProjectB/convex") == {"projectb": "ProjectB/convex"},
          "the caller counts before it has posted")
    check(chat.bare_aliases([P("ProjectB/ui"), P("ProjectB/api")]) == {},
          "two roles -> ambiguous, no alias")
    check(chat.bare_aliases([P("ProjectB"), P("ProjectB/convex")]) == {},
          "someone posting as the bare name -> no alias")
    check(chat.bare_aliases([P("A", to="ProjectB"), P("ProjectB/convex")])
          == {"projectb": "ProjectB/convex"},
          "the bare name only being addressed doesn't count as someone using it")
    out = chat.resolve_bare([P("A", to="projectb"), P("ProjectB/convex", to="A"), None])
    check(out[0]["to"] == "ProjectB/convex" and out[1]["to"] == "A" and out[2] is None,
          "resolve_bare readdresses only the bare turns (any case)")


def test_bare_opener_wakes_the_role() -> None:
    print("an opener to the bare project handle wakes its role session:")
    _fresh()
    r = "bare-open"
    c = LocalClient()
    mcp.chat_begin(chatter="ProjectB/convex", channel=r)
    # Sent as an older version would: the wire says "ProjectB", not the role.
    chat.send_chat(c, r, "CodeCarver", "over", "two questions for you", to="ProjectB")
    st = mcp.chat_status(chatter="ProjectB/convex", channel=r)
    check(st["your_turn"], f"chat_status says it's B's turn: {st.get('pending_turn')}")
    try:
        mcp.chat_say(text="replying blind", chatter="ProjectB/convex", channel=r, wait=False)
        raise AssertionError("B must be told to read the turn first")
    except Exception as e:
        check("haven't read" in str(e), "chat_say refuses until B reads it (it's B's unread turn)")
    b = wait("ProjectB/convex", r)
    check(b["your_turn"] and b["from"] == "CodeCarver" and b["text"] == "two questions for you",
          f"chat_await delivers it: {b.get('from')} {b.get('timed_out')}")
    hn = b.get("handle_note") or ""
    check("take" not in hn.lower(), f"no 'take your old name back' note: {hn[:60]}")


def test_consistent_bare_addressing() -> None:
    print("a peer that keeps addressing the bare handle, turn after turn:")
    _fresh()
    r = "bare-loop"
    c = LocalClient()
    mcp.chat_begin(chatter="ProjectB/convex", channel=r)
    for n in range(3):
        chat.send_chat(c, r, "CodeCarver", "over", f"question {n}", to="ProjectB")
        b = wait("ProjectB/convex", r)
        check(b["your_turn"] and b["text"] == f"question {n}", f"round {n}: B wakes")
        mcp.chat_say(text=f"answer {n}", chatter="ProjectB/convex", channel=r, wait=False)
        last = c.read_messages(r, limit=1)[0]["content"]
        check(last.startswith("[ProjectB/convex>CodeCarver|over]"),
              f"round {n}: B's reply goes back to CodeCarver: {last[:36]}")
        st = mcp.chat_status(chatter="CodeCarver", channel=r)
        check(st["your_turn"], f"round {n}: then it's CodeCarver's turn")
        check(not st.get("multiparty"), "still a two-person chat (bare name isn't a third person)")


def test_sender_spells_out_the_role() -> None:
    print("chat_say(to=<bare>) writes the role on the wire:")
    _fresh()
    r = "bare-send"
    mcp.chat_begin(chatter="ProjectB/convex", channel=r)
    mcp.chat_begin(chatter="CodeCarver", channel=r)
    out = mcp.chat_say(text="hello", chatter="CodeCarver", channel=r, to="ProjectB", wait=False)
    last = LocalClient().read_messages(r, limit=1)[0]["content"]
    check(last.startswith("[CodeCarver>ProjectB/convex|over]"), f"wire: {last[:40]}")
    check("Nobody called" not in str(out.get("note", "")), "no 'unknown handle' warning")
    check(wait("ProjectB/convex", r)["your_turn"], "B wakes")


def test_two_roles_stay_ambiguous() -> None:
    print("with two roles of the project, a bare address wakes neither:")
    _fresh()
    r = "bare-two"
    c = LocalClient()
    for h in ("ProjectB/ui", "ProjectB/api"):
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "ProjectB/ui", "ask", "here", to="X")
    chat.send_chat(c, r, "ProjectB/api", "ask", "here too", to="X")
    chat.send_chat(c, r, "CodeCarver", "over", "which of you?", to="ProjectB")
    for h in ("ProjectB/ui", "ProjectB/api"):
        check(wait(h, r, 0.3)["timed_out"], f"{h} doesn't wake")
        check(not mcp.chat_status(chatter=h, channel=r)["your_turn"], f"{h} isn't told it's its turn")


def test_real_bare_session_keeps_its_turns() -> None:
    print("a session really named the bare handle keeps its own turns:")
    _fresh()
    r = "bare-real"
    c = LocalClient()
    mcp.chat_begin(chatter="ProjectB", channel=r)
    mcp.chat_begin(chatter="ProjectB/convex", channel=r)
    chat.send_chat(c, r, "ProjectB", "ask", "I'm here", to="CodeCarver")
    chat.send_chat(c, r, "CodeCarver", "over", "for the bare one", to="ProjectB")
    check(wait("ProjectB/convex", r, 0.3)["timed_out"], "the role session doesn't take it")
    check(wait("ProjectB", r)["your_turn"], "the bare session gets it")


def test_project_handle_no_false_lost_turn() -> None:
    print("with a project handle set, a bare turn isn't reported as lost:")
    _fresh()
    r = "bare-proj"
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "ProjectB"
    try:
        b0 = mcp.chat_begin(chatter="convex", channel=r)
        check(b0["chatter"] == "ProjectB/convex", f"handle is the role: {b0['chatter']}")
        chat.send_chat(LocalClient(), r, "CodeCarver", "over", "ping", to="ProjectB")
        st = mcp.chat_status(chatter="convex", channel=r)
        check(st["your_turn"], "chat_status: B's turn")
        check("ProjectB'" not in str(st.get("handle_note") or st.get("note") or ""),
              "no note telling B to take back 'ProjectB'")
        b = wait("convex", r)
        check(b["your_turn"] and b["text"] == "ping", "chat_await delivers it")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE", None)


def test_old_role_from_an_ended_chat_doesnt_block() -> None:
    print("an older role from a chat that ended doesn't make the bare name ambiguous:")
    _fresh()
    r = "bare-old"
    c = LocalClient()
    chat.send_chat(c, r, "ProjB/api", "over", "earlier question", to="Peer")
    chat.send_chat(c, r, "Peer", "wrap", "done?", to="ProjB/api")
    chat.send_chat(c, r, "ProjB/api", "end", "done", to="Peer")
    mcp.chat_begin(chatter="ProjB/convex", channel=r)
    mcp.chat_say(text="new question", chatter="ProjB/convex", channel=r, to="Peer", wait=False)
    chat.send_chat(c, r, "Peer", "over", "answer", to="ProjB")  # Peer only knows "ProjB"
    b = wait("ProjB/convex", r)
    check(b["your_turn"] and b["text"] == "answer", f"convex wakes: {b.get('timed_out')}")
    two = [{"participant": "ProjB/api", "to": "Peer", "status": "over"},
           {"participant": "ProjB/convex", "to": "Peer", "status": "over"}]
    check(chat.bare_aliases(two) == {}, "two roles both still chatting: still ambiguous")


def test_two_running_roles_stay_ambiguous_after_their_chats_end() -> None:
    print("two roles running here stay ambiguous once their earlier chats ended:")
    _fresh()
    r = "bare-ended-both"
    c = LocalClient()
    chat.send_chat(c, r, "ProjectB/ui", "over", "q1", to="X")
    chat.send_chat(c, r, "X", "end", "bye", to="ProjectB/ui")
    chat.send_chat(c, r, "ProjectB/api", "over", "q2", to="Y")
    chat.send_chat(c, r, "Y", "end", "bye", to="ProjectB/api")
    for h in ("ProjectB/ui", "ProjectB/api"):  # both start new chats
        mcp.chat_begin(chatter=h, channel=r)
    chat.send_chat(c, r, "Z", "over", "which of you?", to="ProjectB")
    for h in ("ProjectB/ui", "ProjectB/api"):
        check(wait(h, r, 0.3)["timed_out"], f"{h} doesn't wake")
        check(not mcp.chat_status(chatter=h, channel=r)["your_turn"],
              f"{h} isn't told it's its turn")


def test_end_to_the_bare_name_ends_the_roles_chat() -> None:
    print("an end addressed to the bare name ends that role's chat:")
    P = lambda h, s, to: {"participant": h, "to": to, "status": s, "body": ""}  # noqa: E731
    hist = [P("ProjB/api", "over", "Peer"), P("Peer", "end", "ProjB"),
            P("ProjB/convex", "over", "Peer")]
    check(chat.bare_aliases(hist) == {"projb": "ProjB/convex"}, "api is gone, convex is the one")
    check(chat.resolve_bare(hist)[1]["to"] == "ProjB/api", "the end itself was to api")
    _fresh()
    r = "bare-end-bare"
    c = LocalClient()
    chat.send_chat(c, r, "ProjB/api", "over", "earlier question", to="Peer")
    chat.send_chat(c, r, "Peer", "end", "done", to="ProjB")
    mcp.chat_begin(chatter="ProjB/convex", channel=r)
    mcp.chat_say(text="new question", chatter="ProjB/convex", channel=r, to="Peer", wait=False)
    chat.send_chat(c, r, "Peer", "over", "answer", to="ProjB")
    b = wait("ProjB/convex", r)
    check(b["your_turn"] and b["text"] == "answer", f"convex wakes: {b.get('timed_out')}")


def test_old_bare_turns_arent_handed_to_a_newcomer() -> None:
    print("a newer role isn't handed an ended chat's bare turns:")
    _fresh()
    r = "bare-newcomer"
    c = LocalClient()
    chat.send_chat(c, r, "X", "over", "for convex", to="ProjectB")
    chat.send_chat(c, r, "ProjectB/convex", "over", "reply", to="X")
    chat.send_chat(c, r, "X", "over", "again", to="ProjectB")
    chat.send_chat(c, r, "ProjectB/convex", "impasse", "stuck", to="X")
    out = chat.resolve_bare([chat.parse(m["content"]) for m in reversed(c.read_messages(r))],
                            me="ProjectB/newrole")
    check([p["to"] for p in out if p["participant"] == "X"] == ["ProjectB/convex"] * 2,
          "they stay convex's")
    mcp.chat_begin(chatter="ProjectB/newrole", channel=r)
    st = mcp.chat_status(chatter="ProjectB/newrole", channel=r)
    check(not st["your_turn"] and "ProjectB/convex" not in st["participants"],
          f"not newrole's chat: {st['participants']}")


def test_error_event_names_the_room() -> None:
    print("a failed call's event names the room it acted on:")
    from discordinator import events
    os.environ["DISCORDINATOR_CHAT_CHANNEL"] = "the-chat-room"
    try:
        mcp._log_error("chat_say", ValueError("boom"), {})  # channel omitted: the default room
        mcp._log_error("chat_say", ValueError("boom2"), {"channel": "elsewhere"})
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_CHANNEL", None)
    evs = {e.get("message"): e for e in events.read(0)[0] if e.get("kind") == "error"}
    check(evs["boom"].get("room") == "the-chat-room", f"default room: {evs['boom'].get('room')}")
    check(evs["boom2"].get("room") == "elsewhere", "an explicit room")


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


def test_role_that_walked_away_doesnt_block() -> None:
    print("a role that stopped chatting long ago (no end) doesn't block the bare name:")
    _fresh()
    r = "bare-dropped"
    c = LocalClient()
    chat.send_chat(c, r, "ProjB/api", "over", "yesterday's question", to="Y")
    chat.send_chat(c, r, "Y", "over", "yesterday's answer", to="ProjB/api")
    _backdate(r, 20 * 60)  # api just stopped - no end - and isn't running any more
    mcp.chat_begin(chatter="ProjB/convex", channel=r)
    mcp.chat_say(text="new question", chatter="ProjB/convex", channel=r, to="Peer", wait=False)
    check(mcp._expand_bare(c, r, "Peer", "ProjB") == "ProjB/convex",
          "a sender here spells it out as convex")
    chat.send_chat(c, r, "Peer", "over", "answer", to="ProjB")  # an older/remote Peer
    st = mcp.chat_status(chatter="ProjB/convex", channel=r)
    check(st["your_turn"], f"chat_status: convex's turn ({st.get('pending_turn')})")
    check("ProjB" not in st["participants"] and not st["multiparty"],
          f"no phantom bare participant: {st['participants']}")
    b = wait("ProjB/convex", r)
    check(b["your_turn"] and b["text"] == "answer", f"chat_await agrees: {b.get('timed_out')}")


def test_await_and_status_agree() -> None:
    print("chat_await and chat_status agree on who a bare turn is for:")
    _fresh()
    r = "bare-agree"
    c = LocalClient()
    mcp.chat_begin(chatter="ProjectB/api", channel="bare-agree-elsewhere")  # running here
    mcp.chat_begin(chatter="ProjectB/ui", channel=r)
    mcp.chat_say(text="question", chatter="ProjectB/ui", channel=r, to="X", wait=False)
    chat.send_chat(c, r, "X", "over", "reply", to="ProjectB")
    st = mcp.chat_status(chatter="ProjectB/ui", channel=r)
    b = wait("ProjectB/ui", r, 0.3)
    check(not st["your_turn"] and b["timed_out"],
          f"two roles running here: ambiguous for both ({st['your_turn']}, {b.get('timed_out')})")

    _fresh()
    r = "bare-agree-2"
    mcp.chat_begin(chatter="ProjectB/ui", channel=r)
    mcp.chat_say(text="question", chatter="ProjectB/ui", channel=r, to="X", wait=False)
    chat.send_chat(c, r, "X", "over", "reply", to="ProjectB")
    chat.send_chat(c, r, "ProjectB/api", "over", "unrelated", to="Y")  # arrives after
    st = mcp.chat_status(chatter="ProjectB/ui", channel=r)
    b = wait("ProjectB/ui", r)
    check(st["your_turn"] and b["your_turn"] and b["text"] == "reply",
          "a role turning up afterwards doesn't take it from ui, for either")

    _fresh()
    r = "bare-agree-3"
    mcp.chat_begin(chatter="ProjectB/ui", channel=r)
    chat.send_chat(c, r, "ProjectB/ui", "ask", "here", to="X")
    chat.send_chat(c, r, "ProjectB/api", "ask", "here too", to="Y")
    chat.send_chat(c, r, "X", "over", "which of you?", to="ProjectB")
    chat.send_chat(c, r, "ProjectB/api", "end", "bye", to="Y")  # api leaves afterwards
    st = mcp.chat_status(chatter="ProjectB/ui", channel=r)
    b = wait("ProjectB/ui", r, 0.3)
    check(not st["your_turn"] and b["timed_out"],
          "ambiguous when sent stays ambiguous after one role leaves, for both")


def test_running_roles_count_from_when_they_started() -> None:
    print("a session running here only counts for turns sent after it started:")
    P = lambda h, s, to: {"participant": h, "to": to, "status": s, "body": ""}  # noqa: E731
    hist = [P("X", "over", "ProjB")]
    t = ["2026-01-01T12:00:00+00:00"]
    ui = ("ProjB/ui", chat._parse_ts("2026-01-01T11:00:00+00:00"))
    late = ("ProjB/api", chat._parse_ts("2026-01-01T13:00:00+00:00"))
    early = ("ProjB/api", chat._parse_ts("2026-01-01T11:30:00+00:00"))
    check(chat.resolve_bare(hist, None, [ui, late], t)[0]["to"] == "ProjB/ui",
          "api started after it was sent: it was ui's")
    check(chat.resolve_bare(hist, None, [ui, early], t)[0]["to"] == "ProjB",
          "both running then: ambiguous")
    answered = hist + [P("ProjB/api", "over", "X")]
    check(chat.resolve_bare(answered, None, [ui, late], t + t)[0]["to"] == "ProjB/api",
          "a role that answered it beats one that was merely running")
    end = [P("X", "end", "ProjB")]
    check(chat.resolve_bare(end, "ProjB/ui", [], t)[0]["to"] == "ProjB",
          "an end to the bare name with no role around ends nobody's chat")


def test_second_role_to_answer_is_told() -> None:
    print("two roles (one elsewhere) both take a bare opener: the second is told:")
    _fresh()
    r = "bare-race"
    c = LocalClient()
    mcp.chat_begin(chatter="ProjectB/ui", channel=r)
    chat.send_chat(c, r, "X", "over", "anyone from ProjectB?", to="ProjectB")
    check(wait("ProjectB/ui", r)["your_turn"], "ui (the only one it knows of) wakes")
    chat.send_chat(c, r, "ProjectB/api", "over", "api here", to="X")  # another machine's
    try:
        mcp.chat_say(text="ui here", chatter="ProjectB/ui", channel=r, wait=False)
        raise AssertionError("ui's reply must not go out")
    except Exception as e:
        check("ProjectB/api" in str(e) and "answered it first" in str(e), f"told: {str(e)[:90]}")
    last = c.read_messages(r, limit=1)[0]["content"]
    check(last.startswith("[ProjectB/api>X|over]"), "nothing posted")
    out = mcp.chat_say(text="ui here too", chatter="ProjectB/ui", channel=r, to="X", wait=False)
    check(out.get("sent_messages") == 1, "addressing X on purpose still works")


def test_roomless_tool_errors_have_no_room() -> None:
    print("a failed call to a tool that uses no room isn't filed under one:")
    import inspect
    from discordinator import events
    mcp._log_error("whoami", ValueError("roomless"), {}, False)
    evs = {e.get("message"): e for e in events.read(0)[0] if e.get("kind") == "error"}
    check(evs["roomless"].get("room") is None, f"no room: {evs['roomless'].get('room')}")
    for fn in (mcp.whoami, mcp.list_channels, mcp.download_attachment):
        check("channel" not in inspect.signature(fn).parameters, f"{fn.__name__} has no room")
    check("channel" in inspect.signature(mcp.chat_say).parameters, "chat_say has one")


def main() -> int:
    test_aliases_unit()
    test_bare_opener_wakes_the_role()
    test_consistent_bare_addressing()
    test_sender_spells_out_the_role()
    test_two_roles_stay_ambiguous()
    test_real_bare_session_keeps_its_turns()
    test_project_handle_no_false_lost_turn()
    test_old_role_from_an_ended_chat_doesnt_block()
    test_two_running_roles_stay_ambiguous_after_their_chats_end()
    test_end_to_the_bare_name_ends_the_roles_chat()
    test_old_bare_turns_arent_handed_to_a_newcomer()
    test_error_event_names_the_room()
    test_role_that_walked_away_doesnt_block()
    test_await_and_status_agree()
    test_running_roles_count_from_when_they_started()
    test_second_role_to_answer_is_told()
    test_roomless_tool_errors_have_no_room()
    print(f"\nALL {_passed} BARE-HANDLE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
