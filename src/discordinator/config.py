"""Configuration loading/saving for Discordinator.

Config lives at ``~/.discordinator/config.json`` by default. Override the path
with the ``DISCORDINATOR_CONFIG`` env var. The bot token may also be supplied
via the ``DISCORD_BOT_TOKEN`` env var, which takes precedence over the file.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional


def _atomic_write(path: Path, text: str, restrict: bool = False) -> None:
    """Write text to path atomically (temp file in same dir + os.replace) so a
    concurrent reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if restrict:
            try:  # best effort; harmless where unsupported (Windows)
                os.chmod(tmp, 0o600)
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


class ConfigError(Exception):
    """Raised for configuration problems (missing token, unknown channel...)."""


DEFAULTS: dict[str, Any] = {
    "token": None,
    "transport": "discord",  # "discord" (REST) or "local" (no-Discord, files)
    "default_channel": None,
    "chat_channel": None,   # default channel for CHAT tools (a shared room)
    "channels": {},         # friendly name -> channel id (string)
    "machine_label": None,  # optional tag prefixed to outgoing messages
    "ack_on_read": True,    # auto-react ✅ to the newest message on every read
    # Attachment opt-ins, OFF by default and set PER MACHINE: a locked-down box
    # (e.g. a work laptop) never uploads or downloads files unless deliberately
    # turned on. Send = uploading a local file out; receive = downloading an
    # attachment someone sent in. Independent so a machine can receive without
    # being allowed to send.
    "allow_send_attachments": False,
    "allow_receive_attachments": False,
}


def _truthy(val: str) -> bool:
    """Parse an env-var string as a boolean (1/true/yes/on -> True)."""
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def transport(cfg: dict[str, Any]) -> str:
    """Normalize the configured transport to ``"discord"`` or ``"local"``."""
    val = str(cfg.get("transport") or "discord").strip().lower()
    return "local" if val in ("local", "file", "offline") else "discord"


def is_local(cfg: dict[str, Any]) -> bool:
    """True when running the no-Discord local filesystem transport."""
    return transport(cfg) == "local"


def config_path() -> Path:
    env = os.environ.get("DISCORDINATOR_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".discordinator" / "config.json"


def _parse_env_file(path: Path) -> None:
    """Set KEY=VALUE pairs from a .env file into os.environ, without clobbering
    variables already present in the real environment."""
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, sep, val = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


def load_dotenv() -> None:
    """Search the current directory and its parents for a .env file and load
    the nearest one. Lets you keep a project-local, git-ignored token file."""
    start = Path.cwd()
    for directory in (start, *start.parents):
        env_file = directory / ".env"
        if env_file.exists():
            _parse_env_file(env_file)
            return


def load() -> dict[str, Any]:
    """Load config from disk merged with defaults and env overrides."""
    load_dotenv()
    data = dict(DEFAULTS)
    data["channels"] = {}
    path = config_path()
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Config file at {path} is not valid JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"Config file at {path} must contain a JSON object.")
        data.update(loaded)
        data.setdefault("channels", {})

    # Environment overrides win over the file.
    env_token = os.environ.get("DISCORD_BOT_TOKEN")
    if env_token:
        data["token"] = env_token
    env_transport = os.environ.get("DISCORDINATOR_TRANSPORT")
    if env_transport:
        data["transport"] = env_transport
    env_label = os.environ.get("DISCORDINATOR_LABEL")
    if env_label:
        data["machine_label"] = env_label
    env_channel = os.environ.get("DISCORDINATOR_RELAY_CHANNEL")
    if env_channel:
        data["default_channel"] = env_channel
    env_chat = os.environ.get("DISCORDINATOR_CHAT_CHANNEL")
    if env_chat:
        data["chat_channel"] = env_chat
    env_ack = os.environ.get("DISCORDINATOR_ACK")
    if env_ack is not None:
        data["ack_on_read"] = _truthy(env_ack)
    env_send = os.environ.get("DISCORDINATOR_ALLOW_SEND")
    if env_send is not None:
        data["allow_send_attachments"] = _truthy(env_send)
    env_receive = os.environ.get("DISCORDINATOR_ALLOW_RECEIVE")
    if env_receive is not None:
        data["allow_receive_attachments"] = _truthy(env_receive)

    return data


def save(data: dict[str, Any]) -> Path:
    """Persist config to disk atomically, restricting permissions where supported."""
    path = config_path()
    _atomic_write(path, json.dumps(data, indent=2, sort_keys=True), restrict=True)
    return path


def require_token(cfg: dict[str, Any]) -> str:
    token = cfg.get("token")
    if not token:
        raise ConfigError(
            "No bot token configured. Set one with:\n"
            "  discordinator config set-token <TOKEN>\n"
            "or export DISCORD_BOT_TOKEN=<TOKEN>"
        )
    return str(token)


def can_send_attachments(cfg: dict[str, Any]) -> bool:
    """True if this machine is allowed to UPLOAD files (default False)."""
    return bool(cfg.get("allow_send_attachments"))


def can_receive_attachments(cfg: dict[str, Any]) -> bool:
    """True if this machine is allowed to DOWNLOAD attachments (default False)."""
    return bool(cfg.get("allow_receive_attachments"))


def require_send_attachments(cfg: dict[str, Any]) -> None:
    """Raise ConfigError unless sending attachments is enabled on this machine."""
    if not can_send_attachments(cfg):
        raise ConfigError(
            "Sending attachments is disabled on this machine. Enable it with:\n"
            "  discordinator config set-attachments send on\n"
            "or set DISCORDINATOR_ALLOW_SEND=1 for one session."
        )


def require_receive_attachments(cfg: dict[str, Any]) -> None:
    """Raise ConfigError unless downloading attachments is enabled on this machine."""
    if not can_receive_attachments(cfg):
        raise ConfigError(
            "Receiving (downloading) attachments is disabled on this machine. Enable it with:\n"
            "  discordinator config set-attachments receive on\n"
            "or set DISCORDINATOR_ALLOW_RECEIVE=1 for one session."
        )


def resolve_channel(cfg: dict[str, Any], channel: Optional[str]) -> str:
    """Resolve a channel name/id to a channel id string.

    Precedence: explicit ``channel`` arg (a config name or a raw numeric id),
    otherwise the configured ``default_channel``. In **local** transport, any
    string is a valid room name (stored as a JSONL file), and the default relay
    room falls back to ``"relay"`` so local mode needs zero channel setup.
    """
    channels = cfg.get("channels") or {}
    local = is_local(cfg)
    name = channel if channel is not None else cfg.get("default_channel")

    if name is None:
        if local:
            name = "relay"  # built-in default local room; no config needed
        else:
            raise ConfigError(
                "No channel specified and no default configured. Either pass "
                "--channel <name-or-id> or run: discordinator config set-default <name>"
            )

    name = str(name)
    if name in channels:
        return str(channels[name])
    if name.isdigit():
        return name
    if local:
        return name  # arbitrary room name (LocalClient sanitizes for storage)

    known = ", ".join(sorted(channels)) or "(none)"
    raise ConfigError(
        f"Unknown channel '{name}'. Known names: {known}. "
        f"Add one with: discordinator config add-channel <name> <channel_id>"
    )


def resolve_chat_channel(cfg: dict[str, Any], channel: Optional[str]) -> str:
    """Resolve the channel for CHAT tools.

    Precedence: explicit ``channel`` arg, then the configured ``chat_channel``
    (a dedicated shared room), then ``default_channel`` as a fallback. Chat gets
    its OWN default so a live two-way chat never silently lands on a per-project
    relay mailbox — the two modes stay on separate channels without either side
    having to name the room. Set it via ``DISCORDINATOR_CHAT_CHANNEL`` or
    ``discordinator config set-chat-channel``.
    """
    if channel is not None:
        return resolve_channel(cfg, channel)
    fallback = cfg.get("chat_channel") or cfg.get("default_channel")
    if fallback is None:
        if is_local(cfg):
            fallback = "chat"  # built-in default local chat room, distinct from relay
        else:
            raise ConfigError(
                "No chat channel specified and none configured. Pass channel=<name-or-id>, "
                "or set a shared room with: discordinator config set-chat-channel <name-or-id> "
                "(or export DISCORDINATOR_CHAT_CHANNEL)."
            )
    return resolve_channel(cfg, fallback)


# -- relay cursor state ----------------------------------------------------
# Tracks the last message id seen per channel so relay/get_new_messages can
# return only fresh messages. Stored separately from config (no secrets here).


def state_path() -> Path:
    return config_path().parent / "state.json"


def load_state() -> dict[str, Any]:
    path = state_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict[str, Any]) -> None:
    _atomic_write(state_path(), json.dumps(state, indent=2))


def get_cursor(channel_id: str) -> Optional[str]:
    return (load_state().get("cursors") or {}).get(str(channel_id))


def set_cursor(channel_id: str, message_id: str) -> None:
    state = load_state()
    state.setdefault("cursors", {})[str(channel_id)] = str(message_id)
    save_state(state)


def clear_cursor(channel_id: str) -> None:
    state = load_state()
    cursors = state.get("cursors") or {}
    cursors.pop(str(channel_id), None)
    state["cursors"] = cursors
    save_state(state)
