"""tools/chat_diag.py: the read-only dump of a local chat room.

Run:  python tests/test_chat_diag.py
"""

from __future__ import annotations

import atexit
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-diag-"))
os.chdir(_TMP)  # never the repo: a .env there would be loaded into the test
atexit.register(lambda: (os.chdir(tempfile.gettempdir()),
                         shutil.rmtree(_TMP, ignore_errors=True)))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_LABEL"] = "M"
for _k in ("DISCORD_BOT_TOKEN", "DISCORDINATOR_CHAT_HANDLE", "DISCORDINATOR_CHAT_CHANNEL",
           "DISCORDINATOR_RELAY_CHANNEL"):
    os.environ.pop(_k, None)

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "tools"))

import chat_diag  # noqa: E402
from discordinator import chat, events  # noqa: E402
from discordinator.local_client import LocalClient  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def run(*args: str) -> str:
    buf = io.BytesIO()
    out = io.TextIOWrapper(buf, encoding="cp1252")  # (like a redirected Windows console)
    argv, sys.argv = sys.argv, ["chat_diag.py", *args]
    try:
        with contextlib.redirect_stdout(out):
            chat_diag.main()
            out.flush()
    finally:
        sys.argv = argv
    return buf.getvalue().decode("utf-8")


def test_dump() -> None:
    print("the dump: the room by any spelling, emoji, only that room's events:")
    chat.send_chat(LocalClient(), "ops-team", "M:A", "over", "shipped ✅ \U0001F680", to=None)
    events._rotated().write_text(
        json.dumps({"kind": "older", "room": "ops-team"}) + "\n"
        + json.dumps({"kind": "other", "room": "ops-team-2"}) + "\n", encoding="utf-8")
    events.record("newer", room="ops-team")
    events.record("other2", room="ops-team-2")
    text = run("Ops Team")
    check("\U0001F680" in text, "a turn with emoji is dumped, written out as UTF-8")
    check("[M:A|over] shipped" in text, f"'Ops Team' is room ops-team: {text[:300]!a}")
    ev = text.split("== session events")[1].split("== compute_state")[0]
    check('"older"' in ev and '"newer"' in ev, "events from both logs")
    check(ev.index('"older"') < ev.index('"newer"'), "the older log first")
    check("ops-team-2" not in ev, f"not another room's whose name starts the same: {ev!a}")


def main() -> int:
    test_dump()
    print(f"\nALL CHAT_DIAG CHECKS PASSED ({_passed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
