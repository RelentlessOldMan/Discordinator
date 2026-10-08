"""Session handle resolution (handles.py): project handle + optional role, made
unique among live sessions on this machine.

Covers the compose rules (base, base/role, no doubling, no base), the
machine-wide claim registry (a live other process forces a "-2"; a dead or
expired claim is reclaimed; a process keeps its resolved name), suffix length
capping, release at exit, and pid liveness — which must never signal the
process (on Windows os.kill(pid, 0) would terminate it).
Run:  python tests/test_handles.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-handles-"))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
for _k in ("DISCORD_BOT_TOKEN", "DISCORDINATOR_CHAT_HANDLE"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import discordinator.mcp_server as mcp  # noqa: E402
from discordinator import config, handles  # noqa: E402
from discordinator.config import ConfigError  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def _fresh() -> None:
    """Empty registry and forget this process's resolved names."""
    handles.registry_path().unlink(missing_ok=True)
    handles._resolved.clear()
    handles._last_chatter = None


def _plant(handle: str, pid: int, age: float = 0.0) -> None:
    """Record a claim as if another process held ``handle``."""
    path = handles.registry_path()
    reg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    reg[handle.casefold()] = {"handle": handle, "pid": pid, "ts": time.time() - age}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reg), encoding="utf-8")


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_compose() -> None:
    print("compose: project handle + optional role:")
    check(handles.compose(None, "CodeCarver") == "CodeCarver", "no role -> base")
    check(handles.compose("ui", "CodeCarver") == "CodeCarver/ui", "role -> base/role")
    check(handles.compose("codecarver", "CodeCarver") == "CodeCarver", "role == base (any case) -> base")
    check(handles.compose("CodeCarver/ui", "CodeCarver") == "CodeCarver/ui", "already prefixed -> not doubled")
    check(handles.compose("A", None) == "A", "no base -> chatter as-is")
    check(handles.compose("", "Base") == "Base", "empty chatter treated as omitted")
    check(len(handles.compose("x" * 40, "Proj")) == 32, "composed handle capped at 32 chars")
    try:
        handles.compose(None, None)
        raise AssertionError("no chatter and no base must error")
    except ConfigError as exc:
        check("DISCORDINATOR_CHAT_HANDLE" in str(exc), "neither -> ConfigError naming the fix")


def test_pid_alive() -> None:
    print("pid liveness (query only, never signals):")
    check(handles.pid_alive(os.getpid()) is True, "own pid alive")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        check(handles.pid_alive(child.pid) is True, "running child alive")
        check(child.poll() is None, "liveness check did NOT kill the child")
    finally:
        child.kill()
        child.wait()
    check(handles.pid_alive(_dead_pid()) is False, "exited process not alive")
    check(handles.pid_alive(0) is False and handles.pid_alive(-5) is False, "bogus pids not alive")
    check(handles.pid_alive("12") is False, "non-int pid not alive")


def test_collision_gets_suffix() -> None:
    print("a handle held by another LIVE session gets a -N suffix:")
    _fresh()
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _plant("CodeCarver", other.pid)
        cfg = {"chat_handle": "CodeCarver"}
        handle, note = handles.resolve(None, cfg)
        check(handle == "CodeCarver-2", f"second session -> CodeCarver-2 (got {handle})")
        check(note and "CodeCarver-2" in note and "chatter" in note, "note explains and suggests a role")
        again, note2 = handles.resolve(None, cfg)
        check(again == "CodeCarver-2" and note2, "stable for the life of the process")
        role, rnote = handles.resolve("ui", cfg)
        check(role == "CodeCarver/ui" and rnote is None, "a role avoids the collision cleanly")
        _plant("CodeCarver-2", other.pid)
        handles._resolved.clear()
        handles._last_chatter = None  # as a brand-new session
        third, _ = handles.resolve(None, cfg)
        check(third == "CodeCarver-3", "next free suffix is taken")
    finally:
        other.kill()
        other.wait()


def test_dead_or_expired_claims_reclaimed() -> None:
    print("claims from dead or expired sessions don't block the name:")
    _fresh()
    _plant("Proj", _dead_pid())
    check(handles.resolve(None, {"chat_handle": "Proj"})[0] == "Proj", "dead holder -> name reclaimed")
    _fresh()
    _plant("Proj", os.getppid(), age=handles.CLAIM_TTL + 60)
    check(handles.resolve(None, {"chat_handle": "Proj"})[0] == "Proj", "expired lease -> name reclaimed")
    reg = json.loads(handles.registry_path().read_text(encoding="utf-8"))
    check(reg["proj"]["pid"] == os.getpid(), "registry now records this process")


def test_live_session_keeps_name_however_idle() -> None:
    print("a live session keeps its name past the old 24h lease; a recycled pid doesn't:")
    _fresh()
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        started = handles.process_started(other.pid)
        check(started is not None and started == handles.process_started(other.pid),
              "process start time is readable and stable")
        _plant("Idle", other.pid, age=handles.CLAIM_TTL + 3600)
        reg = json.loads(handles.registry_path().read_text(encoding="utf-8"))
        reg["idle"]["started"] = started
        handles.registry_path().write_text(json.dumps(reg), encoding="utf-8")
        check(handles.resolve(None, {"chat_handle": "Idle"})[0] == "Idle-2",
              "same live process, idle for 25h -> still holds the name")
        check("Idle" in handles.live_handles(), "listed as a live handle")
        _fresh()
        _plant("Reused", other.pid)
        reg = json.loads(handles.registry_path().read_text(encoding="utf-8"))
        reg["reused"]["started"] = started + 1  # a different process had this pid
        handles.registry_path().write_text(json.dumps(reg), encoding="utf-8")
        check(handles.resolve(None, {"chat_handle": "Reused"})[0] == "Reused",
              "pid alive but a different process -> name reclaimed")
    finally:
        other.kill()
        other.wait()
    me = json.loads(handles.registry_path().read_text(encoding="utf-8"))["reused"]
    check(me.get("started") == handles.process_started(os.getpid()),
          "own claims record this process's start time")
    check(handles.process_started(-1) is None and handles.process_started(_dead_pid()) is None,
          "no start time for bad or dead pids")
    held = subprocess.Popen([sys.executable, "-c", "pass"])
    held.wait()  # exited, but this Popen still holds a handle to it
    check(handles.process_started(held.pid) is None,
          "an exited process has no start time even while a handle to it is open")


def test_rename_note_on_every_call() -> None:
    print("a renamed handle is reported on chat_say and chat_await too:")
    _fresh()
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "Echo"
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _plant("Echo", other.pid)
        mcp.chat_begin(channel="echo")
        said = mcp.chat_say(text="hi", channel="echo", wait=False)
        check("Echo-2" in said.get("handle_note", ""), "chat_say carries handle_note")
        waited = mcp.chat_await(channel="echo", timeout=0.1, poll=0.02, nudge_after=0)
        check("Echo-2" in waited.get("handle_note", ""), "chat_await carries handle_note")
    finally:
        other.kill()
        other.wait()
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")


def test_renamed_handle_passed_back() -> None:
    print("a renamed session that passes its new name back as chatter stays itself:")
    _fresh()
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "CodeCarver"
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _plant("CodeCarver", other.pid)
        b = mcp.chat_begin(channel="cc2")
        check(b["chatter"] == "CodeCarver-2", "renamed to CodeCarver-2")
        from discordinator import chat as _chat
        from discordinator.local_client import LocalClient
        _chat.send_chat(LocalClient("peer"), "cc2", "Peer", "over", "for you", to="CodeCarver-2")
        got = mcp.chat_await(chatter="CodeCarver-2", channel="cc2", timeout=1, poll=0.02,
                             nudge_after=0)
        check(got["from"] == "Peer" and got["your_turn"],
              "chatter='CodeCarver-2' is the same session, and gets its turn")
        slots = config.load_state()["chat"]["cc2"]
        check("codecarver/codecarver-2" not in slots, f"no stray role handle: {sorted(slots)}")
    finally:
        other.kill()
        other.wait()
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")


def test_status_uses_session_name_without_claiming() -> None:
    print("chat_status() with no chatter answers for this session's name and claims nothing:")
    _fresh()
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "CC"
    try:
        mcp.chat_begin(chatter="ui", channel="cc-room")
        from discordinator import chat as _chat
        from discordinator.local_client import LocalClient
        _chat.send_chat(LocalClient("peer"), "cc-room", "B", "over", "for ui", to="CC/ui")
        st = mcp.chat_status(channel="cc-room")
        check(st["your_turn"] is True, "answered for CC/ui (the turn is owed to it)")
        reg = json.loads(handles.registry_path().read_text(encoding="utf-8"))
        check("cc" not in reg and "cc/ui" in reg, f"bare 'CC' not claimed: {sorted(reg)}")
        check(handles.current(None, {}) is None or True, "current() never raises")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")
    _fresh()
    check(handles.current(None, {}) is None, "no handle configured and no chatter -> None")


def test_suffix_respects_length() -> None:
    print("the -N suffix never pushes a handle past 32 chars:")
    _fresh()
    long = "L" * 32
    _plant(long, os.getppid())
    h, _ = handles.resolve(None, {"chat_handle": long})
    check(len(h) == 32 and h.endswith("-2"), f"truncated to fit: {h}")


def test_release_all() -> None:
    print("release_all drops only this process's claims:")
    _fresh()
    handles.resolve(None, {"chat_handle": "Mine"})
    _plant("Theirs", os.getppid())
    handles.release_all()
    reg = json.loads(handles.registry_path().read_text(encoding="utf-8"))
    check("mine" not in reg and "theirs" in reg, f"own claim gone, other kept: {sorted(reg)}")
    handles.registry_path().unlink()
    handles.release_all()
    check(True, "release with no registry is a no-op")


def test_mcp_begin_reports_rename() -> None:
    print("chat_begin surfaces the rename as a note:")
    _fresh()
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "Twin"
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _plant("Twin", other.pid)
        out = mcp.chat_begin(channel="twins")
        check(out["chatter"] == "Twin-2" and "note" in out, "chatter Twin-2 with a note")
        mcp.chat_say(text="hi", channel="twins", wait=False)
        from discordinator.local_client import LocalClient
        last = LocalClient().read_messages("twins", limit=1)[0]["content"]
        check(last.startswith("[Twin-2|over]"), "chat_say posts under the same resolved name")
        st = mcp.chat_status(channel="twins")
        check(st["your_turn"] is False, "chat_status uses the resolved name (own turn not owed)")
    finally:
        other.kill()
        other.wait()
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")


def _restart() -> None:
    """As if this session's MCP server restarted: same project, nothing remembered."""
    handles.release_all()
    handles._resolved.clear()
    handles._last_chatter = None


def test_restart_takes_its_role_back() -> None:
    print("a restarted session that forgot its role takes back the turn owed to it:")
    from discordinator import chat
    from discordinator.local_client import LocalClient
    _fresh()
    c, room = LocalClient(), "restart"
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "ProjectB"
    try:
        check(mcp.chat_begin(chatter="convex", channel=room)["chatter"] == "ProjectB/convex",
              "before: ProjectB/convex")
        chat.send_chat(c, room, "ProjectB/convex", "over", "I'm ProjectB/convex now")
        chat.send_chat(c, room, "CodeCarver", "over", "noting your new handle. Q1? Q2?",
                       to="ProjectB/convex")
        _restart()
        st = mcp.chat_status(channel=room)
        check(st["your_turn"] is True and st["chatter"] == "ProjectB/convex"
              and "restart" in st["note"], "chat_status sees the turn under the old name")
        check(handles._resolved == {}, "chat_status still claims nothing")
        try:
            mcp.chat_say(text="back - what did you want?", channel=room, wait=False)
            check(False, "a reply without reading the question must be refused")
        except Exception as e:  # noqa: BLE001
            check("chat_await" in str(e), f"chat_say refuses until the question is read: {str(e)[:50]}")
        _restart()
        b = mcp.chat_begin(channel=room)
        check(b["chatter"] == "ProjectB/convex" and b["recovered_pending_turn"]
              and 'chatter="convex"' in b["note"], "chat_begin rejoins as ProjectB/convex")
        r = mcp.chat_await(channel=room, timeout=2, poll=0.2)
        check(r["your_turn"] and "Q1?" in r["text"], "chat_await delivers the question")
        mcp.chat_say(text="A1, A2", channel=room, wait=False)
        check(c.read_messages(room, limit=1)[0]["content"].startswith("[ProjectB/convex>CodeCarver|over]"),
              "the answer goes out under the same name")
        _restart()
        b = mcp.chat_begin(channel=room)  # its own answer is out, waiting for CodeCarver
        check(b["chatter"] == "ProjectB/convex" and "taken that name back" in b["note"],
              "restarted while waiting for a reply: it rejoins under its name too")
        chat.send_chat(c, room, "CodeCarver", "end", "thanks", to="ProjectB/convex")
        _restart()
        b = mcp.chat_begin(channel=room)  # nothing owed or waiting now
        check(b["chatter"] == "ProjectB",
              "with nothing owed, a fresh session is just the project handle")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")


def test_lost_role_not_stolen_or_guessed() -> None:
    print("a role held by a live session isn't taken; two lost roles are only named:")
    from discordinator import chat
    from discordinator.local_client import LocalClient
    _fresh()
    c = LocalClient()
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "ProjectB"
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        chat.send_chat(c, "held", "CodeCarver", "over", "Q?", to="ProjectB/ui")
        _plant("ProjectB/ui", other.pid)
        b = mcp.chat_begin(channel="held")
        check(b["chatter"] == "ProjectB" and "ProjectB/ui" not in (b.get("note") or ""),
              "a live sibling keeps its name and its turn")
        _fresh()
        chat.send_chat(c, "two", "CodeCarver", "over", "Q?", to="ProjectB/ui")
        chat.send_chat(c, "two", "Other", "over", "Q?", to="ProjectB/api")
        b = mcp.chat_begin(channel="two")
        check(b["chatter"] == "ProjectB", "two candidates: no guess")
        check("'ProjectB/ui'" in b["note"] and "'ProjectB/api'" in b["note"]
              and 'chatter="ui"' in b["note"], "both lost turns named, with the fix")
        r = mcp.chat_await(channel="two", timeout=0.3, poll=0.1)
        check(r["timed_out"] and "ProjectB/ui" in r["handle_note"],
              "a timed-out wait points at them too")
    finally:
        other.kill()
        other.wait()
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")


def test_turn_owed_to_the_name_before_a_project_handle() -> None:
    print("a turn owed to the old handle (before the project had one) is pointed out:")
    from discordinator import chat
    from discordinator.local_client import LocalClient
    _fresh()
    c, room = LocalClient(), "oldname"
    chat.send_chat(c, room, "carver", "over", "Q?", to="convex")
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "TDTS_Convex"
    try:
        b = mcp.chat_begin(chatter="convex", channel=room)
        check(b["chatter"] == "TDTS_Convex/convex", "the session keeps its new name")
        check("'convex'" in b["note"] and "read_messages" in b["note"],
              "chat_begin says a turn is owed to its old name and how to answer it")
        st = mcp.chat_status(channel=room)
        check("'convex'" in st.get("note", ""), "chat_status says so too")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")


def test_name_taken_meanwhile_isnt_shared() -> None:
    print("a session passing its full name back never shares it with another:")
    _fresh()
    cfg = {"chat_handle": "ProjQ"}
    h, _ = handles.resolve("ProjQ/ui", cfg)
    check(h == "ProjQ/ui", "first: ProjQ/ui")
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _plant("ProjQ/ui", other.pid)  # someone else got it (e.g. while this one restarted)
        h2, note = handles.resolve("ProjQ/ui", cfg)
        check(h2 == "ProjQ/ui-2", f"this session moves to ProjQ/ui-2, not a shared name: {h2}")
        check(note and "ProjQ/ui-2" in note, "and is told so")
        check("/ui/ui" not in note and "'ProjQ/ui'" in note.split("e.g.")[1],
              f"the note's example is a real handle: {note.split('e.g.')[1]}")
        again, _ = handles.resolve("ProjQ/ui-2", cfg)
        check(again == "ProjQ/ui-2", "and it keeps that name")
    finally:
        other.kill()
        other.wait()


def test_running_sessions_names_are_reserved() -> None:
    print("a running session's names stay its own while its server restarts:")
    _fresh()
    path = handles.sessions_path()
    path.write_text(json.dumps({"id:other-session": {
        "chatter": "ui", "resolved": {"projq/ui": "ProjQ/ui"}, "ts": time.time()}}),
        encoding="utf-8")
    try:
        check("ProjQ/ui" in handles.live_handles(), "listed as held (nobody claims it right now)")
        h, note = handles.resolve("ui", {"chat_handle": "ProjQ"})
        check(h == "ProjQ/ui-2" and note, f"another session asking for it gets -2: {h}")
        check(handles.current("ui", {"chat_handle": "ProjQ"}) == "ProjQ/ui-2",
              "chat_status agrees")
    finally:
        path.unlink(missing_ok=True)


def test_session_is_found_past_launchers() -> None:
    print("the session is the process above any launcher:")
    for name in ("discordinator-mcp.exe", "python.exe", "Python3.12", "pythonw.exe", "py.exe",
                 "python3"):
        check(handles._is_launcher(name), f"{name} is a launcher")
    for name in ("claude.exe", "node", "claude", "bash.exe", None):
        check(not handles._is_launcher(name), f"{name} is not")
    me, name = handles.process_parent(os.getpid())
    check(me == os.getppid() and handles._is_launcher(name),
          f"process_parent reads this process: {me} {name}")
    top = handles.session_pid(os.getpid())  # this python is a launcher-like hop
    check(top != os.getpid() and not handles._is_launcher(handles.process_parent(top)[1]),
          f"walks up to a non-Python process: {handles.process_parent(top)[1]}")


def main() -> int:
    test_compose()
    test_pid_alive()
    test_collision_gets_suffix()
    test_dead_or_expired_claims_reclaimed()
    test_live_session_keeps_name_however_idle()
    test_rename_note_on_every_call()
    test_renamed_handle_passed_back()
    test_status_uses_session_name_without_claiming()
    test_suffix_respects_length()
    test_release_all()
    test_mcp_begin_reports_rename()
    test_restart_takes_its_role_back()
    test_lost_role_not_stolen_or_guessed()
    test_turn_owed_to_the_name_before_a_project_handle()
    test_name_taken_meanwhile_isnt_shared()
    test_running_sessions_names_are_reserved()
    test_session_is_found_past_launchers()
    print(f"\nALL {_passed} HANDLE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
