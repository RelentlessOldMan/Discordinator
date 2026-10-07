"""Configuration loading/saving for Discordinator.

Config lives at ``~/.discordinator/config.json`` by default. Override the path
with the ``DISCORDINATOR_CONFIG`` env var. The bot token may also be supplied
via the ``DISCORD_BOT_TOKEN`` env var, which takes precedence over the file.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

# Windows refuses to replace or open a file another process has open at that
# instant (sharing violation -> PermissionError). It clears within moments, so
# file operations here retry briefly before giving up.
_IO_RETRIES = 50
_IO_PAUSE = 0.02


def _replace(src: str, dst: Path, retries: int = _IO_RETRIES) -> None:
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(_IO_PAUSE)


def read_json(path: Path, default: Any) -> Any:
    """Parse a JSON file, retrying while another process is swapping it in.
    A missing file gives ``default``; so does one that isn't valid JSON (writes
    are atomic, so that means it's corrupt, not half-written). A file that stays
    unreadable raises - never mistaken for "empty", which a later save would
    then write back over everything in it."""
    for attempt in range(_IO_RETRIES):
        try:
            text = path.read_text(encoding="utf-8")
            break
        except FileNotFoundError:
            return default
        except OSError:
            if attempt == _IO_RETRIES - 1:
                raise
            time.sleep(_IO_PAUSE)
    try:
        return json.loads(text)
    except ValueError:
        return default


class FileLock:
    """Cross-process lock via an ``O_EXCL`` lock file beside ``target``.

    Holds are short, so contention is brief. A stale lock (holder crashed) is
    stolen after ``stale`` seconds. If the lock can't be taken within
    ``timeout`` seconds we proceed anyway rather than hang a chat forever.
    """

    # stale < timeout: a crashed holder's lock is stolen well before any waiter
    # gives up and proceeds unlocked.
    def __init__(self, target: Path, timeout: float = 15.0, stale: float = 8.0):
        self.lockpath = str(target) + ".lock"
        self.timeout = timeout
        self.stale = stale
        self.fd: Optional[int] = None

    def __enter__(self) -> "FileLock":
        Path(self.lockpath).parent.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        while True:
            try:
                self.fd = os.open(self.lockpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.lockpath) > self.stale:
                        self._steal()
                except OSError:
                    pass
            except PermissionError:
                pass  # Windows: the lock file is mid-delete; try again
            # Check the deadline and pause on EVERY path - a lock we fail to
            # steal (its holder still has it open) must not spin forever.
            if time.monotonic() - start > self.timeout:
                self.fd = None  # give up waiting; proceed unlocked
                return self
            time.sleep(0.02)

    def _steal(self) -> None:
        """Remove a stale lock. Renamed aside first (atomic: only one waiter can
        win), then re-checked - if another waiter had just replaced it with a
        fresh lock, that one is put back instead of deleted."""
        aside = f"{self.lockpath}.{os.getpid()}.{time.monotonic_ns()}"
        try:
            os.rename(self.lockpath, aside)
        except OSError:
            return  # someone else got there first, or the holder still has it open
        try:
            if time.time() - os.path.getmtime(aside) > self.stale:
                os.remove(aside)
            else:
                os.rename(aside, self.lockpath)
        except OSError:
            try:
                os.remove(aside)
            except OSError:
                pass

    def __exit__(self, *exc: object) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            try:
                os.remove(self.lockpath)
            except OSError:
                pass


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
        _replace(tmp, path)
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
    # Transport is set EXPLICITLY per mode — there is no shared base and no
    # fallback. `relay_transport` backs the relay tools (send/read/get_new_messages);
    # `chat_transport` backs the live chat_* tools. Each is "discord" (REST) or
    # "local" (no-Discord, JSONL files), also settable via
    # DISCORDINATOR_RELAY_TRANSPORT / DISCORDINATOR_CHAT_TRANSPORT. An unset mode
    # is an error (fail fast), not a guess. They default to None so a fresh config
    # must declare them before that mode is used.
    "relay_transport": None,
    "chat_transport": None,
    "default_channel": None,
    "chat_channel": None,   # default channel for CHAT tools (a shared room)
    # This project's fixed chat handle (set per project via DISCORDINATOR_CHAT_HANDLE
    # in .mcp.json), used when a chat_* call omits `chatter` — so one project
    # never shows up under several names.
    "chat_handle": None,
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
    # Local transport only: messages older than this many days are dropped from
    # a room when it is next written to (with their stored attachments). Local
    # rooms exist for sessions chatting now, not as an archive. 0 = keep forever.
    "local_retention_days": 7,
}


def _truthy(val: str) -> bool:
    """Parse an env-var string as a boolean (1/true/yes/on -> True)."""
    return str(val).strip().lower() in ("1", "true", "yes", "on")


_TRANSPORT_KEYS = {"relay": "relay_transport", "chat": "chat_transport"}


def transport(cfg: dict[str, Any], mode: str) -> str:
    """Resolve the transport (``"discord"`` or ``"local"``) for ``mode``.

    ``mode`` is ``"relay"`` or ``"chat"``. Each mode's transport is set
    EXPLICITLY (``relay_transport`` / ``chat_transport``, or the matching
    ``DISCORDINATOR_*_TRANSPORT`` env var) — there is no shared base and no
    fallback, so a single process can run relay over Discord while chatting
    locally. An unset mode raises :class:`ConfigError` (fail fast with a fix)
    rather than guessing a default.
    """
    try:
        key = _TRANSPORT_KEYS[mode]
    except KeyError:
        raise ValueError(f"transport mode must be 'relay' or 'chat', got {mode!r}")
    val = cfg.get(key)
    if not val:
        env = "DISCORDINATOR_RELAY_TRANSPORT" if mode == "relay" else "DISCORDINATOR_CHAT_TRANSPORT"
        raise ConfigError(
            f"No {key} configured. Each transport is set explicitly — there is no "
            f"default. Set it with:\n"
            f"  discordinator config set-{mode}-transport <discord|local>\n"
            f"or export {env}=<discord|local>."
        )
    val = str(val).strip().lower()
    return "local" if val in ("local", "file", "offline") else "discord"


def is_local(cfg: dict[str, Any], mode: str) -> bool:
    """True when ``mode`` runs the no-Discord local filesystem transport."""
    return transport(cfg, mode) == "local"


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


def load_file() -> dict[str, Any]:
    """Just what the config file says - no defaults, no env/.env overrides.
    What `config set-*` edits and saves, so a setting meant for one shell or
    session (an env var) is never written into the shared file."""
    path = config_path()
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file at {path} is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"Config file at {path} must contain a JSON object.")
    return loaded


def load() -> dict[str, Any]:
    """Load config from disk merged with defaults and env overrides."""
    load_dotenv()
    data = dict(DEFAULTS)
    data["channels"] = {}
    data.update(load_file())
    data.setdefault("channels", {})

    # Environment overrides win over the file.
    env_token = os.environ.get("DISCORD_BOT_TOKEN")
    if env_token:
        data["token"] = env_token
    env_relay_transport = os.environ.get("DISCORDINATOR_RELAY_TRANSPORT")
    if env_relay_transport:
        data["relay_transport"] = env_relay_transport
    env_chat_transport = os.environ.get("DISCORDINATOR_CHAT_TRANSPORT")
    if env_chat_transport:
        data["chat_transport"] = env_chat_transport
    env_label = os.environ.get("DISCORDINATOR_LABEL")
    if env_label:
        data["machine_label"] = env_label
    env_channel = os.environ.get("DISCORDINATOR_RELAY_CHANNEL")
    if env_channel:
        data["default_channel"] = env_channel
    env_chat = os.environ.get("DISCORDINATOR_CHAT_CHANNEL")
    if env_chat:
        data["chat_channel"] = env_chat
    env_handle = os.environ.get("DISCORDINATOR_CHAT_HANDLE")
    if env_handle:
        data["chat_handle"] = env_handle
    env_ack = os.environ.get("DISCORDINATOR_ACK")
    if env_ack is not None:
        data["ack_on_read"] = _truthy(env_ack)
    env_send = os.environ.get("DISCORDINATOR_ALLOW_SEND")
    if env_send is not None:
        data["allow_send_attachments"] = _truthy(env_send)
    env_receive = os.environ.get("DISCORDINATOR_ALLOW_RECEIVE")
    if env_receive is not None:
        data["allow_receive_attachments"] = _truthy(env_receive)
    env_retention = os.environ.get("DISCORDINATOR_LOCAL_RETENTION_DAYS")
    if env_retention:
        data["local_retention_days"] = env_retention

    return data


def local_retention_days(cfg: dict[str, Any]) -> float:
    """Validated local-room retention window in days (0 = keep forever)."""
    raw = cfg.get("local_retention_days", DEFAULTS["local_retention_days"])
    try:
        days = float(raw if raw is not None else 0)
    except (TypeError, ValueError):
        raise ConfigError(
            f"local_retention_days must be a number of days (0 = keep forever), got {raw!r}. "
            "Fix with: discordinator config set-local-retention <days>"
        ) from None
    if days < 0 or not math.isfinite(days):
        raise ConfigError(
            f"local_retention_days must be >= 0 (0 = keep forever), got {raw!r}. "
            "Fix with: discordinator config set-local-retention <days>"
        )
    return days


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


def resolve_channel(cfg: dict[str, Any], channel: Optional[str], mode: str = "relay") -> str:
    """Resolve a channel name/id to a channel id string.

    Precedence: explicit ``channel`` arg (a config name or a raw numeric id),
    otherwise the configured ``default_channel``. Whether a name is treated as
    an arbitrary local room depends on the ``mode``'s transport (relay vs chat),
    so the two modes can run different backends. In **local** transport, any
    string is a valid room name (stored as a JSONL file), and the default relay
    room falls back to ``"relay"`` so local mode needs zero channel setup.
    """
    channels = cfg.get("channels") or {}
    local = is_local(cfg, mode)
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
        return resolve_channel(cfg, channel, mode="chat")
    fallback = cfg.get("chat_channel") or cfg.get("default_channel")
    if fallback is None:
        if is_local(cfg, "chat"):
            fallback = "chat"  # built-in default local chat room, distinct from relay
        else:
            raise ConfigError(
                "No chat channel specified and none configured. Pass channel=<name-or-id>, "
                "or set a shared room with: discordinator config set-chat-channel <name-or-id> "
                "(or export DISCORDINATOR_CHAT_CHANNEL)."
            )
    return resolve_channel(cfg, fallback, mode="chat")


# -- relay cursor state ----------------------------------------------------
# Tracks the last message id seen per channel so relay/get_new_messages can
# return only fresh messages. Stored separately from config (no secrets here).


def state_path() -> Path:
    return config_path().parent / "state.json"


def load_state() -> dict[str, Any]:
    data = read_json(state_path(), {})
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, Any]) -> None:
    _atomic_write(state_path(), json.dumps(state, indent=2))


@contextmanager
def update_state() -> Iterator[dict[str, Any]]:
    """Read-modify-write state.json under a cross-process lock. Every session on
    the machine shares this file (each with its own cursors), so an unlocked
    update could write a stale copy back over another session's fresh one."""
    with FileLock(state_path()):
        state = load_state()
        yield state
        save_state(state)


# Each reader (a session's label) has its own position, so two sessions on one
# machine relaying to each other don't consume each other's messages. A reader
# with no position of its own yet starts from the old shared one ("cursors").


def get_cursor(channel_id: str, reader: Optional[str] = None) -> Optional[str]:
    state = load_state()
    if reader:
        mine = (state.get("relay_cursors") or {}).get(str(channel_id)) or {}
        if reader in mine:
            return mine[reader] or None  # None: reset, read from scratch
    return (state.get("cursors") or {}).get(str(channel_id))


def set_cursor(channel_id: str, message_id: str, reader: Optional[str] = None) -> None:
    with update_state() as state:
        if reader:
            state.setdefault("relay_cursors", {}).setdefault(
                str(channel_id), {})[reader] = str(message_id)
        else:
            state.setdefault("cursors", {})[str(channel_id)] = str(message_id)


def clear_cursor(channel_id: str, reader: Optional[str] = None) -> None:
    with update_state() as state:
        if reader:
            state.setdefault("relay_cursors", {}).setdefault(
                str(channel_id), {})[reader] = None
        else:
            cursors = state.get("cursors") or {}
            cursors.pop(str(channel_id), None)
            state["cursors"] = cursors
