"""Transport selection: return the client backend the config asks for.

``transport: "discord"`` (default) → the REST :class:`DiscordClient`.
``transport: "local"`` → the no-Discord filesystem :class:`LocalClient`.

Each call site constructs its client through :func:`make_client` with a *mode*
(``"relay"`` or ``"chat"``), so the two modes can use DIFFERENT backends — e.g.
relay over Discord to reach another machine while chatting locally with a
sibling session on the same box. The mode's transport comes from
``relay_transport`` / ``chat_transport`` (each falling back to the base
``transport``; see :func:`config.transport`). No Discord token is required for a
mode running on the local transport.
"""

from __future__ import annotations

from typing import Any, Optional, Union

from . import config
from .discord_client import DiscordClient
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
        return LocalClient(label=cfg.get("machine_label"))
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
    return LocalClient(label=cfg.get("machine_label"))
