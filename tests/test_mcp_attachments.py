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
os.environ["DISCORDINATOR_TRANSPORT"] = "local"
for _k in ("DISCORDINATOR_ALLOW_SEND", "DISCORDINATOR_ALLOW_RECEIVE"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import discordinator.mcp_server as mcp  # noqa: E402
from discordinator import chat  # noqa: E402
from discordinator.config import ConfigError  # noqa: E402
from discordinator.discord_client import simplify_message  # noqa: E402
from discordinator.local_client import LocalClient  # noqa: E402

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
    try:
        mcp.chat_say(text="no", chatter="A", status="over", channel="mcpchat", files=[str(f)])
        raise AssertionError("chat_say(files) should be gated")
    except ConfigError:
        check(True, "chat_say(files=...) -> ConfigError when send opt-in is off")
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


def test_chat_say_with_files_success() -> None:
    print("chat_say(files=...) attaches to a live turn once enabled:")
    _allow("send", True)
    img = _TMP / "pic.png"
    img.write_bytes(b"\x89PNG\r\n")
    res = mcp.chat_say(text="diagram", chatter="A", status="over", channel="mcpchat", to="B", files=[str(img)])
    check(res["sent_messages"] >= 1 and res["status"] == "over", "the turn is sent")
    # B receives the turn with the attachment.
    chat.reset("mcpchat", "B", "0", 20)
    got = chat.await_turn(LocalClient("B"), "mcpchat", "B", timeout=2, poll=0.02, nudge_after=0)
    check(got["from"] == "A" and len(got["attachments"]) == 1,
          "the awaiting peer sees the turn's attachment")


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
        test_chat_say_with_files_success()
        test_download_success()
    finally:
        for k in ("DISCORDINATOR_ALLOW_SEND", "DISCORDINATOR_ALLOW_RECEIVE"):
            os.environ.pop(k, None)
    print(f"\nALL {_passed} MCP-ATTACHMENT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
