"""Transport selection: return the client backend the config asks for.

``transport: "discord"`` (default) → the REST :class:`DiscordClient`.
``transport: "local"`` → the no-Discord filesystem :class:`LocalClient`.

Every call site (CLI + MCP server) constructs its client through
:func:`make_client`, so a single global switch (config ``transport`` or the
``DISCORDINATOR_TRANSPORT`` env var) flips both relay and chat modes between
Discord and local — no Discord token is required in local mode.
"""

from __future__ import annotations

from typing import Any, Optional, Union

from . import config
from .discord_client import DiscordClient
from .local_client import LocalClient

Client = Union[DiscordClient, LocalClient]


def make_client(cfg: Optional[dict[str, Any]] = None) -> Client:
    """Build the client for the active transport. Loads config if not given."""
    cfg = cfg if cfg is not None else config.load()
    if config.is_local(cfg):
        return LocalClient(label=cfg.get("machine_label"))
    return DiscordClient(config.require_token(cfg))
