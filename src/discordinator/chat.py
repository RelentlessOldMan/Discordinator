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

    [<handle>|<status>] <body>            # unaddressed (broadcast / 2-party)
    [<handle>><target>|<status>] <body>   # addressed to one peer (N-way)

status values:
    say      more of my turn is coming (do not yield)
    working  hold on - I'm doing some work; results will follow (do not yield)
    over     I'm done — your turn
    wrap     I think we can end — do you agree? (yields turn)
    end      ending now (terminal)
    impasse  we're stuck, stop and get the human (terminal)

Addressing & the floor (N-way). With three or more participants a bare status
("your turn") is ambiguous, so a turn may name a recipient: ``[A>B|over]`` yields
the floor to B. The "floor holder" — who may speak next — is derived from history
(the addressee of the last yielded turn), not stored, so it stays correct after a
crash or re-join. ``await_turn`` only wakes when a completed turn is addressed to
you, is a broadcast (unaddressed, or ``to`` in {all, everyone, *, any, anyone}),
or is terminal (which ends the chat for everyone). Two-party chats simply omit
``to`` and behave exactly as before.

Chat state lives under state.json ``["chat"][channel_id][handle_key]`` = {cursor, turns, cap},
separate from the relay cursor so the two never collide.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import config
from .discord_client import (
    MAX_FILES_PER_MESSAGE,
    MAX_MESSAGE_LEN,
    DiscordClient,
    DiscordError,
    chunk_content,
    simplify_message,
)

STATUSES = ("say", "working", "ask", "over", "wrap", "end", "impasse")
TERMINAL_STATUSES = ("end", "impasse")     # ends the chat for everyone
YIELD_STATUSES = ("over", "wrap")          # completes a turn, passes the floor
NON_TURN_STATUSES = ("say", "working", "ask")  # do NOT complete a turn / take the floor
# Keep the floor and tell the others where things stand: `say` = more coming
# now, `working` = "hold on, I'm off doing something; results will follow".
PROGRESS_STATUSES = ("say", "working")
DEFAULT_TURN_CAP = 20

# A participant who hasn't posted (or been addressed) for this long before the
# room's latest chat message has left the conversation: they drop out of the
# participant list so stale handles don't linger in the rotation.
STALE_AFTER = timedelta(minutes=30)

# While someone has said they're `working`, the waiting side's channel reminder
# is held back - but only this long, so a session that died mid-task still
# gets flagged for a human.
WORKING_GRACE = timedelta(minutes=60)

# Addressing: an unaddressed turn, or one whose target is one of these, wakes
# every waiting participant (open floor). Otherwise only the named peer wakes.
BROADCAST_ALIASES = frozenset({"all", "everyone", "*", "any", "anyone"})

# Prefix on system "waiting" reminders. chat_await skips any message starting
# with this so a nudge is never mistaken for a participant's turn.
NUDGE_MARK = "⏳"  # ⏳

# [from] or [from>to], then |status], then the body.
_HEADER_RE = re.compile(r"^\[([^|\]>]{1,32})(?:>([^|\]]{1,32}))?\|([a-z]+)\]\s?(.*)$", re.S)


def sanitize_handle(handle: str) -> str:
    h = re.sub(r"[|\]\[>]", "", str(handle)).strip()
    if not h:
        raise ValueError("participant handle is empty after removing []|> characters")
    return h[:32]


def header(handle: str, status: str, to: Optional[str] = None) -> str:
    if to:
        return f"[{handle}>{to}|{status}] "
    return f"[{handle}|{status}] "


def parse(content: str) -> Optional[dict[str, Optional[str]]]:
    """Return {participant, to, status, body} for a chat-formatted message, else
    None. ``to`` is None when the turn is unaddressed (broadcast / 2-party)."""
    m = _HEADER_RE.match(content or "")
    if not m:
        return None
    return {"participant": m.group(1), "to": m.group(2),
            "status": m.group(3), "body": m.group(4)}


def handle_key(handle: Optional[str]) -> str:
    """Identity key for a handle: case-insensitive, so "Convex" and "convex" are
    the same participant. Display keeps whatever spelling was first seen."""
    return str(handle or "").strip().casefold()


def same_handle(a: Optional[str], b: Optional[str]) -> bool:
    return a is not None and b is not None and handle_key(a) == handle_key(b)


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    try:
        t = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _is_broadcast(to: Optional[str]) -> bool:
    return to is None or str(to).strip().lower() in BROADCAST_ALIASES


def _targets(to: Optional[str], me: str) -> bool:
    """True if a turn addressed ``to`` should wake participant ``me`` — i.e. it's
    a broadcast, or names ``me`` (case-insensitive)."""
    if _is_broadcast(to):
        return True
    return same_handle(to, me)


def is_human_stop(text: str) -> bool:
    t = (text or "").strip().lower()
    if "[[stop]]" in t:
        return True
    if t in ("stop", "halt", "end", "end chat"):
        return True
    # A leading stop word must be a WHOLE word (followed by whitespace or
    # punctuation) — so "stop now" / "end chat please" / "halt!" end the chat,
    # but "endpoint", "ending", "endeavor", "stopgap" do NOT.
    return bool(re.match(r"(stop|halt|end chat|end)[\s.!,:;]", t))


# -- per-(channel, handle) chat state --------------------------------------


def _slot(state: dict[str, Any], channel_id: str, me: str) -> dict[str, Any]:
    return state.setdefault("chat", {}).setdefault(channel_id, {}).setdefault(handle_key(me), {})


def reset(channel_id: str, me: str, cursor: str, cap: int) -> None:
    state = config.load_state()
    state.setdefault("chat", {}).setdefault(channel_id, {})[handle_key(me)] = {
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


def send_chat(client: DiscordClient, channel_id: str, me: str, status: str, text: str,
              to: Optional[str] = None,
              files: Optional[list[Any]] = None) -> list[dict[str, Any]]:
    """Post a chat message, splitting long text so EVERY piece carries the header
    (earlier pieces as ``say`` continuations, the final piece with ``status``).
    ``to`` addresses the turn to one peer; every piece keeps the address so a
    multi-part turn stays targeted.

    ``files`` (optional) attaches one or more files to the turn. They ride the
    FINAL, status-bearing message (so a multi-part turn's attachments arrive with
    its conclusion, and the receiver sees them on the same turn). Capped at
    ``MAX_FILES_PER_MESSAGE`` per turn so every chat message keeps its header —
    use a relay ``send`` for larger batches. Attachments are orthogonal to the
    wire header, so floor/turn/addressing are unaffected."""
    if files and len(files) > MAX_FILES_PER_MESSAGE:
        raise ValueError(
            f"a chat turn can carry at most {MAX_FILES_PER_MESSAGE} files "
            f"(got {len(files)}); use a relay send for larger batches."
        )
    reserve = len(header(me, "impasse", to))  # longest status word, incl. address
    pieces = chunk_content(text, MAX_MESSAGE_LEN - reserve) or [""]
    sent: list[dict[str, Any]] = []
    for i, piece in enumerate(pieces):
        last = i == len(pieces) - 1
        st = status if last else "say"
        content = f"{header(me, st, to)}{piece}"
        if files and last:
            sent.extend(client.send_files(channel_id, content, list(files), label=None))
        else:
            sent.append(client.post(channel_id, content))
    return sent


def _lean(collected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip the duplicated body from message entries — the combined `text`
    field is the single source for the words; here we keep only metadata."""
    return [
        {"id": c["id"], "from": c["from"], "to": c.get("to"),
         "status": c["status"], "timestamp": c["timestamp"],
         "attachments": c.get("attachments", [])}
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
    blocked waiting — no dependence on the dormant side's behavior.

    Two flavors: if I'm owed the floor (I hold it / a turn is addressed to me and
    the other side has gone quiet), name whoever should respond. If instead I've
    raised a hand and am being passed over, name the current floor holder and ask
    them to yield to me — that's the anti-starvation signal."""
    try:
        st = compute_state(client, channel_id, me)
        floor = st.get("floor")
        participants = st.get("participants", [])
        others = [p for p in participants if not same_handle(p, me)]
        mins = max(1, int(waited_s // 60))
        requests = [r["from"] for r in st.get("floor_requests", [])]
        stalled_say = [x for x in st.get("progress", [])
                       if x["status"] == "say" and not same_handle(x["from"], me)]

        if any(same_handle(r, me) for r in requests) and floor and not same_handle(floor, me):
            # I raised a hand and I'm not the one holding the floor — starvation.
            msg = (f"{NUDGE_MARK} [{me}] raised a hand ~{mins}m ago and is waiting to "
                   f"speak. [{floor}] holds the floor — please yield to [{me}] "
                   f"(chat_say(status=\"over\", to=\"{me}\")) or a human can prompt them.")
        elif stalled_say:
            x = stalled_say[-1]
            msg = (f"{NUDGE_MARK} [{x['from']}] sent status 'say' (more coming) but never "
                   f"finished the turn; [{me}] has been waiting ~{mins}m. "
                   f"[{x['from']}]: send chat_say(..., status=\"over\") to hand over. "
                   f"A human watching can poke that session.")
        else:
            who = floor or (", ".join(others) if others else "the other side")
            msg = (f"{NUDGE_MARK} [{me}] has been waiting ~{mins}m for the next turn. "
                   f"{who}: it's your turn (chat_say/chat_await). "
                   f"A human watching can poke that session to resume.")
        client.post(channel_id, msg)
    except DiscordError:
        pass


def _floor_context(client: DiscordClient, channel_id: str, me: str) -> dict[str, Any]:
    """Multiparty fairness snapshot, attached to a return that hands ``me`` the
    floor so the model can rotate fairly without a separate chat_status call.
    Empty for a 2-party chat (nothing to arbitrate)."""
    st = compute_state(client, channel_id, me)
    if not st.get("multiparty"):
        return {}
    return {
        "multiparty": True,
        "floor": st.get("floor"),
        "pending_requests": st.get("floor_requests", []),
        "suggest_next": st.get("suggest_next"),
        "waiting": st.get("waiting", []),
    }


def await_turn(
    client: DiscordClient,
    channel_id: str,
    me: str,
    timeout: float = 120.0,
    poll: float = 3.0,
    nudge_after: float = 240.0,
    from_whom: Optional[str] = None,
) -> dict[str, Any]:
    """Block until a turn comes to ``me``, a human interjects, or ``timeout``
    elapses. Advances this handle's cursor.

    "Comes to me" means another participant completes a turn (``over``/``wrap``)
    that is addressed to me or broadcast, OR any participant ends the chat
    (``end``/``impasse``, which is terminal for everyone). A turn addressed to a
    different peer does NOT wake me — that's the floor token: I keep holding
    until the floor is mine. ``ask`` (a hand-raise) and ``say`` never wake me.
    ``from_whom`` optionally narrows waking to a yielded turn from that one peer.

    On timeout it returns ``timed_out=True`` (not an error): the other side is
    just still thinking. The caller should call ``await_turn`` AGAIN to keep
    waiting — never treat a timeout as "conversation abandoned".

    If the cumulative wait (across repeated calls) exceeds ``nudge_after``
    seconds, one visible reminder is posted to the channel so a watching human
    knows which dormant session to poke (and, if I raised a hand and am being
    passed over, that I'm being starved). Set ``nudge_after<=0`` to disable."""
    deadline = time.monotonic() + timeout

    # Already my turn? (e.g. the reply came back from a waiting chat_say and the
    # model called chat_await anyway.) Waiting now would block on myself, so
    # hand the owed turn straight back instead of a silent wait.
    # Anything newer than my cursor (a human, a plain reply) wins: let the
    # normal loop deliver it.
    cursor0 = get_cursor(channel_id, me)
    fresh = bool(cursor0) and any(
        _from_someone_else(simplify_message(m), me)
        for m in client.read_messages(channel_id, limit=50, after=cursor0))
    st0 = {} if fresh else compute_state(client, channel_id, me)
    # My own turn isn't finished (I sent `say`/`working` and never yielded)?
    # Then everyone is waiting on ME - waiting here too would deadlock the chat.
    mine = [x for x in st0.get("progress", []) if same_handle(x["from"], me)]
    if mine:
        x = mine[-1]
        result = _result(me, channel_id, sender=None, status=None, text="",
                         messages=[], ended=False, stop_reason=None, your_turn=True)
        result["unfinished_turn"] = True
        if x["status"] == "working":
            result["note"] = (
                f"You said you're working on something (\"{x['text'][:120]}\") and still "
                "hold the floor - nobody will reply until you finish. Do the work, then "
                "post the results with chat_say(..., status='over').")
        else:
            result["note"] = (
                f"Your turn isn't finished: your last message (\"{x['text'][:120]}\") was "
                "status='say', which means 'more coming' and keeps the floor - the others "
                "are waiting for YOU, so waiting here would deadlock the chat. Send "
                "chat_say(<message>, status='over') to hand over.")
        return _finish(channel_id, me, result)

    owed = st0.get("pending_turn")
    if (st0.get("your_turn") and owed and cursor0
            and int(owed["id"]) <= int(cursor0)
            and (from_whom is None or same_handle(owed["from"], from_whom))):
        result = _result(
            me, channel_id, sender=owed["from"], status=owed["status"],
            text=st0.get("_pending_text") or "", messages=[], ended=False,
            stop_reason=None, your_turn=True, addressed_to=owed.get("to"))
        result["already_received"] = True
        result["note"] = ("This turn was already delivered to you earlier - it's YOUR "
                          "turn now. Reply with chat_say; don't call chat_await until "
                          "you have.")
        return _finish(channel_id, me, result)

    # Per-sender say-continuation buffers so interleaved multiparty turns don't
    # bleed into each other's text.
    pending_by_sender: dict[str, list[dict[str, Any]]] = {}
    want = from_whom

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
                atts = m.get("attachments", [])
                if m.get("bot"):
                    # A participant replied OUT-OF-BAND via send_message (not
                    # chat_say). Surface it as a `plain` turn so a plain reply
                    # can never strand the awaiter (the stuck-chat footgun).
                    entry = [{"id": m["id"], "from": "participant", "to": None,
                              "status": "plain", "timestamp": m["timestamp"],
                              "attachments": atts}]
                    return _finish(channel_id, me, _result(
                        me, channel_id, sender="participant", status="plain",
                        text=text, messages=entry, ended=False,
                        stop_reason=None, your_turn=True, attachments=atts))
                # A real human (non-bot author) typed in the channel.
                human_entry = [{"id": m["id"], "from": "human", "to": None,
                                "status": None, "timestamp": m["timestamp"],
                                "attachments": atts}]
                if is_human_stop(text):
                    return _finish(channel_id, me, _result(
                        me, channel_id, sender="human", status="stop",
                        text=text, messages=human_entry, ended=True,
                        stop_reason="human", your_turn=False, attachments=atts))
                return _finish(channel_id, me, _result(
                    me, channel_id, sender="human", status="interjection",
                    text=text, messages=human_entry, ended=False,
                    stop_reason=None, your_turn=True, attachments=atts))

            sender = parsed["participant"]
            if same_handle(sender, me):
                continue  # my own message

            status = parsed["status"]
            to = parsed["to"]
            if status == "ask":
                continue  # a hand-raise — recorded in history, doesn't wake me

            buf = pending_by_sender.setdefault(sender, [])
            buf.append({
                "id": m["id"], "from": sender, "to": to, "status": status,
                "timestamp": m["timestamp"],
                "attachments": m.get("attachments", []),
                "body": parsed["body"],  # local only; stripped from output
            })
            if status in PROGRESS_STATUSES:
                continue  # mid-turn / working; keep accumulating for this sender

            pieces = pending_by_sender.pop(sender)
            text = "\n".join(x["body"] for x in pieces)
            atts = [a for x in pieces for a in x.get("attachments", [])]

            if status in TERMINAL_STATUSES:  # ends the chat for everyone
                stop_reason = "agreed" if status == "end" else "impasse"
                return _finish(channel_id, me, _result(
                    me, channel_id, sender=sender, status=status, text=text,
                    messages=_lean(pieces), ended=True, stop_reason=stop_reason,
                    your_turn=False, addressed_to=to, attachments=atts))

            # A yielded turn (over/wrap). Does the floor actually come to me?
            if want is not None and not same_handle(sender, want):
                continue  # waiting specifically for a different peer
            if not _targets(to, me):
                continue  # addressed to another peer — keep holding the wait

            result = _result(
                me, channel_id, sender=sender, status=status, text=text,
                messages=_lean(pieces), ended=False, stop_reason=None,
                your_turn=True, addressed_to=to, attachments=atts)
            result.update(_floor_context(client, channel_id, me))
            return _finish(channel_id, me, result)

        if time.monotonic() >= deadline:
            waited = time.time() - since
            st = compute_state(client, channel_id, me)
            working = _working_note(st, me)
            if (nudge_after > 0 and not nudged and waited >= nudge_after
                    and not _recently_working(st, me)):  # don't cry wolf mid-task
                _post_nudge(client, channel_id, me, waited)
                _set_wait(channel_id, me, since, True)
                nudged = True
            leftover = [e for buf in pending_by_sender.values() for e in buf]
            result = _result(me, channel_id, sender=None, status=None, text="",
                             messages=_lean(leftover), ended=False, stop_reason=None,
                             your_turn=False, timed_out=True)
            result["waited_seconds"] = round(waited)
            result["nudged"] = nudged
            result["progress"] = [x for x in st.get("progress", [])
                                  if not same_handle(x["from"], me)]
            result["note"] = (
                (working + " " if working else
                 "No complete turn yet - the other side is still thinking. ")
                + "Call chat_await again now to keep waiting; waiting a long time is "
                "fine. Do NOT end your turn or ask the human - if you stop, nothing "
                "can wake you when the reply arrives."
                + (" (A channel reminder was posted so a human can poke the other "
                   "session.)" if nudged else ""))
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
    pending_turn (the last over/wrap turn, owed to its addressee), floor (who may
    speak next), floor_requests (outstanding hand-raises, oldest first), waiting
    (participants ranked most-starved first), suggest_next (the fair next
    addressee in a multiparty room), and — if `me` is given — your_turn.
    last_turn/pending_turn carry `to` (the addressee, or None). `_pending_predecessor`
    is an internal cursor hint.
    """
    raw = client.read_messages(channel_id, limit=max(1, min(scan, 100)))
    msgs = list(reversed([simplify_message(m) for m in raw]))  # chronological
    parsed_list = [parse(m["content"]) for m in msgs]

    # Scope to the CURRENT chat. A terminal turn (end/impasse) or a human stop
    # that is followed by more chat traffic closed the previous chat, so its
    # handles and turns must not leak into this one. (A terminal that is still
    # the latest chat message stays in scope, so `ended` can report it.)
    start = 0
    chat_after = False
    for i in range(len(msgs) - 1, -1, -1):
        p = parsed_list[i]
        closes = (p is not None and p["status"] in TERMINAL_STATUSES) or (
            p is None and not msgs[i].get("bot") and is_human_stop(msgs[i]["content"]))
        if closes and chat_after:
            start = i + 1
            break
        if p is not None:
            chat_after = True

    # Handles are case-insensitive identities; each is shown with the spelling
    # first seen, so "Convex" and "convex" are one participant.
    display: dict[str, str] = {}

    def canon(h: str) -> str:
        return display.setdefault(handle_key(h), h)

    real = YIELD_STATUSES + TERMINAL_STATUSES  # statuses that complete a turn
    participants: list[str] = []
    last_seen: dict[str, Optional[datetime]] = {}  # participant -> last post/address time
    latest_ts: Optional[datetime] = None
    last_turn: Optional[dict[str, Any]] = None
    last_turn_i = -1
    last_turn_index_by: dict[str, int] = {}   # participant -> index of their last completed turn
    last_significant: dict[str, dict[str, Any]] = {}  # ignoring pure `say`
    turn_authors: list[str] = []  # author of each completed turn, in order
    progress: dict[str, dict[str, Any]] = {}  # who -> latest say/working since their last turn
    last_text = ""
    for i in range(start, len(msgs)):
        m, p = msgs[i], parsed_list[i]
        if not p:
            continue
        ts = _parse_ts(m["timestamp"])
        if ts is not None:
            latest_ts = ts if latest_ts is None else max(latest_ts, ts)
        who = canon(p["participant"])
        to = p["to"] if _is_broadcast(p["to"]) else canon(p["to"])
        names = [who] + ([to] if to and not _is_broadcast(to) else [])
        # An addressed peer is a known participant even before it has posted.
        for n in names:
            if n not in participants:
                participants.append(n)
            last_seen[n] = ts
        if p["status"] in PROGRESS_STATUSES:
            progress[who] = {"from": who, "status": p["status"],
                             "text": (p["body"] or "")[:300], "ts": m["timestamp"]}
            continue
        progress.pop(who, None)
        last_significant[who] = {"status": p["status"], "i": i}
        if p["status"] in real:
            turn_authors.append(who)
            last_turn_index_by[who] = i
            last_turn = {"from": who, "to": to, "status": p["status"],
                         "id": m["id"], "ts": m["timestamp"]}
            last_turn_i = i
            last_text = p["body"] or ""

    # The querying caller is a participant too (it may not have posted yet).
    me_norm = canon(sanitize_handle(me)) if me is not None else None
    if me_norm and me_norm not in participants:
        participants.append(me_norm)

    ended = bool(last_turn and last_turn["status"] in TERMINAL_STATUSES)

    # The last yielded turn is owed until someone completes a turn after it —
    # and since last_turn IS the most recent completed turn, a yield there is
    # by definition unanswered.
    pending = None
    pending_predecessor = None
    if last_turn and last_turn["status"] in YIELD_STATUSES:
        pending = last_turn
        pending_predecessor = (
            msgs[last_turn_i - 1]["id"] if last_turn_i > 0
            else str(int(last_turn["id"]) - 1)
        )

    # Drop participants who have gone quiet: not seen within STALE_AFTER of the
    # room's latest chat message. Always kept: the caller, both ends of the owed
    # turn, and — when that turn is unaddressed — the speaker it replied to: in
    # a 2-party chat it's owed to them, however long the reply took.
    keep = {me_norm}
    if pending:
        keep |= {pending["from"], pending["to"]}
        if _is_broadcast(pending["to"]):
            replied_to = next((w for w in reversed(turn_authors[:-1])
                               if w != pending["from"]), None)
            if replied_to:
                keep.add(replied_to)
    if latest_ts is not None:
        horizon = latest_ts - STALE_AFTER
        participants = [
            p for p in participants
            if p in keep or last_seen.get(p) is None or last_seen[p] >= horizon
        ]
    multiparty = len(participants) > 2

    # Floor: who may speak next. An addressed pending names its target; an
    # unaddressed pending in a 2-party chat implies the other party; unaddressed
    # in a multiparty room is an open floor (no single holder).
    floor: Optional[str] = None
    if pending:
        if pending["to"] and not _is_broadcast(pending["to"]):
            floor = pending["to"]
        elif not multiparty:
            others = [p for p in participants if p != pending["from"]]
            floor = others[0] if others else None

    # Outstanding hand-raises: a participant whose most recent non-`say` message
    # is an `ask` (and who doesn't already hold the floor) is waiting to speak.
    floor_requests = [
        {"from": p, "i": sig["i"]}
        for p, sig in last_significant.items()
        if sig["status"] == "ask" and p != floor and p in participants
    ]
    floor_requests.sort(key=lambda r: r["i"])
    floor_requests = [{"from": r["from"]} for r in floor_requests]

    # Waiting, most-starved first: everyone but the floor holder, ranked by how
    # long since they last completed a turn (never-spoke sorts first).
    waiting = sorted(
        [p for p in participants if p != floor],
        key=lambda p: last_turn_index_by.get(p, -1),
    )

    # The fair next addressee (multiparty only): an outstanding request wins,
    # else the most-starved participant who isn't the one who just spoke.
    suggest_next: Optional[str] = None
    if multiparty:
        last_speaker = (pending or last_turn or {}).get("from")
        if floor_requests:
            suggest_next = floor_requests[0]["from"]
        else:
            cands = [p for p in waiting if p != last_speaker]
            suggest_next = cands[0] if cands else None

    state: dict[str, Any] = {
        "session_active": bool(last_turn) and not ended,
        "ended": ended,
        "participants": participants,
        "multiparty": multiparty,
        "last_turn": last_turn,
        "pending_turn": pending,
        "floor": floor,
        "floor_requests": floor_requests,
        "waiting": waiting,
        "suggest_next": suggest_next,
        "progress": [v for k, v in progress.items() if k in participants],
        "_pending_predecessor": pending_predecessor,
        "_pending_text": last_text if pending else None,
    }
    if me_norm is not None:
        state["your_turn"] = bool(
            pending and not same_handle(pending["from"], me_norm)
            and _targets(pending.get("to"), me_norm)
        )
    return state


def _recently_working(st: dict[str, Any], me: str) -> bool:
    """True if another participant said `working` within WORKING_GRACE."""
    now = datetime.now(timezone.utc)
    for x in st.get("progress", []):
        if x["status"] != "working" or same_handle(x["from"], me):
            continue
        ts = _parse_ts(x.get("ts"))
        if ts is None or now - ts < WORKING_GRACE:
            return True
    return False


def _from_someone_else(m: dict[str, Any], me: str) -> bool:
    """A message worth delivering to ``me``: not my own chat message and not a
    system waiting-reminder."""
    if (m.get("content") or "").lstrip().startswith(NUDGE_MARK):
        return False
    p = parse(m.get("content") or "")
    return not (p and same_handle(p["participant"], me))


def _working_note(st: dict[str, Any], me: str) -> Optional[str]:
    """A human-readable line if someone else is mid-turn (said `working`/`say`
    and hasn't yielded yet), e.g. while they go off to do a 20-minute task."""
    others = [x for x in st.get("progress", []) if not same_handle(x["from"], me)]
    working = [x for x in others if x["status"] == "working"] or others
    if not working:
        return None
    x = working[-1]
    age = ""
    ts = _parse_ts(x.get("ts"))
    if ts is not None:
        mins = int((datetime.now(timezone.utc) - ts).total_seconds() // 60)
        age = f" ({mins}m ago)" if mins >= 1 else " (just now)"
    verb = "is working on something" if x["status"] == "working" else "is mid-turn"
    return f"{x['from']} {verb}{age}: \"{x['text']}\" - they still hold the floor."


def next_step(result: dict[str, Any]) -> str:
    """One imperative line telling the model exactly what to do next, so a chat
    never stalls because a session ended its turn at the wrong moment."""
    if result.get("ended"):
        return "The chat has ended. You may stop."
    if result.get("unfinished_turn"):
        return ("Finish YOUR turn: chat_say(<message>, status='over'). The others are "
                "waiting on you; don't call chat_await until you have.")
    if result.get("from") == "human":
        return ("A human spoke in the chat. Do what they ask; if the chat should "
                "continue, reply with chat_say.")
    if result.get("your_turn"):
        return ("It's YOUR turn. Reply with chat_say (status='over' to hand back, "
                "'working' if you need time to go do something first, 'wrap' to "
                "propose ending). Don't end your turn without replying.")
    if result.get("timed_out"):
        return ("Call chat_await again NOW. Keep waiting as long as it takes - do NOT "
                "end your turn, or nobody can wake you when the reply arrives.")
    return "Call chat_await to wait for the next turn."


def _result(me: str, channel_id: str, *, sender, status, text, messages,
            ended: bool, stop_reason, your_turn: bool, timed_out: bool = False,
            addressed_to: Optional[str] = None,
            attachments: Optional[list[Any]] = None) -> dict[str, Any]:
    turns, cap = get_meta(channel_id, me)
    return {
        "from": sender,
        "to": addressed_to,
        "status": status,
        "text": text,
        "messages": messages,
        "attachments": attachments or [],
        "your_turn": your_turn,
        "ended": ended,
        "stop_reason": stop_reason,
        "timed_out": timed_out,
        "my_turns": turns,
        "turn_cap": cap,
        "cap_reached": turns >= cap,
    }
