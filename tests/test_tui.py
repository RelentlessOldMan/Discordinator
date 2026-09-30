"""Headless test of the Textual TUI via run_test() — no real terminal needed.

Verifies the app mounts and loads history, that typing submits a human
interjection into the room, and that /stop writes a stop the protocol obeys.
Skips cleanly if `textual` isn't installed. Run:  python tests/test_tui.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-tuitest-"))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_TRANSPORT"] = "local"
os.environ.pop("DISCORD_BOT_TOKEN", None)
os.environ.pop("DISCORDINATOR_LABEL", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    import textual  # noqa: F401
except ImportError:
    print("SKIP: textual not installed (pip install -e .[tui])")
    raise SystemExit(0)

from discordinator import chat  # noqa: E402
from discordinator.discord_client import simplify_message  # noqa: E402
from discordinator.local_client import LocalClient  # noqa: E402
from discordinator.tui import ChatTUI  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def room_contents(c: LocalClient, room: str) -> list[dict]:
    msgs = [simplify_message(m) for m in c.read_messages(room, limit=100)]
    msgs.reverse()  # chronological: [-1] is the newest
    return msgs


async def run() -> None:
    room = "tuiroom"
    c = LocalClient()
    chat.send_chat(c, room, "A", "over", "shall we cache parses?", to="B")
    chat.send_chat(c, room, "B", "over", "maybe — where?", to="A")

    # Unit-check the line renderer (no widgets needed).
    app = ChatTUI(room=room, poll=0.2, limit=50, label=None)
    line = app._line(simplify_message(c.read_messages(room, limit=1)[0]))
    check("maybe" in line.plain and "B ▸ A" in line.plain,
          "_line renders a chat turn with addressing")

    async with app.run_test() as pilot:
        await pilot.pause()
        check(app._cursor != "0", "on_mount loaded existing history (cursor advanced)")

        # Type an interjection and submit.
        app.query_one("#input").value = "focus on correctness first"
        await pilot.press("enter")
        await pilot.pause()
        msgs = room_contents(c, room)
        check(any((not m["bot"]) and "correctness" in m["content"] for m in msgs),
              "typing submits a human interjection into the room")

        # /stop writes a stop.
        app.query_one("#input").value = "/stop"
        await pilot.press("enter")
        await pilot.pause()
        newest = room_contents(c, room)[-1]
        check((not newest["bot"]) and chat.is_human_stop(newest["content"]),
              "/stop writes a human stop the protocol obeys")
        # (an awaiting agent then ends on this stop — covered in test_local_transport.py)

        # /quit exits the app.
        app.query_one("#input").value = "/quit"
        await pilot.press("enter")
        await pilot.pause()


def main() -> int:
    asyncio.run(run())
    print(f"\nALL {_passed} TUI CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
