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
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-lcx-"))
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


def test_append_lock_timeout_proceeds() -> None:
    print("_AppendLock proceeds unlocked rather than hanging forever:")
    c = LocalClient(label="lock2")
    room = "lockroom2"
    c.post(room, "seed")
    target = c._room_path(room)
    lockpath = Path(str(target) + ".lock")
    # A FRESH lock held by someone else (recent mtime, not stale). We should give
    # up waiting after `timeout` and proceed unlocked (fd is None) — a hung peer
    # must never freeze a chat permanently.
    lockpath.write_text("", encoding="utf-8")
    start = time.monotonic()
    with _AppendLock(target, timeout=0.2, stale=30.0) as lk:
        waited = time.monotonic() - start
        check(lk.fd is None, "gives up acquiring and proceeds unlocked (fd is None)")
        check(0.2 <= waited < 3.0, "waited about the timeout, not indefinitely")
    check(lockpath.exists(), "an unowned lock is left in place on exit (not deleted)")
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


def main() -> int:
    test_max_id_large_room_tail_read()
    test_torn_trailing_line_tolerated()
    test_delete_missing_room_noop()
    test_append_lock_stale_steal()
    test_append_lock_steal_is_safe()
    test_append_lock_timeout_proceeds()
    test_surface_parity()
    print(f"\nALL {_passed} LOCAL-CLIENT EDGE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
