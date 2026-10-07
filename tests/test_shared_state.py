"""Shared state files under concurrent sessions.

Every session on a machine shares ``state.json`` (each its own cursors), and
the local rooms / handle registry share one lock implementation. Regression for:
unlocked read-modify-write that let one session's save wipe another's fresh
cursor (and crash on Windows with "Access is denied" while another process had
the file open), a transient read error being taken as "empty state", and a lock
whose holder can't be dislodged spinning past its timeout.
Run:  python tests/test_shared_state.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-state-"))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
for _k in ("DISCORD_BOT_TOKEN", "DISCORDINATOR_LABEL", "DISCORDINATOR_CHAT_HANDLE",
           "DISCORDINATOR_CHAT_CHANNEL", "DISCORDINATOR_RELAY_CHANNEL"):
    os.environ.pop(_k, None)

_SRC = str(Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, _SRC)

from discordinator import chat, config  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


# Each worker process moves only ITS OWN cursor forward, re-reading it after
# every write. With a lost update, a worker sees its cursor go backwards.
_WORKER = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from discordinator import chat
me, n = sys.argv[2], int(sys.argv[3])
regressed = errors = 0
for i in range(1, n + 1):
    try:
        chat.set_cursor("room", me, str(i))
        got = chat.get_cursor("room", me)
        if got is None or int(got) < i:
            regressed += 1
    except Exception as e:
        errors += 1
print(json.dumps({"me": me, "regressed": regressed, "errors": errors}))
"""


def test_concurrent_sessions_keep_their_cursors() -> None:
    print("two sessions writing state.json at once never lose or crash on each other's updates:")
    n = 150
    procs = [subprocess.Popen([sys.executable, "-c", _WORKER, _SRC, me, str(n)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              env=os.environ.copy())
             for me in ("A", "B")]
    results = []
    for p in procs:
        out, err = p.communicate(timeout=300)
        if p.returncode != 0:
            raise AssertionError(err)
        results.append(json.loads(out.strip().splitlines()[-1]))
    for r in results:
        check(r["regressed"] == 0, f"{r['me']}: its cursor never went backwards")
        check(r["errors"] == 0, f"{r['me']}: no write raised (no 'Access is denied')")
    st = config.load_state()["chat"]["room"]
    check(st["a"]["cursor"] == str(n) and st["b"]["cursor"] == str(n),
          "both final cursors survived")


def test_unreadable_state_is_not_empty() -> None:
    print("a state file that can't be read raises instead of passing for empty:")
    chat.set_cursor("keep", "A", "42")
    real = Path.read_text
    fails = {"n": 3}

    def flaky(self, *a, **k):
        if self.name == "state.json" and fails["n"] > 0:
            fails["n"] -= 1
            raise PermissionError("being replaced")
        return real(self, *a, **k)

    Path.read_text = flaky
    try:
        check(chat.get_cursor("keep", "A") == "42", "a brief sharing violation is retried")

        def always(self, *a, **k):
            if self.name == "state.json":
                raise PermissionError("locked")
            return real(self, *a, **k)

        Path.read_text = always
        try:
            chat.set_cursor("keep", "B", "7")
            raise AssertionError("expected the write to fail")
        except PermissionError:
            check(True, "a write that can't read the file fails rather than saving over it")
    finally:
        Path.read_text = real
    check(chat.get_cursor("keep", "A") == "42", "the other session's cursor is still there")


def test_corrupt_state_recovers() -> None:
    print("a corrupt (non-JSON) state file reads as empty so sessions can carry on:")
    config.state_path().write_text("{not json", encoding="utf-8")
    check(config.load_state() == {}, "corrupt -> {}")
    chat.set_cursor("fresh", "A", "1")
    check(chat.get_cursor("fresh", "A") == "1", "and the next write works")


def test_replace_retries_sharing_violation() -> None:
    print("saving retries while another process briefly has the file open:")
    real = os.replace
    fails = {"n": 3}

    def flaky(src, dst):
        if fails["n"] > 0:
            fails["n"] -= 1
            raise PermissionError("[WinError 5] Access is denied")
        return real(src, dst)

    config.os.replace = flaky
    try:
        chat.set_cursor("busy", "A", "9")
    finally:
        config.os.replace = real
    check(fails["n"] == 0 and chat.get_cursor("busy", "A") == "9", "saved after 3 refusals")


def test_lock_unstealable_still_times_out() -> None:
    print("a stale lock that can't be stolen (holder still has it open) waits, then gives up:")
    target = _TMP / "held.jsonl"
    lock = Path(str(target) + ".lock")
    lock.write_text("", encoding="utf-8")
    old = time.time() - 60
    os.utime(lock, (old, old))
    lk = config.FileLock(target, timeout=0.5, stale=0.1)
    tries = {"n": 0}

    def cant_steal() -> None:
        tries["n"] += 1  # the rename fails while the holder has it open

    lk._steal = cant_steal
    t0 = time.monotonic()
    with lk:
        took = time.monotonic() - t0
    check(0.4 < took < 3.0, f"gave up at the timeout ({took:.2f}s), not past it")
    check(tries["n"] < 100, f"paused between attempts ({tries['n']} tries, no busy spin)")
    check(lk.fd is None and lock.exists(), "proceeded unlocked; the holder's lock untouched")
    lock.unlink()


def main() -> int:
    test_concurrent_sessions_keep_their_cursors()
    test_unreadable_state_is_not_empty()
    test_corrupt_state_recovers()
    test_replace_retries_sharing_violation()
    test_lock_unstealable_still_times_out()
    print(f"\nALL {_passed} SHARED-STATE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
