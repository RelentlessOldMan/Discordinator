"""Recovery, resource, and edge-case tests for the local transport.

The happy-path relay/chat round-trips live in test_local_transport.py. This file
targets the failure and resource-management paths that are easy to regress and
costly when they break: the tail-read fast path for id assignment on large
rooms, tolerance of a torn trailing line, delete on a missing room, and the
cross-process append lock's stale-steal / give-up-and-proceed behavior.
Run:  python tests/test_local_client_extra.py
"""

from __future__ import annotations

import json
import atexit
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-lcx-"))
os.chdir(_TMP)  # never the repo: a .env there would be loaded into the test
atexit.register(lambda: (os.chdir(tempfile.gettempdir()),
                         shutil.rmtree(_TMP, ignore_errors=True)))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
os.environ.pop("DISCORD_BOT_TOKEN", None)
os.environ.pop("DISCORDINATOR_LABEL", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discordinator.local_client import LocalClient, _AppendLock  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def test_max_id_large_room_tail_read() -> None:
    print("id assignment stays monotonic on a large room (>64KB tail path):")
    c = LocalClient(label="big")
    room = "bigroom"
    ids = []
    # Enough records that the file exceeds the 64KB tail window, so _max_id takes
    # the seek-to-end fast path rather than a full scan.
    for i in range(500):
        ids.append(int(c.post(room, f"message number {i} with some padding text")["id"]))
    path = c._room_path(room)
    check(path.stat().st_size > 65536, "room file exceeds the 64KB tail window (fast path engaged)")
    check(ids == sorted(ids) and len(set(ids)) == len(ids),
          "all 500 ids are strictly increasing and unique")
    # A fresh client (no in-memory state) must still assign a larger id.
    nxt = int(LocalClient(label="big2").post(room, "after a big backlog")["id"])
    check(nxt > ids[-1], "next id after a large backlog is still greater than the max (tail read correct)")


def test_torn_trailing_line_tolerated() -> None:
    print("a torn trailing line (interrupted append) is tolerated:")
    c = LocalClient(label="torn")
    room = "tornroom"
    good = c.post(room, "intact record")
    # Simulate a crash mid-append: a partial, unparseable JSON line at the end.
    with open(c._room_path(room), "a", encoding="utf-8") as fh:
        fh.write('{"id": "999999999999", "content": "half-writ')  # no newline, truncated
    msgs = c.read_messages(room, limit=100)
    check([m["content"] for m in msgs] == ["intact record"],
          "reader skips the torn line and still returns the valid record")
    # The next append must not crash and must produce a larger id than the good one.
    after = int(c.post(room, "recovered")["id"])
    check(after > int(good["id"]), "append after a torn line succeeds with a monotonic id")
    check([m["content"] for m in c.read_messages(room, limit=100)] == ["recovered", "intact record"],
          "the message appended after the torn line isn't glued onto it and lost")


def test_deleting_a_message_keeps_others_files() -> None:
    print("deleting a message never removes files another message holds:")
    src = _TMP / "keep.txt"
    src.write_text("keep me", encoding="utf-8")
    c = LocalClient(label="files")
    rec = c.send_files("roomB", "file here", [src])[0]
    url = Path(rec["attachments"][0]["url"])
    # Ids are only unique per room: a message in another room (or one with no
    # attachments) can have the id that names this file's folder.
    path = c._room_path("roomA")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": url.parent.name, "author": {"id": "files", "bot": True},
                             "timestamp": "2099-01-01T00:00:00+00:00", "content": "plain note",
                             "attachments": []}) + "\n")
    check(c.delete_messages("roomA", [url.parent.name]) == 1, "the plain note was deleted")
    check(url.exists(), "the other room's attachment is still there")


def test_delete_missing_room_noop() -> None:
    print("delete on a non-existent room is a safe no-op:")
    c = LocalClient(label="d")
    c.delete_message("room-that-never-existed", "123")  # must not raise
    check(True, "deleting from a missing room does not raise")


def test_append_lock_stale_steal() -> None:
    print("_AppendLock steals a stale lock (previous holder crashed):")
    c = LocalClient(label="lock")
    room = "lockroom"
    c.post(room, "seed")  # ensure the room file exists
    target = c._room_path(room)
    lockpath = Path(str(target) + ".lock")
    lockpath.write_text("", encoding="utf-8")
    old = time.time() - 120  # 2 minutes old -> older than the 30s stale window
    os.utime(lockpath, (old, old))
    with _AppendLock(target, timeout=1.0, stale=30.0) as lk:
        check(lk.fd is not None, "a stale lock is stolen and the lock is genuinely acquired")
    check(not lockpath.exists(), "stealing then releasing removes the lock file")


def test_append_lock_steal_is_safe() -> None:
    print("_AppendLock: a crashed holder's lock is stolen before anyone writes unlocked:")
    lk = _AppendLock(_TMP / "steal.jsonl")
    check(lk.stale < lk.timeout, f"default stale ({lk.stale}s) < timeout ({lk.timeout}s)")
    lockpath = Path(str(_TMP / "steal.jsonl") + ".lock")
    # Another waiter replaced the stale lock with a fresh one between our age
    # check and our steal: the fresh lock must be put back, not deleted.
    lockpath.write_text("", encoding="utf-8")
    lk._steal()
    check(lockpath.exists(), "a fresh lock grabbed by mistake is put back")
    old = time.time() - 120
    os.utime(lockpath, (old, old))
    lk._steal()
    check(not lockpath.exists(), "a stale lock is removed")
    lk._steal()
    check(True, "stealing a lock that's already gone is a no-op")
    leftovers = [p for p in lockpath.parent.iterdir() if p.name.startswith(lockpath.name + ".")]
    check(not leftovers, "no renamed-aside files left behind")


def test_read_retries_while_file_swapped() -> None:
    print("a read during a prune/delete file swap retries instead of returning nothing:")
    c = LocalClient(label="swap")
    c.post("swaproom", "kept")
    real = Path.read_text
    fails = {"n": 2}
    def flaky(self, *a, **k):
        if self.name.endswith(".jsonl") and fails["n"] > 0:
            fails["n"] -= 1
            raise PermissionError("being replaced")
        return real(self, *a, **k)
    Path.read_text = flaky
    try:
        got = c.read_messages("swaproom", limit=5)
    finally:
        Path.read_text = real
    check(len(got) == 1 and fails["n"] == 0, "two failed reads, then the real content")


def test_append_lock_timeout_raises() -> None:
    print("_AppendLock gives up with an error rather than hanging or writing unlocked:")
    from discordinator import config as _config
    c = LocalClient(label="lock2")
    room = "lockroom2"
    c.post(room, "seed")
    target = c._room_path(room)
    lockpath = Path(str(target) + ".lock")
    # A FRESH lock held by someone else (recent mtime, not stale). We give up
    # waiting after `timeout` - with an error, so nothing is written over the
    # holder's update - and a hung peer still never freezes a chat forever.
    lockpath.write_text("", encoding="utf-8")
    start = time.monotonic()
    lk = _AppendLock(target, timeout=0.2, stale=30.0)
    try:
        with lk:
            raise AssertionError("acquired a lock someone else holds")
    except _config.LockTimeout:
        pass
    waited = time.monotonic() - start
    check(lk.fd is None, "gives up acquiring (fd is None) and raises LockTimeout")
    check(0.2 <= waited < 3.0, "waited about the timeout, not indefinitely")
    check(lockpath.exists(), "an unowned lock is left in place (not deleted)")
    lockpath.unlink()  # cleanup our manual lock


def test_surface_parity() -> None:
    print("DiscordClient-parity surface behaves sanely:")
    c = LocalClient(label="parity")
    check(c.whoami()["local"] is True and c.whoami()["bot"] is True,
          "whoami reports a local bot identity")
    c.post("alpha", "x")
    c.post("beta", "y")
    rooms = {r["name"] for r in c.list_guild_channels()}
    check({"alpha", "beta"} <= rooms, "list_guild_channels lists existing rooms")
    check(c.get_channel("Some Room")["local"] is True,
          "get_channel returns a local channel descriptor")
    c.add_reaction("alpha", "1")  # no-op locally; must not raise
    check(True, "add_reaction is a harmless no-op in local mode")


def test_attachments_copied_outside_lock() -> None:
    print("attachments are copied before the room lock, and never moved afterwards:")
    src = _TMP / "att.txt"
    src.write_text("payload", encoding="utf-8")
    c = LocalClient(label="att")
    rec = c.send_files("attroom", "see file", [src])[0]
    url = Path(rec["attachments"][0]["url"])
    check(url.read_text(encoding="utf-8") == "payload" and url.parent.name == rec["id"],
          "normally stored under the message's id")
    # Another writer gets a larger id in while our copy is running.
    real_store = LocalClient._store_files

    def slow_store(self, fdir, files):
        out = real_store(self, fdir, files)
        with open(c._room_path("attroom"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": str(int(fdir.name) + 50), "author": {"id": "o", "bot": True},
                                 "timestamp": "2099-01-01T00:00:00+00:00", "content": "jump",
                                 "attachments": []}) + "\n")
        return out

    LocalClient._store_files = slow_store
    try:
        rec2 = c.send_files("attroom", "again", [src])[0]
    finally:
        LocalClient._store_files = real_store
    url2 = Path(rec2["attachments"][0]["url"])
    check(url2.read_text(encoding="utf-8") == "payload" and url2.parent.name != rec2["id"],
          "if the id moved on, the file stays where it was copied and its url says so")
    check(int(rec2["id"]) > int(url2.parent.name), "ids still only go up")
    os.environ["DISCORDINATOR_LOCAL_RETENTION_DAYS"] = "1"
    try:
        lines = c._room_path("attroom").read_text(encoding="utf-8").splitlines()
        recs = [json.loads(x) for x in lines if x.strip()]
        for r in recs:
            r["timestamp"] = "2000-01-01T00:00:00+00:00"  # everything is past the window
        c._room_path("attroom").write_text("".join(json.dumps(r) + "\n" for r in recs),
                                           encoding="utf-8")
        LocalClient(label="pruner").post("attroom", "trigger prune")
    finally:
        os.environ.pop("DISCORDINATOR_LOCAL_RETENTION_DAYS")
    check(not url2.parent.exists() and not url.parent.exists(),
          "retention cleanup follows each url to remove the files")


def test_delete_while_rooms_are_read() -> None:
    print("deleting messages while other sessions read the room (Windows refuses the swap):")
    import threading
    c = LocalClient(label="del")
    room = "delroom"
    ids = [c.post(room, "x" * 1500)["id"] for _ in range(300)]
    stop = threading.Event()

    def reader() -> None:  # a session polling the room, flat out
        while not stop.is_set():
            c.read_messages(room, limit=100)

    t = threading.Thread(target=reader)
    t.start()
    errors = []
    try:
        for mid in ids[:40]:
            try:
                c.delete_message(room, mid)
            except OSError as e:
                errors.append(e)
        n = c.delete_messages(room, ids[40:140])
    finally:
        stop.set()
        t.join()
    check(not errors, f"single deletes all succeed ({len(errors)} failed)")
    check(n == 100, "a bulk delete removes them all at once")
    left = [m["id"] for m in c.read_messages(room, limit=100, after="0")]
    check(len(c._read_all(room)) == 160 and ids[0] not in left, "exactly those are gone")
    check(not list(c._dir.glob("*.tmp")), "no temp files left behind")


def test_delete_when_room_cant_be_rewritten() -> None:
    print("a delete still works when the room can't be rewritten (Windows readers):")
    from discordinator import config as _config
    c = LocalClient(label="tomb")
    room = "tombroom"
    ids = [c.post(room, f"m{i}")["id"] for i in range(6)]
    real = _config._replace

    def refuse(*a, **k):
        raise PermissionError("[WinError 5] Access is denied")

    _config._replace = refuse
    try:
        n = c.delete_messages(room, ids[1:3])
        c.delete_message(room, ids[4])
    finally:
        _config._replace = real
    check(n == 2, "the delete reports what it removed")
    left = [m["content"] for m in reversed(c.read_messages(room, limit=100))]
    check(left == ["m0", "m3", "m5"], f"deleted messages no longer read back: {left}")
    raw = c._read_raw(room)
    check(sum("deletes" in r for r in raw) == 2 and not list(c._dir.glob("*.tmp")),
          "they're marked deleted in the room (no temp files left)")
    check(c.read_messages(room, limit=1)[0]["content"] == "m5", "the marker itself is never read back")
    nxt = c.post(room, "after")["id"]
    check(int(nxt) > max(int(r["id"]) for r in raw), "ids still only go up")
    check(c.delete_messages(room, ids[1:3]) == 0, "deleting them again removes nothing")
    os.environ["DISCORDINATOR_LOCAL_RETENTION_DAYS"] = "1"
    try:
        recs = c._read_raw(room)
        recs[0]["timestamp"] = "2000-01-01T00:00:00+00:00"  # m0 is past the window
        c._room_path(room).write_text("".join(json.dumps(r) + "\n" for r in recs),
                                      encoding="utf-8")
        LocalClient(label="pruner").post(room, "trigger prune")
    finally:
        os.environ.pop("DISCORDINATOR_LOCAL_RETENTION_DAYS")
    raw = c._read_raw(room)
    check(not any("deletes" in r for r in raw) and len(raw) == 4,
          "the next rewrite drops the markers and the deleted messages for good")


def main() -> int:
    test_max_id_large_room_tail_read()
    test_torn_trailing_line_tolerated()
    test_deleting_a_message_keeps_others_files()
    test_delete_missing_room_noop()
    test_append_lock_stale_steal()
    test_append_lock_steal_is_safe()
    test_read_retries_while_file_swapped()
    test_append_lock_timeout_raises()
    test_surface_parity()
    test_attachments_copied_outside_lock()
    test_delete_while_rooms_are_read()
    test_delete_when_room_cant_be_rewritten()
    print(f"\nALL {_passed} LOCAL-CLIENT EDGE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
