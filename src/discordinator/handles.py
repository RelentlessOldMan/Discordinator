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

The registry is per machine. Across machines the machine name tells sessions
apart: chats use the full name ``<machine>:<handle>`` (see config.full_name),
and each claim records the machine name its session runs under.

A session's name also survives its MCP server restarting (an /mcp reconnect):
each process records the role it uses and the names it holds under its
*session* - the Claude Code process that launched it (its parent, or above
any launcher in between, such as discordinator-mcp.exe or a python.exe
running it; the session lives on while the server restarts). The name it goes
by stays reserved for the session while it runs, so another session can't
take it in the gap of a restart. A new server with the same parent picks
the name back up, and takes over a claim the old server (still winding down)
hasn't released yet, instead of becoming "-2" and missing turns sent to it.
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
_restored = False  # this process has looked up its session's earlier identity
_current: Optional[str] = None  # the handle this session's last chat call used
# This machine's label (config set-label) as this session last saw it ("" for
# none; None until it has), so after a rename its relay read position starts
# where the one under the old label got to.
last_label: Optional[str] = None
# When this session first took each of its names (handle key -> unix time), so
# a turn sent before it was running isn't counted as possibly meant for it.
_since: dict[str, float] = {}
# Every full name this session has gone by (machine:handle), so a turn sent to
# one it had before - e.g. under its machine's old name - can be pointed out.
_full_used: list[str] = []

SESSION_TTL = 7 * 24 * 3600.0  # forget a session record after a week unused
# A session named by DISCORDINATOR_SESSION_ID has no process to watch: its name
# is held for it while its server runs, and this long after (a restart).
RESTART_GRACE = 120.0


def registry_path():
    return config.config_path().parent / "handles.json"


def sessions_path():
    return config.config_path().parent / "sessions.json"


_SESSION_KEY: Optional[str] = None


def session_key() -> Optional[str]:
    """Which session this server belongs to: DISCORDINATOR_SESSION_ID if set,
    else the launching process (pid + start time, so a recycled pid is a
    different session). None if that can't be told."""
    global _SESSION_KEY
    if _SESSION_KEY is None:
        explicit = (os.environ.get("DISCORDINATOR_SESSION_ID") or "").strip()
        if explicit:
            _SESSION_KEY = "id:" + explicit
        else:
            try:
                ppid = session_pid(os.getppid())
            except (OSError, AttributeError):
                return None
            started = process_started(ppid)
            if started is None:
                return None
            _SESSION_KEY = f"{ppid}:{started}"
    return _SESSION_KEY


def _is_launcher(name: Optional[str], pid: Optional[int] = None) -> bool:
    """A process that only starts the server: the pip console-script launcher
    (discordinator-mcp.exe) or a Python launcher/venv shim (python.exe, py.exe)
    running it - a new one each time the server starts, so never the session
    itself. A Python process running anything else (a Python MCP host, a test
    harness) is the session - and so is one whose command line can't be read
    (its restarts then come back under a new name, but two such hosts never
    share one)."""
    n = (name or "").casefold()
    if n.endswith(".exe"):
        n = n[:-4]
    if n.startswith("discordinator"):
        return True
    if not (n.startswith("python") or n in ("py", "pyw")):
        return False
    cmd = process_cmdline(pid) if pid else None
    return cmd is not None and any(s in cmd.casefold()
                                   for s in ("discordinator.mcp_server", "discordinator-mcp"))


def session_pid(ppid: int) -> int:
    """The session process: ``ppid``, or above it past any launchers (Windows
    starts the real interpreter as a child of discordinator-mcp.exe or of a
    venv's python.exe)."""
    pid = ppid
    for _ in range(4):
        parent, name = process_parent(pid)
        if not parent or not _is_launcher(name, pid):
            break
        pid = parent
    return pid


def process_cmdline(pid: int) -> Optional[str]:
    """``pid``'s command line, or None if it can't be read."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            ntdll = ctypes.WinDLL("ntdll")
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            ntdll.NtQueryInformationProcess.restype = ctypes.c_long
            ntdll.NtQueryInformationProcess.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                ctypes.POINTER(wintypes.ULONG)]

            class UNICODE_STRING(ctypes.Structure):
                _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                            ("Buffer", ctypes.c_void_p)]

            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                size = wintypes.ULONG(0)
                buf = ctypes.create_string_buffer(4096)
                for _ in range(2):
                    # ProcessCommandLineInformation (60): Windows 8.1+.
                    status = ntdll.NtQueryInformationProcess(
                        handle, 60, buf, len(buf), ctypes.byref(size))
                    if status == 0:
                        us = UNICODE_STRING.from_buffer(buf)
                        return ctypes.wstring_at(us.Buffer, us.Length // 2)
                    if size.value <= len(buf):
                        return None
                    buf = ctypes.create_string_buffer(size.value)
                return None
            finally:
                kernel32.CloseHandle(handle)
        if not _HAS_PROC:
            return _ps(pid, "command")
        with open(f"/proc/{pid}/cmdline", "rb") as fh:  # Linux
            return fh.read().replace(b"\0", b" ").decode(errors="replace")
    except (OSError, ValueError, AttributeError):
        return None


# macOS and the BSDs have no /proc: ask ps instead.
_HAS_PROC = os.name == "nt" or os.path.exists("/proc/self/stat")


def _ps(pid: int, field: str) -> Optional[str]:
    """One ``ps`` field for ``pid``, or None (no such process, or no ps)."""
    import subprocess
    try:
        out = subprocess.run(["ps", "-o", f"{field}=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=5,
                             env={**os.environ, "LC_ALL": "C"})
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def process_parent(pid: int) -> tuple[Optional[int], Optional[str]]:
    """(parent pid, executable name) of ``pid``, or (None, None) if unknown."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                            ("th32ProcessID", wintypes.DWORD),
                            ("th32DefaultHeapID", ctypes.c_void_p),
                            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                            ("th32ParentProcessID", wintypes.DWORD),
                            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                            ("szExeFile", ctypes.c_wchar * 260)]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
            kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
            if not snap or snap == wintypes.HANDLE(-1).value:
                return None, None
            try:
                e = PROCESSENTRY32W()
                e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                ok = kernel32.Process32FirstW(snap, ctypes.byref(e))
                while ok:
                    if e.th32ProcessID == pid:
                        return int(e.th32ParentProcessID), e.szExeFile
                    ok = kernel32.Process32NextW(snap, ctypes.byref(e))
            finally:
                kernel32.CloseHandle(snap)
            return None, None
        if not _HAS_PROC:
            ppid, comm = _ps(pid, "ppid"), _ps(pid, "comm")
            return (int(ppid), os.path.basename(comm)) if ppid and comm else (None, None)
        with open(f"/proc/{pid}/stat", "rb") as fh:  # Linux: "pid (comm) state ppid ..."
            raw = fh.read()
        comm = raw[raw.index(b"(") + 1:raw.rindex(b")")].decode(errors="replace")
        return int(raw.rsplit(b")", 1)[1].split()[1]), comm
    except (OSError, ValueError, IndexError, AttributeError):
        return None, None


def _session_alive(key: str, now: float, entry: Any) -> bool:
    if not isinstance(entry, dict) or now - float(entry.get("ts", 0) or 0) > SESSION_TTL:
        return False
    if key.startswith("id:"):
        return True
    try:
        pid, started = key.split(":", 1)
        return process_started(int(pid)) == int(started)
    except ValueError:
        return False


def restore() -> None:
    """Pick up this session's identity from before its server restarted (once,
    before this process has a name of its own)."""
    global _restored, _last_chatter
    if _restored:
        return
    _restored = True
    key = session_key()
    if not key or _resolved or _last_chatter is not None:
        return
    entry = _load(sessions_path()).get(key)
    if not isinstance(entry, dict) or not _session_alive(key, time.time(), entry):
        return
    if isinstance(entry.get("chatter"), str):
        _last_chatter = entry["chatter"]
    resolved = entry.get("resolved")
    if isinstance(resolved, dict):
        _resolved.update({k: v for k, v in resolved.items()
                          if isinstance(k, str) and isinstance(v, str)})
    since = entry.get("since")
    if isinstance(since, dict):
        _since.update({k: float(v) for k, v in since.items()
                       if isinstance(k, str) and isinstance(v, (int, float))})
    full = entry.get("full")
    if isinstance(full, list):
        _full_used.extend(h for h in full if isinstance(h, str) and h not in _full_used)
    global last_label
    if last_label is None and isinstance(entry.get("label"), str):
        last_label = entry["label"]


def note_full(name: str) -> None:
    """Record a full name this session goes by (see _full_used)."""
    if name not in _full_used:
        _full_used.append(name)
        _remember()


def full_names_used() -> list[str]:
    return list(_full_used)


def renamed_sessions() -> list[tuple[str, str, list[str]]]:
    """(session key, the full name it goes by now, its retired full names)
    for each session running on this machine: the names it went by before its
    machine was renamed - the name it goes by now under the old machine name -
    that no running session goes by now."""
    now = time.time()
    out: list[tuple[str, str, list[str]]] = []
    data = _load(sessions_path())
    mine = session_key()
    held = {chat.handle_key(h) for h, _ in live_full_claims()}
    for key, entry in data.items():
        if not isinstance(entry, dict) or not _session_alive(key, now, entry):
            continue
        full = entry.get("full") if key != mine else _full_used
        handle, machine = entry.get("handle"), _machine_in(entry)
        current = f"{machine}:{handle}" if isinstance(handle, str) and machine else None
        if key == mine and _current is not None:
            current = f"{_machine()}:{_current}"
        if current is None or (key != mine and chat.handle_key(current) not in held):
            continue  # (a session that isn't running now goes by no name)
        out.append((key, current, [
            h for h in (full if isinstance(full, list) else [])
            if (isinstance(h, str) and not chat.same_handle(h, current)
                and chat.same_handle(chat.local_name(h), chat.local_name(current))
                and chat.handle_key(h) not in held
                # every unnamed machine is DefaultMachineName: that name may
                # well be another machine's session now
                and not chat.same_handle(chat.machine_of(h), config.DEFAULT_MACHINE_NAME))]))
    return out


def _remember() -> None:
    """Record this session's role and names, for its next server to pick up."""
    key = session_key()
    if not key:
        return
    path = sessions_path()
    now = time.time()
    try:
        with _AppendLock(path):
            data = _load_to_write(path)
            data = {k: v for k, v in data.items() if _session_alive(k, now, v)}
            before = data.get(key) if isinstance(data.get(key), dict) else {}
            data[key] = {"chatter": _last_chatter, "resolved": dict(_resolved),
                         # (a restarted server that hasn't made a chat call yet
                         # still goes by the name its session had)
                         "handle": _current if _current is not None else before.get("handle"), "since": dict(_since), "ts": now,
                         "full": list(_full_used), "label": last_label,
                         "pid": os.getpid(), "started": _my_start(), "machine": _machine(),
                         "own_label": _own_label()}
            config._atomic_write(path, json.dumps(data, indent=2, sort_keys=True))
    except OSError:
        pass  # only a convenience for the next restart


pid_alive = config.pid_alive  # (kept here: the handle registry's name for it)


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
        if not _HAS_PROC:
            started = _ps(pid, "lstart")  # e.g. "Thu Oct  8 22:06:49 2026"
            return int(time.mktime(time.strptime(started, "%a %b %d %H:%M:%S %Y"))) if started else None
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
    # the project name as chats show it (config.project_name), so a role is
    # checked against the same name everywhere
    base = config.project_name({"chat_handle": base})
    if not chatter and not base:
        base = config.DEFAULT_CHAT_NAME  # no name set anywhere: an obvious placeholder
    if not base:
        return chat.check_own_handle(chat.sanitize_local(chatter))
    if not chatter or chat.same_handle(chatter, base):
        return chat.check_own_handle(chat.sanitize_local(base))
    if chat.handle_key(chatter).startswith(chat.handle_key(base) + "/"):
        return chat.check_own_handle(chat.sanitize_local(chatter))
    return chat.check_own_handle(chat.sanitize_local(f"{base}/{chatter}"))


def _with_suffix(handle: str, n: int) -> str:
    suffix = f"-{n}"
    return handle[: 32 - len(suffix)] + suffix


def _load(path) -> dict[str, Any]:
    try:
        return _load_to_write(path)
    except OSError:
        return {}


def _load_to_write(path) -> dict[str, Any]:
    """_load for a read that's written back: a file that stays unreadable
    raises (OSError) - written back as empty, it would drop every other
    session's entry."""
    data = config.read_json(path, {})  # retries while another session swaps it
    return data if isinstance(data, dict) else {}


def _reserved(now: float) -> dict[str, str]:
    """Names other running sessions are known by (handle key -> handle), from
    their session records: held for them while their server restarts, so a
    new session can't take the name - and the turns sent to it - in the gap
    before their next server claims it again."""
    return {k: h for k, (h, _, _) in _reserved_since(now).items()}


def _reserved_since(now: float) -> dict[str, tuple[str, Optional[float], Optional[str]]]:
    """_reserved, with when each session took the name (None if unknown) and
    the machine name it runs under (None in a record from before v1.0.48),
    keyed by full name (see _key)."""
    mine = session_key()
    out: dict[str, tuple[str, Optional[float], Optional[str]]] = {}
    for key, entry in _load(sessions_path()).items():
        if key == mine or not _session_alive(key, now, entry):
            continue
        if key.startswith("id:"):
            # No session process to watch: held while its server runs, and
            # briefly after (a restart) - not for the week the record is kept.
            pid, started = entry.get("pid"), entry.get("started")
            running = (isinstance(pid, int) and started is not None
                       and process_started(pid) == started)
            if not running and now - float(entry.get("ts", 0) or 0) > RESTART_GRACE:
                continue
        elif not isinstance(entry.get("handle"), str):
            # A record from before v1.0.40 may be keyed by a launcher (before
            # v1.0.39), which is not a session. (Newer ones never are.)
            pid = key.split(":", 1)[0]
            if pid.isdigit() and _is_launcher(process_parent(int(pid))[1], int(pid)):
                continue
        # The name it goes by now (older records: every name it has used).
        names = ([entry["handle"]] if isinstance(entry.get("handle"), str)
                 else (entry.get("resolved") or {}).values())
        since = entry.get("since") if isinstance(entry.get("since"), dict) else {}
        for h in names:
            if isinstance(h, str):
                t = since.get(chat.handle_key(h))
                out[_key(h, _machine_in(entry))] = (
                    h, float(t) if isinstance(t, (int, float)) else None, _machine_in(entry))
    return out


def _key(handle: str, machine: Optional[str] = None) -> str:
    """The registry key for ``handle`` claimed under ``machine`` (this
    session's when None - a claim from before v1.0.48 didn't say): its full
    name. Sessions here with their own machine names (a project's
    DISCORDINATOR_LABEL) can each have the same name."""
    return chat.handle_key(f"{machine or _machine()}:{handle}")


def _claims_of(reg: dict[str, Any], handle: str) -> list[Any]:
    """The claims on ``handle`` under this session's machine name: its own
    (under the machine's name before a rename too, see _machine_in), and an
    older version's (kept with no machine - a session on this machine too)."""
    here = chat.handle_key(_machine())
    own = reg.get(_key(handle))
    if isinstance(own, dict) and chat.handle_key(_machine_in(own) or here) != here:
        own = None  # (a claim kept under the machine's old name: it goes by the new one)
    return [own] + [e for e in reg.values() if e is not own and isinstance(e, dict)
                    and chat.same_handle(e.get("handle"), handle)
                    and chat.handle_key(_machine_in(e) or here) == here]


def _rekeyed(reg: dict[str, Any]) -> dict[str, Any]:
    """The registry with each claim under the full name its session goes by
    now (see _machine_in) - one kept under the machine's name from before a
    `config set-label` moves to the new one, so it never sits on a key another
    session here (with that name as its own label) is about to claim."""
    out: dict[str, Any] = {}
    moved = []
    for k, v in reg.items():
        h = v.get("handle") if isinstance(v, dict) else None
        if isinstance(h, str) and _machine_in(v) is not None and k != _key(h, _machine_in(v)):
            moved.append((_key(h, _machine_in(v)), v))
        else:
            out[k] = v
    for k, v in moved:
        out.setdefault(k, v)
    return out


def _taken(reg: dict[str, Any], handle: str, me: int, now: float) -> bool:
    return any(_held_by_other(e, me, now) for e in _claims_of(reg, handle))


def _recorded_machine(entry: Any) -> Optional[str]:
    m = entry.get("machine") if isinstance(entry, dict) else None
    return m if isinstance(m, str) and m else None


def _machine_in(entry: Any) -> Optional[str]:
    """The machine name a claim's (or session record's) session goes by: the
    one it recorded if a project set its own (DISCORDINATOR_LABEL), else this
    machine's name now - so a running session keeps its name through
    `config set-label` before its next call records the new one. None for a
    record from before v1.0.48."""
    if isinstance(entry, dict) and entry.get("own_label") is False:
        return _config_machine() or _recorded_machine(entry)
    return _recorded_machine(entry)


def _own_label() -> bool:
    """Does this session run under a machine name of its own (a project's
    DISCORDINATOR_LABEL), not this machine's?"""
    return bool(os.environ.get("DISCORDINATOR_LABEL"))


_config_machine_cache: list[Any] = [None, None]  # (config file mtime and size, name)


def _config_machine() -> Optional[str]:
    """This machine's name as the config file has it now (read again only
    when the file changes); None if it can't be read."""
    try:
        path = config.config_path()
        st = path.stat() if path.exists() else None
        mtime = (st.st_mtime_ns, st.st_size) if st is not None else None  # (two saves in one tick)
        if _config_machine_cache[0] != mtime or _config_machine_cache[1] is None:
            _config_machine_cache[:] = [mtime, config.machine_name(config.load_file())]
        return _config_machine_cache[1]
    except Exception:  # noqa: BLE001 - mid-write or malformed: as recorded
        return None


def _machine() -> str:
    """The machine name this session runs under (its label, maybe set per
    project), as recorded with its claims."""
    return config.machine_name(config.load())


def _held_by_other(entry: Any, me: int, now: float) -> bool:
    if not isinstance(entry, dict):
        return False
    pid = entry.get("pid")
    if pid == me or not pid_alive(pid):
        return False
    started = entry.get("started")
    if entry.get("session") and entry.get("session") == session_key():
        # This session's previous server, still winding down - unless it
        # started after this one: then it's the newer server (or a second
        # client sharing the session, e.g. one DISCORDINATOR_SESSION_ID in
        # two sessions), and two live servers never share a name.
        return _newer_than_me(pid, started)
    current = process_started(pid) if started is not None else None
    if current is not None:
        return current == started  # same process: holds it, however long idle
    return now - float(entry.get("ts", 0) or 0) <= CLAIM_TTL


def _newer_than_me(pid: Any, started: Any) -> bool:
    mine = _my_start()
    if not isinstance(started, (int, float)) or mine is None:
        return False
    return (started, pid) > (mine, os.getpid())


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
    reserved = _reserved(now)
    with _AppendLock(path):
        reg = _load_to_write(path)
        handle, n = desired, 1
        while _taken(reg, handle, me, now) or _key(handle) in reserved:
            n += 1
            handle = _with_suffix(desired, n)
        key = _key(handle)
        prev = next((e for e in _claims_of(reg, handle) if e is not None), None)
        if chat.handle_key(handle) not in _since:
            # Since when this session has had the name: its own earlier claim
            # (or its previous server's, still winding down), else now.
            same = isinstance(prev, dict) and (prev.get("pid") == me or (
                prev.get("session") and prev.get("session") == session_key()))
            t = prev.get("since") if same else None
            _since[chat.handle_key(handle)] = float(t) if isinstance(t, (int, float)) else now
        # Prune dead/expired claims while we hold the lock - and, taking a role
        # of a project, the plain project name this session went by before (or,
        # taking any name, the placeholder it went by with none): it would block
        # the next session that wants it and look like a session still running
        # under it.
        bare = _resolved.get(chat.handle_key(handle.split("/", 1)[0])) if "/" in handle else None
        if bare is None and not config.is_default_chat_name(handle):
            bare = _resolved.get(chat.handle_key(config.DEFAULT_CHAT_NAME))
        dropped = _key(bare) if bare and "/" not in bare else None
        # (Its own claims under another machine name are from before the
        # machine was renamed: it goes by none of them now.)
        here = chat.handle_key(_machine())
        reg = {k: v for k, v in reg.items()
               if isinstance(v, dict) and (
                   k != dropped and chat.handle_key(_recorded_machine(v) or here) == here
                   if v.get("pid") == me else _held_by_other(v, me, now))}
        reg = _rekeyed(reg)
        reg[key] = {"handle": handle, "pid": me, "ts": now,
                    "since": _since[chat.handle_key(handle)],
                    "started": _my_start(), "session": session_key(), "machine": _machine(),
                    "own_label": _own_label()}
        config._atomic_write(path, json.dumps(reg, indent=2, sort_keys=True))
    return handle


def resolve(chatter: Optional[str], cfg: dict[str, Any],
            fresh: bool = False) -> tuple[str, Optional[str]]:
    """This session's handle for a chat_* call, plus a note if it had to be
    renamed to avoid another live session. Stable for the life of the session
    (across server restarts too): the same request resolves to the same handle.
    ``fresh`` (an explicit chat_begin) asks for the plain name again if it has
    come free since this session was renamed."""
    global _last_chatter, _current
    restore()
    if chatter in (None, "") and _last_chatter is not None:
        chatter = _last_chatter
    # A session renamed to e.g. "CodeCarver-2" that passes that name back as
    # `chatter` (as told: "pass the same chatter on every call") means itself,
    # not a role "CodeCarver/CodeCarver-2".
    held_key = next((k for k, h in _resolved.items() if chat.same_handle(h, chatter)), None)
    if held_key is not None:
        _last_chatter = str(chatter)
        held = _resolved[held_key]
        # Refresh the lease, as any call does - and if another session took the
        # name meanwhile (this one restarted), use what the claim gave us, never
        # the name the other session now holds.
        handle = claim(held)
        _resolved[held_key] = handle
        _current = handle
        _remember()
        return handle, (None if chat.same_handle(handle, held)
                        else _rename_note(held, handle, cfg))
    desired = compose(chatter, cfg.get("chat_handle"))
    if chatter not in (None, ""):
        _last_chatter = str(chatter)
    key = chat.handle_key(desired)
    handle = claim(desired if fresh else _resolved.get(key, desired))
    _resolved[key] = handle
    _current = handle
    _remember()
    note = None
    if not chat.same_handle(handle, desired):
        note = _rename_note(desired, handle, cfg)
    return handle, note


def _rename_note(desired: str, handle: str, cfg: dict[str, Any]) -> str:
    taken = {chat.handle_key(h) for h in live_handles()}
    role, example = next(
        ((r, e) for r, e in ((r, compose(r, cfg.get("chat_handle")))
                             for r in ("ui", "api", "docs", "tests", "review", "work"))
         if chat.handle_key(e) not in taken),
        ("role2", compose("role2", cfg.get("chat_handle"))))
    def full(h: str) -> str:
        return config.full_name(cfg, h)
    return (f"'{full(desired)}' is already in use by another live session on this "
            f"machine, so you are '{full(handle)}'. Pass chatter=\"<role>\" to pick a "
            f"clearer name (e.g. chatter=\"{role}\" -> '{full(example)}').")


def held_since(handle: Optional[str]) -> Optional[float]:
    """When this session took ``handle`` (unix time), or None if it never has."""
    return _since.get(chat.handle_key(handle))


def current(chatter: Optional[str], cfg: dict[str, Any]) -> Optional[str]:
    """The handle this session would use, WITHOUT claiming anything (for
    read-only calls like chat_status). None when no handle can be formed."""
    restore()
    if chatter in (None, "") and _last_chatter is not None:
        chatter = _last_chatter
    # A name this session already has (e.g. "CodeCarver-2" passed back) is
    # itself, as in resolve - not a role "CodeCarver/CodeCarver-2".
    mine = next((h for h in _resolved.values() if chat.same_handle(h, chatter)), None)
    if mine is not None:
        return mine
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
    reserved = _reserved(now)
    handle, n = desired, 1
    while _taken(reg, handle, me, now) or _key(handle) in reserved:
        n += 1
        handle = _with_suffix(desired, n)
    return handle


def live_handles() -> list[str]:
    """Handles currently held by live sessions on this machine under this
    session's machine name (including a running session's names while its
    server restarts)."""
    return [h for h, _ in live_claims()]


def live_claims() -> list[tuple[str, Optional[float]]]:
    """live_handles, each with when its session took it (unix time; None if
    unknown)."""
    here = chat.handle_key(_machine())
    return [(h, since) for h, since, m in _live() if m is None or chat.handle_key(m) == here]


def live_full_claims() -> list[tuple[str, Optional[float]]]:
    """live_claims by full name: each with the machine name its own session
    runs under (a project can set its own), this one's for an older claim."""
    here = _machine()
    return [(f"{m or here}:{h}", since) for h, since, m in _live()]


def _live() -> list[tuple[str, Optional[float], Optional[str]]]:
    reg = _load(registry_path())
    me, now = os.getpid(), time.time()
    out: dict[str, tuple[str, Optional[float], Optional[str]]] = {}
    for v in reg.values():
        if (isinstance(v, dict) and isinstance(v.get("handle"), str)
                and (v.get("pid") == me or _held_by_other(v, me, now))):
            t = v.get("since")
            out[_key(v["handle"], _machine_in(v))] = (
                v["handle"], float(t) if isinstance(t, (int, float)) else None, _machine_in(v))
    for k, hs in _reserved_since(now).items():
        out.setdefault(k, hs)
    return list(out.values())


def release_all() -> None:
    """Drop this process's claims (run at exit)."""
    path = registry_path()
    if not path.exists():
        return
    me = os.getpid()
    try:
        with _AppendLock(path):
            reg = _load_to_write(path)
            kept = {k: v for k, v in reg.items()
                    if not (isinstance(v, dict) and v.get("pid") == me)}
            if kept != reg:
                config._atomic_write(path, json.dumps(kept, indent=2, sort_keys=True))
    except OSError:
        pass


atexit.register(release_all)
