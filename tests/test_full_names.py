"""Full names: every session is "machine:chat name" - in relay tags and chat
turns alike - so sessions on different machines never get confused, even
with the same project name, or none. Short addresses ("Peer") are filled in
from who's in the room; messages from before names carried the machine still
count.
Run:  python tests/test_full_names.py
"""

from __future__ import annotations

import atexit
import json
import os
import time
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-fullnames-"))
os.chdir(_TMP)  # never the repo: a .env there would be loaded into the test
atexit.register(lambda: (os.chdir(tempfile.gettempdir()),
                         shutil.rmtree(_TMP, ignore_errors=True)))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
for _k in ("DISCORD_BOT_TOKEN", "DISCORDINATOR_CHAT_HANDLE", "DISCORDINATOR_CHAT_CHANNEL",
           "DISCORDINATOR_RELAY_CHANNEL", "DISCORDINATOR_SESSION_ID", "DISCORDINATOR_ACK"):
    os.environ.pop(_k, None)
os.environ["DISCORDINATOR_LABEL"] = "Home"

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


def _fresh() -> None:
    """A new session: nothing claimed or remembered."""
    handles.release_all()
    handles._resolved.clear()
    handles._since.clear()
    handles._last_chatter = None
    handles._current = None


def wait(room: str, t: float = 1.0, **kw) -> dict:
    return mcp.chat_await(channel=room, timeout=t, poll=0.02, nudge_after=0, **kw)


def last(room: str) -> str:
    return LocalClient("probe").read_messages(room, limit=1)[0]["content"]


def test_one_name_everywhere() -> None:
    print("a session's relay tag and chat turns carry the same full name:")
    _fresh()
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "CodeCarver"
    try:
        b = mcp.chat_begin(chatter="ui", channel="names-chat")
        check(b["chatter"] == "Home:CodeCarver/ui", f"chat name: {b['chatter']}")
        mcp.send_message("hi", channel="names-relay")
        check(last("names-relay") == "[Home:CodeCarver/ui] hi", f"relay tag: {last('names-relay')}")
        mcp.chat_say(text="hello", channel="names-chat", to="all", wait=False)
        check(last("names-chat").startswith("[Home:CodeCarver/ui>all|over]"),
              f"chat turn: {last('names-chat')}")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")


def test_same_project_on_two_machines() -> None:
    print("the same project name on two machines: two different sessions:")
    _fresh()
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "CodeCarver"
    r, c = "two-machines", LocalClient()
    try:
        mcp.chat_begin(channel=r)
        chat.send_chat(c, r, "Work:CodeCarver", "over", "Home, did you change the API?",
                       to="Home:CodeCarver")
        got = wait(r)
        check(got["your_turn"] and got["from"] == "Work:CodeCarver",
              "Home gets Work's turn - not skipped as its own")
        mcp.chat_say(text="yes, v2", wait=False, channel=r)
        check(last(r).startswith("[Home:CodeCarver>Work:CodeCarver|over]"),
              f"the reply goes back to Work: {last(r)[:40]}")
        st = chat.compute_state(c, r, "Work:CodeCarver")
        check(st["your_turn"], "and it's Work's turn now")
        chat.send_chat(c, r, "Work:CodeCarver", "over", "for my own peer", to="Work:Peer")
        check(wait(r, 0.3)["timed_out"], "Work's turn to someone else doesn't wake Home")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")


def test_unnamed_sessions_on_two_machines() -> None:
    print("unnamed sessions on two machines can chat - the machine tells them apart:")
    _fresh()
    r, c = "two-unnamed", LocalClient()
    b = mcp.chat_begin(channel=r)
    check(b["chatter"] == "Home:DefaultChatName", f"this one: {b['chatter']}")
    chat.send_chat(c, r, "Work:DefaultChatName", "over", "hello from Work, please review")
    got = wait(r)
    check(got["your_turn"] and got["from"] == "Work:DefaultChatName",
          "Home gets Work's opener (it used to take it for its own)")
    mcp.chat_say(text="on it", wait=False, channel=r)
    check(last(r).startswith("[Home:DefaultChatName>Work:DefaultChatName|over]"),
          f"and answers Work: {last(r)[:50]}")


def test_short_addresses() -> None:
    print("to= without a machine is filled in; refused when it's two or nobody known:")
    _fresh()
    r, c = "short-to", LocalClient()
    mcp.chat_begin(chatter="Asker", channel=r)
    chat.send_chat(c, r, "Work:Peer", "over", "hi all", to="Somebody")
    out = mcp.chat_say(text="Peer, a question", channel=r, to="Peer", wait=False)
    check(last(r).startswith("[Home:Asker>Work:Peer|over]") and "note" not in out,
          f"'Peer' is Work's Peer: {last(r)[:30]}")
    chat.send_chat(c, r, "Lab:Peer", "over", "me too", to="Somebody")
    before = last(r)
    try:
        mcp.chat_say(text="which Peer?", channel=r, to="Peer", wait=False)
        check(False, "an address two machines share should be refused")
    except mcp.ChatSendError as e:
        check("Lab:Peer" in str(e) and "Work:Peer" in str(e) and "Nothing was posted" in str(e),
              f"refused, naming both: {str(e)[:80]}")
    check(last(r) == before, "nothing posted")
    before = last(r)
    try:
        mcp.chat_say(text="Ghost?", channel=r, to="Ghost", wait=False)
        check(False, "a short name nobody has should be refused")
    except mcp.ChatSendError as e:
        check("full name" in str(e) and "Nothing was posted" in str(e) and last(r) == before,
              "nobody by that name yet: refused - which machine it's on isn't known")
    out = mcp.chat_say(text="Ghost?", channel=r, to="Far:Ghost", wait=False)
    check(last(r).startswith("[Home:Asker>Far:Ghost|over]") and "Nobody called" in out.get("note", ""),
          "by its full name it goes out, with a warning that nobody has used it yet")
    got = chat.await_turn(c, r, "Far:Ghost", timeout=1, poll=0.02, nudge_after=0)
    check(got["your_turn"] and got["from"] == "Home:Asker", "and Far's Ghost gets it")
    other = chat.await_turn(c, r, "Lab:Ghost", timeout=0.3, poll=0.02, nudge_after=0)
    check(other["timed_out"], "a Ghost on another machine doesn't")


def test_older_names_are_their_own() -> None:
    print("a name without a machine (an older version's) is a name of its own:")
    _fresh()
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "Carver2"
    r, c = "older", LocalClient()
    try:
        mcp.chat_begin(channel=r)
        chat.send_chat(c, r, "OldPeer", "over", "for Carver2, from an older version", to="Carver2")
        check(wait(r, 0.3)["timed_out"],
              "an old-style address isn't taken for this machine's Carver2 (it could be any)")
        chat.send_chat(c, r, "OldPeer", "over", "this one's for you", to="Home:Carver2")
        got = wait(r)
        check(got["your_turn"] and got["from"] == "OldPeer", "a turn to its full name is")
        mcp.chat_say(text="got it", wait=False, channel=r)
        check(last(r).startswith("[Home:Carver2>OldPeer|over]"),
              f"and the reply goes back to that name as it is: {last(r)[:30]}")
        chat.send_chat(c, r, "Work:OldPeer", "over", "a newer namesake", to="Somebody")
        chat.send_chat(c, r, "OldPeer", "over", "one more", to="Home:Carver2")
        wait(r)
        mcp.chat_say(text="sure", wait=False, channel=r)
        check(last(r).startswith("[Home:Carver2>OldPeer|over]"),
              "a reply still goes back to exactly who asked, namesake or not")
        mcp.chat_say(text="for you, exactly", channel=r, to="OldPeer", wait=False)
        check(last(r).startswith("[Home:Carver2>OldPeer|over]"),
              "to='OldPeer' is the name it posted under, namesake or not")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")


def test_a_project_with_its_own_machine_name() -> None:
    print("a session here with its own machine name (DISCORDINATOR_LABEL) is addressed by it:")
    _fresh()
    r = "own-label"
    os.environ["DISCORDINATOR_LABEL"] = "Lab"
    try:
        check(mcp.chat_begin(chatter="Yp", channel=r)["chatter"] == "Lab:Yp", "Lab:Yp starts")
    finally:
        os.environ["DISCORDINATOR_LABEL"] = "Home"
    _as_other_process("Lab:Yp")  # another session (its claim stays: it's running)
    handles._resolved.clear()
    handles._last_chatter = None
    handles._current = None
    mcp.chat_begin(chatter="Xp", channel=r)
    mcp.chat_say(text="hi Y", chatter="Xp", channel=r, to="Yp", wait=False)
    check(last(r).startswith("[Home:Xp>Lab:Yp|over]"),
          f"to='Yp' is the session running here, under its own name: {last(r)[:24]}")


def _as_other_process(full: str) -> None:
    """Hand this process's claim on ``full`` to a live process standing in for
    another session (one process is one session, on one machine)."""
    path = handles.registry_path()
    reg = json.loads(path.read_text(encoding="utf-8"))
    other = os.getppid()
    reg[chat.handle_key(full)].update(pid=other, started=handles.process_started(other),
                                      session=None)
    path.write_text(json.dumps(reg), encoding="utf-8")


def test_machine_renamed_mid_chat() -> None:
    print("a turn sent to a session's name before its machine was renamed is still its own:")
    _fresh()
    r, c = "renamed-machine", LocalClient()
    mcp.chat_begin(chatter="A", channel=r)
    mcp.chat_say(text="hi B", chatter="A", channel=r, to="Work:B", wait=False)
    chat.send_chat(c, r, "Work:B", "over", "hello A", to="Home:A")
    os.environ["DISCORDINATOR_LABEL"] = "Harbinger"  # config set-label Harbinger
    try:
        got = wait(r, 0.3, chatter="A")
        check(got["your_turn"] and got["from"] == "Work:B" and not got.get("handle_note"),
              f"waiting as Harbinger:A, it gets the turn to Home:A: {got.get('from')}")
        mcp.chat_say(text="hello B", chatter="A", channel=r, wait=False)
        check(last(r).startswith("[Harbinger:A>Work:B|over]"), f"and answers it: {last(r)[:28]}")
        other = chat.await_turn(c, r, "Work:Z", timeout=0.2, poll=0.02, nudge_after=0)
        check(not other.get("your_turn"), "nobody else is handed it")
        st = mcp.chat_status(chatter="Home:A", channel=r)  # raises if it's refused
        check(st["channel"] == r, "and its old full name is still accepted as chatter")
    finally:
        os.environ["DISCORDINATOR_LABEL"] = "Home"


def test_named_machine_leaves_the_placeholder_alone() -> None:
    print("once its machine is named, a session isn't sent after the other unnamed machine's turns:")
    _fresh()
    r, c = "was-unnamed", LocalClient()
    saved = os.environ.pop("DISCORDINATOR_LABEL")
    try:
        mcp.chat_begin(chatter="Carver", channel=r)
        mcp.chat_say(text="hi", chatter="Carver", channel=r, to="Work:Peer2", wait=False)
    finally:
        os.environ["DISCORDINATOR_LABEL"] = saved  # now named Home
    # Another unnamed machine's Carver - the same placeholder name - is asked something.
    chat.send_chat(c, r, "Work:Peer2", "over", "for the other machine", to="DefaultMachineName:Carver")
    got = wait(r, 0.3, chatter="Carver")
    check(got["timed_out"] and "DefaultMachineName:Carver" not in got.get("handle_note", ""),
          "Home:Carver isn't told it's its turn")


def test_old_names_are_pointed_out_not_taken() -> None:
    print("a turn to an older version's name is pointed out, not taken over:")
    _fresh()
    handles.sessions_path().unlink(missing_ok=True)
    r, c = "upgrade", LocalClient()
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "Carver3"
    try:
        chat.send_chat(c, r, "Carver3/ui", "over", "question (from before the upgrade)", to="Work:Peer")
        chat.send_chat(c, r, "Work:Peer", "over", "answer", to="Carver3/ui")
        b = mcp.chat_begin(channel=r)
        check(b["chatter"] == "Home:Carver3", f"it doesn't take the old name over: {b['chatter']}")
        check("'Carver3/ui' (read it with read_messages" in b.get("note", ""),
              "it's told the turn is there and how to answer it")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")


def test_short_names_checked_far_back() -> None:
    print("a short name two sessions answer to is refused, even if one spoke 100+ messages ago:")
    _fresh()
    r, c = "far-back", LocalClient()
    mcp.chat_begin(chatter="Asker2", channel=r)
    chat.send_chat(c, r, "Lab:Peer", "over", "hello", to="Somebody")
    for n in range(120):
        c.send_message(r, f"relay {n}", label="them")
    chat.send_chat(c, r, "Work:Peer", "over", "hi", to="Somebody")
    try:
        mcp.chat_say(text="which?", chatter="Asker2", channel=r, to="Peer", wait=False)
        check(False, "should be refused")
    except mcp.ChatSendError as e:
        check("Lab:Peer" in str(e) and "Work:Peer" in str(e), f"refused, naming both: {str(e)[:60]}")


def test_bare_project_with_two_roles() -> None:
    print("a project's bare name with two sessions of it is refused, not sent to nobody:")
    _fresh()
    r, c = "two-roles", LocalClient()
    mcp.chat_begin(chatter="Asker3", channel=r)
    chat.send_chat(c, r, "Work:ProjectB/a", "over", "a here", to="Somebody")
    chat.send_chat(c, r, "Work:ProjectB/b", "over", "b here", to="Somebody")
    before = last(r)
    try:
        mcp.chat_say(text="which of you?", chatter="Asker3", channel=r, to="ProjectB", wait=False)
        check(False, "should be refused")
    except mcp.ChatSendError as e:
        check("Work:ProjectB/a" in str(e) and "Work:ProjectB/b" in str(e) and last(r) == before,
              f"refused, naming both, nothing posted: {str(e)[:60]}")
    mcp.chat_say(text="a, then", chatter="Asker3", channel=r, to="ProjectB/a", wait=False)
    check(last(r).startswith("[Home:Asker3>Work:ProjectB/a|over]"), "naming the role works")


def test_odd_addresses() -> None:
    print("to='human' still goes out; a typed machine name is cleaned like the machine's own:")
    _fresh()
    r, c = "odd-to", LocalClient()
    mcp.chat_begin(chatter="Asker4", channel=r)
    mcp.chat_say(text="for the human", chatter="Asker4", channel=r, to="human", wait=False)
    check(last(r).startswith("[Home:Asker4>human|over]"), "to='human' is posted")
    out = mcp.chat_say(text="again", chatter="Asker4", channel=r, to="human", wait=False)
    check("Nobody called" not in (out.get("note") or ""), "and not taken for a typo")
    check(chat.sanitize_handle("/:Peer") == "Peer", "a machine part that cleans to nothing is none")
    mcp.chat_say(text="hi", chatter="Asker4", channel=r, to="Lab/Desk:Peer", wait=False)
    check(last(r).startswith("[Home:Asker4>LabDesk:Peer|over]"),
          f"to='Lab/Desk:Peer' reaches LabDesk:Peer: {last(r)[:34]}")


def test_short_name_after_a_rename() -> None:
    print("after this machine is renamed, a session's old name doesn't make it two:")
    _fresh()
    r = "renamed-short"
    os.environ["DISCORDINATOR_LABEL"] = "Old"
    handles._SESSION_KEY = "id:ren"
    try:
        mcp.chat_begin(chatter="Ren", channel=r)
        mcp.chat_say(text="hi X", chatter="Ren", channel=r, to="Work:X", wait=False)
        os.environ["DISCORDINATOR_LABEL"] = "Home"
        mcp.chat_begin(chatter="Ren", channel=r)  # the same session, machine renamed
        mcp.chat_say(text="still me", chatter="Ren", channel=r, to="Work:X", wait=False)
    finally:
        os.environ["DISCORDINATOR_LABEL"] = "Home"
    handles._resolved.clear()  # another session on this machine
    handles._last_chatter = None
    handles._current = None
    handles._full_used.clear()
    handles._SESSION_KEY = "id:other5"
    try:
        mcp.chat_begin(chatter="Other5", channel=r)
        mcp.chat_say(text="Ren?", chatter="Other5", channel=r, to="Ren", wait=False)
        check(last(r).startswith("[Home:Other5>Home:Ren|over]"),
              f"to='Ren' is Home:Ren, not refused over Old:Ren: {last(r)[:30]}")
    finally:
        handles._SESSION_KEY = None
        handles._full_used.clear()


def _other_session(key: str, handle: str, full: list[str], machine: str = "Home") -> None:
    """A record of another session running on this machine (sessions.json)."""
    path = handles.sessions_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data[key] = {"handle": handle, "machine": machine, "full": full, "ts": time.time(),
                 "pid": os.getppid(), "started": handles.process_started(os.getppid())}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _drop_other_sessions() -> None:
    path = handles.sessions_path()
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({k: v for k, v in data.items() if not k.startswith("id:x")}),
                        encoding="utf-8")


def test_an_updated_peer_isnt_its_old_name() -> None:
    print("once a peer posts under its full name, a short address means that, not its old name:")
    _fresh()
    r, c = "upgraded-peer", LocalClient()
    chat.send_chat(c, r, "UpPeer", "end", "bye (an older version)")
    mcp.chat_begin(chatter="Asker6", channel=r)
    chat.send_chat(c, r, "Work:UpPeer", "over", "back, updated", to="Lab:Other")
    mcp.chat_say(text="UpPeer?", chatter="Asker6", channel=r, to="UpPeer", wait=False)
    check(last(r).startswith("[Home:Asker6>Work:UpPeer|over]"),
          f"to='UpPeer' is Work:UpPeer: {last(r)[:34]}")


def test_a_role_switch_isnt_a_retired_name() -> None:
    print("a session that went from its project's plain name to a role leaves the plain name open:")
    _fresh()
    r, c = "role-switch", LocalClient()
    _other_session("id:xs", "Carver7/ui", ["Home:Carver7", "Home:Carver7/ui"])
    try:
        chat.send_chat(c, r, "Home:Carver7/ui", "over", "ui here", to="Somebody")
        chat.send_chat(c, r, "Work:Carver7", "over", "work here", to="Somebody")
        mcp.chat_begin(chatter="Asker7", channel=r)
        before = last(r)
        try:
            mcp.chat_say(text="which?", chatter="Asker7", channel=r, to="Carver7", wait=False)
            check(False, "should be refused")
        except mcp.ChatSendError as e:
            check("Home:Carver7" in str(e) and "Work:Carver7" in str(e) and last(r) == before,
                  f"to='Carver7' could be either, so it's refused: {str(e)[:60]}")
    finally:
        _drop_other_sessions()


def test_own_machine_names_keep_names_apart() -> None:
    print("sessions here under different machine names (DISCORDINATOR_LABEL) can share a name:")
    _fresh()
    r, c = "label-share", LocalClient()
    path = handles.registry_path()
    reg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    other = os.getppid()
    reg[handles._key("Carver8", "Lab")] = {
        "handle": "Carver8", "machine": "Lab", "pid": other, "ts": time.time(),
        "started": handles.process_started(other)}
    path.write_text(json.dumps(reg), encoding="utf-8")
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "Carver8"
    try:
        b = mcp.chat_begin(channel=r)
        check(b["chatter"] == "Home:Carver8" and not b.get("note"),
              f"Home:Carver8 isn't Lab:Carver8's: {b['chatter']}")
        chat.send_chat(c, r, "Work:Peer", "over", "for Home:Carver8", to="Home:Carver8")
        check(wait(r)["your_turn"], "and its turns reach it")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")
        reg = json.loads(path.read_text(encoding="utf-8"))
        reg.pop(handles._key("Carver8", "Lab"), None)
        path.write_text(json.dumps(reg), encoding="utf-8")


def test_renamed_session_keeps_its_place() -> None:
    print("after its machine is renamed, a session keeps its place in the chat:")
    _fresh()
    handles._SESSION_KEY = "id:kp"
    r, c = "keeps-place", LocalClient()
    try:
        mcp.chat_begin(chatter="Kp", channel=r)
        mcp.chat_say(text="hi B", chatter="Kp", channel=r, to="Work:B", wait=False)
        chat.send_chat(c, r, "Work:B", "over", "hello Kp", to="Home:Kp")
        check(wait(r, chatter="Kp")["your_turn"], "its turn arrives")
        os.environ["DISCORDINATOR_LABEL"] = "Harbinger"
        mcp.chat_say(text="answer", chatter="Kp", channel=r, wait=False)
        check(last(r).startswith("[Harbinger:Kp>Work:B|over]"),
              f"an unaddressed reply still goes back to who asked: {last(r)[:30]}")
        chat.send_chat(c, r, "Work:B", "over", "and again", to="Harbinger:Kp")
        wait(r, chatter="Kp")
        mcp.send_message("by relay", channel=r)
        check(last(r).startswith("[Harbinger:Kp>Work:B|over]"),
              f"as does a send_message answer: {last(r)[:30]}")
        # Its server restarts: the old full name is still its own as chatter.
        handles._resolved.clear()
        handles._full_used.clear()
        handles._last_chatter = None
        handles._current = None
        handles._restored = False
        st = mcp.chat_status(chatter="Home:Kp", channel=r)
        check(st["channel"] == r, "its old full name is accepted after a restart too")
    finally:
        os.environ["DISCORDINATOR_LABEL"] = "Home"
        handles._SESSION_KEY = None
        handles._full_used.clear()


def test_old_posts_are_only_mine_to_me() -> None:
    print("a renamed session's old posts are its own to it, and as posted to anyone else:")
    _fresh()
    handles._SESSION_KEY = "id:oo"
    r, c = "own-old-posts", LocalClient()
    try:
        mcp.chat_begin(chatter="Oo", channel=r)
        mcp.chat_say(text="hi B", chatter="Oo", channel=r, to="Work:B", wait=False)
        os.environ["DISCORDINATOR_LABEL"] = "Harbinger"
        mcp.chat_status(chatter="Oo", channel=r)
        mine = chat.compute_state(c, r, "Harbinger:Oo").get("last_turn") or {}
        check(mine.get("from") == "Harbinger:Oo", f"to itself it's its own: {mine.get('from')}")
        z = chat.compute_state(c, r, "Work:Z").get("last_turn") or {}
        check(z.get("from") != "Work:Z", f"to anyone else, not theirs: {z.get('from')}")
    finally:
        os.environ["DISCORDINATOR_LABEL"] = "Home"
        handles._SESSION_KEY = None
        handles._full_used.clear()


def test_an_older_versions_turn_to_my_name() -> None:
    print("a turn an older version sent to this session's name (no machine) is pointed out:")
    _fresh()
    r, c = "older-me", LocalClient()
    chat.send_chat(c, r, "Carver9", "over", "hi OldPeer9", to="OldPeer9")
    chat.send_chat(c, r, "OldPeer9", "over", "hi Carver9", to="Carver9")
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "Carver9"
    try:
        mcp.chat_begin(channel=r)
        got = wait(r, 0.3)
        check("'Carver9' (read it with read_messages" in (got.get("handle_note") or ""),
              f"chat_await points it out: {(got.get('handle_note') or '')[:50]}")
        mcp.chat_say(text="hi again", channel=r, to="OldPeer9", wait=False)
        got = wait(r, 0.3)
        check(got["timed_out"] and "Carver9'" not in (got.get("handle_note") or "")
              and "handle_note" not in got.get("next", ""),
              f"once answered, it isn't pointed out again: {(got.get('handle_note') or '')[:50]}")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")


def test_older_state_carries_on() -> None:
    print("chat state an older version kept under the name alone carries over:")
    _fresh()
    from discordinator import config
    with config.update_state() as state:
        state.setdefault("chat", {}).setdefault("older-state", {})["carver10"] = {
            "cursor": "1", "reply_to": "Work:Peer"}
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "Carver10"
    try:
        mcp.chat_status(channel="older-state")
        room = config.load_state()["chat"]["older-state"]
        check(room.get("home:carver10", {}).get("reply_to") == "Work:Peer" and "carver10" not in room,
              "it's under the full name now")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")


def test_a_sibling_by_project_name() -> None:
    print("a session's own project name with one other role of it means that role:")
    _fresh()
    r = "sibling"
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "Carver11"
    try:
        mcp.chat_begin(chatter="api", channel=r)
        _as_other_process("Home:Carver11/api")
        handles._resolved.clear()
        handles._last_chatter = None
        handles._current = None
        mcp.chat_begin(chatter="ui", channel=r)
        mcp.chat_say(text="api?", chatter="ui", channel=r, to="Carver11", wait=False)
        check(last(r).startswith("[Home:Carver11/ui>Home:Carver11/api|over]"),
              f"to='Carver11' is the api session: {last(r)[:44]}")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")
        reg = json.loads(handles.registry_path().read_text(encoding="utf-8"))
        reg.pop(handles._key("Carver11/api"), None)
        handles.registry_path().write_text(json.dumps(reg), encoding="utf-8")


def test_machine_names_are_cleaned() -> None:
    print("a machine name keeps no separators or trailing space, and the CLI still knows its own:")
    from discordinator import cli, config
    check(config.machine_name({"machine_label": "Lab/Desk:2"}) == "LabDesk2", "/ and : are dropped")
    long = "Barnes Gaming Desktop Workstat 2"  # 31st character is a space
    check(not config.machine_name({"machine_label": long}).endswith(" "), "no trailing space")
    r = "cli-own"
    LocalClient("x").send_message(r, "from me", label="LabDesk2:Proj")
    LocalClient("x").send_message(r, "from them", label="Work:Proj")
    out = cli._relay_poll(LocalClient("x"), r, "Lab/Desk:2", False, 50)
    check([m["content"] for m in out] == ["[Work:Proj] from them"],
          "the CLI relay skips this machine's own posts though its label was cleaned")


def test_names_and_chatter() -> None:
    print("long full names parse; chatter may be the session's own full name, not another machine's:")
    p = chat.parse("[DefaultMachineName:DefaultChatName>Work:Peer|over] hi")
    check(p and p["participant"] == "DefaultMachineName:DefaultChatName" and p["to"] == "Work:Peer",
          "a 34-character name reads back")
    _fresh()
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "Proj"
    try:
        check(mcp.chat_begin(chatter="Home:ui", channel="own")["chatter"] == "Home:Proj/ui",
              "chatter='Home:ui' is the role ui on this machine")
        check(mcp.chat_begin(chatter="Home:Proj/ui", channel="own")["chatter"] == "Home:Proj/ui",
              "its own full name passed back is itself")
        try:
            mcp.chat_begin(chatter="Work:ui", channel="own")
            check(False, "another machine's name should be refused")
        except Exception as e:  # noqa: BLE001
            check("Work" in str(e) and "Home" in str(e), f"refused: {str(e)[:60]}")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")


def test_unnamed_machine_is_told() -> None:
    print("a session on a machine with no name is told so:")
    _fresh()
    saved = os.environ.pop("DISCORDINATOR_LABEL")
    try:
        b = mcp.chat_begin(chatter="solo", channel="no-label")
        check(b["chatter"] == "DefaultMachineName:solo", f"name: {b['chatter']}")
        check("config set-label" in b.get("note", ""), "the note says how to name the machine")
    finally:
        os.environ["DISCORDINATOR_LABEL"] = saved


def test_deleting_after_a_rename() -> None:
    print("deleting a turn after a machine rename doesn't count the session as a reader:")
    _fresh()
    handles._SESSION_KEY = "id:dl"
    r, c = "delete-renamed", LocalClient()
    try:
        mcp.chat_begin(chatter="Dl", channel=r)
        first = mcp.chat_say(text="hi B", chatter="Dl", channel=r, to="Work:B", wait=False)
        chat.send_chat(c, r, "Work:B", "over", "hello", to="Home:Dl")
        os.environ["DISCORDINATOR_LABEL"] = "Harbinger"
        wait(r, chatter="Dl")
        mcp.chat_say(text="answer", chatter="Dl", channel=r, wait=False)
        check(last(r).startswith("[Harbinger:Dl>"), f"posted as Harbinger:Dl: {last(r)[:20]}")
        out = mcp.delete_messages(message_ids=first["message_ids"], channel=r)
        note = out.get("note", "")
        check("Work:B" in note and "Dl" not in note, f"only Work:B posted since: {note[:60]}")
    finally:
        os.environ["DISCORDINATOR_LABEL"] = "Home"
        handles._SESSION_KEY = None
        handles._full_used.clear()


def test_events_name_the_full_name() -> None:
    print("the event log names a session by its full name in every event:")
    from discordinator import events
    _fresh()
    mcp.chat_begin(chatter="Ev", channel="events-names")
    mcp._log_error("chat_say", RuntimeError("boom"), {"channel": "events-names"})
    evs, _ = events.read()
    err = [e for e in evs if e.get("kind") == "error"][-1]
    joined = [e for e in evs if e.get("kind") == "joined"][-1]
    check(err.get("handle") == joined.get("handle") == "Home:Ev",
          f"error and joined alike: {err.get('handle')}, {joined.get('handle')}")


def test_waiting_for_a_short_name() -> None:
    print("chat_await(from_whom=) without a machine means whom to= would:")
    _fresh()
    r, c = "from-short", LocalClient()
    mcp.chat_begin(chatter="Fw", channel=r)
    chat.send_chat(c, r, "Work:Peer", "over", "from Work", to="Home:Fw")
    got = wait(r, from_whom="Peer")
    check(got["your_turn"] and got["from"] == "Work:Peer", f"the one Peer's turn: {got.get('from')}")
    chat.send_chat(c, r, "Lab:Peer", "over", "from Lab", to="Home:Fw")
    try:
        wait(r, from_whom="Peer")
        check(False, "a from_whom two machines share should be refused")
    except mcp.ChatSendError as e:
        check("from_whom=" in str(e) and "Lab:Peer" in str(e) and "Nothing was read" in str(e),
              f"refused, naming both: {str(e)[:70]}")
    got = wait(r, from_whom="Lab:Peer")
    check(got["your_turn"] and got["from"] == "Lab:Peer", "nothing was read: the turn is still there")
    got = wait(r, 0.2, from_whom="Ghost")
    check(got["timed_out"], "nobody known by it yet: it waits for them")


def test_odd_project_names_compose() -> None:
    print("a project name with odd characters makes the same name everywhere:")
    check(handles.compose("ui", "Code:Carver") == "CodeCarver/ui",
          f"role under the cleaned name: {handles.compose('ui', 'Code:Carver')}")
    check(handles.compose(None, "[Code]") == "Code", f"plain: {handles.compose(None, '[Code]')}")
    got = handles.compose("CodeCarver/ui", "Code:Carver")
    check(got == "CodeCarver/ui", f"its own role name passed back isn't doubled: {got}")


def _drop_claim(full: str) -> None:
    path = handles.registry_path()
    reg = json.loads(path.read_text(encoding="utf-8"))
    reg.pop(chat.handle_key(full), None)
    path.write_text(json.dumps(reg), encoding="utf-8")


def test_set_label_keeps_running_names() -> None:
    print("after config set-label, a running session keeps its name from a newcomer:")
    from discordinator import config
    _fresh()
    label = os.environ.pop("DISCORDINATOR_LABEL")
    try:
        config.save({**config.load_file(), "machine_label": "Old"})
        handles.claim("Keeper")  # a running session, named by this machine's label
        _as_other_process("Old:Keeper")
        config.save({**config.load_file(), "machine_label": "New"})  # config set-label New
        got = handles.claim("Keeper")
        check(got == "Keeper-2", f"the newcomer doesn't take it: {got}")
    finally:
        config.save({**config.load_file(), "machine_label": None})
        os.environ["DISCORDINATOR_LABEL"] = label
        _drop_claim("Old:Keeper")
        _fresh()


def test_an_unnamed_machines_name_isnt_retired() -> None:
    print("a session here that was once unnamed doesn't hide an unnamed machine's session:")
    _fresh()
    r, c = "unnamed-retired", LocalClient()
    try:
        handles.claim("Carver8")
        _as_other_process("Home:Carver8")
        _other_session("id:x8", "Carver8", ["DefaultMachineName:Carver8", "Home:Carver8"])
        mcp.chat_begin(chatter="Asker8", channel=r)
        chat.send_chat(c, r, "DefaultMachineName:Carver8", "over", "hi", to="Somebody")
        try:
            mcp.chat_say(text="which?", chatter="Asker8", channel=r, to="Carver8", wait=False)
            check(False, "an address that could be two sessions should be refused")
        except mcp.ChatSendError as e:
            check("DefaultMachineName:Carver8" in str(e) and "Home:Carver8" in str(e),
                  f"refused, naming both: {str(e)[:70]}")
    finally:
        _drop_other_sessions()
        _drop_claim("Home:Carver8")


def test_relay_position_after_a_rename() -> None:
    print("after its machine is renamed, a session's relay read position carries on:")
    from discordinator import config
    _fresh()
    r, c = "relay-renamed", LocalClient()
    label = os.environ.pop("DISCORDINATOR_LABEL")
    try:
        config.save({**config.load_file(), "machine_label": "Home"})
        c.send_message(r, "[Work:B] one")
        got = mcp.get_new_messages(channel=r)
        check([m["content"] for m in got] == ["[Work:B] one"], "it reads what's new")
        config.save({**config.load_file(), "machine_label": "Harbinger"})  # config set-label
        got = mcp.get_new_messages(channel=r)
        check(got == [], f"renamed, it isn't handed it again: {[m['content'] for m in got]}")
        c.send_message(r, "[Work:B] two")
        got = mcp.get_new_messages(channel=r)
        check([m["content"] for m in got] == ["[Work:B] two"], "and gets what's new since")
    finally:
        config.save({**config.load_file(), "machine_label": None})
        os.environ["DISCORDINATOR_LABEL"] = label


def main() -> int:
    test_one_name_everywhere()
    test_same_project_on_two_machines()
    test_unnamed_sessions_on_two_machines()
    test_short_addresses()
    test_older_names_are_their_own()
    test_a_project_with_its_own_machine_name()
    test_machine_renamed_mid_chat()
    test_named_machine_leaves_the_placeholder_alone()
    test_old_names_are_pointed_out_not_taken()
    test_short_names_checked_far_back()
    test_bare_project_with_two_roles()
    test_odd_addresses()
    test_short_name_after_a_rename()
    test_an_updated_peer_isnt_its_old_name()
    test_a_role_switch_isnt_a_retired_name()
    test_own_machine_names_keep_names_apart()
    test_renamed_session_keeps_its_place()
    test_old_posts_are_only_mine_to_me()
    test_an_older_versions_turn_to_my_name()
    test_older_state_carries_on()
    test_a_sibling_by_project_name()
    test_machine_names_are_cleaned()
    test_names_and_chatter()
    test_unnamed_machine_is_told()
    test_deleting_after_a_rename()
    test_events_name_the_full_name()
    test_waiting_for_a_short_name()
    test_odd_project_names_compose()
    test_set_label_keeps_running_names()
    test_an_unnamed_machines_name_isnt_retired()
    test_relay_position_after_a_rename()
    print(f"\nALL {_passed} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
