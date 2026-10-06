"""Session handle resolution (handles.py): project handle + optional role, made
unique among live sessions on this machine.

Covers the compose rules (base, base/role, no doubling, no base), the
machine-wide claim registry (a live other process forces a "-2"; a dead or
expired claim is reclaimed; a process keeps its resolved name), suffix length
capping, release at exit, and pid liveness — which must never signal the
process (on Windows os.kill(pid, 0) would terminate it).
Run:  python tests/test_handles.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-handles-"))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
for _k in ("DISCORD_BOT_TOKEN", "DISCORDINATOR_CHAT_HANDLE"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import discordinator.mcp_server as mcp  # noqa: E402
from discordinator import handles  # noqa: E402
from discordinator.config import ConfigError  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def _fresh() -> None:
    """Empty registry and forget this process's resolved names."""
    handles.registry_path().unlink(missing_ok=True)
    handles._resolved.clear()


def _plant(handle: str, pid: int, age: float = 0.0) -> None:
    """Record a claim as if another process held ``handle``."""
    path = handles.registry_path()
    reg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    reg[handle.casefold()] = {"handle": handle, "pid": pid, "ts": time.time() - age}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reg), encoding="utf-8")


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_compose() -> None:
    print("compose: project handle + optional role:")
    check(handles.compose(None, "CodeCarver") == "CodeCarver", "no role -> base")
    check(handles.compose("ui", "CodeCarver") == "CodeCarver/ui", "role -> base/role")
    check(handles.compose("codecarver", "CodeCarver") == "CodeCarver", "role == base (any case) -> base")
    check(handles.compose("CodeCarver/ui", "CodeCarver") == "CodeCarver/ui", "already prefixed -> not doubled")
    check(handles.compose("A", None) == "A", "no base -> chatter as-is")
    check(handles.compose("", "Base") == "Base", "empty chatter treated as omitted")
    check(len(handles.compose("x" * 40, "Proj")) == 32, "composed handle capped at 32 chars")
    try:
        handles.compose(None, None)
        raise AssertionError("no chatter and no base must error")
    except ConfigError as exc:
        check("DISCORDINATOR_CHAT_HANDLE" in str(exc), "neither -> ConfigError naming the fix")


def test_pid_alive() -> None:
    print("pid liveness (query only, never signals):")
    check(handles.pid_alive(os.getpid()) is True, "own pid alive")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        check(handles.pid_alive(child.pid) is True, "running child alive")
        check(child.poll() is None, "liveness check did NOT kill the child")
    finally:
        child.kill()
        child.wait()
    check(handles.pid_alive(_dead_pid()) is False, "exited process not alive")
    check(handles.pid_alive(0) is False and handles.pid_alive(-5) is False, "bogus pids not alive")
    check(handles.pid_alive("12") is False, "non-int pid not alive")


def test_collision_gets_suffix() -> None:
    print("a handle held by another LIVE session gets a -N suffix:")
    _fresh()
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _plant("CodeCarver", other.pid)
        cfg = {"chat_handle": "CodeCarver"}
        handle, note = handles.resolve(None, cfg)
        check(handle == "CodeCarver-2", f"second session -> CodeCarver-2 (got {handle})")
        check(note and "CodeCarver-2" in note and "chatter" in note, "note explains and suggests a role")
        again, note2 = handles.resolve(None, cfg)
        check(again == "CodeCarver-2" and note2, "stable for the life of the process")
        role, rnote = handles.resolve("ui", cfg)
        check(role == "CodeCarver/ui" and rnote is None, "a role avoids the collision cleanly")
        _plant("CodeCarver-2", other.pid)
        handles._resolved.clear()
        third, _ = handles.resolve(None, cfg)
        check(third == "CodeCarver-3", "next free suffix is taken")
    finally:
        other.kill()
        other.wait()


def test_dead_or_expired_claims_reclaimed() -> None:
    print("claims from dead or expired sessions don't block the name:")
    _fresh()
    _plant("Proj", _dead_pid())
    check(handles.resolve(None, {"chat_handle": "Proj"})[0] == "Proj", "dead holder -> name reclaimed")
    _fresh()
    _plant("Proj", os.getppid(), age=handles.CLAIM_TTL + 60)
    check(handles.resolve(None, {"chat_handle": "Proj"})[0] == "Proj", "expired lease -> name reclaimed")
    reg = json.loads(handles.registry_path().read_text(encoding="utf-8"))
    check(reg["proj"]["pid"] == os.getpid(), "registry now records this process")


def test_suffix_respects_length() -> None:
    print("the -N suffix never pushes a handle past 32 chars:")
    _fresh()
    long = "L" * 32
    _plant(long, os.getppid())
    h, _ = handles.resolve(None, {"chat_handle": long})
    check(len(h) == 32 and h.endswith("-2"), f"truncated to fit: {h}")


def test_release_all() -> None:
    print("release_all drops only this process's claims:")
    _fresh()
    handles.resolve(None, {"chat_handle": "Mine"})
    _plant("Theirs", os.getppid())
    handles.release_all()
    reg = json.loads(handles.registry_path().read_text(encoding="utf-8"))
    check("mine" not in reg and "theirs" in reg, f"own claim gone, other kept: {sorted(reg)}")
    handles.registry_path().unlink()
    handles.release_all()
    check(True, "release with no registry is a no-op")


def test_mcp_begin_reports_rename() -> None:
    print("chat_begin surfaces the rename as a note:")
    _fresh()
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "Twin"
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _plant("Twin", other.pid)
        out = mcp.chat_begin(channel="twins")
        check(out["chatter"] == "Twin-2" and "note" in out, "chatter Twin-2 with a note")
        mcp.chat_say(text="hi", channel="twins")
        from discordinator.local_client import LocalClient
        last = LocalClient().read_messages("twins", limit=1)[0]["content"]
        check(last.startswith("[Twin-2|over]"), "chat_say posts under the same resolved name")
        st = mcp.chat_status(channel="twins")
        check(st["your_turn"] is False, "chat_status uses the resolved name (own turn not owed)")
    finally:
        other.kill()
        other.wait()
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")


def main() -> int:
    test_compose()
    test_pid_alive()
    test_collision_gets_suffix()
    test_dead_or_expired_claims_reclaimed()
    test_suffix_respects_length()
    test_release_all()
    test_mcp_begin_reports_rename()
    print(f"\nALL {_passed} HANDLE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
