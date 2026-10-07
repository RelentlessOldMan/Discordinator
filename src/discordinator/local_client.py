"""Local (no-Discord) transport backend.

A drop-in replacement for :class:`~discordinator.discord_client.DiscordClient`
that stores each "channel" as an append-only JSONL file under
``~/.discordinator/local/``. Two sessions/processes on the **same machine** talk
through the shared files — the exact same relay + chat protocols as Discord, but
with no bot token and no network. (Cross-machine still needs the Discord
transport: there is no shared filesystem between machines.)

Message records mirror the subset of the Discord message shape that
``simplify_message()`` and the chat protocol consume::

    {"id", "author": {"id", "username", "global_name", "bot"},
     "timestamp", "content", "attachments"}

IDs are strictly increasing integer strings (``max(last + 1, time_ns())``), so
the ``after=`` / ``before=`` cursors and the protocol's integer id comparisons
behave like Discord snowflakes.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union

from . import config
from .discord_client import (
    MAX_MESSAGE_LEN,
    _unique_in_dir,
    attachment_info,
    chunk_content,
    guess_content_type,
)


def local_dir() -> Path:
    """Directory holding the per-room JSONL files (beside the config file)."""
    return config.config_path().parent / "local"


def _record_time(rec: Any) -> Optional[datetime]:
    """A record's UTC timestamp, or None if missing/unparseable (never pruned)."""
    try:
        ts = datetime.fromisoformat(str(rec["timestamp"]))
    except (KeyError, TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _image_dimensions(path: Path) -> Optional[tuple[int, int]]:
    """(width, height) for an image file via Pillow, or None if Pillow isn't
    installed or the file can't be read as an image. Discord fills these in
    server-side; local mode has no server, so this is best-effort parity and
    Pillow stays an OPTIONAL dependency."""
    try:
        from PIL import Image  # optional; not a hard dependency

        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:
        return None


def sanitize_room(name: str) -> str:
    """Map a room name to a safe filename stem (alnum/dash/underscore)."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(name))
    return safe.strip("-").lower() or "room"


# One lock implementation for every shared file (rooms, handles, state).
_AppendLock = config.FileLock

# A delete swaps in a rewritten room. Windows refuses while any session is
# reading it, so it tries for a moment (0.02s apart) and otherwise appends a
# deletion record instead (see delete_messages).
_DELETE_RETRIES = 25


class LocalClient:
    """Filesystem-backed transport with the DiscordClient method surface."""

    def __init__(self, label: Optional[str] = None, retention_days: Optional[float] = None):
        self._label = label or "local"
        self._dir = local_dir()
        # Messages older than this are pruned on write; 0 = keep forever.
        self._retention_days = float(
            config.DEFAULTS["local_retention_days"] if retention_days is None else retention_days
        )

    # -- context-manager parity with DiscordClient -------------------------
    def close(self) -> None:  # nothing to close
        pass

    def __enter__(self) -> "LocalClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- storage helpers ---------------------------------------------------
    def _room_path(self, channel_id: str) -> Path:
        return self._dir / f"{sanitize_room(channel_id)}.jsonl"

    def _read_all(self, channel_id: str) -> list[dict[str, Any]]:
        """The room's messages, minus deleted ones (see delete_messages)."""
        raw = self._read_raw(channel_id)
        deleted = {str(i) for r in raw if "deletes" in r for i in r["deletes"] or []}
        if not deleted and not any("deletes" in r for r in raw):
            return raw
        return [r for r in raw if "deletes" not in r and str(r["id"]) not in deleted]

    def _read_raw(self, channel_id: str) -> list[dict[str, Any]]:
        """Every record in the room file, deletion records included."""
        path = self._room_path(channel_id)
        if not path.exists():
            return []
        text = None
        for _ in range(10):  # a prune/delete may be swapping the file in right now
            try:
                text = path.read_text(encoding="utf-8")
                break
            except OSError:
                time.sleep(0.02)
        if text is None:
            return []
        out: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate a torn trailing line written mid-append
            if isinstance(rec, dict) and "id" in rec:
                out.append(rec)
        return out

    def _max_id(self, channel_id: str) -> int:
        """Largest id in the room, read from the file TAIL (ids are appended in
        increasing order, so the last physical line holds the max) — avoids
        re-reading the whole file on every append. Falls back to a full scan
        only if the tail can't be parsed."""
        path = self._room_path(channel_id)
        if not path.exists():
            return 0
        try:
            size = path.stat().st_size
            with open(path, "rb") as fh:
                if size > 65536:
                    fh.seek(-65536, os.SEEK_END)  # last record is well under this
                data = fh.read()
        except OSError:
            return 0
        for line in reversed(data.splitlines()):  # a seek can split the FIRST
            line = line.strip()                    # line, never the last — safe
            if not line:
                continue
            try:
                rec = json.loads(line.decode("utf-8"))
                return int(rec["id"])
            except (json.JSONDecodeError, UnicodeDecodeError, KeyError, ValueError, TypeError):
                continue
        return max((int(r["id"]) for r in self._read_raw(channel_id)), default=0)

    def _files_dir(self, message_id: str) -> Path:
        """Per-message directory holding copies of that message's attachments."""
        return self._dir / "files" / str(message_id)

    def _store_files(
        self, fdir: Path, source_files: list[Union[str, Path]]
    ) -> list[dict[str, Any]]:
        """Copy each source file into ``fdir`` and return attachment dicts (same
        shape as a Discord read) whose ``url`` is the stored path."""
        fdir.mkdir(parents=True, exist_ok=True)
        attachments: list[dict[str, Any]] = []
        for src in source_files:
            src = Path(src)
            dest = _unique_in_dir(fdir, src.name)  # don't let same-named files collide
            shutil.copyfile(src, dest)
            info = attachment_info(
                {
                    "url": str(dest),
                    "filename": src.name,
                    "content_type": guess_content_type(src.name),
                    "size": dest.stat().st_size,
                }
            )
            if info["is_image"]:
                dims = _image_dimensions(dest)
                if dims:
                    info["width"], info["height"] = dims
            attachments.append(info)
        return attachments

    def _prune_locked(self, channel_id: str) -> int:
        """Drop records older than the retention window (caller holds the room
        lock). Returns how many were removed.

        Cheap in the common case: only the FIRST line is read, and the room is
        rewritten only once its oldest record is past the window plus 10% slack —
        so a steady stream rewrites the file roughly once per tenth of the
        window, not on every append. Best-effort: any I/O hiccup (e.g. a reader
        holding the file open on Windows) just leaves pruning for the next write.
        """
        if self._retention_days <= 0:
            return 0
        path = self._room_path(channel_id)
        now = datetime.now(timezone.utc)
        window = timedelta(days=self._retention_days)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                first = fh.readline()
        except OSError:
            return 0
        try:
            oldest = _record_time(json.loads(first))
        except ValueError:
            oldest = None
        records: Optional[list[dict[str, Any]]] = None
        if oldest is None:
            # Unusable head record (no/odd timestamp, damaged line): find the
            # oldest dated record the slow way so one bad line can't switch
            # retention off for the room forever.
            records = self._read_all(channel_id)
            oldest = min((t for t in map(_record_time, records) if t is not None), default=None)
            if oldest is None:
                return 0
        if oldest > now - window * 1.1:
            return 0
        cutoff = now - window
        kept: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        for r in records if records is not None else self._read_all(channel_id):
            ts = _record_time(r)
            (dropped if ts is not None and ts < cutoff else kept).append(r)
        if not dropped:
            return 0
        tmp = str(path) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                for r in kept:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            config._replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return 0
        for r in dropped:  # their stored attachment copies go too
            for d in self._attachment_dirs(r):
                shutil.rmtree(d, ignore_errors=True)
        return len(dropped)

    def _attachment_dirs(self, record: dict[str, Any]) -> set[Path]:
        """Folders holding a message's stored attachments: its id's folder, plus
        wherever each attachment's url points under the files root (a file
        stays where it was copied if the message's id moved on - see _append)."""
        dirs = {self._files_dir(str(record["id"]))}
        files_root = self._files_dir("x").parent.resolve()
        for a in record.get("attachments") or []:
            folder = Path(str(a.get("url") or "")).parent
            try:
                if folder.resolve().parent == files_root:
                    dirs.add(folder)
            except OSError:
                pass
        return dirs

    def _append(
        self,
        channel_id: str,
        content: str,
        *,
        author: Optional[str] = None,
        bot: bool = True,
        source_files: Optional[list[Union[str, Path]]] = None,
    ) -> dict[str, Any]:
        who = author or self._label
        path = self._room_path(channel_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Copy attachments BEFORE taking the room lock (a big copy could outlast
        # the stale-lock limit and let another writer in). They go in a folder
        # named for the id this message will almost always get; if another
        # writer got in first, the files simply stay there (each attachment's
        # url says where, and retention cleanup follows the urls). Nothing is
        # moved afterwards - Windows can refuse to move a folder of fresh files.
        candidate = time.time_ns()
        attachments: list[dict[str, Any]] = []
        if source_files:
            files_root = self._files_dir("x").parent
            files_root.mkdir(parents=True, exist_ok=True)
            while True:
                try:
                    (files_root / str(candidate)).mkdir()
                    break
                except FileExistsError:
                    candidate += 1
            staged = files_root / str(candidate)
            try:
                attachments = self._store_files(staged, source_files)
            except BaseException:
                shutil.rmtree(staged, ignore_errors=True)
                raise
        with _AppendLock(path):
            self._prune_locked(channel_id)
            new_id = max(self._max_id(channel_id) + 1, candidate)  # monotonic
            rec = {
                "id": str(new_id),
                "author": {
                    "id": who,
                    "username": who,
                    "global_name": who,
                    "bot": bot,
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "content": content,
                "attachments": attachments,
            }
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def post_human(self, channel_id: str, content: str, author: str = "human") -> dict[str, Any]:
        """Write a NON-bot (human) record. The chat protocol surfaces non-bot
        authors as ``from="human"`` (an interjection), or ends the chat if the
        text is a stop word — this is how a human steers/halts a local chat that
        has no Discord UI to type into."""
        return self._append(channel_id, content, author=author, bot=False)

    # -- DiscordClient-compatible surface ----------------------------------
    def whoami(self) -> dict[str, Any]:
        return {
            "id": self._label,
            "username": self._label,
            "global_name": self._label,
            "bot": True,
            "local": True,
        }

    def send_message(
        self, channel_id: str, content: str, label: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Send ``content`` (label-prefixed on every chunk), mirroring
        DiscordClient so relay self-filtering behaves identically."""
        prefix = f"[{label}] " if label else ""
        body_limit = MAX_MESSAGE_LEN - len(prefix)
        sent: list[dict[str, Any]] = []
        for piece in chunk_content(content, body_limit, prefixed=bool(prefix)):
            sent.append(self._append(channel_id, f"{prefix}{piece}"))
        return sent

    def send_files(
        self,
        channel_id: str,
        content: str,
        file_paths: list[Union[str, Path]],
        label: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Attach files to a local message by copying them into the room's file
        store. Mirrors :meth:`DiscordClient.send_files` so callers are
        transport-blind; local mode has no CDN, size cap, or 10-file limit, so
        all files ride one message. Missing sources raise ``FileNotFoundError``
        before anything is written. Returns the created record."""
        if not file_paths:
            raise ValueError("send_files requires at least one file.")
        for p in file_paths:
            if not Path(p).is_file():
                raise FileNotFoundError(f"File not found: {p}")
        prefix = f"[{label}] " if label else ""
        full = f"{prefix}{content}" if content else prefix.strip()
        return [self._append(channel_id, full, source_files=list(file_paths))]

    def post(self, channel_id: str, content: str) -> dict[str, Any]:
        """Post a single message verbatim (chat mode manages its own headers)."""
        return self._append(channel_id, content)

    def read_messages(
        self,
        channel_id: str,
        limit: int = 20,
        after: Optional[str] = None,
        before: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Return messages newest-first, matching Discord's ordering and its
        ``after`` (forward-paging) / ``before`` (backward-paging) semantics."""
        recs = self._read_all(channel_id)
        recs.sort(key=lambda r: int(r["id"]))
        if after is not None:
            a = int(after)
            recs = [r for r in recs if int(r["id"]) > a]
        if before is not None:
            b = int(before)
            recs = [r for r in recs if int(r["id"]) < b]
        cap = max(1, min(int(limit), 100))
        if after is not None and before is None:
            window = recs[:cap]   # oldest-after-cursor (forward paging)
        else:
            window = recs[-cap:]  # newest (default read + backward paging)
        window.reverse()          # Discord returns newest-first
        return window

    def download_attachment(self, url: str, dest: Union[str, Path]) -> Path:
        """"Download" a local attachment and return the written path.

        Local mode has no CDN — an attachment ``url`` IS a filesystem path on the
        shared disk — so this copies the stored file to ``dest`` (keeping the
        source filename when ``dest`` is a directory). Mirrors
        :meth:`DiscordClient.download_attachment` so callers are transport-blind.
        """
        src = Path(url)
        if not src.exists():
            raise FileNotFoundError(f"Local attachment not found: {url}")
        dest = Path(dest)
        if dest.is_dir():
            dest = _unique_in_dir(dest, src.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return dest

    def delete_message(self, channel_id: str, message_id: str) -> None:
        self.delete_messages(channel_id, [message_id])

    def delete_messages(self, channel_id: str, message_ids: list[str]) -> int:
        """Delete several messages; returns how many were removed. Never fails
        because sessions are reading the room: the room is rewritten without
        them if Windows allows the swap within a moment, else a deletion record
        is appended (appending never conflicts with readers) and every read
        skips those ids until the next rewrite (retention pruning) drops both."""
        path = self._room_path(channel_id)
        if not path.exists():
            return 0
        ids = {str(i) for i in message_ids}
        with _AppendLock(path):
            records = self._read_all(channel_id)
            gone = [r for r in records if str(r["id"]) in ids]
            if not gone:
                return 0
            tmp = str(path) + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    for r in records:
                        if str(r["id"]) not in ids:
                            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                config._replace(tmp, path, retries=_DELETE_RETRIES)
            except OSError:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                self._append_deletion(channel_id, path, [str(r["id"]) for r in gone])
        # Drop any stored attachment files for the deleted messages (hygiene).
        for r in gone:
            for d in self._attachment_dirs(r):
                shutil.rmtree(d, ignore_errors=True)
        return len(gone)

    def _append_deletion(self, channel_id: str, path: Path, ids: list[str]) -> None:
        """Record that ``ids`` are deleted (caller holds the room lock). It has an
        id like a message, so ids stay increasing, and reads like a reminder
        line, so an older reader running alongside skips it."""
        rec = {
            "id": str(max(self._max_id(channel_id) + 1, time.time_ns())),
            "author": {"id": "discordinator", "username": "discordinator",
                       "global_name": "discordinator", "bot": True},
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "content": f"⏳ ({len(ids)} message(s) deleted)",  # chat's NUDGE_MARK
            "attachments": [],
            "deletes": ids,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def add_reaction(
        self, channel_id: str, message_id: str, emoji: str = "✅"
    ) -> None:
        # No reaction UI locally; ✅ read-acks are cosmetic. No-op (best-effort
        # parity so _try_ack callers don't need to special-case the transport).
        return None

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        stem = sanitize_room(channel_id)
        return {"id": stem, "name": stem, "local": True}

    def list_guild_channels(self, guild_id: Optional[str] = None) -> list[dict[str, Any]]:
        rooms: list[dict[str, Any]] = []
        if self._dir.exists():
            for p in sorted(self._dir.glob("*.jsonl")):
                rooms.append({"id": p.stem, "name": p.stem, "type": 0})
        return rooms
