"""A per-machine log of things that happen to chat sessions but never reach a
room: a session's server starting or exiting, joining a room, being renamed or
taking its name back, a chat call refused or failing. `discordinator watch`
merges these into its view, so a human can see why a chat went quiet.

One JSON line per event in ~/.discordinator/events.jsonl. Writing is best
effort (it must never break a tool call). Once the log grows past ``ROTATE_AT``
it becomes events.1.jsonl (replacing the one before) and a new log starts, so
an append never rewrites the file and a follower never loses its place.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import config

ROTATE_AT = 512 * 1024  # bytes: start a new log once this one is this big

# Where a follower is: the first line of the log it's reading (which names that
# log, since every line is unique) and the byte offset it read up to.
Cursor = tuple[Optional[bytes], int]


def log_path() -> Path:
    return config.config_path().parent / "events.jsonl"


def _rotated() -> Path:
    return log_path().with_name("events.1.jsonl")


def record(kind: str, **fields: Any) -> None:
    """Append one event. Never raises."""
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, "pid": os.getpid()}
    entry.update({k: v for k, v in fields.items() if v is not None})
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        try:  # so a rotation can't move the file away mid-append
            lock: Optional[config.FileLock] = config.FileLock(path, timeout=1.0).__enter__()
        except config.LockTimeout:
            lock = None  # a stuck lock mustn't cost the event
        try:
            if lock is not None and path.exists() and path.stat().st_size > ROTATE_AT:
                try:
                    os.replace(path, _rotated())  # once: Windows refuses while it's open
                except OSError:
                    pass  # rotate on a later event; this one is appended regardless
            with open(path, "ab") as fh:
                fh.write(line)
        finally:
            if lock is not None:
                lock.__exit__(None, None, None)
    except Exception:  # noqa: BLE001 - the log is a convenience
        pass


def _first_line(path: Path) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.readline() or None
    except OSError:
        return None


def _tail(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    """Whole-line events in ``path`` from byte ``offset``, and where they end."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return [], offset
    end = data.rfind(b"\n") + 1  # only whole lines; a half-written one waits
    out = []
    for line in data[:end].splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out, offset + end


def read(cursor: Any = 0) -> tuple[list[dict[str, Any]], Cursor]:
    """Events after ``cursor`` (0: every event kept, oldest first), and the
    cursor to continue from. A log rotated since is read to its end first."""
    path = log_path()
    current = _first_line(path)
    if not cursor:
        older, _ = _tail(_rotated(), 0)
        newer, offset = _tail(path, 0)
        return older + newer, (current, offset)
    mark, offset = cursor
    if current is None:
        return [], (mark, offset)
    if current == mark:
        evs, offset = _tail(path, offset)
        return evs, (current, offset)
    rest: list[dict[str, Any]] = []
    if mark is not None and _first_line(_rotated()) == mark:
        rest, _ = _tail(_rotated(), offset)  # the rest of the log we were following
    newer, offset = _tail(path, 0)
    return rest + newer, (current, offset)


def recent(since: datetime) -> tuple[list[dict[str, Any]], Cursor]:
    """Events at or after ``since``, and the cursor to follow on from."""
    events, cursor = read(0)
    return [e for e in events if (_ts(e) or since) >= since], cursor


def _ts(e: dict[str, Any]) -> Optional[datetime]:
    try:
        t = datetime.fromisoformat(str(e.get("ts")))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def describe(e: dict[str, Any]) -> str:
    """One line for a viewer."""
    who = e.get("handle") or e.get("project") or f"pid {e.get('pid')}"
    kind = e.get("kind")
    room = f" #{e['room']}" if e.get("room") else ""
    if kind == "server_start":
        where = f" in {e['cwd']}" if e.get("cwd") else ""
        return f"▶ {who}: session connected (server pid {e.get('pid')}{where})"
    if kind == "server_exit":
        return f"■ {who}: session disconnected (server pid {e.get('pid')})"
    if kind == "server_gone":
        return f"■ {who}: server pid {e.get('pid')} is gone (ended without saying so)"
    if kind == "joined":
        extra = " - picked up a turn owed to it" if e.get("recovered") else ""
        return f"→ {who} joined{room}{extra}"
    if kind == "renamed":
        return f"↻ {who}{room}: {e.get('note', '')}"
    if kind == "error":
        return f"✖ {who}{room}: {e.get('tool')} failed - {e.get('message', '')}"
    if kind == "lost_turn":
        return f"? {who}{room}: {e.get('note', '')}"
    return f"· {who}{room}: {kind}"
