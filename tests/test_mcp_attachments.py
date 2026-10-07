"""Tests for the MCP attachment tool glue (gates + success), over local transport.

The MCP tools (`send_file`, `download_attachment`, `chat_say(files=...)`) are
thin wrappers over tested primitives, but they own the opt-in GATE enforcement —
so this pins that an un-opted-in machine is refused, and that the happy path
actually moves bytes. No network (local transport).
Run:  python tests/test_mcp_attachments.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-mcp-att-"))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
for _k in ("DISCORDINATOR_ALLOW_SEND", "DISCORDINATOR_ALLOW_RECEIVE"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import discordinator.mcp_server as mcp  # noqa: E402
from discordinator import chat  # noqa: E402
from discordinator.config import ConfigError  # noqa: E402
from discordinator.discord_client import simplify_message  # noqa: E402
from discordinator.local_client import LocalClient  # noqa: E402
from discordinator.mcp_server import ChatSendError  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def _allow(flag: str, on: bool) -> None:
    key = {"send": "DISCORDINATOR_ALLOW_SEND", "receive": "DISCORDINATOR_ALLOW_RECEIVE"}[flag]
    if on:
        os.environ[key] = "1"
    else:
        os.environ.pop(key, None)


def test_gates_block_when_off() -> None:
    print("every upload/download MCP tool is refused while its opt-in is OFF:")
    _allow("send", False)
    _allow("receive", False)
    f = _TMP / "x.txt"
    f.write_text("hi", encoding="utf-8")
    try:
        mcp.send_file(paths=[str(f)], text="no", channel="mcproom")
        raise AssertionError("send_file should be gated")
    except ConfigError:
        check(True, "send_file -> ConfigError when send opt-in is off")
    os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "discord"  # the gate is Discord-only
    try:
        mcp.chat_say(text="no", chatter="A", status="over", channel="mcpchat", files=[str(f)])
        raise AssertionError("chat_say(files) should be gated")
    except ChatSendError as e:
        check(isinstance(e.__cause__, ConfigError) and "set-attachments send on" in str(e),
              "chat_say(files=...) on Discord is gated when send opt-in is off")
        check("Nothing was posted" in str(e) and "If it was your turn, it still is" in str(e),
              "...and the error says nothing was posted and the turn is still yours")
    finally:
        os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
    try:
        mcp.download_attachment(url=str(f))
        raise AssertionError("download_attachment should be gated")
    except ConfigError:
        check(True, "download_attachment -> ConfigError when receive opt-in is off")


def test_send_file_success() -> None:
    print("send_file uploads once the send opt-in is on (list and single-string paths):")
    _allow("send", True)
    f = _TMP / "doc.yaml"
    f.write_text("a: 1\n", encoding="utf-8")
    out = mcp.send_file(paths=[str(f)], text="here", channel="mcproom", label="M")
    check("Sent" in out and "1 file" in out, "send_file reports what it sent")
    msgs = LocalClient("probe").read_messages("mcproom", limit=1)
    atts = simplify_message(msgs[0])["attachments"]
    check(len(atts) == 1 and atts[0]["filename"] == "doc.yaml", "the file was stored in the room")

    # paths as a single string (not a list) is accepted too.
    out2 = mcp.send_file(paths=str(f), text="again", channel="mcproom")
    check("1 file" in out2, "a single string path is treated as one file")


def test_local_chat_shares_paths() -> None:
    print("local chat_say(files=...) shares paths - no copy, no opt-in needed:")
    _allow("send", False)
    img = _TMP / "pic.png"
    img.write_bytes(b"\x89PNG\r\n")
    res = mcp.chat_say(text="diagram", chatter="A", status="over", channel="mcpchat", to="B", files=str(img), wait=False)
    check(res["sent_messages"] == 1 and res["status"] == "over", "the turn is sent with the opt-in off")
    check(res["files_shared"] == [str(img.resolve())], "files_shared lists the absolute path")
    chat.reset("mcpchat", "B", "0", 20)
    got = chat.await_turn(LocalClient("B"), "mcpchat", "B", timeout=2, poll=0.02, nudge_after=0)
    check(got["from"] == "A" and got["attachments"] == [], "nothing was copied as an attachment")
    check(got["text"].startswith("diagram") and str(img.resolve()) in got["text"],
          "the peer gets the path in the message text")


def test_failed_send_keeps_turn_3way() -> None:
    print("a failed chat_say posts nothing, and in a 3-way chat the turn stays with the floor holder:")
    room = "trio"
    for h in ("A", "B", "C"):
        mcp.chat_begin(chatter=h, channel=room)
    mcp.chat_say(text="A, your call", chatter="B", status="over", channel=room, to="A", wait=False)
    got = mcp.chat_await(chatter="A", channel=room, timeout=2, poll=0.02, nudge_after=0)
    check(got["from"] == "B" and got["your_turn"], "A holds the floor")
    before = len(LocalClient("probe").read_messages(room, limit=50))
    try:
        mcp.chat_say(text="see file", chatter="A", channel=room, to="C", files=[str(_TMP / "nope.txt")])
        raise AssertionError("a missing file should fail")
    except ChatSendError as e:
        check(isinstance(e.__cause__, FileNotFoundError) and "Nothing was posted" in str(e),
              "missing file -> ChatSendError saying nothing was posted")
    check(len(LocalClient("probe").read_messages(room, limit=50)) == before, "the room is unchanged")
    back = mcp.chat_await(chatter="A", channel=room, timeout=2, poll=0.02, nudge_after=0)
    check(back.get("already_received") and back["your_turn"] and back["from"] == "B",
          "A's chat_await hands the still-owed turn straight back")
    other = mcp.chat_await(chatter="C", channel=room, timeout=0.3, poll=0.02, nudge_after=0)
    check(other["timed_out"] and not other["your_turn"], "C (not addressed) keeps waiting")

    many = [str(_TMP / "pic.png")] * 11
    for kw, cause, what in ((dict(files=many), ValueError, "11 files"),
                            (dict(status="bogus"), ValueError, "a bad status")):
        try:
            mcp.chat_say(text="x", chatter="A", channel=room, **kw)
            raise AssertionError(what)
        except ChatSendError as e:
            check(isinstance(e.__cause__, cause) and "Nothing was posted" in str(e),
                  f"{what} -> nothing posted, says so")
    orig = mcp.chat.send_chat
    def boom(*a, **k):
        raise RuntimeError("disk full")
    mcp.chat.send_chat = boom
    try:
        mcp.chat_say(text="x", chatter="A", channel=room, to="C")
        raise AssertionError("a send failure should raise")
    except ChatSendError as e:
        check("disk full" in str(e) and "did NOT go through" in str(e),
              "a failure mid-send says the message did not go through")
    finally:
        mcp.chat.send_chat = orig


def test_download_success() -> None:
    print("download_attachment fetches once the receive opt-in is on:")
    _allow("send", True)
    _allow("receive", True)
    src = _TMP / "payload.bin"
    src.write_bytes(b"\x00\x09bytes")
    mcp.send_file(paths=[str(src)], text="", channel="dlroom")
    url = simplify_message(LocalClient("probe").read_messages("dlroom", limit=1)[0])["attachments"][0]["url"]
    dest = _TMP / "mcp-dl"
    dest.mkdir(parents=True, exist_ok=True)  # point at an existing dir (filename kept)
    res = mcp.download_attachment(url=url, dest=str(dest))
    check(res["filename"] == "payload.bin", "download reports the saved filename")
    check(Path(res["saved"]).read_bytes() == b"\x00\x09bytes", "the exact bytes were written")


def main() -> int:
    try:
        test_gates_block_when_off()
        test_send_file_success()
        test_local_chat_shares_paths()
        test_failed_send_keeps_turn_3way()
        test_download_success()
    finally:
        for k in ("DISCORDINATOR_ALLOW_SEND", "DISCORDINATOR_ALLOW_RECEIVE"):
            os.environ.pop(k, None)
    print(f"\nALL {_passed} MCP-ATTACHMENT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
