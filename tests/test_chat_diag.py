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
from discordinator import chat, config, events  # noqa: E402
from discordinator.local_client import LocalClient, local_dir  # noqa: E402

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


def test_what_it_shows() -> None:
    print("what's dumped: order, cut lines, each name once, kept state, the last events:")
    c = LocalClient()
    chat.send_chat(c, "dev-team", "M:A", "over", "first " + "x" * 150 + "TAILMARK", to="M:B")
    chat.send_chat(c, "dev-team", "M:B", "over", "second", to="M:A")
    chat.send_chat(c, "dev-team", "M:A", "over", "third", to="M:B")
    chat.reset("dev-team", "M:A", "12345", 20)
    for n in range(70):
        events.record(f"ev{n:02d}", room="dev-team")
    events.record("connected-somewhere")  # (about no room: a server connecting, say)
    with open(config.config_path().parent / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write("GARBAGE-LINE dev-team\n")
    text = run("Dev Team")
    msgs = text.split("== messages")[1].split("== chat state")[0]
    check(msgs.index("first") < msgs.index("second") < msgs.index("third"), "messages oldest first")
    check("TAILMARK" not in msgs, "each message cut to its first 100 characters")
    per = text.split("== compute_state per name ==")[1]
    check(per.count("M:A {") == 1 and per.count("M:B {") == 1, "each name dumped once")
    check('"cursor": "12345"' in per, "the kept cursor is shown")
    check('"reply_to"' in per, "reply_to is shown")
    st = text.split("== chat state kept for the room ==")[1].split("== session events")[0]
    check("12345" in st, f"the state kept under the room's id: {st[:80]!r}")
    ev = text.split("== session events")[1].split("== compute_state")[0]
    check("GARBAGE-LINE" not in ev, "a line that isn't JSON isn't about the room")
    check('"ev69"' in ev and '"ev05"' not in ev, "the last 60 events, not the first")
    check('"connected-somewhere"' in ev, "events about no room are shown too")
    orig = chat.compute_state
    chat.compute_state = (lambda cl, cid, who: (_ for _ in ()).throw(RuntimeError("boom"))
                          if who == "M:A" else orig(cl, cid, who))
    try:
        t2 = run("dev-team")
    finally:
        chat.compute_state = orig
    check("error RuntimeError('boom')" in t2 and "M:B {" in t2,
          "one name's error is printed and the others still dumped")


def test_which_room() -> None:
    print("which room: a configured name, else the room sessions last used, else chat:")
    c = LocalClient()
    chat.send_chat(c, "chat", "M:Z", "over", "default-room-turn", to=None)
    check("default-room-turn" in run(), "nothing named, no room used yet: room 'chat'")
    chat.send_chat(c, "lab", "M:Z", "over", "lab-turn", to=None)
    chat.note_room("lab", True)
    check("lab-turn" in run(), "nothing named: the local room sessions last used")
    cfg = config.load_file()
    cfg.setdefault("channels", {})["hub"] = "1400000000000000001"
    config.save(cfg)
    chat.send_chat(c, "1400000000000000001", "M:Z", "over", "hub-turn", to=None)
    check("hub-turn" in run("hub"), "a configured channel name is its room")


def test_a_damaged_line() -> None:
    print("a hand-written line with no timestamp or author:")
    c = LocalClient()
    chat.send_chat(c, "patchy", "M:A", "over", "hello", to="M:B")
    with open(local_dir() / "patchy.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "99999999999999999999",
                             "content": "[M:B>M:A|over] by hand"}) + "\n")
    text = run("patchy")
    check("hello" in text and "by hand" in text and "== compute_state" in text,
          f"dumped in full, the odd line too: {text[-300:]!a}")


def main() -> int:
    test_dump()
    test_what_it_shows()
    test_which_room()
    test_a_damaged_line()
    print(f"\nALL CHAT_DIAG CHECKS PASSED ({_passed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
