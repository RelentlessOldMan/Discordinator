"""A per-machine log of things that happen to chat sessions but never reach a
room: a session's server starting or exiting, joining a room, being renamed or
taking its name back, a chat call refused or failing. `discordinator watch`
merges these into its view, so a human can see why a chat went quiet.

One JSON line per event in ~/.discordinator/events.jsonl. Writing is best
effort (it must never break a tool call), and old lines are dropped once the
file grows, like local rooms (7 days).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import config

KEEP = timedelta(days=7)
TRIM_AT = 512 * 1024  # bytes: prune old lines once the log is this big


def log_path():
    return config.config_path().parent / "events.jsonl"


def record(kind: str, **fields: Any) -> None:
    """Append one event. Never raises."""
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, "pid": os.getpid()}
    entry.update({k: v for k, v in fields.items() if v is not None})
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if path.stat().st_size > TRIM_AT:
            _trim(path)
    except Exception:  # noqa: BLE001 - the log is a convenience
        pass


def _trim(path) -> None:
    horizon = datetime.now(timezone.utc) - KEEP
    with config.FileLock(path, timeout=1.0):
        kept = [line for line, e in _lines(path) if (_ts(e) or horizon) >= horizon]
        config._atomic_write(path, "".join(kept))


def _lines(path):
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict):
                    yield line, e
    except OSError:
        return


def _ts(e: dict[str, Any]) -> Optional[datetime]:
    try:
        t = datetime.fromisoformat(str(e.get("ts")))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def read(offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Events from byte ``offset`` on, and the offset to continue from."""
    path = log_path()
    try:
        size = path.stat().st_size
    except OSError:
        return [], 0
    if size < offset:
        offset = 0  # trimmed since: start over
    out = []
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return [], offset
    end = data.rfind(b"\n") + 1  # only whole lines; a half-written one waits
    for line in data[:end].splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out, offset + end


def recent(since: datetime) -> tuple[list[dict[str, Any]], int]:
    """Events at or after ``since``, and the offset to follow on from."""
    events, offset = read(0)
    return [e for e in events if (_ts(e) or since) >= since], offset


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

