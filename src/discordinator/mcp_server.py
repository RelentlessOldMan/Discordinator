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

from . import config
from .discord_client import DiscordClient, simplify_message

# Keep the HTTP client quiet: it logs an INFO line per request to stderr, which
# is noise for a stdio MCP server (stdout carries the JSON-RPC protocol).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

mcp = MCPServer("discordinator")


def _client() -> DiscordClient:
    cfg = config.load()
    token = config.require_token(cfg)
    return DiscordClient(token)


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
) -> list[dict[str, Any]]:
    """Read recent messages from a Discord channel.

    Args:
        channel: A configured channel name or raw channel id. Defaults to the
            configured default channel.
        limit: Number of messages to fetch (1-100).
        after: Only return messages after this message id (for polling new ones).
        before: Only return messages before this message id (for paging back).
        newest_first: If false (default), messages are returned oldest-first.

    Returns a list of simplified message objects with id, author, timestamp,
    content and attachment urls.
    """
    cfg = config.load()
    channel_id = config.resolve_channel(cfg, channel)
    with _client() as client:
        raw = client.read_messages(channel_id, limit=limit, after=after, before=before)
    messages = [simplify_message(m) for m in raw]
    if not newest_first:
        messages.reverse()
    return messages


@mcp.tool()
def get_new_messages(
    channel: Optional[str] = None,
    include_self: bool = False,
    limit: int = 50,
    ack: bool = False,
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
        ack: If true, react ✅ to the newest returned message so the other side
            can see it was read (needs Add Reactions permission).

    Returns simplified message objects in chronological order.
    """
    cfg = config.load()
    channel_id = config.resolve_channel(cfg, channel)
    own_label = cfg.get("machine_label")
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

        if ack and messages:
            newest = max(messages, key=lambda m: int(m["id"]))
            client.add_reaction(channel_id, newest["id"])
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
