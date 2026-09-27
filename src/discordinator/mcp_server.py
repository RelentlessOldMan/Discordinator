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
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer

from . import chat, config, use_system_certs
from .discord_client import DiscordClient, DiscordError, simplify_message

# Keep the HTTP client quiet: it logs an INFO line per request to stderr, which
# is noise for a stdio MCP server (stdout carries the JSON-RPC protocol).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

mcp = MCPServer("discordinator")


def _client() -> DiscordClient:
    cfg = config.load()
    token = config.require_token(cfg)
    return DiscordClient(token)


def _try_ack(client: DiscordClient, channel_id: str, messages: list[dict[str, Any]]) -> None:
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
    with _client() as client:
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
    content and attachment urls.
    """
    cfg = config.load()
    channel_id = config.resolve_channel(cfg, channel)
    if ack is None:
        ack = bool(cfg.get("ack_on_read"))
    with _client() as client:
        raw = client.read_messages(channel_id, limit=limit, after=after, before=before)
        messages = [simplify_message(m) for m in raw]
        if ack:
            _try_ack(client, channel_id, messages)
    if not newest_first:
        messages.reverse()
    return messages


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
    with _client() as client:
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

    with _client() as client:
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
    with _client() as client:
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


@mcp.tool()
def chat_begin(chatter: str, channel: Optional[str] = None, turn_cap: int = 20) -> dict[str, Any]:
    """Start or join a turn-based chat as participant `chatter`.

    Seeds your read position to *now* (prior history is ignored) and resets your
    turn counter. BOTH participants call this first, with DISTINCT `chatter`
    handles (e.g. "A" and "B"). Then the initiator calls `chat_say`; the other
    calls `chat_await`. Do NOT have both call `chat_await` first — that deadlocks.

    Args:
        chatter: your participant handle (short; used to tag and self-filter).
        channel: chat channel name/id (a dedicated channel like claudes-chatroom
            is recommended). Defaults to the configured default channel.
        turn_cap: soft cap on your turns before you're nudged to wrap up.
    """
    me = chat.sanitize_handle(chatter)
    cfg = config.load()
    channel_id = config.resolve_channel(cfg, channel)
    with _client() as client:
        latest = client.read_messages(channel_id, limit=1)
    cursor = latest[0]["id"] if latest else "0"
    chat.reset(channel_id, me, cursor, turn_cap)
    return {"channel": channel_id, "chatter": me, "turn_cap": turn_cap,
            "next": "initiator: chat_say(...); other: chat_await(...)"}


@mcp.tool()
def chat_say(
    text: str,
    chatter: str,
    status: str = "over",
    channel: Optional[str] = None,
) -> dict[str, Any]:
    """Send a chat message as `chatter` with an explicit turn status.

    status values:
      - "say"     more of my turn is coming — do NOT yield (send more, then a
                  terminal status).
      - "over"    I'm done — your turn (the normal handoff).
      - "wrap"    I think we can end this — do you agree? (yields your turn).
      - "end"     ending now (use to confirm after the other proposed "wrap",
                  or to end unilaterally). Terminal.
      - "impasse" we're stuck — stop and get the human. Terminal.

    Long text is split across messages, each re-tagged, so multi-part turns stay
    intact. Returns your turn count and whether the cap was reached.
    """
    me = chat.sanitize_handle(chatter)
    if status not in chat.STATUSES:
        raise ValueError(f"status must be one of {chat.STATUSES}, got {status!r}")
    cfg = config.load()
    channel_id = config.resolve_channel(cfg, channel)
    with _client() as client:
        sent = chat.send_chat(client, channel_id, me, status, text)
    turns = chat.bump_turn(channel_id, me) if status != "say" else chat.get_meta(channel_id, me)[0]
    _, cap = chat.get_meta(channel_id, me)
    return {
        "sent_messages": len(sent),
        "status": status,
        "my_turns": turns,
        "turn_cap": cap,
        "cap_reached": turns >= cap,
        "ended": status in chat.TERMINAL_STATUSES,
    }


@mcp.tool()
def chat_await(
    chatter: str,
    channel: Optional[str] = None,
    timeout: float = 120.0,
    poll: float = 3.0,
) -> dict[str, Any]:
    """Block until the OTHER participant completes a turn, a human interjects, or
    `timeout` seconds pass. This is how you wait for a reply — just call it and
    it spins server-side; you don't poll yourself.

    Returns a dict with:
      - from: the sender's handle, or "human", or null on timeout
      - status: their turn status (over/wrap/end/impasse), "interjection"/"stop"
        for a human message, or null on timeout
      - text: their turn's combined body (or the human's text) — the words live
        here; `messages` is metadata-only ({id, from, status, timestamp})
      - your_turn: True if it's now your turn to `chat_say`
      - ended: True if the conversation is over (their "end"/"impasse", or a
        human "stop")
      - stop_reason: "agreed" | "impasse" | "human" | null
      - timed_out: True if nothing arrived in `timeout`s. This is NOT the end —
        the other side is still thinking. Immediately call `chat_await` again to
        keep waiting. Never treat a timeout as "abandoned" or ask the human.
      - cap_reached: True if you've hit your turn cap (move toward "wrap"/"end")

    A human typing anything in the channel is surfaced (from="human"); if it
    looks like a stop command ("stop"/"halt"/"[[STOP]]") the chat ends.
    """
    me = chat.sanitize_handle(chatter)
    cfg = config.load()
    channel_id = config.resolve_channel(cfg, channel)
    with _client() as client:
        return chat.await_turn(client, channel_id, me, timeout=timeout, poll=poll)


def main() -> None:
    use_system_certs()
    mcp.run()


if __name__ == "__main__":
    main()
