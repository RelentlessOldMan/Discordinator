"""Phase 2.5 + 3: attachments inside live chat turns, and local image dimensions.

A chat turn can now carry files (they ride the final, status-bearing message), and
`await_turn` surfaces them on the receiving side alongside the turn text — without
disturbing the `[from|status]` header, so addressing / floor / turn-taking are
untouched. Also checks Phase 3's local-transport extras: image width/height are
filled in via Pillow when available. All over the local transport (no network).

Run:  python tests/test_chat_attachments.py
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-chatatt-"))
os.chdir(_TMP)  # never the repo: a .env there would be loaded into the test
atexit.register(lambda: (os.chdir(tempfile.gettempdir()),
                         shutil.rmtree(_TMP, ignore_errors=True)))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discordinator import chat  # noqa: E402
from discordinator.local_client import LocalClient  # noqa: E402

# A valid 1x1 transparent PNG (so is_image + Pillow dimension lookup are real).
PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def _img(name: str = "diagram.png") -> Path:
    p = _TMP / name
    p.write_bytes(PNG_1x1)
    return p


def _file(name: str, data: bytes = b"data") -> Path:
    p = _TMP / name
    p.write_bytes(data)
    return p


def test_chat_turn_carries_file() -> None:
    print("send_chat(files=...) -> await_turn surfaces the attachment with the turn:")
    room = "chat-att-1"
    a, b = LocalClient("A"), LocalClient("B")
    chat.reset(room, "B", "0", 20)
    chat.send_chat(a, room, "A", "over", "here is the diagram", to="B", files=[_img()])

    res = b and chat.await_turn(b, room, "B", timeout=2, poll=0.02, nudge_after=0)
    check(res["from"] == "A" and res["your_turn"] is True, "the turn comes to B (floor logic intact)")
    check(res["text"] == "here is the diagram", "the turn text is unaffected by the attachment")
    check(res["status"] == "over", "the status header still parses normally")
    check(len(res["attachments"]) == 1, "the attachment is surfaced on the turn")
    att = res["attachments"][0]
    check(att["filename"] == "diagram.png" and att["is_image"] is True,
          "attachment metadata (filename, is_image) comes through")
    check(Path(att["url"]).exists(), "the stored file exists and is addressable")


def test_files_ride_final_message_of_multipart_turn() -> None:
    print("a long (multi-part) turn's files ride its final message and arrive once:")
    room = "chat-att-2"
    a, b = LocalClient("A"), LocalClient("B")
    chat.reset(room, "B", "0", 20)
    long_text = ("x" * 2500)  # forces >1 piece
    chat.send_chat(a, room, "A", "over", long_text, to="B", files=[_file("notes.txt", b"hi")])

    res = chat.await_turn(b, room, "B", timeout=2, poll=0.02, nudge_after=0)
    # Pieces rejoin with a '\n' separator (existing protocol behavior); verify no
    # content is lost, which is the real concern for a multi-part turn.
    check(res["text"].replace("\n", "") == long_text, "the multi-part body reassembles with no data loss")
    check(len(res["attachments"]) == 1 and res["attachments"][0]["filename"] == "notes.txt",
          "the file arrives exactly once, on the final piece")


def test_terminal_turn_with_file() -> None:
    print("a terminal turn (end) can also carry a file:")
    room = "chat-att-3"
    a, b = LocalClient("A"), LocalClient("B")
    chat.reset(room, "B", "0", 20)
    chat.send_chat(a, room, "A", "end", "final handoff", to="B", files=[_file("final.log", b"bye")])

    res = chat.await_turn(b, room, "B", timeout=2, poll=0.02, nudge_after=0)
    check(res["ended"] is True and res["stop_reason"] == "agreed", "the chat ends as normal")
    check(len(res["attachments"]) == 1 and res["attachments"][0]["filename"] == "final.log",
          "a file on the terminal turn is still surfaced")


def test_out_of_band_file_is_plain_turn() -> None:
    print("a file sent outside the chat (no chat header) is relay traffic, not a turn:")
    room = "chat-att-4"
    a, b = LocalClient("A"), LocalClient("B")
    chat.reset(room, "B", "0", 20)
    a.send_files(room, "oops, raw send", [_file("stray.bin", b"")])  # not via send_chat

    res = chat.await_turn(b, room, "B", timeout=0.5, poll=0.02, nudge_after=0)
    check(res["timed_out"] and not res["your_turn"],
          "it doesn't count as anyone's turn (a bystander's post can't answer for a peer)")


def test_messages_metadata_includes_attachments() -> None:
    print("the per-message `messages` metadata carries attachments:")
    room = "chat-att-5"
    a, b = LocalClient("A"), LocalClient("B")
    chat.reset(room, "B", "0", 20)
    chat.send_chat(a, room, "A", "over", "meta check", to="B", files=[_img("m.png")])
    res = chat.await_turn(b, room, "B", timeout=2, poll=0.02, nudge_after=0)
    meta = res["messages"]
    check(len(meta) == 1 and "attachments" in meta[0], "each message entry exposes attachments")
    check(meta[0]["attachments"][0]["filename"] == "m.png", "metadata attachment filename matches")


def test_chat_file_cap() -> None:
    print("a chat turn refuses more files than a single message allows:")
    room = "chat-att-6"
    a = LocalClient("A")
    too_many = [_file(f"c{i}.bin", bytes([i])) for i in range(11)]
    try:
        chat.send_chat(a, room, "A", "over", "too many", to="B", files=too_many)
        raise AssertionError("expected ValueError for >10 files in one chat turn")
    except ValueError as e:
        check("at most" in str(e), "11 files in a chat turn -> ValueError (use relay for big batches)")


def test_local_image_dimensions() -> None:
    print("Phase 3: local image width/height are filled in when Pillow is available:")
    room = "dims"
    a = LocalClient("A")
    sent = a.send_files(room, "", [_img("one.png")])
    att = sent[0]["attachments"][0]
    try:
        import PIL  # noqa: F401
        have_pillow = True
    except ImportError:
        have_pillow = False
    if have_pillow:
        check(att["width"] == 1 and att["height"] == 1,
              "a 1x1 PNG reports 1x1 dimensions locally (parity with Discord)")
    else:
        check(att["width"] is None and att["height"] is None,
              "without Pillow, dimensions are gracefully None (no hard dependency)")


def main() -> int:
    test_chat_turn_carries_file()
    test_files_ride_final_message_of_multipart_turn()
    test_terminal_turn_with_file()
    test_out_of_band_file_is_plain_turn()
    test_messages_metadata_includes_attachments()
    test_chat_file_cap()
    test_local_image_dimensions()
    print(f"\nALL {_passed} CHAT-ATTACHMENT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
