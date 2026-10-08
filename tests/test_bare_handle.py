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


def main() -> int:
    test_aliases_unit()
    test_bare_opener_wakes_the_role()
    test_consistent_bare_addressing()
    test_sender_spells_out_the_role()
    test_two_roles_stay_ambiguous()
    test_real_bare_session_keeps_its_turns()
    test_project_handle_no_false_lost_turn()
    print(f"\nALL {_passed} BARE-HANDLE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
