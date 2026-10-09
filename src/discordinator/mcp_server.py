"""MCP server exposing Discord send/read tools.

Run with the ``discordinator-mcp`` entry point (stdio transport). Configure it
in an MCP client (e.g. Claude Code / Claude Desktop) like:

    {
      "mcpServers": {
        "discordinator": {
          "command": "discordinator-mcp",
          "env": { "DISCORD_BOT_TOKEN": "...",
                   "DISCORDINATOR_RELAY_TRANSPORT": "discord",
                   "DISCORDINATOR_CHAT_TRANSPORT": "discord" }
        }
      }
    }

Channels can be referenced by the friendly names stored in the Discordinator
config file, or by raw numeric channel ids.
"""

from __future__ import annotations

import difflib
import atexit
import functools
import inspect
import io
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional, Union

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import chat, config, events, handles, use_system_certs
from .client_factory import Client, make_client, make_client_for_url, purge, purge_targets, read_new
from .discord_client import DiscordError, simplify_message

# Keep the HTTP client quiet: it logs an INFO line per request to stderr, which
# is noise for a stdio MCP server (stdout carries the JSON-RPC protocol).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

mcp = MCPServer("discordinator")


# Tool calls in progress: a disconnect lets them finish (a long turn going out
# piece by piece) before the server exits.
_running = 0
_running_lock = threading.Lock()
FINISH_WAIT = 20.0  # seconds, at most


def _tool():
    """Register a tool so its errors reach the model in full. The MCP SDK passes
    on only a ToolError's text; anything else arrives as a bare "Error executing
    tool <name>", and every "nothing was posted, because ..." we write would be
    lost. The module keeps the plain function (tests call it directly)."""
    def register(fn):
        in_room = "channel" in inspect.signature(fn).parameters

        @functools.wraps(fn)
        def reported(*args, **kwargs):
            global _running
            with _running_lock:
                _running += 1
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - re-raised with its text
                _log_error(fn.__name__, e, kwargs, in_room)
                if isinstance(e, ToolError):
                    raise
                raise ToolError(f"{type(e).__name__}: {e}") from e
            finally:
                with _running_lock:
                    _running -= 1
        mcp.tool()(reported)
        return fn
    return register


def _me_now() -> Optional[str]:
    """This session's chat handle if it has one yet (for the event log)."""
    return next(iter(handles._resolved.values()), None)


def _log_error(tool: str, e: Exception, kwargs: dict[str, Any], in_room: bool = True) -> None:
    """Log a failed tool call; ``in_room`` False for a tool that acts on no
    room (whoami, list_channels, download_attachment)."""
    first = (str(e).strip().splitlines() or [type(e).__name__])[0]
    events.record("error", tool=tool, message=first[:200], handle=_me_now(),
                  room=_event_room(tool, kwargs.get("channel")) if in_room else None)


def _event_room(tool: str, channel: Optional[str]) -> Optional[str]:
    """The room a tool call acted on, as `watch` names it (the resolved
    channel, so an error made with the default room shows up in that room)."""
    try:
        cfg = config.load()
        if tool.startswith("chat_"):
            return config.resolve_chat_channel(cfg, channel)
        return config.resolve_channel(cfg, channel)
    except Exception:
        return channel


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


@_tool()
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

    Sent to a chat room where this session owes a chat reply, it goes out as
    that reply (a chat turn back to whoever handed you the turn), so a reply
    sent with the wrong tool can't be mistaken for someone else's.
    """
    cfg = config.load()
    channel_id = config.resolve_channel(cfg, channel)
    owed = _owed_chat_reply(cfg, channel_id)
    if owed is not None:
        me, peer = owed
        out = chat_say(text=text, chatter=me, channel=channel_id, to=peer, wait=False)
        return (f"Sent as your chat turn to {peer} ({out['sent_messages']} message(s), "
                f"ids: {', '.join(out.get('message_ids', []))}) - "
                "you owed them a reply in this chat room. Use chat_say for chat turns; "
                "call chat_await to wait for their answer.")
    tag = label if label is not None else cfg.get("machine_label")
    with _client("relay") as client:
        sent = client.send_message(channel_id, text, label=tag)
    _remember_sent(cfg, sent, channel_id)
    ids = ", ".join(str(m.get("id")) for m in sent)
    return f"Sent {len(sent)} message(s) to channel {channel_id} (ids: {ids})."


# Ids of the relay messages this session sent, so its own inbox skips exactly
# those - not everything carrying its label, which another session on the
# machine may share. Also kept under the session (its Claude Code process), so
# a restarted server doesn't hand the session back its own messages.
_sent_ids: set[str] = set()


def _reader(cfg: dict[str, Any]) -> Optional[str]:
    """This session's relay read position key."""
    handles.restore()
    return config.relay_reader(cfg, handles.current(None, cfg) if handles._resolved else None)


# This server's posts, for delete_messages (also kept under the session).
_my_posts: list[dict[str, Any]] = []


def _remember_post(mode: str, channel_id: str, sent: list[dict[str, Any]],
                   handle: Optional[str] = None) -> list[str]:
    """Record a post (all its pieces) as this session's; returns its ids."""
    ids = [str(m.get("id")) for m in sent if isinstance(m, dict) and m.get("id") is not None]
    if not ids:
        return ids
    entries = [{"id": i, "channel": str(channel_id), "mode": mode, "post": ids[0],
                **({"handle": handle} if handle else {})} for i in ids]
    _my_posts.extend(entries)
    session = handles.session_key()
    if session is not None:
        try:
            config.note_posts(session, entries)
        except OSError:
            pass  # this server still remembers them
    return ids


def _remember_sent(cfg: dict[str, Any], sent: list[dict[str, Any]],
                   channel_id: Optional[str] = None) -> None:
    if channel_id is not None:
        _remember_post("relay", channel_id, sent)
    ids = [str(m.get("id")) for m in sent if isinstance(m, dict)]
    _sent_ids.update(ids)
    session = handles.session_key()
    if session is None:
        return  # no way to tell this session's next server; memory only
    try:
        config.note_sent(session, ids)
    except OSError:
        pass  # the in-memory list still covers this server


def _owed_chat_reply(cfg: dict[str, Any], channel_id: str) -> Optional[tuple[str, str]]:
    """(my handle, the peer) if this session is chatting in this room and owes
    a reply there - a send_message there is that reply. Else None."""
    if not handles._resolved:
        return None  # this session hasn't chatted
    try:
        me = handles.current(None, cfg)
        if not me or not chat.get_cursor(channel_id, me):
            return None
        with _client("chat") as client:
            st = chat.compute_state(client, channel_id, me)
    except Exception:
        return None
    owed = st.get("_owed_turn")
    if not st.get("your_turn") or not owed or owed["from"] in ("human", "participant"):
        return None
    return me, owed["from"]


@_tool()
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


@_tool()
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
    _remember_sent(cfg, sent, channel_id)
    ids = ", ".join(str(m.get("id")) for m in sent)
    return (f"Sent {len(sent)} message(s) with {len(file_list)} file(s) to channel "
            f"{channel_id} (ids: {ids}).")


@_tool()
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


@_tool()
def get_new_messages(
    channel: Optional[str] = None,
    include_self: bool = False,
    limit: int = 50,
    ack: Optional[bool] = None,
) -> list[dict[str, Any]]:
    """Get only NEW messages since the last time this tool was called for the
    channel — the relay primitive for two-way session handoff.

    A per-channel cursor is stored and advanced on each call, so repeated calls
    return only fresh messages (not the whole history). Each session (label +
    its chat handle, e.g. "CodeCarver/ui") has its own cursor, so two sessions
    on one machine don't consume each other's messages - for two sessions of
    one project that only relay (never chat), give each its own
    DISCORDINATOR_LABEL. By default the messages this session sent
    are filtered out, so you see just what the other session/machine said -
    another session on this machine is someone else, even with the same label.

    Args:
        channel: A configured channel name or raw channel id. Defaults to the
            configured default channel.
        include_self: If true, also include the messages this session sent.
        limit: How many recent messages to return on the FIRST call (before a
            cursor exists). Later calls return what's new since, up to 100
            per call - call again until it comes back empty to catch up.
        ack: React ✅ to the newest returned message so the other side can see
            it was read (needs Add Reactions permission). Defaults to the
            configured ack_on_read setting when omitted.

    Returns simplified message objects in chronological order.
    """
    cfg = config.load()
    channel_id = config.resolve_channel(cfg, channel)
    reader = _reader(cfg)
    if ack is None:
        ack = bool(cfg.get("ack_on_read"))

    capped = max(1, min(int(limit), 100))
    with _client("relay") as client:
        messages = read_new(client, channel_id, reader, capped)
        if not include_self:
            session = handles.session_key()
            mine = _sent_ids | (config.sent_ids(session) if session else set())
            messages = [m for m in messages if str(m["id"]) not in mine]

        if ack:
            _try_ack(client, channel_id, messages)
    return messages


def _all_my_posts() -> list[dict[str, Any]]:
    """This session's posts, oldest first: this server's, plus its earlier
    servers' (kept under the session)."""
    session = handles.session_key()
    saved = config.my_posts(session) if session is not None else []
    seen: set[str] = set()
    out = []
    for e in saved + _my_posts:
        if str(e.get("id")) not in seen:
            seen.add(str(e.get("id")))
            out.append(e)
    return out


@_tool()
def delete_messages(message_ids: Optional[Union[str, list[str]]] = None,
                    channel: Optional[str] = None) -> dict[str, Any]:
    """Delete messages YOU (this session) posted - to fix a mistake, then post
    the corrected version. With no `message_ids`, deletes your most recent post
    (every piece of a long one). Ids come back from send_message, send_file and
    chat_say (`message_ids`); naming one piece deletes its whole post.

    Only your own messages: anything else is refused. To clear a channel of
    everyone's messages, that's a purge (purge_messages).

    A deleted chat turn is gone from the chat (if it was your turn you hold the
    floor again) - but if someone already read it, they've seen it: the result
    says when others have posted since, so you can send a correction too.

    Args:
        message_ids: id(s) of your messages to delete (default: your latest post).
        channel: with no ids, your latest post in this channel (default: anywhere).
    """
    cfg = config.load()
    posts = _all_my_posts()
    if channel is not None:
        rooms = {config.resolve_channel(cfg, channel)}
        try:
            rooms.add(config.resolve_chat_channel(cfg, channel))
        except config.ConfigError:
            pass
        posts = [e for e in posts if e.get("channel") in rooms]
    if message_ids in (None, "", []):
        if not posts:
            raise ValueError("You haven't posted anything this session can delete"
                             + (" in that channel." if channel else "."))
        wanted = {posts[-1]["post"]}
    else:
        ids = [message_ids] if isinstance(message_ids, str) else [str(i) for i in message_ids]
        by_id = {str(e["id"]): e for e in posts}
        foreign = [i for i in ids if i not in by_id]
        if foreign:
            raise ValueError(
                f"Not your message(s): {', '.join(foreign)}. You can only delete what "
                "this session posted; nothing was deleted. Clearing a channel of "
                "everyone's messages is a purge (purge_messages).")
        wanted = {by_id[i]["post"] for i in ids}
    doomed = [e for e in posts if e["post"] in wanted]
    groups: dict[tuple[str, str], list[str]] = {}
    poster: dict[tuple[str, str], Optional[str]] = {}
    for e in doomed:
        groups.setdefault((e["mode"], e["channel"]), []).append(str(e["id"]))
        poster[(e["mode"], e["channel"])] = e.get("handle")
    deleted, notes = 0, []
    for (mode, channel_id), ids in groups.items():
        with _client(mode) as client:
            if hasattr(client, "delete_messages"):
                deleted += client.delete_messages(channel_id, ids)
            else:
                for i in ids:
                    try:
                        client.delete_message(channel_id, i)
                        deleted += 1
                    except DiscordError as e:
                        if "(404)" not in str(e):  # already gone is fine
                            raise
            if mode == "chat":
                since = [chat.parse_msg(simplify_message(m)) for m in
                         client.read_messages(channel_id, limit=100, after=max(ids, key=int))]
                me = poster[(mode, channel_id)] or handles._current
                others = sorted({p["participant"] for p in since if p is not None
                                 and not chat.same_handle(p["participant"], me)})
                notes.append(
                    f"{', '.join(others)} posted since - they may have read it already; "
                    "post a correction as your next turn." if others else
                    "Nobody had replied to it - post the corrected version now.")
    gone = {str(e["id"]) for e in doomed}
    _my_posts[:] = [e for e in _my_posts if str(e.get("id")) not in gone]
    session = handles.session_key()
    if session is not None:
        config.forget_posts(session, gone)
    out: dict[str, Any] = {"deleted": deleted, "message_ids": sorted(gone, key=int)}
    if notes:
        out["note"] = " ".join(notes)
    return out


@_tool()
def purge_messages(
    channel: Optional[str] = None,
    dry_run: bool = True,
    older_than_days: float = 0,
    scan_limit: Optional[int] = None,
) -> dict[str, Any]:
    """Purge a channel: delete EVERY message in it - every machine's and
    session's, a human's, interjections - any chat going on there included.

    dry_run=True (default) only reports how many would go; call again with
    dry_run=False to delete. On Discord, messages a person typed need the bot to
    have the Manage Messages permission (the result says if any were left).

    Args:
        channel: Configured channel name or raw id. Defaults to the default channel.
        dry_run: If true (default), report but do not delete.
        older_than_days: Only messages older than this many days (0 = all, default).
        scan_limit: Only the newest this many messages (default: all of them).

    Returns a summary: how many would be / were deleted, and anything left.
    """
    cfg = config.load()
    channel_id = config.resolve_channel(cfg, channel)
    with _client("relay") as client:
        matched = purge_targets(client, channel_id, older_than_days * 86400 or None, scan_limit)
        if dry_run:
            return {"dry_run": True, "would_delete": len(matched),
                    "message_ids": [m["id"] for m in matched]}
        deleted, problems = purge(client, channel_id, matched)
    out: dict[str, Any] = {"dry_run": False, "deleted": deleted}
    if problems:
        out["not_deleted"] = problems
    return out


@_tool()
def list_channels() -> dict[str, Any]:
    """List the friendly channel names configured for Discordinator."""
    cfg = config.load()
    return {
        "default_channel": cfg.get("default_channel"),
        "channels": cfg.get("channels") or {},
        "machine_label": cfg.get("machine_label"),
    }


@_tool()
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


def _chatter(chatter: Optional[str], cfg: dict[str, Any],
             channel: Optional[str] = None, fresh: bool = False) -> tuple[str, Optional[str]]:
    """This session's handle (see handles.resolve): the project's fixed handle,
    optionally with a role (`CodeCarver/ui`), made unique among live sessions on
    this machine. Plus a note when it isn't the name the session asked for -
    returned on every chat result so a rename can't go unnoticed.

    A restarted server normally picks its session's name back up (see
    handles.restore). A brand-new session (quit and resumed) can't, so if it
    doesn't name a role and exactly one `<project handle>/<role>` that no
    running session holds is owed a turn in the room - or is waiting on its own
    unanswered turn there - that was this session: it takes the name back
    instead of becoming the bare project handle and never seeing the reply."""
    role = _lost_role(chatter, cfg, channel)
    if role is None:
        return handles.resolve(chatter, cfg, fresh=fresh and chatter not in (None, ""))
    me, note = handles.resolve(role, cfg)
    back = (f"'{me}' has a chat going in this room that no running session holds, "
            "so this session has taken that name back (its role was forgotten when "
            f"it restarted). Pass chatter=\"{role}\" on your chat calls.")
    return me, f"{note} {back}" if note else back


def _lost_role(chatter: Optional[str], cfg: dict[str, Any],
               channel: Optional[str]) -> Optional[str]:
    """The role to rejoin as (see _chatter), or None."""
    base = cfg.get("chat_handle")
    if chatter not in (None, ""):
        return None  # it named itself
    handles.restore()
    if handles._last_chatter is not None or handles._resolved or not base:
        return None  # already has a name in this session
    prefix = chat.handle_key(base) + "/"
    try:
        channel_id = config.resolve_chat_channel(cfg, channel)
        with _client("chat") as client:
            lost = _lost_turns(client, channel_id, chat.sanitize_handle(base), cfg)
            mine = {chat.handle_key(h): h for h in lost
                    if chat.handle_key(h).startswith(prefix)}
            mine.update({chat.handle_key(h): h for h in _left_waiting(client, channel_id, base)})
    except Exception:
        return None
    return next(iter(mine.values()))[len(prefix):] if len(mine) == 1 else None


def _left_waiting(client: Any, channel_id: str, base: str) -> list[str]:
    """`<base>/<role>` handles no running session holds whose own turn in the
    room is still waiting for its answer - a session that restarted while
    waiting for a reply."""
    prefix = chat.handle_key(base) + "/"
    held = {chat.handle_key(h) for h in handles.live_handles()}
    seen: dict[str, str] = {}
    for m in client.read_messages(channel_id, limit=100):
        p = chat.parse_msg(simplify_message(m))
        if p and chat.handle_key(p["participant"]).startswith(prefix):
            seen.setdefault(chat.handle_key(p["participant"]), p["participant"])
    out = []
    for k, h in seen.items():
        if k in held:
            continue
        st = chat.compute_state(client, channel_id, h)
        pending = st.get("pending_turn")
        if (pending and chat.same_handle(pending["from"], h) and not st.get("ended")
                and not st.get("your_turn")):
            out.append(h)
    return out


def _lost_turns(client: Any, channel_id: str, me: str, cfg: dict[str, Any]) -> list[str]:
    """Handles that look like this session's own earlier name - another role of
    its project, the bare project handle, or its role used as a whole handle
    (the name before the project got a fixed handle) - that a turn in the room
    is owed to and no running session on this machine holds."""
    base = cfg.get("chat_handle")
    me_key = chat.handle_key(me)
    looks: set[str] = set()
    if base:
        b = chat.handle_key(base)
        looks.add(b)
        if me_key.startswith(b + "/"):
            looks.add(me_key[len(b) + 1:])  # "convex" for "ProjectB/convex"
    held = {chat.handle_key(h) for h in handles.live_handles()}
    found: dict[str, str] = {}
    for p in chat._room(client, channel_id, me)[1]:
        if not p or p["status"] not in chat.YIELD_STATUSES or chat._is_broadcast(p["to"]):
            continue
        k = chat.handle_key(p["to"])
        if k == me_key or k in held or k in found:
            continue
        if k in looks or (base and k.startswith(chat.handle_key(base) + "/")):
            found[k] = p["to"]
    return [h for h in found.values()
            if chat.compute_state(client, channel_id, h).get("your_turn")]


def _lost_next(lost: list[str], cfg: dict[str, Any]) -> str:
    """The step for a session whose turn went to its old name (see _lost_note)."""
    return ("A turn in this room went to a name that looks like yours from before a "
            "restart (see handle_note). If that was you, do what handle_note says "
            "now - waiting under your current name won't receive it. Otherwise call "
            "chat_await again.")


def _lost_note(lost: list[str], cfg: dict[str, Any]) -> str:
    base = cfg.get("chat_handle")
    how = []
    for h in lost:
        k = chat.handle_key(h)
        if base and k.startswith(chat.handle_key(base) + "/"):
            how.append(f"'{h}' (chat_begin(chatter=\"{h[len(base) + 1:]}\") takes it back)")
        elif base and k == chat.handle_key(base):
            how.append(f"'{h}' (chat_begin(chatter=\"{base}\") takes it back)")
        else:
            how.append(f"'{h}' (read it with read_messages and answer its sender with "
                       "chat_say(to=...))")
    return ("A turn in this room is owed to " + ", ".join(how) + ", and no running "
            "session holds that name. If that was you (before a restart or a handle "
            "change), take it back as shown - otherwise you won't see the turn.")


@_tool()
def chat_begin(chatter: Optional[str] = None, channel: Optional[str] = None, turn_cap: int = 20) -> dict[str, Any]:
    """Start or join a turn-based chat as participant `chatter`.

    Seeds your read position to *now* (prior history is ignored) and resets your
    turn counter. BOTH participants call this first (two sessions of the same
    project each pass their own role as `chatter`; see below). Then the
    initiator calls `chat_say`; the other
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
            With no chat channel set, falls back to the relay default channel.
        turn_cap: soft cap on your turns before you're nudged to wrap up.
    """
    cfg = config.load()
    me, note = _chatter(chatter, cfg, channel, fresh=True)
    channel_id = config.resolve_chat_channel(cfg, channel)
    with _client("chat") as client:
        st = chat.compute_state(client, channel_id, me)
        lost = [] if st.get("your_turn") else _lost_turns(client, channel_id, me, cfg)
        # A turn already owed to me (e.g. I ended/dropped and the other side
        # spoke again) is re-delivered by chat_await - recovery instead of a
        # silent stall; a long turn in progress arrives whole.
        cursor = chat.seed_cursor(client, channel_id, me)
    chat.reset(channel_id, me, cursor, turn_cap)
    chat.note_room(channel_id, config.is_local(cfg, "chat"))
    owed = bool(st.get("your_turn"))
    out = {
        "channel": channel_id, "chatter": me, "turn_cap": turn_cap,
        "recovered_pending_turn": owed,
        "state": {k: v for k, v in st.items() if not k.startswith("_")},
        "next": ("A turn is owed to you — call chat_await now to receive it." if owed
                 else "The last chat here was stopped by the human. Start a new one "
                      "only if you've been asked to (initiator: chat_say(...); other: "
                      "chat_await(...))." if st.get("stop_reason") == "human"
                 else "initiator: chat_say(...); other: chat_await(...)"),
    }
    others = st.get("_others_chatting", [])
    joined = any(chat.same_handle(p, me) for p in st.get("_posters", []))
    if lost:
        note = f"{note} {_lost_note(lost, cfg)}" if note else _lost_note(lost, cfg)
    elif others and not owed and not joined:
        busy = (f"Another chat is going on in this room ({', '.join(others)}). Address "
                "your opener (to='<your peer>') so it reaches the right session.")
        note = f"{note} {busy}" if note else busy
    if note:
        out["note"] = note
    events.record("joined", handle=me, room=channel_id, recovered=owed or None)
    if note and (lost or "name" in note):
        events.record("renamed" if not lost else "lost_turn", handle=me, room=channel_id,
                      note=note[:200])
    return out


@_tool()
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
                  or to end unilaterally). Terminal - for your conversation,
                  not other chats in the same room.
      - "impasse" we're stuck — stop and get the human. Terminal.

    `to`: address this turn to ONE participant by handle (e.g. to="C"). Required
    discipline in a 3+ party room — an addressed `over`/`wrap` passes the floor to
    exactly that peer, so only they wake. Omitted, the turn is addressed to
    whoever handed you the turn (so a reply goes back to its asker, even with
    other chats in the room); with nobody to reply to (an opener) it's open to
    anyone - so address an opener when you know your peer. Special targets
    all/everyone/* broadcast explicitly. If `to` names nobody known (nobody by
    that name has posted, and no live session here has it), it returns at once
    with a `note` ("did you mean ...?") instead of waiting.

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
    was your turn, it still is - fix the problem and call chat_say again. (One
    exception, also spelled out in the error: a long message that failed
    part-way had its first pieces posted as `say` - send just the rest.) It
    refuses on purpose when something for you arrived that you haven't read
    (call chat_await first) or when an unaddressed turn would talk over someone
    else's floor. If it returns `posted: true` with an `error`, the message WAS
    posted and something failed afterwards - don't send it again; follow `next`.
    """
    cfg = config.load()
    try:
        me, handle_note = _chatter(chatter, cfg, channel)
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
        # Check-then-post under a lock, so two sessions on this machine answering
        # the same message at the same moment can't both get through the check.
        lock = config.FileLock(_post_lock(channel_id), timeout=30)
        try:
            lock.__enter__()
        except config.LockTimeout as e:
            raise ChatSendError(
                f"{e}\n\nAnother session in this room is busy posting. Nothing was "
                "posted - send it again in a moment.") from e
        try:
            try:
                target, auto_note = _check_turn(client, channel_id, me, status, target)
            except Exception as e:
                raise ChatSendError(
                    f"{e}\n\nNothing was posted, so nobody saw this message.") from e
            chat.note_room(channel_id, config.is_local(cfg, "chat"))
            target = _expand_bare(client, channel_id, me, target)
            to_note = _unknown_to_note(client, channel_id, me, target)
            try:
                sent = chat.send_chat(client, channel_id, me, status, text, to=target,
                                      files=file_list)
            except Exception as e:
                _remember_post("chat", channel_id, getattr(e, "chat_sent", []), me)
                raise _send_failed(e, status) from e
            message_ids = _remember_post("chat", channel_id, sent, me)
        finally:
            lock.__exit__(None, None, None)
        try:
            out = _after_post(client, channel_id, me, status, target, sent, shared,
                              handle_note, auto_note, to_note, wait, timeout)
            out["message_ids"] = message_ids
            return out
        except Exception as e:
            # The message IS out - the others can see it. Say so, so it isn't
            # sent twice; the reply (if any) is fetched with chat_await.
            out = {"sent_messages": len(sent), "message_ids": message_ids,
                   "status": status, "to": target, "posted": True, "ended": status in chat.TERMINAL_STATUSES,
                   "error": f"{type(e).__name__}: {e}"}
            if status in chat.TERMINAL_STATUSES:
                out["next"] = "The chat has ended. You may stop."
            else:
                out["next"] = ("Your message WAS posted - don't send it again. Something "
                               "failed afterwards (see `error`). " + _say_next(status))
            return out


def _post_lock(channel_id: str) -> Path:
    """The lock that serializes check-then-post in one room on this machine."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(channel_id))[:80]
    return config.state_path().parent / "locks" / f"post-{safe}"


def _send_failed(e: Exception, status: str) -> ChatSendError:
    """What to tell the model when posting failed, part-way or before anything."""
    done = getattr(e, "chat_pieces_sent", 0)
    if done:
        total = getattr(e, "chat_pieces_total", "?")
        rest = getattr(e, "chat_rest", "")
        return ChatSendError(
            f"{e}\n\nOnly the first {done} of {total} pieces of this long "
            "message were posted (as status 'say', so the others are still "
            "waiting on you). Don't resend those: send just the rest, which "
            f"starts: \"{rest[:80]}\", with chat_say(..., status='{status}').")
    return ChatSendError(
        f"{e}\n\nNothing was posted, so nobody saw this message. If it was "
        "your turn, it still is - the others are waiting on you. Fix this and "
        "send it again with chat_say(...) before calling chat_await.")


def _check_turn(client: Client, channel_id: str, me: str, status: str,
                target: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Checks before posting a turn. Returns the address to use (an unaddressed
    reply goes back to whoever handed me the turn) and a note if it was filled
    in. Raises - so nothing is posted - when posting now would be talking over
    someone: something new for me arrived that I haven't read, or another
    participant holds the floor in a conversation I'm not part of."""
    if status in chat.TERMINAL_STATUSES or status == "ask":
        # Ending or raising a hand is always allowed. An ending goes to the one
        # I'm talking with, like a reply (it ends our conversation, not others').
        if target is None and status in chat.TERMINAL_STATUSES:
            target = chat.get_reply_to(channel_id, me)
        return target, None
    if not chat.get_cursor(channel_id, me):
        # Never joined (no chat_begin) and speaking now: start reading from
        # here, so the reply wait doesn't pick up turns from an older chat.
        latest = client.read_messages(channel_id, limit=1)
        chat.set_cursor(channel_id, me, latest[0]["id"] if latest else "0")
    st = chat.compute_state(client, channel_id, me)
    mid_turn = any(chat.same_handle(x["from"], me) for x in st.get("progress", []))
    # (Finishing my own say/working turn is never blocked: I hold the floor,
    # and chat_await would only tell me to finish it first.)
    unread = [] if mid_turn else chat.unread_for_me(client, channel_id, me)
    if unread:
        m = unread[0]
        p = chat.parse_msg(m)
        who = "A human" if p is None else p["participant"]
        raise RuntimeError(
            f"{who} posted something you haven't read yet (\"{(p['body'] if p else m['content'])[:80]}\"). "
            "Call chat_await to read it first, then reply.")
    if target is None and not st.get("your_turn") and status in chat.YIELD_STATUSES:
        # Two roles of my project both took a turn sent to our bare project
        # handle (one on another machine): it was for whichever answered first.
        sender = chat.get_reply_to(channel_id, me)
        taken = chat.bare_taken(client, channel_id, me, sender) if sender else None
        if taken:
            raise RuntimeError(
                f"{sender}'s turn to your project's name went to {taken}, which "
                "answered it first - it was theirs, not yours. Call chat_await to "
                "wait for a turn meant for you, or pass to=... to say something anyway.")
    note = None
    if target is None:
        back = chat.get_reply_to(channel_id, me)
        if back:
            target = back
            note = (f"Addressed to {back} (who handed you the turn). Pass to=... to "
                    "pick someone else, or to='all' to let anyone answer.")
    if target is None and status in chat.YIELD_STATUSES:
        pend, floor = st.get("pending_turn"), st.get("floor")
        if (pend and floor and not st.get("your_turn")
                and not chat._is_broadcast(pend.get("to"))
                and not chat.same_handle(floor, me)
                and not chat.same_handle(pend["from"], me)):
            raise RuntimeError(
                f"{floor} has the floor ({pend['from']} handed it to them), so an "
                "unaddressed turn would talk over them. To join their conversation, "
                "raise a hand with status='ask'. To talk to someone else in this room, "
                "address it: to='<their handle>'.")
    return target, note


def _after_post(client: Client, channel_id: str, me: str, status: str,
                target: Optional[str], sent: list, shared: list,
                handle_note: Optional[str], auto_note: Optional[str],
                to_note: Optional[str], wait: bool, timeout: float) -> dict[str, Any]:
    """Everything chat_say does once the message is out: count the turn, report
    the room, and wait for the reply."""
    # `say`/`ask` don't complete a turn, so they don't count against the cap.
    took_turn = status not in chat.NON_TURN_STATUSES
    turns = chat.bump_turn(channel_id, me) if took_turn else chat.get_meta(channel_id, me)[0]
    _, cap = chat.get_meta(channel_id, me)
    out = {
        "sent_messages": len(sent),
        "message_ids": [str(m.get("id")) for m in sent if isinstance(m, dict)],
        "status": status,
        "to": target,
        "my_turns": turns,
        "turn_cap": cap,
        "cap_reached": turns >= cap,
        "ended": status in chat.TERMINAL_STATUSES,
    }
    if shared:
        out["files_shared"] = shared
    if handle_note:
        out["handle_note"] = handle_note
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
    if auto_note and "note" not in out:
        out["note"] = auto_note
    if to_note:
        # Probably a typo: waiting would just sit there, so say so now.
        out["note"] = to_note
        out["next"] = (f"Check `to`: if '{target}' is right, call chat_await to "
                       "wait for their reply; if not, re-send to the right handle.")
    elif status in chat.YIELD_STATUSES and wait:
        reply = chat.await_turn(client, channel_id, me, timeout=timeout)
        out["reply"] = reply
        out["next"] = chat.next_step(reply)
    else:
        out["next"] = _say_next(status)
    return out


def _expand_bare(client: Client, channel_id: str, me: str,
                 target: Optional[str]) -> Optional[str]:
    """`to` with a bare project handle ("ProjectB") spelled out as the one
    session of that project in the room or running here ("ProjectB/convex"),
    so the turn names who it's for (see chat.bare_aliases)."""
    if not target or chat._is_broadcast(target) or "/" in target:
        return target
    msgs = [simplify_message(m) for m in reversed(client.read_messages(channel_id, limit=100))]
    full = chat.bare_target([chat.parse_msg(m) for m in msgs],
                            [m.get("timestamp") for m in msgs], me, target,
                            handles.live_claims)
    return full if full and not chat.same_handle(full, me) else target


def _unknown_to_note(client: Client, channel_id: str, me: str,
                     target: Optional[str]) -> Optional[str]:
    """A warning when `to` names nobody known: not anyone who has posted in this
    chat, nor a live session on this machine. (Could still be right - a peer on
    another machine that hasn't spoken yet - so warn, don't refuse.)"""
    if not target or chat._is_broadcast(target):
        return None
    st = chat.compute_state(client, channel_id, me)
    known = [h for h in st.get("_room_posters", []) + handles.live_handles()
             if not chat.same_handle(h, me)]
    if any(chat.same_handle(target, h) for h in known):
        return None
    if chat.has_posted(client, channel_id, target):
        return None  # a peer (maybe on another machine) who spoke further back
    names = sorted({chat.handle_key(h): h for h in known}.values())
    close = difflib.get_close_matches(target.casefold(),
                                      [h.casefold() for h in names], n=1, cutoff=0.6)
    hint = next((h for h in names if close and h.casefold() == close[0]), None)
    return (f"Nobody called '{target}' has posted in this room and no live session "
            f"on this machine has that name"
            + (f" - did you mean '{hint}'?" if hint else ".")
            + (f" Known: {', '.join(names)}." if names else "")
            + " Your message was sent, but only they will be woken by it.")


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


@_tool()
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
    and addresses you (or broadcasts), OR someone in your chat ends it. Other
    conversations in the same room never wake you. A turn addressed
    to a DIFFERENT peer does not wake you — you keep holding the wait (the floor
    token). `ask` (a hand-raise), `say` and `working` never wake you. `from_whom`
    optionally waits for a yielded turn from that one specific peer; a turn for
    you from anyone else meanwhile is kept, and your next chat_await returns it.

    It never blocks on yourself: if the turn is already yours it returns at once
    (`already_received`, with that turn's text), and if your own last message was
    `say`/`working` (your turn isn't finished, so everyone is waiting on you) it
    returns `unfinished_turn` and tells you to send `over`.

    Returns a dict with:
      - from: the sender's handle, or "human", or null on timeout
      - to: who that turn was addressed to (null if broadcast/unaddressed)
      - status: their turn status (over/wrap/end/impasse), "interjection"/"stop"
        for a human message, or null on timeout (an untagged bot post - a
        relay message - is never a turn and never wakes you)
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
    me, handle_note = _chatter(chatter, cfg, channel)
    channel_id = config.resolve_chat_channel(cfg, channel)
    with _client("chat") as client:
        result = chat.await_turn(client, channel_id, me, timeout=timeout, poll=poll,
                                 nudge_after=nudge_after, from_whom=from_whom)
        # Waiting in vain because the turn went to this session's old name?
        lost = _lost_turns(client, channel_id, me, cfg) if result.get("timed_out") else []
    result["next"] = chat.next_step(result)
    if lost:
        handle_note = f"{handle_note} {_lost_note(lost, cfg)}" if handle_note else _lost_note(lost, cfg)
        result["next"] = _lost_next(lost, cfg)
        events.record("lost_turn", handle=me, room=channel_id, note=_lost_note(lost, cfg)[:200])
    if handle_note:
        result["handle_note"] = handle_note
    return result


@_tool()
def chat_status(chatter: Optional[str] = None, channel: Optional[str] = None) -> dict[str, Any]:
    """Report the current chat state on a channel, derived from history. **Call
    this when (re)engaging a chat channel** — it tells you whether a turn is owed
    to you, instead of eyeballing message tags. This is how you recover from a
    stall (you ended/dropped, or restarted and lost track).

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
    role = _lost_role(chatter, cfg, channel)
    me = handles.current(role or chatter, cfg)  # read-only: never claims a name
    with _client("chat") as client:
        st = chat.compute_state(client, channel_id, me)
        lost = (_lost_turns(client, channel_id, me, cfg)
                if me and not st.get("your_turn") else [])
    public = {k: v for k, v in st.items() if not k.startswith("_")}
    public["channel"] = channel_id
    if role is not None:
        public["chatter"] = me
        public["note"] = (f"A turn here is owed to '{me}', which no running session "
                          "holds - you, before a restart. Your next chat call takes "
                          "that name back.")
    elif lost:
        public["note"] = _lost_note(lost, cfg)
    return public


class _WatchedStdin(io.TextIOWrapper):
    """The server's stdin, noticing when the client hangs up (EOF)."""

    def readline(self, *args: Any) -> str:  # type: ignore[override]
        line = super().readline(*args)
        if not line:
            _disconnected()
        return line


_exit_logged = False


def _log_exit() -> None:
    global _exit_logged
    if not _exit_logged:
        _exit_logged = True
        try:
            project = None if _me_now() else config.load().get("chat_handle")
        except Exception:  # noqa: BLE001 - a bad config mustn't stop the exit
            project = None
        events.record("server_exit", handle=_me_now(), project=project)


def _disconnected() -> None:
    """The client closed the connection (an /mcp reconnect, the session
    ending). Stop now: a chat_await still waiting would otherwise keep this
    process - and its claim on the session's name - alive for minutes, and
    could read the reply meant for the session's new server."""
    chat.SHUTDOWN.set()

    def finish() -> None:
        # Let calls in progress finish - a chat_await returns at once; a long
        # post would otherwise stop halfway, leaving its peer an unfinished turn.
        time.sleep(1.0)
        deadline = time.monotonic() + FINISH_WAIT
        while _running and time.monotonic() < deadline:
            time.sleep(0.1)
        handles.release_all()
        _log_exit()
        os._exit(0)

    threading.Thread(target=finish, daemon=True).start()


def _watch_stdin() -> None:
    """Serve over a stdin that reports EOF (see _WatchedStdin). Best effort: if
    this MCP SDK is laid out differently, serve as usual."""
    try:
        import anyio
        from mcp.server.mcpserver import server as srv
        original = srv.stdio_server
    except (ImportError, AttributeError):
        return

    def watched(stdin: Any = None, stdout: Any = None) -> Any:
        if stdin is None:
            stdin = anyio.wrap_file(_WatchedStdin(sys.stdin.buffer, encoding="utf-8",
                                                  errors="replace"))
        return original(stdin=stdin, stdout=stdout)

    srv.stdio_server = watched


def main() -> None:
    use_system_certs()
    _watch_stdin()
    try:
        project = config.load().get("chat_handle")
    except Exception:  # noqa: BLE001 - a bad config is reported by the tools
        project = None
    events.record("server_start", project=project, cwd=os.getcwd(),
                  session=handles.session_key())
    atexit.register(_log_exit)
    mcp.run()


if __name__ == "__main__":
    main()
