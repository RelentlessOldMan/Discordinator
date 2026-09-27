"""Thin synchronous Discord REST client.

We deliberately avoid a gateway (websocket) connection: sending and reading
channel messages are simple REST calls, which keeps one-shot CLI/MCP
invocations fast. Reading historical message content over REST does NOT require
the privileged Message Content Intent — the bot only needs View Channel and
Read Message History permissions in the target channel.
"""

from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import quote

import httpx

API_BASE = "https://discord.com/api/v10"
MAX_MESSAGE_LEN = 2000  # Discord hard limit per message


class DiscordError(Exception):
    """Raised when a Discord API request fails."""


def chunk_content(content: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Split ``content`` into pieces that respect Discord's per-message limit,
    preferring to break on newline boundaries."""
    if len(content) <= limit:
        return [content] if content else [""]

    chunks: list[str] = []
    remaining = content
    while len(remaining) > limit:
        window = remaining[:limit]
        split = window.rfind("\n")
        if split <= 0:
            split = limit  # no newline; hard split
        chunks.append(remaining[:split])
        remaining = remaining[split:].lstrip("\n") if split != limit else remaining[split:]
    if remaining:
        chunks.append(remaining)
    return chunks


class DiscordClient:
    def __init__(self, token: str, timeout: float = 20.0):
        if not token:
            raise DiscordError("A bot token is required.")
        self._client = httpx.Client(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bot {token}",
                "User-Agent": "Discordinator (https://github.com/discordinator, 0.1.0)",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DiscordClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        last_exc: Optional[Exception] = None
        for _ in range(4):
            try:
                resp = self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:  # network-level failure
                last_exc = exc
                time.sleep(1.0)
                continue

            if resp.status_code == 429:  # rate limited
                retry_after = 1.0
                try:
                    retry_after = float(resp.json().get("retry_after", 1.0))
                except Exception:
                    pass
                time.sleep(min(retry_after, 10.0))
                continue

            if resp.status_code == 401:
                raise DiscordError(
                    "Unauthorized (401). The bot token is missing or invalid."
                )
            if resp.status_code == 403:
                raise DiscordError(
                    f"Forbidden (403) for {method} {path}. The bot lacks permission "
                    "in that channel (needs View Channel + Read Message History / Send Messages)."
                )
            if resp.status_code == 404:
                raise DiscordError(
                    f"Not found (404) for {method} {path}. Check the channel id and "
                    "that the bot has been invited to that server."
                )
            if resp.status_code >= 400:
                raise DiscordError(f"{resp.status_code} {method} {path}: {resp.text}")
            return resp

        if last_exc is not None:
            raise DiscordError(f"Network error contacting Discord: {last_exc}")
        raise DiscordError("Rate limited by Discord repeatedly; try again shortly.")

    # -- API surface -------------------------------------------------------

    def whoami(self) -> dict[str, Any]:
        return self._request("GET", "/users/@me").json()

    def send_message(
        self, channel_id: str, content: str, label: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Send ``content`` to a channel, splitting into multiple messages if it
        exceeds Discord's length limit. If ``label`` is given it is prefixed to
        EVERY chunk (so each part is self-identifying and relay self-filtering
        works on multi-part messages). Returns the created message objects."""
        prefix = f"[{label}] " if label else ""
        body_limit = MAX_MESSAGE_LEN - len(prefix)
        sent: list[dict[str, Any]] = []
        for piece in chunk_content(content, body_limit):
            resp = self._request(
                "POST",
                f"/channels/{channel_id}/messages",
                json={"content": f"{prefix}{piece}"},
            )
            sent.append(resp.json())
        return sent

    def post(self, channel_id: str, content: str) -> dict[str, Any]:
        """Post a single message verbatim (no chunking, no label). Used by chat
        mode, which manages its own per-message headers and chunking."""
        return self._request(
            "POST", f"/channels/{channel_id}/messages", json={"content": content}
        ).json()

    def read_messages(
        self,
        channel_id: str,
        limit: int = 20,
        after: Optional[str] = None,
        before: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Fetch messages (Discord returns newest-first)."""
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 100))}
        if after:
            params["after"] = after
        if before:
            params["before"] = before
        return self._request(
            "GET", f"/channels/{channel_id}/messages", params=params
        ).json()

    def delete_message(self, channel_id: str, message_id: str) -> None:
        """Delete a single message. Deleting the bot's OWN messages needs no
        special permission; deleting others' messages requires Manage Messages."""
        self._request("DELETE", f"/channels/{channel_id}/messages/{message_id}")

    def add_reaction(
        self, channel_id: str, message_id: str, emoji: str = "✅"
    ) -> None:
        """Add a reaction (default ✅) as the bot. Requires Add Reactions
        permission. Used to acknowledge 'read up to here'."""
        encoded = quote(emoji, safe="")
        self._request(
            "PUT",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me",
        )

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        return self._request("GET", f"/channels/{channel_id}").json()

    def list_guild_channels(self, guild_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/guilds/{guild_id}/channels").json()


def simplify_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Reduce a raw Discord message object to the fields we care about."""
    author = msg.get("author") or {}
    name = author.get("global_name") or author.get("username") or "unknown"
    return {
        "id": msg.get("id"),
        "author": name,
        "author_id": author.get("id"),
        "bot": bool(author.get("bot")),
        "timestamp": msg.get("timestamp"),
        "content": msg.get("content", ""),
        "attachments": [a.get("url") for a in msg.get("attachments", []) if a.get("url")],
    }
