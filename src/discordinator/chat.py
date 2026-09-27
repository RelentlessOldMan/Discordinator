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
from .discord_client import MAX_MESSAGE_LEN, DiscordClient, chunk_content, simplify_message

STATUSES = ("say", "over", "wrap", "end", "impasse")
TERMINAL_STATUSES = ("end", "impasse")
DEFAULT_TURN_CAP = 20

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


def await_turn(
    client: DiscordClient,
    channel_id: str,
    me: str,
    timeout: float = 50.0,
    poll: float = 3.0,
) -> dict[str, Any]:
    """Block until another participant completes a turn (a non-``say`` status),
    a human interjects, or ``timeout`` elapses. Advances this handle's cursor."""
    deadline = time.monotonic() + timeout
    collected: list[dict[str, Any]] = []

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
                # Not a chat message => a human (or non-chat bot post) interjection.
                text = m["content"]
                if is_human_stop(text):
                    return _result(me, channel_id, sender="human", status="stop",
                                   text=text, messages=[m], ended=True,
                                   stop_reason="human", your_turn=False)
                return _result(me, channel_id, sender="human", status="interjection",
                               text=text, messages=[m], ended=False,
                               stop_reason=None, your_turn=True)

            if parsed["participant"] == me:
                continue  # my own message

            collected.append({**m, "chat_status": parsed["status"], "body": parsed["body"]})
            if parsed["status"] == "say":
                continue  # mid-turn; keep accumulating

            st = parsed["status"]
            ended = st in TERMINAL_STATUSES
            stop_reason = "agreed" if st == "end" else ("impasse" if st == "impasse" else None)
            return _result(
                me, channel_id, sender=parsed["participant"], status=st,
                text="\n".join(x["body"] for x in collected),
                messages=collected, ended=ended, stop_reason=stop_reason,
                your_turn=not ended,
            )

        if time.monotonic() >= deadline:
            return _result(me, channel_id, sender=None, status=None, text="",
                           messages=collected, ended=False, stop_reason=None,
                           your_turn=False, timed_out=True)
        time.sleep(poll)


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
