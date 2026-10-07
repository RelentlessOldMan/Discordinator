"""MCP server exposing Discord send/read tools.

Run with the ``discordinator-mcp`` entry point (stdio transport). Configure it
in an MCP client (e.g. Claude Code / Claude Desktop) like:

    {
      "mcpServers": {
        "discordinator": {
          "command": "discordinator-mcp",
          "env": { "DISCORD_BOT_TOKEN": "..." }
        }
      }
    }

Channels can be referenced by the friendly names stored in the Discordinator
config file, or by raw numeric channel ids.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer

from . import chat, config, handles, use_system_certs
from .client_factory import Client, make_client, make_client_for_url
from .discord_client import DiscordError, simplify_message

# Keep the HTTP client quiet: it logs an INFO line per request to stderr, which
# is noise for a stdio MCP server (stdout carries the JSON-RPC protocol).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

mcp = MCPServer("discordinator")


def _client(mode: Optional[str] = None) -> Client:
    """Client for ``mode`` ("relay"/"chat"/None). Relay and chat may run on
    different transports, so each tool builds its client with its own mode."""
    return make_client(config.load(), mode)


class ChatSendError(RuntimeError):
    """A chat_say that didn't post its turn; the message says the turn is still yours."""


def _local_paths(file_list: list[Any]) -> list[str]:
    """Absolute paths of files to share in a local chat (both sides are on this
    machine, so the path IS the attachment). Raises if any file is missing."""
    if len(file_list) > chat.MAX_FILES_PER_MESSAGE:
        raise ValueError(f"a chat turn can carry at most {chat.MAX_FILES_PER_MESSAGE} "
                         f"files (got {len(file_list)}).")
    paths = []
    for f in file_list:
        p = Path(str(f)).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"File not found: {f}")
        paths.append(str(p))
    return paths


def _try_ack(client: Client, channel_id: str, messages: list[dict[str, Any]]) -> None:
    """React ✅ to the newest message. Best-effort: never fail a read because the
    bot lacks the Add Reactions permission."""
    if not messages:
        return
    newest = max(messages, key=lambda m: int(m["id"]))
    try:
        client.add_reaction(channel_id, newest["id"])
    except DiscordError:
        pass


@mcp.tool()
def send_message(
    text: str,
    channel: Optional[str] = None,
    label: Optional[str] = None,
) -> str:
    """Send a message to a Discord channel.

    Args:
        text: The message body. Long text is split across multiple messages.
        channel: A configured channel name or a raw channel id. Defaults to the
            configured default channel if omitted.
        label: Optional tag prefixed to the message (e.g. the machine/session
            name). Falls back to the configured machine_label.
    """
    cfg = config.load()
    channel_id = config.resolve_channel(cfg, channel)
    tag = label if label is not None else cfg.get("machine_label")
    with _client("relay") as client:
        sent = client.send_message(channel_id, text, label=tag)
    return f"Sent {len(sent)} message(s) to channel {channel_id}."


@mcp.tool()
def read_messages(
    channel: Optional[str] = None,
    limit: int = 20,
    after: Optional[str] = None,
    before: Optional[str] = None,
    newest_first: bool = False,
    ack: Optional[bool] = None,
) -> list[dict[str, Any]]:
    """Read recent messages from a Discord channel.

    Args:
        channel: A configured channel name or raw channel id. Defaults to the
            configured default channel.
        limit: Number of messages to fetch (1-100).
        after: Only return messages after this message id (for polling new ones).
        before: Only return messages before this message id (for paging back).
        newest_first: If false (default), messages are returned oldest-first.
        ack: React ✅ to the newest message read (needs Add Reactions). Defaults
            to the configured ack_on_read setting when omitted.

    Returns a list of simplified message objects with id, author, timestamp,
    content and attachments. Each attachment is an object
    {url, filename, content_type, size, width, height, is_image} — pass its url
    to download_attachment to fetch the bytes.
    """
    cfg = config.load()
    channel_id = config.resolve_channel(cfg, channel)
    if ack is None:
        ack = bool(cfg.get("ack_on_read"))
    with _client("relay") as client:
        raw = client.read_messages(channel_id, limit=limit, after=after, before=before)
        messages = [simplify_message(m) for m in raw]
        if ack:
            _try_ack(client, channel_id, messages)
    if not newest_first:
        messages.reverse()
    return messages


@mcp.tool()
def send_file(
    paths: Any,
    text: str = "",
    channel: Optional[str] = None,
    label: Optional[str] = None,
) -> str:
    """Upload one or more files (images included) to a channel, with optional text.

    Images auto-embed in Discord so a remote human can just look at them; other
    files land as downloadable attachments. Large lists are split across messages
    (Discord caps a message at 10 files); over-limit or missing files error
    before anything is sent.

    GATED: this machine must opt in to SENDING attachments, which is OFF by
    default. Enable with `discordinator config set-attachments send on` (or
    DISCORDINATOR_ALLOW_SEND=1). This is what stops a locked-down machine from
    uploading local files unless deliberately allowed.

    Args:
        paths: a file path, or a list of file paths, to upload.
        text: optional message body (rides the first message).
        channel: configured channel name or raw id; defaults to the default channel.
        label: tag prefixed to the message; falls back to the configured machine_label.
    """
    cfg = config.load()
    config.require_send_attachments(cfg)  # raises if this machine hasn't opted in
    channel_id = config.resolve_channel(cfg, channel)
    tag = label if label is not None else cfg.get("machine_label")
    file_list = [paths] if isinstance(paths, str) else list(paths)
    with _client("relay") as client:
        sent = client.send_files(channel_id, text, file_list, label=tag)
    return f"Sent {len(sent)} message(s) with {len(file_list)} file(s) to channel {channel_id}."


@mcp.tool()
def download_attachment(url: str, dest: Optional[str] = None) -> dict[str, Any]:
    """Download an attachment to local disk and return where it was saved.

    Attachments show up on read results as objects with a ``url`` (plus
    filename, content_type, is_image). Pass that ``url`` here to fetch the bytes
    — e.g. to read a config file someone relayed, or save an image. Works on
    both transports (Discord downloads over HTTP; local mode copies the file
    off the shared disk).

    GATED: this machine must opt in to RECEIVING attachments, which is OFF by
    default. Enable with `discordinator config set-attachments receive on` (or
    DISCORDINATOR_ALLOW_RECEIVE=1). This keeps locked-down machines from pulling
    files unless deliberately allowed.

    Args:
        url: the attachment url from a read result (fetch a FRESH read — Discord
            CDN urls are signed and expire).
        dest: a directory (filename taken from the url) or a full file path.
            Defaults to the current working directory.

    Returns {"saved": "<path>", "filename": "<name>"}.
    """
    cfg = config.load()
    config.require_receive_attachments(cfg)  # raises if this machine hasn't opted in
    target = dest if dest is not None else "."
    # The url shape decides the backend (http -> Discord CDN, path -> local
    # disk), so this works whether the url came from a relay or a chat turn even
    # when those two modes run on different transports.
    with make_client_for_url(url, cfg) as client:
        saved = client.download_attachment(url, target)
    return {"saved": str(saved), "filename": saved.name}


@mcp.tool()
def get_new_messages(
    channel: Optional[str] = None,
    include_self: bool = False,
    limit: int = 50,
    ack: Optional[bool] = None,
) -> list[dict[str, Any]]:
    """Get only NEW messages since the last time this tool was called for the
    channel — the relay primitive for two-way session handoff.

    A per-channel cursor is stored and advanced on each call, so repeated calls
    return only fresh messages (not the whole history). By default your own
    machine's messages (matched by the configured machine_label prefix) are
    filtered out, so you see just what the other session/machine said.

    Args:
        channel: A configured channel name or raw channel id. Defaults to the
            configured default channel.
        include_self: If true, also include your own machine's messages.
        limit: How many recent messages to return on the FIRST call (before a
            cursor exists). Subsequent calls return everything new since.
        ack: React ✅ to the newest returned message so the other side can see
            it was read (needs Add Reactions permission). Defaults to the
            configured ack_on_read setting when omitted.

    Returns simplified message objects in chronological order.
    """
    cfg = config.load()
    channel_id = config.resolve_channel(cfg, channel)
    own_label = cfg.get("machine_label")
    if ack is None:
        ack = bool(cfg.get("ack_on_read"))
    cursor = config.get_cursor(channel_id)

    capped = max(1, min(int(limit), 100))
    with _client("relay") as client:
        if cursor:
            raw = client.read_messages(channel_id, limit=100, after=cursor)
        else:
            raw = client.read_messages(channel_id, limit=capped)
        messages = [simplify_message(m) for m in raw]
        messages.reverse()

        if messages:
            config.set_cursor(channel_id, messages[-1]["id"])

        if not include_self and own_label:
            prefix = f"[{own_label}]"
            messages = [m for m in messages if not m["content"].startswith(prefix)]

        if ack:
            _try_ack(client, channel_id, messages)
    return messages


@mcp.tool()
def purge_messages(
    channel: Optional[str] = None,
    older_than_days: float = 7.0,
    only_mine: bool = True,
    scan_limit: int = 200,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Delete old messages from a channel (housekeeping; destructive).

    Defaults are safe: dry_run=True (nothing deleted, just reports what would be),
    only_mine=True (only the bot's own messages), and a 7-day age floor. Set
    dry_run=False to actually delete. Deleting other users' messages
    (only_mine=False) requires the Manage Messages permission.

    Args:
        channel: Configured channel name or raw id. Defaults to the default channel.
        older_than_days: Only affect messages older than this many days.
        only_mine: If true (default), only delete the bot's own messages.
        scan_limit: How many recent messages to scan.
        dry_run: If true (default), report but do not delete.

    Returns a summary dict with counts and (on dry-run) the matched message ids.
    """
    from datetime import datetime, timedelta, timezone

    cfg = config.load()
    channel_id = config.resolve_channel(cfg, channel)
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)

    with _client("relay") as client:
        my_id = client.whoami().get("id")
        collected: list[dict[str, Any]] = []
        before: Optional[str] = None
        while len(collected) < scan_limit:
            batch = client.read_messages(
                channel_id, limit=min(100, scan_limit - len(collected)), before=before
            )
            if not batch:
                break
            collected.extend(batch)
            before = batch[-1]["id"]
            if len(batch) < 100:
                break

        matched: list[dict[str, Any]] = []
        for raw in collected:
            m = simplify_message(raw)
            if only_mine and m["author_id"] != my_id:
                continue
            ts = datetime.fromisoformat(m["timestamp"]) if m["timestamp"] else None
            if ts is None or ts > cutoff:
                continue
            matched.append(m)

        if dry_run:
            return {
                "dry_run": True,
                "would_delete": len(matched),
                "message_ids": [m["id"] for m in matched],
            }

        deleted = 0
        for m in matched:
            client.delete_message(channel_id, m["id"])
            deleted += 1
        return {"dry_run": False, "deleted": deleted}


@mcp.tool()
def list_channels() -> dict[str, Any]:
    """List the friendly channel names configured for Discordinator."""
    cfg = config.load()
    return {
        "default_channel": cfg.get("default_channel"),
        "channels": cfg.get("channels") or {},
        "machine_label": cfg.get("machine_label"),
    }


@mcp.tool()
def whoami() -> dict[str, Any]:
    """Return the bot's own identity (verifies the token is valid)."""
    with _client("relay") as client:
        me = client.whoami()
    return {
        "id": me.get("id"),
        "username": me.get("username"),
        "global_name": me.get("global_name"),
    }


# ==========================================================================
# CHAT MODE — a separate, turn-based agent<->agent protocol. Distinct from the
# relay tools above so the two are never conflated. Every call takes a `chatter`
# id (your handle) because both sides may be on the SAME machine and must stay
# distinguishable; each chatter has its own read cursor.
# ==========================================================================


def _chatter(chatter: Optional[str], cfg: dict[str, Any]) -> str:
    """This session's handle (see handles.resolve): the project's fixed handle,
    optionally with a role (`CodeCarver/ui`), made unique among live sessions on
    this machine."""
    return handles.resolve(chatter, cfg)[0]


@mcp.tool()
def chat_begin(chatter: Optional[str] = None, channel: Optional[str] = None, turn_cap: int = 20) -> dict[str, Any]:
    """Start or join a turn-based chat as participant `chatter`.

    Seeds your read position to *now* (prior history is ignored) and resets your
    turn counter. BOTH participants call this first, with DISTINCT `chatter`
    handles (e.g. "A" and "B"). Then the initiator calls `chat_say`; the other
    calls `chat_await`. Do NOT have both call `chat_await` first — that deadlocks.

    Args:
        chatter: optional ROLE for this session. Your handle is the project's
            fixed handle (DISCORDINATOR_CHAT_HANDLE, e.g. "CodeCarver"); a
            chatter is appended to it ("ui" -> "CodeCarver/ui"). Omit it when
            this is the only session of the project in the chat; pass a short
            role when two sessions of the same project need to talk (each a
            different role). If another live session on this machine already
            has your handle you get a "-2" suffix and a `note` saying so. With
            no project handle configured, chatter is used as-is.
        channel: chat channel name/id. Omit to use the dedicated chat channel
            (chat_channel / DISCORDINATOR_CHAT_CHANNEL — a shared room like
            claudes-chatroom); both sides then meet there with no negotiation.
            Never defaults to a per-project relay channel unless one isn't set.
        turn_cap: soft cap on your turns before you're nudged to wrap up.
    """
    cfg = config.load()
    me, note = handles.resolve(chatter, cfg)
    channel_id = config.resolve_chat_channel(cfg, channel)
    with _client("chat") as client:
        st = chat.compute_state(client, channel_id, me)
        if st.get("your_turn") and st.get("_pending_predecessor"):
            # A turn is already owed to me (e.g. I ended/dropped and the other
            # side spoke again). Position the cursor so chat_await re-delivers it
            # immediately — recovery instead of a silent stall.
            cursor = st["_pending_predecessor"]
        else:
            latest = client.read_messages(channel_id, limit=1)
            cursor = latest[0]["id"] if latest else "0"
    chat.reset(channel_id, me, cursor, turn_cap)
    owed = bool(st.get("your_turn"))
    out = {
        "channel": channel_id, "chatter": me, "turn_cap": turn_cap,
        "recovered_pending_turn": owed,
        "state": {k: v for k, v in st.items() if not k.startswith("_")},
        "next": ("A turn is owed to you — call chat_await now to receive it."
                 if owed else "initiator: chat_say(...); other: chat_await(...)"),
    }
    if note:
        out["note"] = note
    return out


@mcp.tool()
def chat_say(
    text: str,
    chatter: Optional[str] = None,
    status: str = "over",
    channel: Optional[str] = None,
    to: Optional[str] = None,
    files: Any = None,
    wait: bool = True,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Send a chat message as `chatter` with an explicit turn status. Pass the
    same `chatter` (role) you gave chat_begin, or omit it if you omitted it there.

    When you hand over the turn (`over`/`wrap`) this call ALSO WAITS for the
    reply (`wait=True`, default) and returns it under `reply` — the same shape
    chat_await returns. So one chat_say per turn: post, get the answer, respond.
    Every result has a `next` line saying exactly what to do; follow it. If
    `reply.timed_out`, the other side is still busy: call chat_await to keep
    waiting (for as long as it takes) — never end your turn mid-chat.

    status values — almost always use "over" (the default):
      - "say"     ONLY to split one long turn: more of THIS turn follows right
                  now in your next chat_say. It keeps the floor, so the others
                  stay waiting until you send "over". Never end on "say".
      - "working" "hold on, I'm going to go do something" — keeps the floor and
                  tells the others you're busy (they keep waiting, and see your
                  note). Then DO the work and post the results with "over". Use
                  this instead of "over" when you need time (a build, tests, a
                  20-minute task) before you can really answer.
      - "ask"     raise a hand — "I'd like the floor" — WITHOUT taking the current
                  turn. Use when someone else holds the floor and you want in; it
                  doesn't interrupt them, it just registers a request others see.
      - "over"    I'm done — your turn (the normal handoff).
      - "wrap"    I think we can end this — do you agree? (yields your turn).
      - "end"     ending now (use to confirm after the other proposed "wrap",
                  or to end unilaterally). Terminal.
      - "impasse" we're stuck — stop and get the human. Terminal.

    `to`: address this turn to ONE participant by handle (e.g. to="C"). Required
    discipline in a 3+ party room — an addressed `over`/`wrap` passes the floor to
    exactly that peer, so only they wake; leaving it unset broadcasts (anyone may
    answer, which can collide). In a 2-party chat just omit it. Special targets
    all/everyone/* broadcast explicitly.

    Long text is split across messages, each re-tagged (and re-addressed), so
    multi-part turns stay intact. Returns your turn count and, in a multiparty
    room after you yield, who is waiting and the fair next addressee.

    `files`: optional file path (or list of paths) to attach to this turn —
    images included (they auto-embed in Discord). They ride the turn's final
    message, so the receiver sees them on the same turn via chat_await's
    `attachments`. GATED: needs this machine's send opt-in (off by default;
    `config set-attachments send on` / DISCORDINATOR_ALLOW_SEND=1) and, on
    Discord, the bot's Attach Files permission. At most 10 files per turn.
    On a LOCAL chat (same machine, same disk) nothing is copied and no opt-in is
    needed: the files' full paths are added to your message for the others to
    open directly (returned as `files_shared`). You can also just write the
    paths in `text` yourself.

    If this call raises, the message was NOT posted (the error says so); if it
    was your turn, it still is - fix the problem and call chat_say again.
    """
    cfg = config.load()
    try:
        me = _chatter(chatter, cfg)
        if status not in chat.STATUSES:
            raise ValueError(f"status must be one of {chat.STATUSES}, got {status!r}")
        target = chat.sanitize_handle(to) if to else None
        file_list = None
        shared: list[str] = []
        if files:
            file_list = [files] if isinstance(files, str) else list(files)
            if config.is_local(cfg, "chat"):
                # Same machine, same disk: share the paths, don't copy the files.
                shared = _local_paths(file_list)
                text = f"{text}\n\nFiles (on this machine):\n" + "\n".join(
                    f"- {p}" for p in shared)
                file_list = None
            else:
                config.require_send_attachments(cfg)  # raises if this machine hasn't opted in
        channel_id = config.resolve_chat_channel(cfg, channel)
        chat_client = _client("chat")
    except Exception as e:
        raise ChatSendError(
            f"{e}\n\nNothing was posted, so nobody saw this message. If it was your "
            "turn, it still is - the others are waiting on you. Fix this and send "
            "it again with chat_say(...) before calling chat_await.") from e
    with chat_client as client:
        try:
            sent = chat.send_chat(client, channel_id, me, status, text, to=target,
                                  files=file_list)
        except Exception as e:
            raise ChatSendError(
                f"{e}\n\nThis message did NOT go through (nothing, or only part of "
                "it, was posted). If it was your turn, it still is - the others are "
                "waiting on you. Fix this and send it again with chat_say(...) "
                "before calling chat_await.") from e
        # `say`/`ask` don't complete a turn, so they don't count against the cap.
        took_turn = status not in chat.NON_TURN_STATUSES
        turns = chat.bump_turn(channel_id, me) if took_turn else chat.get_meta(channel_id, me)[0]
        _, cap = chat.get_meta(channel_id, me)
        out = {
            "sent_messages": len(sent),
            "status": status,
            "to": target,
            "my_turns": turns,
            "turn_cap": cap,
            "cap_reached": turns >= cap,
            "ended": status in chat.TERMINAL_STATUSES,
        }
        if shared:
            out["files_shared"] = shared
        # After yielding in a multiparty room, tell the model who's waiting so it
        # can rotate fairly (address suggest_next next time to avoid starving).
        if status in chat.YIELD_STATUSES:
            st = chat.compute_state(client, channel_id, me)
            if st.get("multiparty"):
                out["pending_requests"] = st.get("floor_requests", [])
                out["waiting"] = st.get("waiting", [])
                out["suggest_next"] = st.get("suggest_next")
                if not target:
                    out["note"] = ("Multiparty room: you yielded without a `to`, so "
                                   "anyone may answer. Address your next turn "
                                   "(to=...) to avoid collisions and starvation — "
                                   f"suggested: {st.get('suggest_next')}.")
        if status in chat.YIELD_STATUSES and wait:
            reply = chat.await_turn(client, channel_id, me, timeout=timeout)
            out["reply"] = reply
            out["next"] = chat.next_step(reply)
        else:
            out["next"] = _say_next(status)
    return out


def _say_next(status: str) -> str:
    if status in chat.TERMINAL_STATUSES:
        return "The chat has ended. You may stop."
    if status == "working":
        return ("You still hold the floor. Go do the work now; when it's done, post "
                "the results with chat_say(status='over').")
    if status == "say":
        return "You still hold the floor. Send the rest with chat_say, ending with status='over'."
    if status == "ask":
        return "Hand raised. Call chat_await to wait for the floor."
    return ("Call chat_await now to wait for the reply — keep calling it until one "
            "arrives; don't end your turn mid-chat.")


@mcp.tool()
def chat_await(
    chatter: Optional[str] = None,
    channel: Optional[str] = None,
    timeout: float = 120.0,
    poll: float = 3.0,
    nudge_after: float = 240.0,
    from_whom: Optional[str] = None,
) -> dict[str, Any]:
    """Block until a turn comes to YOU, a human interjects, or `timeout` seconds
    pass. This is how you wait for a reply — just call it and it spins
    server-side; you don't poll yourself. Pass the same `chatter` (role) you gave
    chat_begin, or omit it if you omitted it there.

    Floor rules: a turn "comes to you" when another participant `over`/`wrap`s
    and addresses you (or broadcasts), OR anyone ends the chat. A turn addressed
    to a DIFFERENT peer does not wake you — you keep holding the wait (the floor
    token). `ask` (a hand-raise), `say` and `working` never wake you. `from_whom`
    optionally waits for a yielded turn from that one specific peer.

    It never blocks on yourself: if the turn is already yours it returns at once
    (`already_received`, with that turn's text), and if your own last message was
    `say`/`working` (your turn isn't finished, so everyone is waiting on you) it
    returns `unfinished_turn` and tells you to send `over`.

    Returns a dict with:
      - from: the sender's handle, or "human", or null on timeout
      - to: who that turn was addressed to (null if broadcast/unaddressed)
      - status: their turn status (over/wrap/end/impasse), "interjection"/"stop"
        for a human message, "plain" for an out-of-band send, or null on timeout
      - text: their turn's combined body (or the human's text) — the words live
        here; `messages` is metadata-only ({id, from, to, status, timestamp,
        attachments})
      - attachments: files attached to this turn, each {url, filename,
        content_type, size, width, height, is_image}. Pass a url to
        download_attachment to fetch the bytes (needs the receive opt-in).
      - your_turn: True if it's now your turn to `chat_say`
      - ended: True if the conversation is over (their "end"/"impasse", or a
        human "stop")
      - stop_reason: "agreed" | "impasse" | "human" | null
      - timed_out: True if nothing arrived in `timeout`s. This is NOT the end —
        the other side is still thinking. Immediately call `chat_await` again to
        keep waiting. Never treat a timeout as "abandoned" or ask the human.
      - cap_reached: True if you've hit your turn cap (move toward "wrap"/"end")
      - (multiparty only, when the floor comes to you) floor, pending_requests,
        waiting, suggest_next — so you can rotate fairly and not starve a peer.
      - note (when present): what happened in plain words, e.g. what the other
        side said it's `working` on; progress: their unfinished say/working posts
      - next: the exact next step. Always do what it says.

    If the cumulative wait exceeds `nudge_after` seconds (default 240), the tool
    posts ONE visible reminder to the channel — naming who should respond, or (if
    you raised a hand and are being passed over) asking the floor holder to yield
    to you. Recovery doesn't depend on the other agent reading anything. Set
    `nudge_after=0` to disable.

    A human typing anything in the channel is surfaced (from="human"); if it
    looks like a stop command ("stop"/"halt"/"[[STOP]]") the chat ends.
    """
    cfg = config.load()
    me = _chatter(chatter, cfg)
    channel_id = config.resolve_chat_channel(cfg, channel)
    with _client("chat") as client:
        result = chat.await_turn(client, channel_id, me, timeout=timeout, poll=poll,
                                 nudge_after=nudge_after, from_whom=from_whom)
    result["next"] = chat.next_step(result)
    return result


@mcp.tool()
def chat_status(chatter: Optional[str] = None, channel: Optional[str] = None) -> dict[str, Any]:
    """Report the current chat state on a channel, derived from history. **Call
    this when (re)engaging a chat channel** — it tells you whether a turn is owed
    to you, instead of eyeballing message tags. This is how you recover from a
    stall (you ended/dropped, or the other side replied out-of-band).

    Returns:
      - session_active / ended
      - participants: handles in the CURRENT chat (since the last end/stop),
        case-insensitive, minus anyone silent for 30+ minutes
      - multiparty: True if >2 participants
      - last_turn: {from, to, status, id, ts} — the most recent completed turn
      - pending_turn: the last `over`/`wrap` turn awaiting an answer, or null
      - floor: who may speak next (the pending turn's addressee; in a 2-party
        chat the other party; null = open floor / nobody owes a turn)
      - floor_requests: [{from}] outstanding hand-raises (`ask`), oldest first
      - waiting: everyone except the floor holder, ranked most-starved first
        (longest since they last took a turn). A fairness ORDER, not a list of
        sessions actually blocked in chat_await.
      - suggest_next: the fair next addressee in a multiparty room (an
        outstanding request, else the most-starved non-speaker), or null
      - your_turn (if `chatter` is given or this project has a fixed handle):
        True if the pending turn is owed to you — it's addressed to you (or broadcast) and isn't your own.

    If your_turn is True: call `chat_begin` (it repositions you to receive the
    pending turn) then `chat_await`, or `chat_say` if already in the session.

    3+ chatters: supported via addressing + a derived floor. Address turns with
    `chat_say(..., to="handle")` so the floor passes to exactly one peer; raise a
    hand with `status="ask"` when someone else holds the floor. Leave `to` unset
    only in a 2-party chat (or to deliberately broadcast — anyone may answer).
    """
    cfg = config.load()
    channel_id = config.resolve_chat_channel(cfg, channel)
    me = (handles.resolve(chatter, cfg)[0]
          if chatter not in (None, "") or cfg.get("chat_handle") else None)
    with _client("chat") as client:
        st = chat.compute_state(client, channel_id, me)
    public = {k: v for k, v in st.items() if not k.startswith("_")}
    public["channel"] = channel_id
    return public


def main() -> None:
    use_system_certs()
    mcp.run()


if __name__ == "__main__":
    main()
