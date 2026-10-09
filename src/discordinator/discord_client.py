"""Thin synchronous Discord REST client.

We deliberately avoid a gateway (websocket) connection: sending and reading
channel messages are simple REST calls, which keeps one-shot CLI/MCP
invocations fast.

IMPORTANT — the Message Content Intent is REQUIRED to read other users'
messages. The privileged "Message Content Intent" (Developer Portal → your app
→ Bot → Privileged Gateway Intents) gates ``content``, ``attachments``,
``embeds`` and ``mentions`` on messages the bot did NOT author — over REST too,
not just the gateway. Without it, reads of a human's (or another bot's) message
come back with empty content and an empty attachments list, even though the
message id/author/timestamp are visible. A bot always sees its OWN messages in
full regardless, which is why a send->read round-trip (our tests) passes while a
human-posted attachment silently reads as empty. Besides the intent, the bot
needs View Channel and Read Message History in the target channel.
"""

from __future__ import annotations

import json
import mimetypes
import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import quote, unquote, urlparse

import httpx

API_BASE = "https://discord.com/api/v10"
MAX_MESSAGE_LEN = 2000  # Discord hard limit per message
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # Discord's default (non-boosted) per-file size limit
MAX_FILES_PER_MESSAGE = 10           # Discord caps attachments per message at 10

# Extensions treated as images when a message's attachment carries no
# content_type (Discord usually sets one, but local-transport records and odd
# clients may not). content_type, when present, is authoritative.
IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".svg", ".tiff", ".tif", ".ico", ".heic", ".avif",
}


class DiscordError(Exception):
    """Raised when a Discord API request fails."""


def chunk_content(content: str, limit: int = MAX_MESSAGE_LEN,
                  prefixed: bool = False) -> list[str]:
    """Split ``content`` into pieces that respect Discord's per-message limit,
    preferring to break on newline boundaries. ``prefixed``: each piece will be
    sent after a label (``[laptop] ...``), so it may start with whitespace.
    A piece of nothing but whitespace (a long blank run with no label) is
    dropped: Discord would refuse it as empty, failing the send midway."""
    pieces = [c for c, _sep in split_chunks(content, limit, prefixed)]
    return [c for c in pieces if prefixed or c.strip()] or pieces[:1]


def split_chunks(content: str, limit: int = MAX_MESSAGE_LEN,
                 prefixed: bool = False) -> list[tuple[str, str]]:
    """``chunk_content`` with what each cut removed: ``(chunk, sep)`` pairs where
    ``"".join(c + sep ...)`` is exactly ``content``. A cut at a newline drops
    just that one newline (``sep="\\n"``), else it falls mid-line (``sep=""``).

    Discord trims whitespace off both ends of a message, so a cut never leaves
    any at the end of a chunk - nor at the start, unless each chunk goes out
    after a label (``prefixed``), which shields it. So blank lines and
    indentation at a cut survive. Preferred cuts: a newline, then (prefixed) a
    word break, then mid-word; only a window with no clean cut at all is cut
    at the limit."""
    if len(content) <= limit:
        return [(content, "")]
    if limit < 1:  # a label as long as a whole message leaves no room
        raise ValueError(f"no room for text in a {MAX_MESSAGE_LEN}-character message "
                         "after the label - use a shorter label")

    def ws(i: int) -> bool:
        return 0 <= i < len(rest) and rest[i].isspace()

    def clean(i: int) -> bool:  # rest[:i] / rest[i:] lose nothing to a trim
        return not ws(i - 1) and (prefixed or not ws(i))

    out: list[tuple[str, str]] = []
    rest = content
    while len(rest) > limit:
        cut, sep = None, ""
        for i in range(min(limit, len(rest) - 1), 0, -1):  # a newline
            if rest[i] == "\n" and not ws(i - 1) and (prefixed or not ws(i + 1)):
                cut, sep = i, "\n"
                break
        if cut is None and prefixed:
            for i in range(limit, 0, -1):  # a word break: "word| next"
                if not ws(i - 1) and ws(i):
                    cut = i
                    break
        if cut is None:
            for i in range(limit, 0, -1):  # mid-word
                if clean(i):
                    cut = i
                    break
        if cut is None:
            cut = limit  # a window of nothing but whitespace
        out.append((rest[:cut], sep))
        rest = rest[cut + len(sep):]
    out.append((rest, ""))
    return out


def _nonce() -> dict[str, Any]:
    """Fields that make a message POST safe to retry: if Discord already created
    the message (the response was lost to a timeout), a retry carrying the same
    nonce returns that message instead of posting a second copy."""
    return {"nonce": secrets.token_hex(12), "enforce_nonce": True}


class DiscordClient:
    def __init__(self, token: str, timeout: float = 20.0):
        if not token:
            raise DiscordError("A bot token is required.")
        self._client = httpx.Client(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bot {token}",
                "User-Agent": "Discordinator (https://github.com/discordinator, 0.1.0)",
                # NB: no global Content-Type. httpx sets it per request from the
                # body kind (json= -> application/json, files= -> multipart with
                # a boundary). A global application/json would clobber the
                # multipart content-type and break file uploads.
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
                time.sleep(min(retry_after, 30.0))
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
            if resp.headers.get("X-RateLimit-Remaining") == "0":
                # The route's allowance is used up: wait it out now rather than
                # be refused (a purge deleting one by one hits this).
                try:
                    time.sleep(min(float(resp.headers.get("X-RateLimit-Reset-After", 0)), 30.0))
                except ValueError:
                    pass
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
        try:
            for piece in chunk_content(content, body_limit, prefixed=bool(prefix)):
                resp = self._request(
                    "POST",
                    f"/channels/{channel_id}/messages",
                    json={"content": f"{prefix}{piece}", **_nonce()},
                )
                sent.append(resp.json())
        except Exception as e:
            e.sent = sent  # type: ignore[attr-defined]  # the pieces that did go out
            raise
        return sent

    def send_files(
        self,
        channel_id: str,
        content: str,
        file_paths: list[Union[str, Path]],
        label: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Upload one or more files (images included — Discord auto-embeds image
        types) to a channel as attachments, with optional text ``content``.

        Each file must exist and be within ``MAX_UPLOAD_BYTES``; over-limit or
        missing files raise a ``DiscordError`` *before* anything is sent. Discord
        caps a message at ``MAX_FILES_PER_MESSAGE`` attachments, so larger lists
        are split across several messages (the text ``content`` rides only the
        first; text too long for one message goes out ahead of it, split as
        :meth:`send_message` does). ``label`` is prefixed to the content, as with
        :meth:`send_message`, so relay self-filtering still works. Returns the
        created message object(s).

        Note: each batch reads its files fully into memory (bounded by the
        ≤10 files × ``MAX_UPLOAD_BYTES`` size check above).
        """
        if not file_paths:
            raise ValueError("send_files requires at least one file.")
        paths = [Path(p) for p in file_paths]
        for p in paths:
            if not p.is_file():
                raise DiscordError(f"File not found: {p}")
            size = p.stat().st_size
            if size > MAX_UPLOAD_BYTES:
                raise DiscordError(
                    f"'{p.name}' is {size} bytes, over the {MAX_UPLOAD_BYTES}-byte "
                    "per-file upload limit (Discord, non-boosted server)."
                )
        sent: list[dict[str, Any]] = []
        try:
            return self._send_files(channel_id, content, paths, label, sent)
        except Exception as e:
            # The messages that did go out (a failed text piece reports its own).
            e.sent = sent + list(getattr(e, "sent", []))  # type: ignore[attr-defined]
            raise

    def _send_files(self, channel_id: str, content: str, paths: list[Path],
                    label: Optional[str], sent: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prefix = f"[{label}] " if label else ""
        # Text past one message goes out first; its last piece rides with the files.
        pieces = chunk_content(content, MAX_MESSAGE_LEN - len(prefix),
                               prefixed=bool(prefix)) if content else [""]
        for piece in pieces[:-1]:
            sent += self.send_message(channel_id, piece, label=label)
        full = f"{prefix}{pieces[-1]}" if pieces[-1] else prefix.strip()

        for start in range(0, len(paths), MAX_FILES_PER_MESSAGE):
            batch = paths[start : start + MAX_FILES_PER_MESSAGE]
            files_payload = [
                (
                    f"files[{i}]",
                    (p.name, p.read_bytes(), guess_content_type(p.name) or "application/octet-stream"),
                )
                for i, p in enumerate(batch)
            ]
            payload = {
                "content": full if start == 0 else "",  # text only on the first message
                "attachments": [{"id": i, "filename": p.name} for i, p in enumerate(batch)],
                **_nonce(),
            }
            resp = self._request(
                "POST",
                f"/channels/{channel_id}/messages",
                data={"payload_json": json.dumps(payload)},
                files=files_payload,
            )
            sent.append(resp.json())
        return sent

    def post(self, channel_id: str, content: str) -> dict[str, Any]:
        """Post a single message verbatim (no chunking, no label). Used by chat
        mode, which manages its own per-message headers and chunking."""
        return self._request(
            "POST", f"/channels/{channel_id}/messages", json={"content": content, **_nonce()}
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

    def download_attachment(self, url: str, dest: Union[str, Path]) -> Path:
        """Download an attachment ``url`` to ``dest`` and return the written path.

        If ``dest`` is an existing directory the filename is derived from the url
        (:func:`_filename_from_url`); otherwise ``dest`` is used verbatim as the
        file path. Discord CDN urls are public (signed), so no special auth is
        needed — this reuses the client's retry/backoff and error mapping. Note
        the signed urls expire, so download from a FRESH read rather than a
        stashed url.
        """
        # Blank the bot token for the CDN host — the signed url needs no auth, and
        # there's no reason to hand our token to a different origin.
        resp = self._request("GET", url, headers={"Authorization": ""})
        dest = Path(dest)
        if dest.is_dir():
            dest = _unique_in_dir(dest, _filename_from_url(url))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return dest

    def delete_message(self, channel_id: str, message_id: str) -> None:
        """Delete a single message. Deleting the bot's OWN messages needs no
        special permission; deleting others' messages requires Manage Messages."""
        self._request("DELETE", f"/channels/{channel_id}/messages/{message_id}")

    def bulk_delete(self, channel_id: str, message_ids: list[str]) -> None:
        """Delete 2-100 messages in one request. Discord allows it only for
        messages under 14 days old, and only with Manage Messages (even for
        the bot's own)."""
        self._request("POST", f"/channels/{channel_id}/messages/bulk-delete",
                      json={"messages": list(message_ids)})

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


def guess_content_type(filename: str) -> Optional[str]:
    """Best-effort MIME type from a filename (None if unknown). Used to label a
    multipart upload part and to derive ``is_image`` for locally stored files."""
    content_type, _ = mimetypes.guess_type(filename)
    return content_type


def _looks_like_image(content_type: Optional[str], filename: Optional[str]) -> bool:
    """True if an attachment is an image — by content_type if present, else by
    the filename's extension."""
    if content_type and content_type.lower().startswith("image/"):
        return True
    name = (filename or "").lower()
    return any(name.endswith(ext) for ext in IMAGE_EXTENSIONS)


def attachment_info(a: dict[str, Any]) -> dict[str, Any]:
    """Normalize ONE raw attachment object into the shape consumers use.

    Transport-agnostic: Discord supplies these fields on read; the local
    transport stores records in this same shape. ``is_image`` is derived so
    callers can decide whether to render inline vs. treat as a file. Keep in
    sync with :func:`simplify_message`, which maps it over a message's list.
    """
    content_type = a.get("content_type")
    filename = a.get("filename")
    return {
        "url": a.get("url"),
        "filename": filename,
        "content_type": content_type,
        "size": a.get("size"),
        "width": a.get("width"),
        "height": a.get("height"),
        "is_image": _looks_like_image(content_type, filename),
    }


def _filename_from_url(url: str) -> str:
    """Best-effort filename for an attachment whose download dest is a directory:
    strip the query (Discord CDN urls are signed with ?ex=&is=&hm=) and take the
    last path segment, percent-decoded. Falls back to 'attachment'."""
    path = urlparse(url).path
    name = unquote(path.rsplit("/", 1)[-1]) if path else ""
    # Decoding can bring back separators ("..%2F", "C%3A%5C..."): keep only the
    # final component so the file always lands inside the chosen directory.
    name = re.split(r"[/\\]", name)[-1].rsplit(":", 1)[-1].strip()
    return name if name not in ("", ".", "..") else "attachment"


def _unique_in_dir(directory: Path, name: str) -> Path:
    """A path in ``directory`` for ``name`` that doesn't collide with an existing
    file — appends ``-1``, ``-2``, … so saving several attachments that share a
    name (or re-downloading) never silently overwrites another file's bytes."""
    dest = directory / name
    if not dest.exists():
        return dest
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 1
    while True:
        candidate = directory / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def simplify_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Reduce a raw Discord message object to the fields we care about.

    ``attachments`` is a list of normalized dicts (see :func:`attachment_info`),
    not bare url strings — so filename / content_type / is_image travel with
    each one. Attachments without a url are dropped.
    """
    author = msg.get("author") or {}
    name = author.get("global_name") or author.get("username") or "unknown"
    return {
        "id": msg.get("id"),
        "author": name,
        "author_id": author.get("id"),
        "bot": bool(author.get("bot")),
        "timestamp": msg.get("timestamp"),
        "content": msg.get("content", ""),
        "attachments": [
            attachment_info(a) for a in msg.get("attachments", []) if a.get("url")
        ],
    }
