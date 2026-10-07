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
or is terminal (which ends the chat for everyone in it). Two-party chats may
omit ``to``. Several conversations can share one room: each sees only its own
turns (see _conversation).

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
    simplify_message,
)

STATUSES = ("say", "working", "ask", "over", "wrap", "end", "impasse")
TERMINAL_STATUSES = ("end", "impasse")     # ends the chat for everyone in it
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


# Ends a piece that was cut mid-line (no newline to split on): the reader joins
# it to the next piece with nothing in between instead of a newline. Invisible
# in Discord, so a reader that doesn't know it just sees the old behavior.
GLUE = "\u2060"  # WORD JOINER


def split_turn(text: str, limit: int) -> list[str]:
    """Split ``text`` into pieces of at most ``limit`` chars that join_pieces
    puts back together exactly: cut at the last newline that fits (consuming
    just that one newline), else mid-line with a trailing GLUE. No piece ends
    in whitespace (Discord trims it off a message): such a piece is cut
    mid-line instead, and a last piece ending in whitespace gets a GLUE."""
    pieces: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit + 1)
        # A piece that happens to end in GLUE itself would read as glued, so
        # such a line is cut mid-line instead (always unambiguous).
        if cut > 0 and not rest[:cut].endswith(GLUE) and not rest[cut - 1].isspace():
            pieces.append(rest[:cut])
            rest = rest[cut + 1:]
        else:
            pieces.append(rest[:limit - 1] + GLUE)
            rest = rest[limit - 1:]
    if rest[-1:].isspace() or rest.endswith(GLUE):
        if len(rest) + 1 > limit:  # no room for the marker: one more cut
            pieces.append(rest[:limit - 1] + GLUE)
            rest = rest[limit - 1:]
        rest += GLUE
    pieces.append(rest)
    return pieces


def join_pieces(bodies: list[str]) -> str:
    """Rejoin a turn's pieces: newline between them, except after a GLUE piece.
    A GLUE ending the last piece only protects trailing whitespace, so it's
    dropped too."""
    out = []
    for i, b in enumerate(bodies):
        if b.endswith(GLUE):
            out.append(b[:-1])
        elif i == len(bodies) - 1:
            out.append(b)
        else:
            out.append(b + "\n")
    return "".join(out)


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


_STOP_RE = re.compile(
    r"(please\s+)?(stop|halt|end)(\s+((the|this)\s+)?chat)?(\s+(now|please|here))*[\s.!]*")


def is_human_stop(text: str) -> bool:
    """A human's stop command: ``[[STOP]]`` anywhere, or a message that is ONLY
    a stop word ("stop", "halt!", "end chat now", "please stop"). A remark that
    merely starts with one ("Stop arguing and look at the test", "End users will
    see this") is an ordinary remark, not a halt."""
    t = (text or "").strip().lower()
    return "[[stop]]" in t or bool(_STOP_RE.fullmatch(t))


# -- per-(channel, handle) chat state --------------------------------------


def _slot(state: dict[str, Any], channel_id: str, me: str) -> dict[str, Any]:
    return state.setdefault("chat", {}).setdefault(channel_id, {}).setdefault(handle_key(me), {})


def reset(channel_id: str, me: str, cursor: str, cap: int) -> None:
    with config.update_state() as state:
        state.setdefault("chat", {}).setdefault(channel_id, {})[handle_key(me)] = {
            "cursor": cursor,
            "turns": 0,
            "cap": cap,
        }


def get_cursor(channel_id: str, me: str) -> Optional[str]:
    return _slot(config.load_state(), channel_id, me).get("cursor")


def set_cursor(channel_id: str, me: str, message_id: str) -> None:
    with config.update_state() as state:
        _slot(state, channel_id, me)["cursor"] = str(message_id)


def get_meta(channel_id: str, me: str) -> tuple[int, int]:
    slot = _slot(config.load_state(), channel_id, me)
    return int(slot.get("turns", 0)), int(slot.get("cap", DEFAULT_TURN_CAP))


def bump_turn(channel_id: str, me: str) -> int:
    with config.update_state() as state:
        slot = _slot(state, channel_id, me)
        slot["turns"] = int(slot.get("turns", 0)) + 1
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
    pieces = split_turn(text, MAX_MESSAGE_LEN - reserve)
    sent: list[dict[str, Any]] = []
    for i, piece in enumerate(pieces):
        last = i == len(pieces) - 1
        st = status if last else "say"
        content = f"{header(me, st, to)}{piece}"
        try:
            if files and last:
                sent.extend(client.send_files(channel_id, content, list(files), label=None))
            else:
                sent.append(client.post(channel_id, content))
        except Exception as e:
            # Tell the caller how far it got, so a retry doesn't repeat pieces.
            e.chat_pieces_sent, e.chat_pieces_total = i, len(pieces)  # type: ignore[attr-defined]
            e.chat_rest = join_pieces(pieces[i:])  # type: ignore[attr-defined]
            raise
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
    with config.update_state() as state:
        slot = _slot(state, channel_id, me)
        slot["waiting_since"] = since
        slot["nudged"] = nudged


def _clear_wait(channel_id: str, me: str) -> None:
    with config.update_state() as state:
        slot = _slot(state, channel_id, me)
        slot.pop("waiting_since", None)
        slot.pop("nudged", None)


def get_reply_to(channel_id: str, me: str) -> Optional[str]:
    """Who last handed me the turn - where an unaddressed reply of mine goes."""
    return _slot(config.load_state(), channel_id, me).get("reply_to")


def set_reply_to(channel_id: str, me: str, who: str) -> None:
    if handle_key(get_reply_to(channel_id, me)) == handle_key(who):
        return
    with config.update_state() as state:
        _slot(state, channel_id, me)["reply_to"] = who


def seed_cursor(client: DiscordClient, channel_id: str, me: str) -> str:
    """Where a session joining now starts reading: just before a turn owed to it
    (so it's delivered whole), else before a long turn someone is partway
    through, else the latest message."""
    st = compute_state(client, channel_id, me)
    if st.get("_owed_predecessor"):
        return st["_owed_predecessor"]
    if st.get("_open_run_predecessor"):
        return st["_open_run_predecessor"]
    latest = client.read_messages(channel_id, limit=1)
    return latest[0]["id"] if latest else "0"


def unread_for_me(client: DiscordClient, channel_id: str, me: str) -> list[dict[str, Any]]:
    """Messages after my cursor that I'd be woken for and haven't read: a human
    remark, the end of my chat, or a yielded turn for me. (Not my own posts, a
    plain bot message, a reminder, someone's say/working/ask, or another
    conversation's turns.)"""
    cursor = get_cursor(channel_id, me)
    if not cursor:
        return []
    out = []
    for raw in client.read_messages(channel_id, limit=100, after=cursor):
        m = simplify_message(raw)
        if parse(m["content"]) is None and m.get("bot"):
            continue
        if _wakes(client, channel_id, m, me):
            out.append(m)
    return list(reversed(out))


def note_room(channel_id: str) -> None:
    """Remember the room this machine's chat sessions are using, so the human's
    `discordinator stop`/`interject`/`watch` find it without being told (the
    room is usually set in a project's .mcp.json, which the shell never sees)."""
    try:
        if config.load_state().get("last_chat_room") == channel_id:
            return
        with config.update_state() as state:
            state["last_chat_room"] = channel_id
    except OSError:
        pass  # only a convenience for the human's commands


def last_room() -> Optional[str]:
    room = config.load_state().get("last_chat_room")
    return str(room) if room else None


def _load_partial(channel_id: str, me: str) -> dict[str, list[dict[str, Any]]]:
    """Pieces of other senders' unfinished turns, saved by my last await."""
    part = _slot(config.load_state(), channel_id, me).get("partial")
    return {k: list(v) for k, v in part.items()} if isinstance(part, dict) else {}


def _save_partial(channel_id: str, me: str, partial: dict[str, list[dict[str, Any]]]) -> None:
    kept = {k: v for k, v in partial.items() if v}
    if not kept and "partial" not in _slot(config.load_state(), channel_id, me):
        return
    with config.update_state() as state:
        slot = _slot(state, channel_id, me)
        if kept:
            slot["partial"] = kept
        else:
            slot.pop("partial", None)


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
    that is addressed to me or broadcast in my conversation, OR someone in it
    ends the chat (``end``/``impasse``). Another conversation in the same room
    never wakes me. A turn addressed to a
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
    if not cursor0:
        # Never joined (no chat_begin): start where chat_begin would - at a
        # turn owed to me, a long turn in progress, or now - never at whatever
        # old turns happen to be in the room.
        cursor0 = seed_cursor(client, channel_id, me)
        set_cursor(channel_id, me, cursor0)
    fresh = any(
        _wakes(client, channel_id, simplify_message(m), me, from_whom)
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

    owed = st0.get("_owed_turn")
    if (owed and int(owed["id"]) <= int(cursor0)
            and (from_whom is None or same_handle(owed["from"], from_whom))):
        result = _result(
            me, channel_id, sender=owed["from"], status=owed["status"],
            text=st0.get("_owed_text") or "", messages=[], ended=False,
            stop_reason=None, your_turn=True, addressed_to=owed.get("to"))
        result["already_received"] = True
        result["note"] = ("This turn was already delivered to you earlier - it's YOUR "
                          "turn now. Reply with chat_say; don't call chat_await until "
                          "you have.")
        set_reply_to(channel_id, me, owed["from"])
        return _finish(channel_id, me, result)

    # Per-sender say-continuation buffers so interleaved multiparty turns don't
    # bleed into each other's text. Kept across calls (in my state slot), so a
    # turn that's half in when I return - for a timeout, a human, another
    # peer's turn - still arrives whole later.
    pending_by_sender = _load_partial(channel_id, me)
    want = from_whom

    def done(result: dict[str, Any]) -> dict[str, Any]:
        _save_partial(channel_id, me, pending_by_sender)
        sender = result.get("from")
        if result.get("your_turn") and sender not in (None, "human", "participant"):
            set_reply_to(channel_id, me, sender)  # an unaddressed reply goes back to them
        return _finish(channel_id, me, result)

    # Track cumulative wait across repeated calls (for the nudge).
    since, nudged = _get_wait(channel_id, me)
    if since is None:
        since = time.time()
        _set_wait(channel_id, me, since, False)
        nudged = False

    try:
        return _await_loop(client, channel_id, me, deadline, poll, nudge_after, want,
                           pending_by_sender, done, since, nudged)
    except BaseException:
        # A read failed (or we were interrupted) mid-way: the cursor has already
        # moved past any pieces in the buffers, so keep them for the next call.
        try:
            _save_partial(channel_id, me, pending_by_sender)
        except Exception:
            pass
        raise


def _await_loop(client: DiscordClient, channel_id: str, me: str, deadline: float,
                poll: float, nudge_after: float, want: Optional[str],
                pending_by_sender: dict[str, list[dict[str, Any]]], done: Any,
                since: float, nudged: bool) -> dict[str, Any]:
    while True:
        cursor = get_cursor(channel_id, me)
        raw = client.read_messages(channel_id, limit=100, after=cursor)
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
                    # can never strand the awaiter (the stuck-chat footgun) -
                    # but only to the side it answers (whoever handed over the
                    # floor), not to its own sender or a bystander.
                    if not _plain_for_me(client, channel_id, me, m):
                        continue
                    entry = [{"id": m["id"], "from": "participant", "to": None,
                              "status": "plain", "timestamp": m["timestamp"],
                              "attachments": atts}]
                    return done(_result(
                        me, channel_id, sender="participant", status="plain",
                        text=text, messages=entry, ended=False,
                        stop_reason=None, your_turn=True, attachments=atts))
                # A real human (non-bot author) typed in the channel.
                human_entry = [{"id": m["id"], "from": "human", "to": None,
                                "status": None, "timestamp": m["timestamp"],
                                "attachments": atts}]
                if is_human_stop(text):
                    pending_by_sender.clear()
                    return done(_result(
                        me, channel_id, sender="human", status="stop",
                        text=text, messages=human_entry, ended=True,
                        stop_reason="human", your_turn=False, attachments=atts))
                # A remark doesn't move the floor: it's my turn only if it
                # already was (or nobody holds it). Otherwise two sides would
                # both answer and the chat would fork.
                st_h = compute_state(client, channel_id, me)
                owed = bool(st_h.get("your_turn")) or st_h.get("pending_turn") is None
                result = _result(
                    me, channel_id, sender="human", status="interjection",
                    text=text, messages=human_entry, ended=False,
                    stop_reason=None, your_turn=owed, attachments=atts)
                if not owed:
                    result["floor"] = st_h.get("floor")
                return done(result)

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
            text = join_pieces([x["body"] for x in pieces])
            atts = [a for x in pieces for a in x.get("attachments", [])]

            if status in TERMINAL_STATUSES:  # ends the chat for everyone in it
                if not _ends_mine(client, channel_id, me, parsed, m["id"]):
                    continue  # another conversation in this room ended
                stop_reason = "agreed" if status == "end" else "impasse"
                pending_by_sender.clear()
                return done(_result(
                    me, channel_id, sender=sender, status=status, text=text,
                    messages=_lean(pieces), ended=True, stop_reason=stop_reason,
                    your_turn=False, addressed_to=to, attachments=atts))

            # A yielded turn (over/wrap). Does the floor actually come to me?
            if want is not None and not same_handle(sender, want):
                continue  # waiting specifically for a different peer
            if not _targets(to, me):
                continue  # addressed to another peer — keep holding the wait
            if _is_broadcast(to) and not _broadcast_for_me(client, channel_id, me, m["id"]):
                continue  # open to all - but in a conversation I'm not part of

            result = _result(
                me, channel_id, sender=sender, status=status, text=text,
                messages=_lean(pieces), ended=False, stop_reason=None,
                your_turn=True, addressed_to=to, attachments=atts)
            result.update(_floor_context(client, channel_id, me))
            return done(result)

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
            _save_partial(channel_id, me, pending_by_sender)
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
    client: DiscordClient, channel_id: str, me: Optional[str] = None, scan: int = 100,
    upto: Optional[str] = None,
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
    last_turn/pending_turn carry `to` (the addressee, or None). `your_turn` is
    worked out for `me` alone (the turn owed to me, which in a shared room need
    not be the room's latest); `_owed_turn`/`_owed_text`/`_owed_predecessor`
    describe it (the last is a cursor that re-reads all of it).

    With `me`, everything is about `me`'s own conversation: the room is shared,
    so other pairs chatting there (their turns, their ending, their members)
    are left out - see _conversation. Without `me` (a viewer) it's the room's
    latest chat. ``upto`` stops at that message id, as if nothing came after.
    """
    raw = _history(client, channel_id, me, scan, upto)
    msgs = list(reversed([simplify_message(m) for m in raw]))  # chronological
    parsed_list = [parse(m["content"]) for m in msgs]
    plain_as = _attribute_plain(msgs, parsed_list)
    scope, room_others = _conversation(msgs, parsed_list, plain_as, me)

    # Scope to the CURRENT chat. A terminal turn (end/impasse) or a human stop
    # that is followed by more chat traffic closed the previous chat, so its
    # handles and turns must not leak into this one. (A terminal that is still
    # the latest chat message stays in scope, so `ended` can report it.)
    start = 0
    chat_after = False
    for i in reversed(scope):
        p = parsed_list[i]
        closes = (p is not None and p["status"] in TERMINAL_STATUSES) or (
            p is None and not msgs[i].get("bot") and is_human_stop(msgs[i]["content"]))
        if closes and chat_after:
            start = i + 1
            break
        if p is not None:
            chat_after = True
    scope = [i for i in scope if i >= start]

    # Handles are case-insensitive identities; each is shown with the spelling
    # first seen, so "Convex" and "convex" are one participant.
    display: dict[str, str] = {}

    def canon(h: str) -> str:
        return display.setdefault(handle_key(h), h)

    real = YIELD_STATUSES + TERMINAL_STATUSES  # statuses that complete a turn
    participants: list[str] = []
    posters: list[str] = []  # who has actually posted in this chat
    addressed_at: dict[str, tuple[str, int]] = {}  # addressee -> (by whom, index), latest
    last_seen: dict[str, Optional[datetime]] = {}  # participant -> last post/address time
    latest_ts: Optional[datetime] = None
    last_turn: Optional[dict[str, Any]] = None
    last_turn_index_by: dict[str, int] = {}   # participant -> index of their last completed turn
    last_significant: dict[str, dict[str, Any]] = {}  # ignoring pure `say`
    turn_authors: list[str] = []  # author of each completed turn, in order
    progress: dict[str, dict[str, Any]] = {}  # who -> latest say/working since their last turn
    run_start: dict[str, int] = {}         # who -> index of their first say/working piece
    run_bodies: dict[str, list[str]] = {}  # who -> bodies of those pieces
    last_text = ""
    turns: list[dict[str, Any]] = []  # every completed turn, in order
    for i in scope:
        m, p = msgs[i], parsed_list[i]
        if not p:
            # A participant answering with a plain send_message instead of
            # chat_say: count it as the reply of whoever had the floor, so the
            # turn goes back to the one who handed it to them. (Worked out
            # over the whole room, so it's the same answer in every chat.)
            if i in plain_as:
                responder = canon(plain_as[i][0])
                to_back = canon(plain_as[i][1])
                turn_authors.append(responder)
                last_turn_index_by[responder] = i
                last_significant[responder] = {"status": "over", "i": i}
                progress.pop(responder, None)
                run_start.pop(responder, None)
                run_bodies.pop(responder, None)
                last_turn = {"from": responder, "to": to_back, "status": "over",
                             "id": m["id"], "ts": m["timestamp"], "plain": True}
                last_text = m["content"]
                turns.append({**last_turn, "i": i, "first": i, "text": last_text})
            continue
        ts = _parse_ts(m["timestamp"])
        if ts is not None:
            latest_ts = ts if latest_ts is None else max(latest_ts, ts)
        who = canon(p["participant"])
        if who not in posters:
            posters.append(who)
        to = p["to"] if _is_broadcast(p["to"]) else canon(p["to"])
        names = [who] + ([to] if to and not _is_broadcast(to) else [])
        if to and not _is_broadcast(to) and p["status"] in YIELD_STATUSES:
            addressed_at[to] = (who, i)
        # An addressed peer is a known participant even before it has posted.
        for n in names:
            if n not in participants:
                participants.append(n)
            last_seen[n] = ts
        if p["status"] in PROGRESS_STATUSES:
            progress[who] = {"from": who, "status": p["status"],
                             "text": (p["body"] or "")[:300], "ts": m["timestamp"]}
            run_start.setdefault(who, i)
            run_bodies.setdefault(who, []).append(p["body"] or "")
            continue
        progress.pop(who, None)
        # A long turn arrives as say/working pieces then its final message:
        # the turn starts at the first piece and its text is all of them.
        first = run_start.pop(who, i)
        bodies = run_bodies.pop(who, []) + [p["body"] or ""]
        last_significant[who] = {"status": p["status"], "i": i}
        if p["status"] in real:
            turn_authors.append(who)
            last_turn_index_by[who] = i
            last_turn = {"from": who, "to": to, "status": p["status"],
                         "id": m["id"], "ts": m["timestamp"]}
            last_text = join_pieces(bodies)
            turns.append({**last_turn, "i": i, "first": first, "text": last_text})

    # The querying caller is a participant too (it may not have posted yet).
    me_norm = canon(sanitize_handle(me)) if me is not None else None
    if me_norm and me_norm not in participants:
        participants.append(me_norm)

    # A human stop after the last chat message ends the chat right there.
    last_chat_i = max((i for i in scope if parsed_list[i]), default=-1)
    human_stop = any(
        parsed_list[i] is None and not msgs[i].get("bot") and is_human_stop(msgs[i]["content"])
        for i in scope if i > last_chat_i)
    if human_stop:
        progress.clear()
        run_start.clear()
    ended = human_stop or bool(last_turn and last_turn["status"] in TERMINAL_STATUSES)

    # The last yielded turn is owed until someone completes a turn after it —
    # and since last_turn IS the most recent completed turn, a yield there is
    # by definition unanswered.
    pending = None
    if last_turn and last_turn["status"] in YIELD_STATUSES and not human_stop:
        pending = last_turn

    # Someone who has never posted stops counting once whoever addressed them
    # has moved on to a later turn (a mistyped `to=` that was then re-sent to
    # the right peer) - so a typo doesn't leave a phantom in the room.
    def _superseded(p: str) -> bool:
        if p in posters or p == me_norm or (pending and p == pending["to"]):
            return False
        by, at = addressed_at.get(p, (None, -1))
        return by is not None and last_turn_index_by.get(by, -1) > at
    participants = [p for p in participants if not _superseded(p)]

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
        # A say/working is a turn in progress only from whoever may speak now: in
        # a 3+ room, a non-holder posting `working` doesn't hold anyone up.
        "progress": [v for k, v in progress.items()
                     if k in participants and (floor is None or k == floor)],
        "_posters": posters,
        # Everyone who has posted in the room, and who is chatting in it now
        # outside my conversation.
        "_room_posters": sorted({handle_key(p["participant"]): p["participant"]
                                 for p in parsed_list if p}.values()),
        "_others_chatting": room_others,
        "_plain_ids": [msgs[i]["id"] for i in plain_as],
        "_open_run_predecessor": _before(msgs, min(
            (i for w, i in run_start.items() if not same_handle(w, me_norm)), default=-1)),
        "stop_reason": "human" if human_stop else (
            {"end": "agreed", "impasse": "impasse"}.get(last_turn["status"])
            if ended and last_turn else None),
    }
    if me_norm is not None:
        owed = None if ended else _owed_to(turns, me_norm, last_turn_index_by)
        state["your_turn"] = owed is not None
        state["_owed_turn"] = (
            {k: owed[k] for k in ("from", "to", "status", "id", "ts")} if owed else None)
        state["_owed_text"] = owed["text"] if owed else None
        state["_owed_predecessor"] = _before(msgs, owed["first"]) if owed else None
    return state


def _plain_responder(m: dict[str, Any], last_turn: Optional[dict[str, Any]],
                     turn_authors: list[str]) -> Optional[str]:
    """Who a plain (untagged) bot message is the reply of: whoever held the floor
    after the last yielded turn - its addressee, or in an unaddressed exchange
    the previous speaker (``participant`` if nobody else has spoken yet). None
    if it isn't a reply (a human, a reminder, or no turn to answer)."""
    if not m.get("bot") or (m.get("content") or "").lstrip().startswith(NUDGE_MARK):
        return None
    if not last_turn or last_turn["status"] not in YIELD_STATUSES:
        return None
    if last_turn["to"] and not _is_broadcast(last_turn["to"]):
        return last_turn["to"]
    return next((w for w in reversed(turn_authors)
                 if not same_handle(w, last_turn["from"])), "participant")


# How far back compute_state pages to reach a session's read position, so a
# turn owed to it isn't lost just because a busy room moved on past one page.
HISTORY_CAP = 500


def _history(client: DiscordClient, channel_id: str, me: Optional[str], scan: int,
             upto: Optional[str]) -> list[dict[str, Any]]:
    """Recent messages, newest first: one page, plus older pages back to my read
    position if more than a page has arrived since (whatever is owed to me lies
    after it). ``upto`` reads as if that message were the latest."""
    kw = {"before": str(int(upto) + 1)} if upto is not None else {}
    page = client.read_messages(channel_id, limit=max(1, min(scan, 100)), **kw)
    out = list(page)
    try:
        mark = get_cursor(channel_id, me) if me else None
        cursor = int(mark) if mark is not None else None
    except ValueError:
        cursor = None
    while (cursor is not None and len(page) == 100 and len(out) < HISTORY_CAP
           and int(page[-1]["id"]) > cursor):
        page = client.read_messages(channel_id, limit=100, before=page[-1]["id"])
        out.extend(page)
    return out


def _attribute_plain(msgs: list[dict[str, Any]],
                     parsed_list: list[Optional[dict[str, Any]]]) -> dict[int, tuple[str, str]]:
    """For each plain (untagged) bot message that answers a turn: index ->
    (who it's the reply of, who it goes back to). See _plain_responder."""
    out: dict[int, tuple[str, str]] = {}
    last: Optional[dict[str, Any]] = None
    authors: list[str] = []
    for i, (m, p) in enumerate(zip(msgs, parsed_list)):
        if p is None:
            responder = _plain_responder(m, last, authors)
            if responder and last:
                out[i] = (responder, last["from"])
                authors.append(responder)
                last = {"from": responder, "to": last["from"], "status": "over"}
            continue
        if p["status"] in YIELD_STATUSES + TERMINAL_STATUSES:
            authors.append(p["participant"])
            last = {"from": p["participant"], "to": p["to"], "status": p["status"]}
    return out


def _conversation(msgs: list[dict[str, Any]], parsed_list: list[Optional[dict[str, Any]]],
                  plain_as: dict[int, tuple[str, str]],
                  me: Optional[str]) -> tuple[list[int], list[str]]:
    """Which messages belong to ``me``'s conversation, and who else is chatting
    in the room right now.

    The room is shared, so several chats can run in it at once. A conversation
    is the people linked by addressed turns (A>B joins A and B; replies are
    addressed automatically). An end/impasse closes the conversation it was
    sent in (its members start afresh), a human stop closes every one. Mine
    is every turn of my conversation, the human's messages, and - only while
    I'm in no conversation yet - unaddressed turns from others who aren't in
    one either (an opener, or an old-style unaddressed two-party chat).
    Without ``me``: the whole room."""
    if me is None:
        return list(range(len(msgs))), []
    comp: dict[str, set[str]] = {}
    epoch: dict[str, int] = {}  # handle -> where its current conversation starts
    room_stop = 0
    recent: Optional[str] = None  # who sent the latest addressed turn

    def grp(k: str) -> set[str]:
        if k not in comp:
            comp[k] = {k}
        return comp[k]

    def link(a: str, b: str) -> None:
        sa, sb = grp(a), grp(b)
        if sa is not sb:
            sa |= sb
            for k in sb:
                comp[k] = sa

    def close(keys: list[str], i: int) -> None:
        for k in keys:
            comp[k] = {k}
            epoch[k] = i

    for i, m in enumerate(msgs):
        p = parsed_list[i]
        if p is None:
            if i in plain_as:
                link(handle_key(plain_as[i][0]), handle_key(plain_as[i][1]))
            elif not m.get("bot") and is_human_stop(m.get("content") or ""):
                close(list(comp), i)
                room_stop = i
            continue
        k = handle_key(p["participant"])
        grp(k)
        if not _is_broadcast(p["to"]):
            link(k, handle_key(p["to"]))
            recent = k
        elif (p["status"] in ("ask", "working") and len(grp(k)) == 1
              and recent and len(grp(recent)) > 1):
            # Someone in no conversation raising a hand / saying they're on it
            # wants into the one going on (the most recent).
            link(k, recent)
        if p["status"] in TERMINAL_STATUSES:
            if len(grp(k)) == 1:  # an unaddressed chat: everyone not in a conversation
                close([h for h, s in comp.items() if len(s) == 1], i)
            else:
                close(list(grp(k)), i)

    my = handle_key(me)
    mine = grp(my)
    alone = len(mine) == 1

    def ep(k: str) -> int:
        return max(epoch.get(k, 0), room_stop)

    scope: list[int] = []
    others: dict[str, tuple[str, Optional[datetime]]] = {}
    latest: Optional[datetime] = None
    for i, m in enumerate(msgs):
        p = parsed_list[i]
        if p is None:
            if i in plain_as:
                r, t = handle_key(plain_as[i][0]), handle_key(plain_as[i][1])
                if (r in mine or t in mine) and i >= ep(r if r in mine else t):
                    scope.append(i)
            elif not m.get("bot"):
                scope.append(i)  # a human speaks to the whole room
            continue
        k = handle_key(p["participant"])
        ts = _parse_ts(m["timestamp"])
        if ts is not None:
            latest = ts if latest is None else max(latest, ts)
        if i == epoch.get(my):
            scope.append(i)  # what closed my last conversation (so `ended` shows)
        elif i < ep(k):
            continue
        elif k in mine or (alone and _is_broadcast(p["to"]) and len(grp(k)) == 1):
            scope.append(i)
        else:
            others[k] = (p["participant"], ts)
    busy = [name for name, ts in others.values()
            if ts is None or latest is None or ts >= latest - STALE_AFTER]
    return scope, busy


def _ends_mine(client: DiscordClient, channel_id: str, me: str,
               p: dict[str, Any], message_id: str) -> bool:
    """Does this end/impasse end MY conversation (not another one in the room)?"""
    if same_handle(p.get("to"), me):
        return True
    return bool(compute_state(client, channel_id, me, upto=message_id).get("ended"))


def _broadcast_for_me(client: DiscordClient, channel_id: str, me: str,
                      message_id: str) -> bool:
    """Is this unaddressed turn one I'm meant to answer (my conversation's, or
    an opener while I'm in none) rather than another conversation's?"""
    owed = compute_state(client, channel_id, me, upto=message_id).get("_owed_turn")
    return bool(owed) and str(owed["id"]) == str(message_id)


def _plain_for_me(client: DiscordClient, channel_id: str, me: str,
                  m: dict[str, Any]) -> bool:
    """Is this plain (untagged) bot message a reply to me? Yes if it answers a
    turn I handed over, or if it answers nothing and nothing is pending."""
    st = compute_state(client, channel_id, me, upto=m["id"])
    owed = st.get("_owed_turn")
    if owed:
        return str(owed["id"]) == str(m["id"])
    return m["id"] not in st.get("_plain_ids", []) and st.get("pending_turn") is None


def _wakes(client: DiscordClient, channel_id: str, m: dict[str, Any], me: str,
           from_whom: Optional[str] = None) -> bool:
    """_would_wake, plus the checks that need the room: an ending or an
    unaddressed turn only counts if it's from my conversation."""
    if not _would_wake(m, me, from_whom):
        return False
    p = parse(m.get("content") or "")
    if p is None:
        return not m.get("bot") or _plain_for_me(client, channel_id, me, m)
    if p["status"] in TERMINAL_STATUSES:
        return _ends_mine(client, channel_id, me, p, m["id"])
    if _is_broadcast(p["to"]):
        return _broadcast_for_me(client, channel_id, me, m["id"])
    return True


def _owed_to(turns: list[dict[str, Any]], me: str,
             last_by: dict[str, int]) -> Optional[dict[str, Any]]:
    """The turn owed to ``me``: the latest yield that targets me, that I haven't
    answered since (no completed turn of mine after it), and whose sender hasn't
    moved on (it's still their latest turn). Worked out per caller, so two
    conversations sharing one room don't hide each other's owed turns. An
    unaddressed turn counts as answered once anyone else replies to it."""
    mine = last_by.get(me, -1)
    for t in reversed(turns):
        if t["i"] <= mine:
            return None
        if same_handle(t["from"], me) or t["status"] not in YIELD_STATUSES:
            continue
        if not _targets(t["to"], me) or last_by.get(t["from"]) != t["i"]:
            continue
        if _is_broadcast(t["to"]) and any(
                u["i"] > t["i"] and not same_handle(u["from"], t["from"]) for u in turns):
            continue
        return t
    return None


def _before(msgs: list[dict[str, Any]], i: int) -> Optional[str]:
    """A cursor that re-reads ``msgs[i]`` onward (None if i < 0)."""
    if i < 0:
        return None
    return msgs[i - 1]["id"] if i > 0 else str(int(msgs[i]["id"]) - 1)


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


def _would_wake(m: dict[str, Any], me: str, from_whom: Optional[str] = None) -> bool:
    """Would the await loop deliver this message to ``me``? (A human or plain
    post, an ending, or a yielded turn that targets me - not my own posts, a
    reminder, or someone's say/working/ask, which wake nobody.)"""
    content = m.get("content") or ""
    if content.lstrip().startswith(NUDGE_MARK):
        return False
    p = parse(content)
    if p is None:
        return True
    if same_handle(p["participant"], me) or p["status"] in PROGRESS_STATUSES + ("ask",):
        return False
    if p["status"] in TERMINAL_STATUSES:
        return True
    if from_whom is not None and not same_handle(p["participant"], from_whom):
        return False
    return _targets(p["to"], me)


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
        if not result.get("your_turn"):
            holder = result.get("floor") or "the other side"
            return (f"A human spoke in the chat, but it's not your turn: {holder} has "
                    "the floor. Take their note into account (act on it if it's for "
                    "you), then call chat_await to keep waiting - don't post a turn.")
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
