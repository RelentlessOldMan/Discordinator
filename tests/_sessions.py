"""One MCP server is one session, with one name at a time. Tests that play
several chatters from one process make each one a session of its own: every
mcp tool call runs as the session its ``chatter`` names (a call without one
stays in the session that made the last call), with that session's own
server state swapped in.

    import _sessions; _sessions.install(mcp)
    with _sessions.one("ui"):  # chatter "ui": the session the last call was
        ...
"""

from __future__ import annotations

import functools
import json
from contextlib import contextmanager
from typing import Any, Iterator

from discordinator import chat, handles

_HANDLES = ("_SESSION_KEY", "_resolved", "_last_chatter", "_restored", "_current",
            "_since", "_full_used", "last_label")
_SERVER = ("_sent_ids", "_my_posts", "_named", "_lost_turn", "_told")
_TOOLS = ("chat_begin", "chat_say", "chat_await", "chat_status", "send_message",
          "send_file", "get_new_messages", "read_messages", "delete_messages",
          "purge_messages", "download_attachment")

_states: dict[str, dict[str, Any]] = {}
_alias: dict[str, str] = {}
_cur: list[str] = [""]
_pinned: list[bool] = [False]
_mcp: list[Any] = [None]


def _fresh(key: str) -> dict[str, Any]:
    return {"_SESSION_KEY": f"id:test-{key or 'default'}", "_resolved": {},
            "_last_chatter": None, "_restored": False, "_current": None, "_since": {},
            "_full_used": [], "last_label": None, "_sent_ids": set(), "_my_posts": [],
            "_named": [None], "_lost_turn": {}, "_told": {}}


def _raw(chatter: Any) -> str:
    return chat.handle_key(chat.local_name(str(chatter))) if chatter else ""


def _key(chatter: Any) -> str:
    k = _raw(chatter)
    return _alias.get(k, k)


def _save() -> None:
    st = _states.setdefault(_cur[0], {})
    st.update({n: getattr(handles, n) for n in _HANDLES})
    st.update({n: getattr(_mcp[0], n) for n in _SERVER})


def switch(chatter: Any) -> None:
    """Run the next calls as the session ``chatter`` names ("" or None: the
    session that gave no chatter)."""
    _to(_key(chatter))


def pin(session: str) -> None:
    """Run every call as session ``session``, whatever chatter it gives (a
    test that starts its sessions' servers itself), until unpin()."""
    _to("pin:" + session)
    _pinned[0] = True


def unpin() -> None:
    _pinned[0] = False


def end(session: str) -> None:
    """Session ``session`` (a pinned one) exits: its claims and record go."""
    was = _cur[0]
    _to("pin:" + session)
    handles.release_all()
    path = handles.sessions_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop(handles.session_key(), None)
        path.write_text(json.dumps(data), encoding="utf-8")
    except (OSError, ValueError):
        pass
    _to(was)


def _to(key: str) -> None:
    if key == _cur[0]:
        return
    _save()
    st = _states.setdefault(key, _fresh(key))
    for n in _HANDLES:
        setattr(handles, n, st[n])
    for n in _SERVER:
        setattr(_mcp[0], n, st[n])
    _cur[0] = key


@contextmanager
def one(*chatters: str) -> Iterator[None]:
    """Within it, ``chatters`` are all the session the last call ran as (one
    session going by another name: a role, say, or the name given none)."""
    keys = [_raw(c) for c in chatters]
    saved = {k: _alias.get(k) for k in keys}
    _alias.update({k: _cur[0] for k in keys if k != _cur[0]})
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                _alias.pop(k, None)
            else:
                _alias[k] = v


def install(mcp: Any) -> None:
    if _mcp[0] is not None:
        return
    _mcp[0] = mcp
    _states[""] = _fresh("")
    switch_to = _states[""]
    for n in _HANDLES:
        setattr(handles, n, switch_to[n])
    for n in _SERVER:
        setattr(mcp, n, switch_to[n])
    for name in _TOOLS:
        fn = getattr(mcp, name, None)
        if fn is None:
            continue

        @functools.wraps(fn)
        def call(*args: Any, __fn: Any = fn, **kw: Any) -> Any:
            if kw.get("chatter") and not _pinned[0]:
                switch(kw["chatter"])
            return __fn(*args, **kw)
        setattr(mcp, name, call)
