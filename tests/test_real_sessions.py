"""Real sessions: each one a separate `python -m discordinator.mcp_server`
process driven over stdio, the way Claude Code runs it.

The other suites call the tool functions directly inside one process, which
can't show what breaks in real use: a server restarting (and forgetting what
it knew), an old server left running after a reconnect, errors passing
through the MCP layer, separate processes racing. Each check here replays one
of those, most of them taken from real stalls.

DISCORDINATOR_SESSION_ID stands in for the Claude Code process that owns a
server (normally its parent process): two servers with the same id are one
session restarting; different ids are different sessions. One check runs
without it, through a launcher process, to cover the real lookup.
Run:  python tests/test_real_sessions.py
"""

from __future__ import annotations

import asyncio
import json
import atexit
import os
import shutil
import sys
import tempfile
import time
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
_TMP = Path(tempfile.mkdtemp(prefix="discordinator-real-"))
os.chdir(_TMP)  # never the repo: a .env there would be loaded into the test
atexit.register(lambda: (os.chdir(tempfile.gettempdir()),
                         shutil.rmtree(_TMP, ignore_errors=True)))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
for _k in ("DISCORD_BOT_TOKEN", "DISCORDINATOR_CHAT_HANDLE", "DISCORDINATOR_CHAT_CHANNEL",
           "DISCORDINATOR_LABEL", "DISCORDINATOR_RELAY_CHANNEL", "DISCORDINATOR_SESSION_ID"):
    os.environ.pop(_k, None)
os.environ["PYTHONPATH"] = SRC + os.pathsep + os.environ.get("PYTHONPATH", "")
os.environ["DISCORDINATOR_LABEL"] = "M"  # this machine's name, in every full name

sys.path.insert(0, SRC)

from discordinator import chat, events  # noqa: E402
from discordinator.local_client import LocalClient, local_dir  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


class Session:
    """One real MCP server process, as a Claude Code session would run it."""

    def __init__(self, session_id: str | None, handle: str | None = None,
                 launcher: bool = False, **env: str):
        self.env = {**os.environ, **env}
        if session_id is not None:  # None: the real thing - the parent process
            self.env["DISCORDINATOR_SESSION_ID"] = session_id
        if handle:
            self.env["DISCORDINATOR_CHAT_HANDLE"] = handle
        self.launcher = launcher
        self.stack = AsyncExitStack()

    async def start(self) -> "Session":
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        args = ["-m", "discordinator.mcp_server"]
        if self.launcher:
            # Like discordinator-mcp.exe or a venv's python.exe on Windows: a
            # process (a new one each start) that runs the server as its child.
            args = ["-c", "import subprocess, sys; sys.exit(subprocess.call("
                    "[sys.executable, '-m', 'discordinator.mcp_server']))"]
        params = StdioServerParameters(command=sys.executable, args=args,
                                       env=self.env, cwd=str(_TMP))
        r, w = await self.stack.enter_async_context(stdio_client(params))
        self.s = await self.stack.enter_async_context(ClientSession(r, w))
        await self.s.initialize()
        return self

    async def stop(self) -> None:
        await self.stack.aclose()

    async def call(self, tool: str, **args) -> dict:
        res = await self.s.call_tool(tool, args)
        text = "\n".join(c.text for c in res.content if getattr(c, "text", None) is not None)
        if res.is_error:
            return {"error": text}
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text}


def run(coro) -> None:
    asyncio.run(coro)


def backdate(room: str, hours: float) -> None:
    """Make every message in a local room look ``hours`` old."""
    path = local_dir() / f"{room}.jsonl"
    when = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        rec["timestamp"] = when
        lines.append(json.dumps(rec))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_reconnect_while_waiting_keeps_the_name() -> None:
    print("a session reconnected mid-wait keeps its name and gets the reply:")

    async def go():
        a1 = await Session("sess-a", handle="ProjA").start()
        check((await a1.call("chat_begin", chatter="ui", channel="r1"))["chatter"] == "M:ProjA/ui",
              "before: ProjA/ui")
        await a1.call("chat_say", text="please review X", chatter="ui", channel="r1",
                      to="ProjB", wait=False)
        await a1.stop()  # /mcp reconnect: the server goes, the session stays
        a2 = await Session("sess-a", handle="ProjA").start()
        first = await a2.call("chat_await", channel="r1", timeout=0.5, poll=0.1)  # no chatter
        check(first.get("timed_out"), "nothing yet - still waiting")
        chat.send_chat(LocalClient(), "r1", "ProjB", "over", "reviewed: fine", to="M:ProjA/ui")
        r = await a2.call("chat_await", channel="r1", timeout=5, poll=0.2)
        check(r.get("from") == "ProjB" and r.get("text") == "reviewed: fine",
              f"the reply that comes later reaches it: {str(r)[:80]}")
        await a2.stop()
    run(go())


def test_old_server_left_running_doesnt_take_the_name() -> None:
    print("an old server Claude Code left running can't push its session to '-2':")

    async def go():
        old = await Session("sess-z", handle="ProjZ").start()
        check((await old.call("chat_begin", channel="r2"))["chatter"] == "M:ProjZ", "old: ProjZ")
        new = await Session("sess-z", handle="ProjZ").start()  # reconnect; old still alive
        b = await new.call("chat_begin", channel="r2")
        check(b["chatter"] == "M:ProjZ" and "note" not in b,
              f"the new server is ProjZ, not ProjZ-2: {b.get('chatter')}")
        chat.send_chat(LocalClient(), "r2", "Peer", "over", "hello ProjZ", to="M:ProjZ")
        r = await new.call("chat_await", channel="r2", timeout=5, poll=0.2)
        check(r.get("text") == "hello ProjZ", "and it gets the turns sent to that name")
        other = await Session("sess-other", handle="ProjZ").start()
        o = await other.call("chat_begin", channel="r2")
        check(o["chatter"] == "M:ProjZ-2" and "note" in o,
              "a genuinely different session of the project still gets ProjZ-2")
        for s in (other, new, old):  # the client library closes newest first
            await s.stop()
    run(go())


def test_disconnect_ends_a_wait_promptly() -> None:
    print("a server whose client hangs up mid-wait exits at once:")
    import subprocess
    env = {**os.environ, "DISCORDINATOR_SESSION_ID": "sess-w", "DISCORDINATOR_CHAT_HANDLE": "ProjW"}
    p = subprocess.Popen([sys.executable, "-m", "discordinator.mcp_server"], cwd=str(_TMP),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=env)

    def send(msg: dict) -> None:
        p.stdin.write((json.dumps(msg) + "\n").encode())
        p.stdin.flush()

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                     "clientInfo": {"name": "test", "version": "1"}}})
    p.stdout.readline()
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
          "params": {"name": "chat_begin", "arguments": {"channel": "r3"}}})
    p.stdout.readline()
    send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
          "params": {"name": "chat_await",
                     "arguments": {"channel": "r3", "timeout": 60, "poll": 0.2, "nudge_after": 0}}})
    time.sleep(1.0)
    t0 = time.monotonic()
    p.stdin.close()  # the client hangs up; nothing kills the process
    try:
        p.wait(timeout=30)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    took = time.monotonic() - t0
    check(took < 10, f"exited {took:.1f}s after the client hung up (its wait had 60s left)")

    async def go():
        chat.send_chat(LocalClient(), "r3", "Peer", "over", "for ProjW", to="M:ProjW")
        s2 = await Session("sess-w", handle="ProjW").start()
        r = await s2.call("chat_await", channel="r3", timeout=5, poll=0.2)
        check(r.get("text") == "for ProjW", "the session's next server gets the reply")
        await s2.stop()
    run(go())


def test_two_sessions_of_a_project_keep_their_own_names() -> None:
    print("two sessions of one project restarting together don't swap names:")

    async def go():
        x = await Session("sess-x", handle="ProjQ").start()
        y = await Session("sess-y", handle="ProjQ").start()
        check((await x.call("chat_begin", channel="r4"))["chatter"] == "M:ProjQ", "X: ProjQ")
        check((await y.call("chat_begin", channel="r4"))["chatter"] == "M:ProjQ-2", "Y: ProjQ-2")
        await y.stop()
        await x.stop()
        c = LocalClient()
        chat.send_chat(c, "r4", "PeerB", "over", "answer for X", to="M:ProjQ")
        chat.send_chat(c, "r4", "PeerC", "over", "answer for Y", to="M:ProjQ-2")
        y2 = await Session("sess-y", handle="ProjQ").start()  # Y comes back first
        ry = await y2.call("chat_await", channel="r4", timeout=5, poll=0.2)
        x2 = await Session("sess-x", handle="ProjQ").start()
        rx = await x2.call("chat_await", channel="r4", timeout=5, poll=0.2)
        check(ry.get("text") == "answer for Y" and rx.get("text") == "answer for X",
              f"each gets its own reply: Y={ry.get('text')!r} X={rx.get('text')!r}")
        await x2.stop()
        await y2.stop()
    run(go())


def test_two_answers_at_once() -> None:
    print("two sessions answering the same human at the same moment: one gets through:")

    async def go():
        a = await Session("sess-ra", handle="RaceA").start()
        b = await Session("sess-rb", handle="RaceB").start()
        for s in (a, b):
            await s.call("chat_begin", channel="race")
        LocalClient().post_human("race", "what do you both think?")
        for s in (a, b):
            await s.call("chat_await", channel="race", timeout=5, poll=0.1, nudge_after=0)
        ra, rb = await asyncio.gather(
            a.call("chat_say", text="A's view", channel="race", wait=False),
            b.call("chat_say", text="B's view", channel="race", wait=False))
        await b.stop()
        await a.stop()
        got = sorted("refused" if "error" in r else "posted" for r in (ra, rb))
        check(got == ["posted", "refused"], f"one posts, one is refused: {ra} {rb}")
    run(go())


def test_bystander_post_is_nobodys_reply() -> None:
    print("a relay post into the chat room doesn't answer for anyone:")

    async def go():
        a = await Session("sess-ba", handle="ProjA").start()
        b = await Session("sess-bb", handle="ProjB").start()
        c = await Session("sess-bc", handle="ProjC").start()
        await a.call("chat_begin", channel="r5")
        await b.call("chat_begin", channel="r5")
        await a.call("chat_say", text="question for B", channel="r5", to="ProjB", wait=False)
        await c.call("send_message", text="FYI: CI is green", channel="r5")
        r = await a.call("chat_await", channel="r5", timeout=1, poll=0.2)
        check(r.get("timed_out") and not r.get("your_turn"), "A keeps waiting for B")
        st = await b.call("chat_status", channel="r5")
        check(st.get("your_turn") is True, "B still owes A its answer")
        for s in (c, b, a):
            await s.stop()
    run(go())


def test_errors_reach_the_model_over_stdio() -> None:
    print("a tool's refusal reaches the model in full over real stdio:")

    async def go():
        s = await Session("sess-e", handle="ProjE").start()
        r = await s.call("chat_say", text="x", channel="r6", status="bogus")
        check("status must be one of" in r.get("error", "") and "Nothing was posted" in r["error"],
              f"the reason arrives: {r.get('error', '')[:60]}")
        await s.stop()
    run(go())


def test_old_turns_expire() -> None:
    print("a long-dead conversation's turn isn't handed to a new session:")

    async def go():
        chat.send_chat(LocalClient(), "r7", "ProjB", "wrap", "done?", to="M:ProjF")
        backdate("r7", 72)
        s = await Session("sess-f", handle="ProjF").start()
        b = await s.call("chat_begin", channel="r7")
        check(b.get("recovered_pending_turn") is False, "3-day-old turn not recovered")
        await s.stop()
    run(go())


def test_abandoned_conversation_hears_a_new_opener() -> None:
    print("an unaddressed opener reaches a session whose last chat was dropped:")

    async def go():
        c = LocalClient()
        chat.send_chat(c, "r8", "M:ProjG", "over", "q", to="M:ProjH")
        chat.send_chat(c, "r8", "M:ProjH", "over", "a", to="M:ProjG")
        chat.send_chat(c, "r8", "M:ProjG", "over", "thanks, one more?", to="M:ProjH")  # dropped
        backdate("r8", 1)
        g = await Session("sess-g", handle="ProjG").start()
        await g.call("chat_begin", channel="r8")
        chat.send_chat(c, "r8", "ProjK", "over", "anyone free to help?")  # unaddressed
        r = await g.call("chat_await", channel="r8", timeout=5, poll=0.2)
        check(r.get("from") == "ProjK" and r.get("your_turn"), f"ProjG hears it: {str(r)[:70]}")
        await g.call("chat_say", text="sure", channel="r8", wait=False)
        st = chat.compute_state(c, "r8", "ProjK")
        check(st["your_turn"], "the reply goes back to the opener")
        await g.stop()
    run(go())


def test_launcher_restart_keeps_the_name() -> None:
    print("a server started through a launcher keeps its name across a restart:")

    async def go():
        # No DISCORDINATOR_SESSION_ID: the session is found from the real
        # process tree, past the launcher (a new one on every reconnect).
        l1 = await Session(None, handle="ProjL", launcher=True).start()
        check((await l1.call("chat_begin", chatter="ui", channel="r10"))["chatter"] == "M:ProjL/ui",
              "before: ProjL/ui")
        await l1.stop()
        l2 = await Session(None, handle="ProjL", launcher=True).start()
        b = await l2.call("chat_begin", channel="r10")  # chatter omitted, as models do
        check(b.get("chatter") == "M:ProjL/ui",
              f"after the restart it's still ProjL/ui, not the bare name: {b.get('chatter')}")
        await l2.stop()
    run(go())


def test_restart_gap_doesnt_hand_the_name_to_a_newcomer() -> None:
    print("a new session can't take a restarting session's name (and its reply):")

    async def go(explicit: bool):
        room, P = ("r11e", "ProjR") if explicit else ("r11", "ProjQ")
        kw = {"chatter": f"{P}/ui" if explicit else "ui"}
        x1 = await Session("sess-x" + room, handle=P).start()
        await x1.call("chat_begin", channel=room, **kw)
        await x1.call("chat_say", text="please review", channel=room, to="Work:Peer", wait=False, **kw)
        await x1.stop()  # reconnect: the old server is gone, the new one not yet started
        await asyncio.sleep(1.5)
        y = await Session("sess-y" + room, handle=P).start()  # another session of ProjQ
        yb = await y.call("chat_begin", channel=room)
        check(yb.get("chatter") != f"M:{P}/ui", f"the newcomer isn't {P}/ui: {yb.get('chatter')}")
        x2 = await Session("sess-x" + room, handle=P).start()
        chat.send_chat(LocalClient(), room, "Work:Peer", "over", "reviewed: fine", to=f"M:{P}/ui")
        a = await x2.call("chat_await", channel=room, timeout=5, poll=0.2,
                          **({"chatter": f"{P}/ui"} if explicit else {}))
        check(a.get("text") == "reviewed: fine" and not a.get("handle_note"),
              f"the restarted session, still {P}/ui, gets the reply: {str(a)[:80]}")
        ya = await y.call("chat_await", channel=room, timeout=0.5, poll=0.1)
        check(ya.get("timed_out"), "the newcomer doesn't")
        await x2.stop()
        await y.stop()
    run(go(False))
    print(" ...and when the session passes its full name as chatter:")
    run(go(True))


def _set_label(name: str) -> None:
    """`config set-label` (the sessions below go by the config file's name)."""
    path = Path(os.environ["DISCORDINATOR_CONFIG"])
    cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg["machine_label"] = name
    cfg["machine_label_since"] = time.time()
    path.write_text(json.dumps(cfg), encoding="utf-8")


def _unlabelled(session_id: str, handle: str) -> Session:
    s = Session(session_id, handle=handle)
    s.env.pop("DISCORDINATOR_LABEL", None)
    return s


def test_two_sessions_here_renamed_together() -> None:
    print("two sessions on a machine renamed mid-chat go on talking:")
    room = "renamed-together"

    async def go() -> None:
        _set_label("Home")
        a = await _unlabelled("sess-rt-a", "Carver").start()
        b = await _unlabelled("sess-rt-b", "Other").start()
        for s in (a, b):
            await s.call("chat_begin", channel=room)
        await a.call("chat_say", text="q1", channel=room, to="Other", wait=False)
        await b.call("chat_await", channel=room, timeout=5, poll=0.1)
        await b.call("chat_say", text="a1", channel=room, wait=False)
        await a.call("chat_await", channel=room, timeout=5, poll=0.1)
        await a.call("chat_say", text="q2", channel=room, wait=False)
        _set_label("Harbinger")
        got = await b.call("chat_await", channel=room, timeout=5, poll=0.1)
        check(got.get("text") == "q2", f"B gets the turn sent to its old name: {str(got)[:80]}")
        await b.call("chat_say", text="a2", channel=room, wait=False)
        last = LocalClient().read_messages(room, limit=1)[0]["content"]
        check(last.startswith("[Harbinger:Other>Harbinger:Carver|over]"),
              f"its reply goes to A's name now: {last[:45]}")
        got = await a.call("chat_await", channel=room, timeout=5, poll=0.1)
        check(got.get("text") == "a2", f"and reaches A: {str(got)[:80]}")
        st = await a.call("chat_status", channel=room)
        check(not st.get("multiparty") and st.get("floor") == "Harbinger:Carver",
              f"still two of them, by the names they go by now: {st.get('participants')}")
        await b.stop()
        await a.stop()
    try:
        run(go())
    finally:
        _set_label("")


def test_deleted_old_post_still_counts_after_a_restart() -> None:
    print("a deleted post under the old name still says whom it talked to, after a restart:")
    room = "deleted-old-post"

    async def go() -> None:
        _set_label("Home")
        c = LocalClient()
        a = await _unlabelled("sess-do-a", "Deleter").start()
        try:
            await a.call("chat_begin", channel=room)
            chat.send_chat(c, room, "Work:Peer", "over", "q1", to="Home:Deleter")
            await a.call("chat_await", channel=room, timeout=5, poll=0.1)
            first = (await a.call("chat_say", text="a1", channel=room, wait=False))["message_ids"]
            _set_label("Harbinger")
            await a.call("delete_messages", message_ids=first, channel=room)
        finally:
            await a.stop()
        a = await _unlabelled("sess-do-a", "Deleter").start()
        try:
            chat.send_chat(c, room, "Work:Peer", "over", "q2", to="Home:Deleter")
            got = await a.call("chat_await", channel=room, timeout=5, poll=0.1)
            check(got.get("text") == "q2",
                  f"its peer's turn to the old name reaches it: {str(got)[:80]}")
        finally:
            await a.stop()
    try:
        run(go())
    finally:
        _set_label("")


def test_session_events_are_logged() -> None:
    print("session events land in the machine's event log:")
    seen = [e["kind"] for e in events.read(0)[0]]
    for kind in ("server_start", "joined", "error", "server_exit", "renamed"):
        check(kind in seen, f"{kind} logged")


def main() -> int:
    test_reconnect_while_waiting_keeps_the_name()
    test_old_server_left_running_doesnt_take_the_name()
    test_disconnect_ends_a_wait_promptly()
    test_two_sessions_of_a_project_keep_their_own_names()
    test_two_answers_at_once()
    test_bystander_post_is_nobodys_reply()
    test_errors_reach_the_model_over_stdio()
    test_old_turns_expire()
    test_abandoned_conversation_hears_a_new_opener()
    test_launcher_restart_keeps_the_name()
    test_restart_gap_doesnt_hand_the_name_to_a_newcomer()
    test_two_sessions_here_renamed_together()
    test_deleted_old_post_still_counts_after_a_restart()
    test_session_events_are_logged()
    print(f"\nALL {_passed} REAL-SESSION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
