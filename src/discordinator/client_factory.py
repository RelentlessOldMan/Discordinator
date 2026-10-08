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
from .discord_client import DiscordClient, simplify_message
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
