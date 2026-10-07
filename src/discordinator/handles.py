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
process is gone is ignored, so a restarted session gets its name back. Each
claim records its process's start time, so a recycled pid can't hold a dead
session's name, and a live session keeps its name however long it's idle.

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

# Fallback only, for a claim whose process start time can't be compared (an
# old registry entry, or a platform without it): ignored after this long, so
# a recycled pid can't hold a name forever.
CLAIM_TTL = 24 * 3600.0

# This process's resolved handles: requested key -> handle actually in use.
_resolved: dict[str, str] = {}
# The role (chatter) this session last asked for, used when a later call omits
# it - so forgetting `chatter` once doesn't switch the session's identity.
_last_chatter: Optional[str] = None


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


def process_started(pid: int) -> Optional[int]:
    """An opaque start-time stamp for ``pid`` (same process -> same value, a
    recycled pid -> a different one), or None if it can't be read."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
                ctypes.POINTER(wintypes.FILETIME)] * 4
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                # An exited process still answers while anything holds a handle
                # to it - it has no live start time.
                code = wintypes.DWORD()
                if (not kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                        or code.value != 259):  # STILL_ACTIVE
                    return None
                t = [wintypes.FILETIME() for _ in range(4)]
                if not kernel32.GetProcessTimes(handle, *[ctypes.byref(x) for x in t]):
                    return None
                return (t[0].dwHighDateTime << 32) | t[0].dwLowDateTime
            finally:
                kernel32.CloseHandle(handle)
        with open(f"/proc/{pid}/stat", "rb") as fh:  # Linux; field 22 = starttime
            return int(fh.read().rsplit(b")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError, AttributeError):
        return None


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
        data = config.read_json(path, {})  # retries while another session swaps it
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


def _held_by_other(entry: Any, me: int, now: float) -> bool:
    if not isinstance(entry, dict):
        return False
    pid = entry.get("pid")
    if pid == me or not pid_alive(pid):
        return False
    started = entry.get("started")
    current = process_started(pid) if started is not None else None
    if current is not None:
        return current == started  # same process: holds it, however long idle
    return now - float(entry.get("ts", 0) or 0) <= CLAIM_TTL


def _my_start() -> Optional[int]:
    global _MY_START
    if _MY_START is None:
        _MY_START = process_started(os.getpid())
    return _MY_START


_MY_START: Optional[int] = None


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
        reg[chat.handle_key(handle)] = {"handle": handle, "pid": me, "ts": now,
                                        "started": _my_start()}
        config._atomic_write(path, json.dumps(reg, indent=2, sort_keys=True))
    return handle


def resolve(chatter: Optional[str], cfg: dict[str, Any]) -> tuple[str, Optional[str]]:
    """This session's handle for a chat_* call, plus a note if it had to be
    renamed to avoid another live session. Stable for the life of the process:
    the same request always resolves to the same handle."""
    global _last_chatter
    if chatter in (None, "") and _last_chatter is not None:
        chatter = _last_chatter
    # A session renamed to e.g. "CodeCarver-2" that passes that name back as
    # `chatter` (as told: "pass the same chatter on every call") means itself,
    # not a role "CodeCarver/CodeCarver-2".
    held = next((h for h in _resolved.values() if chat.same_handle(h, chatter)), None)
    if held is not None:
        _last_chatter = str(chatter)
        claim(held)  # refresh the lease, as any call does
        return held, None
    desired = compose(chatter, cfg.get("chat_handle"))
    if chatter not in (None, ""):
        _last_chatter = str(chatter)
    key = chat.handle_key(desired)
    handle = claim(_resolved.get(key, desired))
    _resolved[key] = handle
    note = None
    if not chat.same_handle(handle, desired):
        note = (f"'{desired}' is already in use by another live session on this "
                f"machine, so you are '{handle}'. Pass chatter=\"<role>\" to pick a "
                f"clearer name (e.g. chatter=\"ui\" -> '{desired}/ui').")
    return handle, note


def current(chatter: Optional[str], cfg: dict[str, Any]) -> Optional[str]:
    """The handle this session would use, WITHOUT claiming anything (for
    read-only calls like chat_status). None when no handle can be formed."""
    if chatter in (None, "") and _last_chatter is not None:
        chatter = _last_chatter
    try:
        desired = compose(chatter, cfg.get("chat_handle"))
    except config.ConfigError:
        return None
    held = _resolved.get(chat.handle_key(desired))
    if held is not None:
        return held
    # Not claimed yet: the name chat_begin would give, so a second session of
    # the project isn't answered for its sibling (which holds the bare name).
    reg = _load(registry_path())
    me, now = os.getpid(), time.time()
    handle, n = desired, 1
    while _held_by_other(reg.get(chat.handle_key(handle)), me, now):
        n += 1
        handle = _with_suffix(desired, n)
    return handle


def live_handles() -> list[str]:
    """Handles currently held by live sessions on this machine."""
    reg = _load(registry_path())
    me, now = os.getpid(), time.time()
    return [v["handle"] for v in reg.values()
            if isinstance(v, dict) and isinstance(v.get("handle"), str)
            and (v.get("pid") == me or _held_by_other(v, me, now))]


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
