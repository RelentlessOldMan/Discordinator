"""Transport selection: return the client backend the config asks for.

``"discord"`` → the REST :class:`DiscordClient`; ``"local"`` → the no-Discord
filesystem :class:`LocalClient`.

Each call site constructs its client through :func:`make_client` with a *mode*
(``"relay"`` or ``"chat"``), so the two modes can use DIFFERENT backends — e.g.
relay over Discord to reach another machine while chatting locally with a
sibling session on the same box. The mode's transport comes from
``relay_transport`` / ``chat_transport``, each set explicitly - there is no base
transport and no default (see :func:`config.transport`). No Discord token is
required for a mode running on the local transport.
"""

from __future__ import annotations

from typing import Any, Optional, Union

from . import config
from .discord_client import DiscordClient, DiscordError, simplify_message
from .local_client import LocalClient

Client = Union[DiscordClient, LocalClient]


def make_client(cfg: Optional[dict[str, Any]] = None, mode: Optional[str] = None) -> Client:
    """Build the client for ``mode``'s transport. Loads config if not given.

    ``mode`` must be ``"relay"`` or ``"chat"`` — each is configured explicitly
    and independently (there is no base transport). Passing no mode, or a mode
    whose transport is unset, raises (``ValueError`` / ``ConfigError``) rather
    than guessing. A Discord token is required only when ``mode``'s transport is
    Discord.
    """
    cfg = cfg if cfg is not None else config.load()
    if config.is_local(cfg, mode):
        return LocalClient(label=cfg.get("machine_label"),
                           retention_days=config.local_retention_days(cfg))
    return DiscordClient(config.require_token(cfg))


def make_client_for_url(url: str, cfg: Optional[dict[str, Any]] = None) -> Client:
    """Build the backend that can fetch attachment ``url``, independent of mode.

    An ``http(s)`` url is a Discord CDN link → the REST :class:`DiscordClient`
    (needs a token); anything else is a filesystem path on the shared disk →
    :class:`LocalClient`. This lets ``download_attachment`` work regardless of
    which mode's transport produced the url (relay and chat may differ), since
    the url shape alone determines how to fetch it.
    """
    cfg = cfg if cfg is not None else config.load()
    if str(url).lower().startswith(("http://", "https://")):
        return DiscordClient(config.require_token(cfg))
    return LocalClient(label=cfg.get("machine_label"),
                           retention_days=config.local_retention_days(cfg))


def read_new(client: Client, channel_id: str, reader: Optional[str],
             backfill: int) -> list[dict[str, Any]]:
    """Relay messages newer than ``reader``'s read position (the newest
    ``backfill`` on a first read), chronological and simplified, and advance
    the position past them.

    A position well past the channel's newest message - kept from the other
    transport (local ids are larger than Discord's) - would hide every new
    message for good, so it starts over. (Only well past: a position just past
    the newest is normal once the last message read is deleted.)"""
    cursor = config.get_cursor(channel_id, reader)
    raw = (client.read_messages(channel_id, limit=100, after=cursor) if cursor
           else client.read_messages(channel_id, limit=backfill))
    if cursor and not raw:
        newest = client.read_messages(channel_id, limit=1)
        try:
            # Ids of either kind grow by under 1% a year; the two schemes are
            # ~15% apart. A deletion leaves the position barely ahead.
            ahead = bool(newest) and int(cursor) > int(newest[0]["id"]) * 1.05
        except (KeyError, TypeError, ValueError):
            ahead = False
        if ahead:
            config.clear_cursor(channel_id, reader)
            raw = client.read_messages(channel_id, limit=backfill)
    messages = [simplify_message(m) for m in raw]
    messages.reverse()  # chronological
    if messages:
        config.set_cursor(channel_id, messages[-1]["id"], reader)
    return messages


def purge_room(cfg: dict[str, Any], channel: Optional[str]) -> tuple[str, str]:
    """The (mode, channel id) a purge of ``channel`` acts on. When relay and
    chat run on different transports, the configured chat room - or a name only
    the chat transport knows (any name, on local) - is purged where the chats
    happen; anything else, and no channel, is the relay channel."""
    try:
        split = config.transport(cfg, "chat") != config.transport(cfg, "relay")
    except config.ConfigError:
        split = False  # an unset mode: only the relay channel
    if channel is None or not split:
        return "relay", config.resolve_channel(cfg, channel)
    try:
        chat_id: Optional[str] = config.resolve_chat_channel(cfg, channel)
    except config.ConfigError:
        chat_id = None
    if chat_id is not None and str(channel) == str(cfg.get("chat_channel") or ""):
        return "chat", chat_id
    try:
        return "relay", config.resolve_channel(cfg, channel)
    except config.ConfigError:
        if chat_id is None:
            raise
        return "chat", chat_id


def purge_targets(client: Client, channel_id: str, older_than: Optional[float] = None,
                  limit: Optional[int] = None) -> list[dict[str, Any]]:
    """Every message in the channel - whoever posted it (any machine, a human,
    an interjection) - optionally only those older than ``older_than`` seconds,
    and at most the newest ``limit``. Simplified, newest first."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than)
              if older_than else None)
    out: list[dict[str, Any]] = []
    before: Optional[str] = None
    while limit is None or len(out) < limit:
        want = 100 if limit is None else min(100, limit - len(out))
        batch = client.read_messages(channel_id, limit=want, before=before)
        if not batch:
            break
        for raw in batch:
            m = simplify_message(raw)
            ts = datetime.fromisoformat(m["timestamp"]) if m["timestamp"] else None
            if cutoff is None or (ts is not None and ts <= cutoff):
                out.append(m)
        before = batch[-1]["id"]
        if len(batch) < want:
            break
    return out


# Discord bulk-deletes only messages younger than this (its limit is 14 days;
# a margin for clock skew and the time the purge itself takes).
BULK_MAX_AGE = 13.5 * 86400


def _bulk_delete(client: DiscordClient, channel_id: str,
                 messages: list[dict[str, Any]]) -> set[str]:
    """Delete what Discord lets go 100 at a time (recent messages, with Manage
    Messages); returns their ids. The rest - or all, without the permission -
    are left for one-by-one deletes."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    recent = []
    for m in messages:
        try:
            age = (now - datetime.fromisoformat(m["timestamp"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            continue
        if age < BULK_MAX_AGE:
            recent.append(str(m["id"]))
    done: set[str] = set()
    for start in range(0, len(recent), 100):
        chunk = recent[start:start + 100]
        if len(chunk) < 2:
            break  # Discord's minimum; one goes on its own
        try:
            client.bulk_delete(channel_id, chunk)
        except DiscordError:
            break  # e.g. no Manage Messages: one by one instead
        done.update(chunk)
    return done


def purge(client: Client, channel_id: str,
          messages: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """Delete ``messages``; returns (how many were deleted, why the rest
    weren't). On Discord, other authors' messages need Manage Messages: once
    one is refused, the rest of theirs are skipped rather than each refused."""
    if hasattr(client, "delete_messages"):  # local: one rewrite for all of them
        return client.delete_messages(channel_id, [m["id"] for m in messages]), []
    me = client.whoami().get("id")
    deleted, problems, no_perm = 0, [], False
    done = _bulk_delete(client, channel_id, messages)
    deleted += len(done)
    for m in messages:
        if m["id"] in done or (no_perm and m["author_id"] != me):
            continue
        try:
            client.delete_message(channel_id, m["id"])
            deleted += 1
        except DiscordError as e:
            if "(404)" in str(e):
                continue  # already gone
            if "(403)" in str(e) and m["author_id"] != me:
                no_perm = True
            else:
                problems.append(f"{m['id']}: {e}")
    others = sum(1 for m in messages if m["author_id"] != me)
    if no_perm:
        problems.insert(0, f"{others} message(s) by people (or other bots) weren't deleted: "
                           "the bot needs the Manage Messages permission (re-invite it "
                           "with permissions=109632).")
    return deleted, problems
