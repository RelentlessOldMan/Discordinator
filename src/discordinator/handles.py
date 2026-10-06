"""Chat handle resolution: one recognizable name per session.

A session's chat handle is built from the project's fixed ``chat_handle``
(``DISCORDINATOR_CHAT_HANDLE`` in its .mcp.json) plus an optional role the
session picks (``chatter="ui"`` -> ``CodeCarver/ui``), so a project is always
recognizable and never drifts to another spelling.

Because two sessions in the same project directory get the same project handle,
each MCP server process (= one session) also *claims* its handle in a small
machine-wide registry (``~/.discordinator/handles.json``). If another live
process on this machine already holds it, this one gets ``<handle>-2`` (then
``-3``, ...) instead — so two sessions can never silently share a name and
ignore each other's turns. Claims are released at exit, and a claim whose
process is gone (or that hasn't been refreshed for a day) is ignored, so a
restarted session gets its name back.

The registry is per machine. Across machines, give each machine's project its
own handle in that machine's .mcp.json (e.g. ``CodeCarverWork``).
"""

from __future__ import annotations

import atexit
import json
import os
import time
from typing import Any, Optional

from . import chat, config
from .local_client import _AppendLock

# A claim not refreshed for this long is ignored even if its pid looks alive —
# guards against a recycled pid holding a name forever.
CLAIM_TTL = 24 * 3600.0

# This process's resolved handles: requested key -> handle actually in use.
_resolved: dict[str, str] = {}


def registry_path():
    return config.config_path().parent / "handles.json"


def pid_alive(pid: int) -> bool:
    """True if a process with this pid is running. Never signals the process
    (on Windows, ``os.kill(pid, 0)`` would TERMINATE it, so query instead)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: exists
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def compose(chatter: Optional[str], base: Optional[str]) -> str:
    """The handle a session asks for: ``base/role`` when the project has a fixed
    handle and the session names a role; the bare base when it doesn't; the
    explicit chatter as-is when there's no base. A chatter that already is (or
    starts with) the base isn't doubled up."""
    chatter = None if chatter in (None, "") else str(chatter).strip()
    base = None if base in (None, "") else str(base).strip()
    if not chatter and not base:
        raise config.ConfigError(
            "No chat handle: pass chatter=\"...\", or (recommended) give this project "
            "a fixed handle by setting DISCORDINATOR_CHAT_HANDLE in its .mcp.json env."
        )
    if not base:
        return chat.sanitize_handle(chatter)
    if not chatter or chat.same_handle(chatter, base):
        return chat.sanitize_handle(base)
    if chat.handle_key(chatter).startswith(chat.handle_key(base) + "/"):
        return chat.sanitize_handle(chatter)
    return chat.sanitize_handle(f"{base}/{chatter}")


def _with_suffix(handle: str, n: int) -> str:
    suffix = f"-{n}"
    return handle[: 32 - len(suffix)] + suffix


def _load(path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _held_by_other(entry: Any, me: int, now: float) -> bool:
    if not isinstance(entry, dict):
        return False
    pid = entry.get("pid")
    if pid == me:
        return False
    if now - float(entry.get("ts", 0) or 0) > CLAIM_TTL:
        return False
    return pid_alive(pid)


def claim(desired: str) -> str:
    """Claim ``desired`` for this process (or the first free ``-N`` variant) and
    return the handle to use. Re-claiming refreshes the lease."""
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    me, now = os.getpid(), time.time()
    with _AppendLock(path):
        reg = _load(path)
        handle, n = desired, 1
        while _held_by_other(reg.get(chat.handle_key(handle)), me, now):
            n += 1
            handle = _with_suffix(desired, n)
        # Prune dead/expired claims while we hold the lock.
        reg = {k: v for k, v in reg.items()
               if isinstance(v, dict) and (v.get("pid") == me or _held_by_other(v, me, now))}
        reg[chat.handle_key(handle)] = {"handle": handle, "pid": me, "ts": now}
        config._atomic_write(path, json.dumps(reg, indent=2, sort_keys=True))
    return handle


def resolve(chatter: Optional[str], cfg: dict[str, Any]) -> tuple[str, Optional[str]]:
    """This session's handle for a chat_* call, plus a note if it had to be
    renamed to avoid another live session. Stable for the life of the process:
    the same request always resolves to the same handle."""
    desired = compose(chatter, cfg.get("chat_handle"))
    key = chat.handle_key(desired)
    handle = claim(_resolved.get(key, desired))
    _resolved[key] = handle
    note = None
    if not chat.same_handle(handle, desired):
        note = (f"'{desired}' is already in use by another live session on this "
                f"machine, so you are '{handle}'. Pass chatter=\"<role>\" to pick a "
                f"clearer name (e.g. chatter=\"ui\" -> '{desired}/ui').")
    return handle, note


def release_all() -> None:
    """Drop this process's claims (run at exit)."""
    path = registry_path()
    if not path.exists():
        return
    me = os.getpid()
    try:
        with _AppendLock(path):
            reg = _load(path)
            kept = {k: v for k, v in reg.items()
                    if not (isinstance(v, dict) and v.get("pid") == me)}
            if kept != reg:
                config._atomic_write(path, json.dumps(kept, indent=2, sort_keys=True))
    except OSError:
        pass


atexit.register(release_all)
