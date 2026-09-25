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
    "default_channel": None,
    "channels": {},        # friendly name -> channel id (string)
    "machine_label": None,  # optional tag prefixed to outgoing messages
}


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
    env_label = os.environ.get("DISCORDINATOR_LABEL")
    if env_label:
        data["machine_label"] = env_label

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


def resolve_channel(cfg: dict[str, Any], channel: Optional[str]) -> str:
    """Resolve a channel name/id to a numeric channel id string.

    Precedence: explicit ``channel`` arg (a config name or a raw numeric id),
    otherwise the configured ``default_channel``.
    """
    channels = cfg.get("channels") or {}
    name = channel if channel is not None else cfg.get("default_channel")

    if name is None:
        raise ConfigError(
            "No channel specified and no default configured. Either pass "
            "--channel <name-or-id> or run: discordinator config set-default <name>"
        )

    name = str(name)
    if name in channels:
        return str(channels[name])
    if name.isdigit():
        return name

    known = ", ".join(sorted(channels)) or "(none)"
    raise ConfigError(
        f"Unknown channel '{name}'. Known names: {known}. "
        f"Add one with: discordinator config add-channel <name> <channel_id>"
    )


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
