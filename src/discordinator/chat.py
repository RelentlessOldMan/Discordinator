"""Turn-based chat protocol for two agents talking over a Discord channel.

Layered on top of the plain relay. Design goals (from real ad-hoc use):

1. Identity — chat messages are tagged by a participant HANDLE passed per call
   (``me``), not the machine label, so two agents on the SAME machine are
   distinguishable. Each handle gets its own read cursor.
2. Waiting — ``await_turn`` blocks server-side, polling until the other side
   finishes a turn, a human interjects, or it times out. No ad-hoc polling.
3. Protocol — every message carries an explicit ``status`` that is visible in
   the chat text, to the tool, and to the model.

Wire format of a chat message::

    [<handle>|<status>] <body>

status values:
    say      more of my turn is coming (do not yield)
    over     I'm done — your turn
    wrap     I think we can end — do you agree? (yields turn)
    end      ending now (terminal)
    impasse  we're stuck, stop and get the human (terminal)

Chat state lives under state.json ``["chat"][channel_id][handle]`` = {cursor, turns, cap},
separate from the relay cursor so the two never collide.
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from . import config
from .discord_client import (
    MAX_MESSAGE_LEN,
    DiscordClient,
    DiscordError,
    chunk_content,
    simplify_message,
)

STATUSES = ("say", "over", "wrap", "end", "impasse")
TERMINAL_STATUSES = ("end", "impasse")
DEFAULT_TURN_CAP = 20

# Prefix on system "waiting" reminders. chat_await skips any message starting
# with this so a nudge is never mistaken for a participant's turn.
NUDGE_MARK = "⏳"  # ⏳

_HEADER_RE = re.compile(r"^\[([^|\]]{1,32})\|([a-z]+)\]\s?(.*)$", re.S)


def sanitize_handle(handle: str) -> str:
    h = re.sub(r"[|\]\[]", "", str(handle)).strip()
    if not h:
        raise ValueError("participant handle is empty after removing []| characters")
    return h[:32]


def header(handle: str, status: str) -> str:
    return f"[{handle}|{status}] "


def parse(content: str) -> Optional[dict[str, str]]:
    """Return {participant, status, body} for a chat-formatted message, else None."""
    m = _HEADER_RE.match(content or "")
    if not m:
        return None
    return {"participant": m.group(1), "status": m.group(2), "body": m.group(3)}


def is_human_stop(text: str) -> bool:
    t = (text or "").strip().lower()
    return t.startswith(("stop", "halt", "end chat", "end")) or "[[stop]]" in t


# -- per-(channel, handle) chat state --------------------------------------


def _slot(state: dict[str, Any], channel_id: str, me: str) -> dict[str, Any]:
    return state.setdefault("chat", {}).setdefault(channel_id, {}).setdefault(me, {})


def reset(channel_id: str, me: str, cursor: str, cap: int) -> None:
    state = config.load_state()
    state.setdefault("chat", {}).setdefault(channel_id, {})[me] = {
        "cursor": cursor,
        "turns": 0,
        "cap": cap,
    }
    config.save_state(state)


def get_cursor(channel_id: str, me: str) -> Optional[str]:
    return _slot(config.load_state(), channel_id, me).get("cursor")


def set_cursor(channel_id: str, me: str, message_id: str) -> None:
    state = config.load_state()
    _slot(state, channel_id, me)["cursor"] = str(message_id)
    config.save_state(state)


def get_meta(channel_id: str, me: str) -> tuple[int, int]:
    slot = _slot(config.load_state(), channel_id, me)
    return int(slot.get("turns", 0)), int(slot.get("cap", DEFAULT_TURN_CAP))


def bump_turn(channel_id: str, me: str) -> int:
    state = config.load_state()
    slot = _slot(state, channel_id, me)
    slot["turns"] = int(slot.get("turns", 0)) + 1
    config.save_state(state)
    return slot["turns"]


# -- send / await ----------------------------------------------------------


def send_chat(client: DiscordClient, channel_id: str, me: str, status: str, text: str) -> list[dict[str, Any]]:
    """Post a chat message, splitting long text so EVERY piece carries the header
    (earlier pieces as ``say`` continuations, the final piece with ``status``)."""
    reserve = len(header(me, "impasse"))  # reserve for the longest status word
    pieces = chunk_content(text, MAX_MESSAGE_LEN - reserve) or [""]
    sent: list[dict[str, Any]] = []
    for i, piece in enumerate(pieces):
        st = status if i == len(pieces) - 1 else "say"
        sent.append(client.post(channel_id, f"{header(me, st)}{piece}"))
    return sent


def _lean(collected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip the duplicated body from message entries — the combined `text`
    field is the single source for the words; here we keep only metadata."""
    return [
        {"id": c["id"], "from": c["from"], "status": c["status"], "timestamp": c["timestamp"]}
        for c in collected
    ]


def _get_wait(channel_id: str, me: str) -> tuple[Optional[float], bool]:
    slot = _slot(config.load_state(), channel_id, me)
    return slot.get("waiting_since"), bool(slot.get("nudged", False))


def _set_wait(channel_id: str, me: str, since: float, nudged: bool) -> None:
    state = config.load_state()
    slot = _slot(state, channel_id, me)
    slot["waiting_since"] = since
    slot["nudged"] = nudged
    config.save_state(state)


def _clear_wait(channel_id: str, me: str) -> None:
    state = config.load_state()
    slot = _slot(state, channel_id, me)
    slot.pop("waiting_since", None)
    slot.pop("nudged", None)
    config.save_state(state)


def _finish(channel_id: str, me: str, result: dict[str, Any]) -> dict[str, Any]:
    """Clear the waiting flag on any real (non-timeout) return."""
    _clear_wait(channel_id, me)
    return result


def _post_nudge(client: DiscordClient, channel_id: str, me: str, waited_s: float) -> None:
    """Post one human-visible line so whoever is watching knows a session is
    blocked waiting — no dependence on the dormant side's behavior."""
    try:
        others = [p for p in compute_state(client, channel_id).get("participants", []) if p != me]
        who = ", ".join(others) if others else "the other side"
        mins = max(1, int(waited_s // 60))
        client.post(
            channel_id,
            f"{NUDGE_MARK} [{me}] has been waiting ~{mins}m for the next turn. "
            f"{who}: it's your turn (chat_say/chat_await). "
            f"A human watching can poke that session to resume.",
        )
    except DiscordError:
        pass


def await_turn(
    client: DiscordClient,
    channel_id: str,
    me: str,
    timeout: float = 120.0,
    poll: float = 3.0,
    nudge_after: float = 240.0,
) -> dict[str, Any]:
    """Block until another participant completes a turn (a non-``say`` status),
    a human interjects, or ``timeout`` elapses. Advances this handle's cursor.

    On timeout it returns ``timed_out=True`` (not an error): the other side is
    just still thinking. The caller should call ``await_turn`` AGAIN to keep
    waiting — never treat a timeout as "conversation abandoned".

    If the cumulative wait (across repeated calls) exceeds ``nudge_after``
    seconds, one visible reminder is posted to the channel so a watching human
    knows which dormant session to poke. Set ``nudge_after<=0`` to disable."""
    deadline = time.monotonic() + timeout
    collected: list[dict[str, Any]] = []

    # Track cumulative wait across repeated calls (for the nudge).
    since, nudged = _get_wait(channel_id, me)
    if since is None:
        since = time.time()
        _set_wait(channel_id, me, since, False)
        nudged = False

    while True:
        cursor = get_cursor(channel_id, me)
        if cursor:
            raw = client.read_messages(channel_id, limit=100, after=cursor)
        else:
            raw = client.read_messages(channel_id, limit=20)
        messages = list(reversed([simplify_message(m) for m in raw]))

        for m in messages:
            set_cursor(channel_id, me, m["id"])
            parsed = parse(m["content"])

            if parsed is None:
                text = m["content"]
                if text.lstrip().startswith(NUDGE_MARK):
                    continue  # system "waiting" reminder — not a turn
                if m.get("bot"):
                    # A participant replied OUT-OF-BAND via send_message (not
                    # chat_say). Surface it as a `plain` turn so a plain reply
                    # can never strand the awaiter (the stuck-chat footgun).
                    entry = [{"id": m["id"], "from": "participant",
                              "status": "plain", "timestamp": m["timestamp"]}]
                    return _finish(channel_id, me, _result(
                        me, channel_id, sender="participant", status="plain",
                        text=text, messages=entry, ended=False,
                        stop_reason=None, your_turn=True))
                # A real human (non-bot author) typed in the channel.
                human_entry = [{"id": m["id"], "from": "human",
                                "status": None, "timestamp": m["timestamp"]}]
                if is_human_stop(text):
                    return _finish(channel_id, me, _result(
                        me, channel_id, sender="human", status="stop",
                        text=text, messages=human_entry, ended=True,
                        stop_reason="human", your_turn=False))
                return _finish(channel_id, me, _result(
                    me, channel_id, sender="human", status="interjection",
                    text=text, messages=human_entry, ended=False,
                    stop_reason=None, your_turn=True))

            if parsed["participant"] == me:
                continue  # my own message

            collected.append({
                "id": m["id"],
                "from": parsed["participant"],
                "status": parsed["status"],
                "timestamp": m["timestamp"],
                "body": parsed["body"],  # kept locally to build `text`; stripped from output
            })
            if parsed["status"] == "say":
                continue  # mid-turn; keep accumulating

            st = parsed["status"]
            ended = st in TERMINAL_STATUSES
            stop_reason = "agreed" if st == "end" else ("impasse" if st == "impasse" else None)
            return _finish(channel_id, me, _result(
                me, channel_id, sender=parsed["participant"], status=st,
                text="\n".join(x["body"] for x in collected),
                messages=_lean(collected), ended=ended, stop_reason=stop_reason,
                your_turn=not ended,
            ))

        if time.monotonic() >= deadline:
            waited = time.time() - since
            if nudge_after > 0 and not nudged and waited >= nudge_after:
                _post_nudge(client, channel_id, me, waited)
                _set_wait(channel_id, me, since, True)
                nudged = True
            result = _result(me, channel_id, sender=None, status=None, text="",
                             messages=_lean(collected), ended=False, stop_reason=None,
                             your_turn=False, timed_out=True)
            result["waited_seconds"] = round(waited)
            result["nudged"] = nudged
            result["note"] = ("No complete turn yet — the other side is still "
                              "thinking. Call chat_await again to keep waiting; do "
                              "NOT abandon the chat or ask the human." +
                              (" (A channel reminder was posted so a human can poke"
                               " the other session.)" if nudged else ""))
            return result
        time.sleep(poll)


def compute_state(
    client: DiscordClient, channel_id: str, me: Optional[str] = None, scan: int = 100
) -> dict[str, Any]:
    """Derive the current chat state purely from recent channel history — no
    reliance on shared mutable turn state. Used by chat_status and chat_begin so
    an idle/re-joining session can tell whether a turn is owed to it (the fix for
    the silent stuck-chat deadlock).

    Returns: session_active, ended, participants, multiparty, last_turn,
    pending_turn (a completed over/wrap turn with no reply after it), and — if
    `me` is given — your_turn. `_pending_predecessor` is an internal cursor hint.
    """
    raw = client.read_messages(channel_id, limit=max(1, min(scan, 100)))
    msgs = list(reversed([simplify_message(m) for m in raw]))  # chronological
    parsed_list = [parse(m["content"]) for m in msgs]

    participants: list[str] = []
    last_turn: Optional[dict[str, Any]] = None
    last_turn_i = -1
    for i, (m, p) in enumerate(zip(msgs, parsed_list)):
        if not p:
            continue
        if p["participant"] not in participants:
            participants.append(p["participant"])
        if p["status"] != "say":
            last_turn = {"from": p["participant"], "status": p["status"],
                         "id": m["id"], "ts": m["timestamp"]}
            last_turn_i = i

    ended = bool(last_turn and last_turn["status"] in TERMINAL_STATUSES)
    pending = None
    pending_predecessor = None
    if last_turn and last_turn["status"] in ("over", "wrap"):
        replied = any(
            parsed_list[j] and parsed_list[j]["participant"] != last_turn["from"]
            for j in range(last_turn_i + 1, len(msgs))
        )
        if not replied:
            pending = last_turn
            pending_predecessor = (
                msgs[last_turn_i - 1]["id"] if last_turn_i > 0
                else str(int(last_turn["id"]) - 1)
            )

    state: dict[str, Any] = {
        "session_active": bool(last_turn) and not ended,
        "ended": ended,
        "participants": participants,
        "multiparty": len(participants) > 2,
        "last_turn": last_turn,
        "pending_turn": pending,
        "_pending_predecessor": pending_predecessor,
    }
    if me is not None:
        me = sanitize_handle(me)
        state["your_turn"] = bool(pending and pending["from"] != me)
    return state


def _result(me: str, channel_id: str, *, sender, status, text, messages,
            ended: bool, stop_reason, your_turn: bool, timed_out: bool = False) -> dict[str, Any]:
    turns, cap = get_meta(channel_id, me)
    return {
        "from": sender,
        "status": status,
        "text": text,
        "messages": messages,
        "your_turn": your_turn,
        "ended": ended,
        "stop_reason": stop_reason,
        "timed_out": timed_out,
        "my_turns": turns,
        "turn_cap": cap,
        "cap_reached": turns >= cap,
    }
