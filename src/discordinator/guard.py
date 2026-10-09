"""Claude Code Stop-hook guard: don't let a session drop out of a live chat.

A chat only advances while each session keeps calling the chat tools. If a
model posts its turn, says "I'll wait for their reply", and ENDS its turn,
nothing can wake it when the reply lands — a human has to kick it. This guard
runs as a Claude Code ``Stop`` hook (``discordinator chat-guard``) and blocks
that stop ONCE, with the exact next step, when:

  * the session called a chat tool (chat_begin/chat_say/chat_await) during the
    current turn (i.e. since the user's last message), and
  * its last chat result doesn't show the chat as ended (or its last chat_say
    failed, so its turn probably never went out).

It identifies the session from its own transcript (the hook payload's
``transcript_path``), so two sessions in the same directory are never
confused. Insisting works: a stop attempt with no chat activity since the last
reminder is allowed, so a session that genuinely needs the human (or is told
to stop) can still end its turn — but one that keeps chatting and drops out
AGAIN is reminded again. Any error means "allow": the guard must never wedge a
session.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from . import config

CHAT_TOOLS = ("chat_begin", "chat_say", "chat_await")
TAIL_BYTES = 4 * 1024 * 1024  # the current turn is near the end of the transcript
STATE_TTL = 24 * 3600.0       # forget per-session reminder records after a day


def _state_path():
    return config.config_path().parent / "guard.json"


def _load_state() -> dict[str, Any]:
    try:
        data = config.read_json(_state_path(), {})
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict[str, Any]) -> bool:
    now = time.time()
    state = {k: v for k, v in state.items()
             if isinstance(v, dict) and now - float(v.get("ts", 0)) < STATE_TTL}
    try:
        config._atomic_write(_state_path(), json.dumps(state))
        return True
    except OSError:
        return False


def _chat_tool(name: str) -> Optional[str]:
    """``mcp__<server>__chat_say`` -> ``chat_say`` (any server name), else None."""
    if not name.startswith("mcp__"):
        return None
    short = name.rsplit("__", 1)[-1]
    return short if short in CHAT_TOOLS or short == "send_message" else None


def _sent_as_turn(content: Any) -> bool:
    """A send_message that went out as the session's owed chat turn (it says so)."""
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return isinstance(content, str) and "Sent as your chat turn to" in content


def _result_obj(content: Any) -> Optional[dict]:
    """Parse an MCP tool result (string or text blocks) into the tool's dict."""
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    try:
        obj = json.loads(content)
    except (TypeError, ValueError):
        return None
    if isinstance(obj, dict) and isinstance(obj.get("result"), dict) and len(obj) == 1:
        obj = obj["result"]
    return obj if isinstance(obj, dict) else None


# Lines Claude Code writes as ordinary user messages that the human didn't type:
# a background task finishing, a scheduled check-in, the summary that continues
# a compacted session. A session woken by one is still in the same chat, so
# they don't start a new turn.
_NOT_TYPED = ("<task-notification>", "This session is being continued from a previous")


def _is_real_user_message(entry: dict) -> bool:
    """A message the human typed (not a tool result, not a meta/system line,
    not a notification Claude Code delivers as a user message)."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    if entry.get("isCompactSummary"):
        return False
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return not content.lstrip().startswith(_NOT_TYPED)
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "text" for b in content)


def _read_tail(path: str) -> list[dict]:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - TAIL_BYTES))
        data = fh.read()
    entries = []
    for line in data.splitlines():
        try:
            e = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue  # a partial first line from the seek
        if isinstance(e, dict):
            entries.append(e)
    return entries


def _last_live(calls: list, results: dict) -> bool:
    """True if the latest chat call before these that returned a readable result
    showed the chat still going."""
    for call_id, _tool, _args in reversed(calls):
        res = _result_obj(results.get(call_id))
        if res is None:
            continue
        reply = res.get("reply") if isinstance(res.get("reply"), dict) else {}
        return not (res.get("ended") or reply.get("ended"))
    return False


def evaluate(payload: dict) -> Optional[str]:
    """Return a block reason if this stop would strand a live chat, else None."""
    path = payload.get("transcript_path")
    if not path or not os.path.exists(path):
        return None
    entries = _read_tail(path)

    # The current turn: everything after the human's last message.
    start = 0
    for i in range(len(entries) - 1, -1, -1):
        if _is_real_user_message(entries[i]):
            start = i + 1
            break

    calls: list[tuple[str, str, dict]] = []  # (tool_use_id, tool, input)
    results: dict[str, Any] = {}
    unreadable: set[Any] = set()  # results that came back but aren't errors
    for e in entries[start:]:
        if e.get("isSidechain"):
            continue  # a subagent's tools aren't this session's chat
        content = (e.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                tool = _chat_tool(str(b.get("name", "")))
                if tool:
                    calls.append((b.get("id"), tool, b.get("input") or {}))
            elif b.get("type") == "tool_result":
                results[b.get("tool_use_id")] = b.get("content")
                if _unreadable(b):
                    unreadable.add(b.get("tool_use_id"))

    # A send_message is a chat call only when it was posted as a chat turn.
    calls = [c for c in calls if c[1] != "send_message" or _sent_as_turn(results.get(c[0]))]
    if not calls:
        return None  # no chat activity this turn: not our business

    # Already reminded during this continuation? Only remind again if the
    # session kept chatting since; a bare repeat stop means "I mean it".
    # (Compared by the last chat call's id, not a count, so it doesn't matter
    # how the reminder itself shows up in the transcript. No record at all means
    # another Stop hook did the blocking - we haven't reminded yet.)
    key = str(payload.get("session_id") or path)
    # Every session's Stop hook shares guard.json: update it under the lock
    # (briefly - the guard must never hold a session up).
    with config.FileLock(_state_path(), timeout=2.0):
        return _decide(payload, key, calls, results, unreadable)


def _unreadable(block: dict) -> bool:
    """A result that isn't an error but can't be read as the tool's dict - e.g.
    a long one Claude Code cut short. Whether the chat is still going is then
    unknown, so it's not a reason to block."""
    if block.get("is_error") is True:
        return False
    if _result_obj(block.get("content")) is not None:
        return False
    if block.get("is_error") is False:
        return True
    content = block.get("content")
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return isinstance(content, str) and content.lstrip().startswith("{")


def _decide(payload: dict, key: str, calls: list, results: dict,
            unreadable: Optional[set] = None) -> Optional[str]:
    state = _load_state()
    call_id, tool, args = calls[-1]
    if payload.get("stop_hook_active") and state.get(key, {}).get("last") == call_id:
        state.pop(key, None)
        _save_state(state)
        return None

    res = _result_obj(results.get(call_id))
    if tool == "send_message":
        res = {"next": "Your send_message went out as your chat turn (don't send it again "
                       "with chat_say) - call chat_await to wait for the reply."}
    if tool == "chat_say" and args.get("status") in ("end", "impasse") and (
            res is not None or call_id not in results):
        return None  # ended it (or the call never returned): nothing to strand
    if res is None:
        if call_id not in results or call_id in (unreadable or ()):
            return None  # never returned, or can't be read: don't guess
        if tool == "chat_say":
            # A failed chat_say: usually nothing was posted, so if it was this
            # session's turn the others are still waiting on it.
            step = ("Your last chat_say returned an error. If the error says nothing "
                    "was posted, fix the problem and send it again; otherwise call "
                    "chat_await. If you can't continue, send chat_say(status='impasse') "
                    "so the others aren't left waiting.")
        elif tool == "chat_await" or _last_live(calls[:-1], results):
            # A wait (or a re-join) that failed once, mid-chat: the chat is
            # still going - a transient error is no reason to drop out.
            step = (f"Your last {tool} returned an error, but the chat is still "
                    "going. Call chat_await again to keep waiting. If it keeps "
                    "failing, send chat_say(status='impasse') so the others aren't "
                    "left waiting.")
        else:
            return None  # a failed chat_begin with nothing before it: don't guess
    else:
        reply = res.get("reply") if isinstance(res.get("reply"), dict) else {}
        if res.get("ended") or reply.get("ended"):
            return None
        step = res.get("next") or reply.get("next") or (
            "Call chat_await to keep waiting for the reply.")
    state[key] = {"last": call_id, "ts": time.time()}
    if not _save_state(state) and payload.get("stop_hook_active"):
        return None  # can't record the reminder: never risk blocking in a loop
    return ("You're in a live discordinator chat that hasn't ended, and ending your "
            "turn now would strand it (nothing can wake you when the reply arrives). "
            f"Next step: {step} "
            "If you genuinely need the human right now (e.g. to ask them something), "
            "end your turn again and it will be allowed.")


def main(stdin_text: str) -> str:
    """Hook entry: stdin JSON in, hook JSON (or nothing) out. Never raises."""
    try:
        payload = json.loads(stdin_text or "{}")
        reason = evaluate(payload if isinstance(payload, dict) else {})
    except Exception:  # noqa: BLE001 - a broken guard must never wedge a session
        return ""
    if not reason:
        return ""
    return json.dumps({"decision": "block", "reason": reason})
