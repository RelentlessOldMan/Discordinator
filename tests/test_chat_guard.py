"""Stop-hook guard (``discordinator chat-guard``): block a stop that would strand
a live chat, exactly once, and stay out of the way otherwise.

Uses synthetic Claude Code transcripts (JSONL: user/assistant entries with
tool_use / tool_result blocks) — the same shape the real hook payload's
``transcript_path`` points at.
Run:  python tests/test_chat_guard.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-guard-"))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "cfg" / "config.json")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discordinator import guard  # noqa: E402

_passed = 0
_n = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def call(tool: str, args: dict, result: object, *, tid: str = None,
         server: str = "discordinator", sidechain: bool = False) -> list[dict]:
    global _n
    _n += 1
    tid = tid or f"toolu_{_n}"
    content = result if isinstance(result, (str, list)) else json.dumps(result)
    a = {"type": "assistant", "isSidechain": sidechain, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": f"mcp__{server}__{tool}", "input": args}]}}
    u = {"type": "user", "isSidechain": sidechain, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": content}]}}
    return [a, u]


def say(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": text}]}}


def transcript(*entries) -> str:
    flat: list[dict] = []
    for e in entries:
        flat.extend(e if isinstance(e, list) else [e])
    path = _TMP / f"t{len(list(_TMP.iterdir()))}.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in flat) + "\n", encoding="utf-8")
    return str(path)


def run(path: str, **extra) -> str:
    return guard.main(json.dumps({"transcript_path": path, "hook_event_name": "Stop", **extra}))


WAITING = {"sent_messages": 1, "status": "over", "ended": False,
           "reply": {"timed_out": True, "ended": False, "your_turn": False,
                     "next": "Call chat_await again NOW."},
           "next": "Call chat_await again NOW."}


def test_blocks_dropping_out_while_waiting() -> None:
    print("posted a turn, reply not in yet, model tries to stop -> blocked once:")
    p = transcript(user("chat with B"), call("chat_begin", {}, {"chatter": "A"}),
                   call("chat_say", {"text": "hi", "status": "over"}, WAITING),
                   say("I'll wait for B to reply."))
    out = json.loads(run(p))
    check(out["decision"] == "block", "stop blocked")
    check("chat_await again NOW" in out["reason"], "reason carries the exact next step")
    check("end your turn again" in out["reason"], "reason says how to stop if truly needed")
    check(run(p, stop_hook_active=True) == "", "second stop attempt is allowed")


def test_reminds_again_if_it_keeps_chatting() -> None:
    print("kept chatting after the reminder, then dropped out again -> reminded again:")
    first = [user("chat with B"), call("chat_say", {"status": "over"}, WAITING)]
    p = transcript(*first)
    sid = {"session_id": "sess-repeat"}
    check(json.loads(run(p, **sid))["decision"] == "block", "first drop-out blocked")
    more = first + [call("chat_await", {}, {"timed_out": True, "ended": False,
                                            "next": "Call chat_await again NOW."})]
    p2 = transcript(*more)
    out = run(p2, stop_hook_active=True, **sid)
    check(out and json.loads(out)["decision"] == "block",
          "continued (one more chat_await) then stopped again -> blocked again")
    check(run(p2, stop_hook_active=True, **sid) == "",
          "stopping again with no new chat activity -> allowed (it means it)")
    check(json.loads(run(p2, **{"session_id": "other"}))["decision"] == "block",
          "another session's record is independent")


def test_reminder_logged_as_user_message() -> None:
    print("works even if the hook's reminder is logged as a user message:")
    first = [user("chat with B"), call("chat_say", {"status": "over"}, WAITING)]
    sid = {"session_id": "sess-feedback"}
    check(json.loads(run(transcript(*first), **sid))["decision"] == "block", "first stop blocked")
    after = first + [user("Stop hook feedback: You're in a live discordinator chat..."),
                     call("chat_await", {}, {"timed_out": True, "ended": False,
                                             "next": "Call chat_await again NOW."})]
    p = transcript(*after)
    out = run(p, stop_hook_active=True, **sid)
    check(out and json.loads(out)["decision"] == "block",
          "kept chatting after the reminder, stopped again -> blocked again")
    check(run(p, stop_hook_active=True, **sid) == "", "then a bare repeat stop is allowed")


def test_failed_chat_say() -> None:
    print("a chat_say that errored (turn probably never went out) -> blocked once:")
    p = transcript(user("chat"), call("chat_await", {}, {"from": "B", "your_turn": True,
                                                         "ended": False}),
                   call("chat_say", {"text": "see file", "status": "over"},
                        "Error executing tool chat_say: File not found. Nothing was posted"))
    sid = {"session_id": "sess-failed-say"}
    out = json.loads(run(p, **sid))
    check(out["decision"] == "block" and "returned an error" in out["reason"]
          and "impasse" in out["reason"], "blocked with how to recover")
    check(run(p, stop_hook_active=True, **sid) == "", "a repeat stop is allowed")
    a = [user("chat"), {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_noresult", "name": "mcp__discordinator__chat_say",
         "input": {"status": "over"}}]}}]
    check(run(transcript(*a)) == "", "a chat_say with no result yet (interrupted) -> allowed")


def test_blocks_when_its_your_turn() -> None:
    print("got the reply (your turn) but stops without answering -> blocked:")
    res = {"from": "B", "your_turn": True, "ended": False, "next": "It's YOUR turn. Reply with chat_say"}
    p = transcript(user("go"), call("chat_await", {}, res))
    check("YOUR turn" in json.loads(run(p))["reason"], "told to reply")


def test_allows_when_ended() -> None:
    print("chat over -> stopping is fine:")
    p1 = transcript(user("go"), call("chat_await", {}, {"ended": True, "stop_reason": "agreed"}))
    check(run(p1) == "", "ended result -> allowed")
    p2 = transcript(user("go"), call("chat_say", {"status": "over"},
                                     {"ended": False, "reply": {"ended": True}}))
    check(run(p2) == "", "ended reply inside chat_say -> allowed")
    p3 = transcript(user("go"), call("chat_say", {"status": "end"}, {"ended": True}))
    check(run(p3) == "", "own end -> allowed")
    p4 = transcript(user("go"), call("chat_say", {"status": "impasse"}, "Error executing tool"))
    out = run(p4)
    check(out and "returned an error" in json.loads(out)["reason"],
          "own impasse that FAILED -> blocked (the others never saw it)")
    check(run(p4, stop_hook_active=True) == "", "...once")


def test_only_current_turn_counts() -> None:
    print("chat activity from an EARLIER turn doesn't hold later turns hostage:")
    p = transcript(user("chat with B"), call("chat_say", {"status": "over"}, WAITING),
                   user("ok forget the chat, what's 2+2?"), say("4"))
    check(run(p) == "", "new user message -> not our business")
    p2 = transcript(user("chat"), call("chat_say", {"status": "over"}, WAITING),
                    {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta"}})
    check(json.loads(run(p2))["decision"] == "block", "meta lines don't count as the human")


def test_ignores_other_tools_and_subagents() -> None:
    print("non-chat tools and subagent (sidechain) chats are ignored:")
    p = transcript(user("hi"), call("send_message", {}, {"result": "Sent"}),
                   call("read_messages", {}, {"result": []}))
    check(run(p) == "", "relay tools only -> allowed")
    p2 = transcript(user("hi"), call("chat_say", {"status": "over"}, WAITING, sidechain=True))
    check(run(p2) == "", "a subagent's chat isn't this session's")
    p3 = transcript(user("hi"), call("chat_status", {}, {"session_active": True}))
    check(run(p3) == "", "chat_status alone isn't participation")


def test_result_shapes() -> None:
    print("tolerates every MCP result shape:")
    blocks = [{"type": "text", "text": json.dumps(WAITING)}]
    p = transcript(user("x"), call("chat_say", {"status": "over"}, blocks))
    check(json.loads(run(p))["decision"] == "block", "text-block list parsed")
    p2 = transcript(user("x"), call("chat_await", {}, {"result": {"timed_out": True, "ended": False}}))
    check("chat_await" in json.loads(run(p2))["reason"], "{'result': {...}} unwrapped; default next")
    p3 = transcript(user("x"), call("chat_await", {}, "Error executing tool chat_await"))
    check("chat_await again" in json.loads(run(p3))["reason"],
          "a failed chat_await mid-chat blocks: wait again (it was waiting, so the chat is live)")
    p3b = transcript(user("x"), call("chat_begin", {}, "Error executing tool chat_begin"))
    check(run(p3b) == "", "a failed chat_begin with nothing before it -> allowed (don't guess)")
    p3c = transcript(user("x"), call("chat_await", {}, "Error executing tool chat_await"),
                     call("chat_say", {"status": "over"}, WAITING),
                     call("chat_begin", {}, "Error executing tool chat_begin"))
    check("chat_await again" in json.loads(run(p3c))["reason"],
          "a failed chat_begin after a live chat result blocks (skipping unreadable results)")
    p3d = transcript(user("x"), call("chat_await", {}, {"ended": True}),
                     call("chat_begin", {}, "Error executing tool chat_begin"))
    check(run(p3d) == "", "...but not when that result showed the chat ended")
    p4 = transcript(user("x"), call("chat_say", {"status": "over"}, WAITING, server="disco2"))
    check(json.loads(run(p4))["decision"] == "block", "any MCP server name works")


def test_never_breaks() -> None:
    print("bad input never raises or blocks:")
    check(guard.main("not json") == "", "garbage stdin -> allow")
    check(guard.main("") == "", "empty stdin -> allow")
    check(guard.main(json.dumps({"transcript_path": str(_TMP / "missing.jsonl")})) == "",
          "missing transcript -> allow")
    check(guard.main("[1,2]") == "", "non-object payload -> allow")
    p = transcript(user("x"), {"type": "assistant", "message": {"role": "assistant", "content": [
        "stray", {"type": "tool_use", "id": "t-bash", "name": "Bash", "input": {}}]}},
        call("chat_say", {"status": "over"}, WAITING))
    check(json.loads(run(p))["decision"] == "block", "stray blocks and non-MCP tools skipped")
    orig = guard.config._atomic_write
    def fail(*a, **k):
        raise OSError("read-only")
    guard.config._atomic_write = fail
    try:
        check(json.loads(run(p))["decision"] == "block", "unwritable state file -> still works")
        check(run(p, stop_hook_active=True, session_id="no-record") == "",
              "...but can't record a reminder on a repeat stop -> allow (never loop)")
    finally:
        guard.config._atomic_write = orig
    bad = _TMP / "torn.jsonl"
    bad.write_text("{torn\n" + json.dumps(user("x")) + "\n", encoding="utf-8")
    check(run(str(bad)) == "", "torn lines tolerated")


def test_cli_end_to_end() -> None:
    print("`discordinator chat-guard` works as a real hook process:")
    p = transcript(user("chat"), call("chat_say", {"status": "over"}, WAITING))
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    r = subprocess.run([sys.executable, "-m", "discordinator.cli", "chat-guard"],
                       input=json.dumps({"transcript_path": p}), capture_output=True,
                       text=True, env=env, timeout=60)
    check(r.returncode == 0, "exit 0 (decision travels in the JSON)")
    check(json.loads(r.stdout)["decision"] == "block", "prints the block decision")
    r2 = subprocess.run([sys.executable, "-m", "discordinator.cli", "chat-guard"],
                        input="{}", capture_output=True, text=True, env=env, timeout=60)
    check(r2.returncode == 0 and r2.stdout.strip() == "", "no-op prints nothing")
    import io
    from discordinator import cli
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps({"transcript_path": p})), io.StringIO()
    try:
        rc = cli.main(["chat-guard"])
        printed = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = old_in, old_out
    check(rc == 0 and json.loads(printed)["decision"] == "block", "in-process cli entry too")


def main() -> int:
    test_blocks_dropping_out_while_waiting()
    test_reminds_again_if_it_keeps_chatting()
    test_reminder_logged_as_user_message()
    test_failed_chat_say()
    test_blocks_when_its_your_turn()
    test_allows_when_ended()
    test_only_current_turn_counts()
    test_ignores_other_tools_and_subagents()
    test_result_shapes()
    test_never_breaks()
    test_cli_end_to_end()
    print(f"\nALL {_passed} CHAT-GUARD CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
