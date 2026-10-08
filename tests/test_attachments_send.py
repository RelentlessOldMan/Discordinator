"""Tests for attachment SEND (Phase 2) — uploading files/images out.

Discord side: multipart/form-data uploads (payload_json + files[n] parts),
label prefixing, the per-file size limit, the 10-files-per-message cap
(batched into multiple messages), and missing-file handling — all via an
httpx.MockTransport, no network. Local side: files are copied into a per-message
store and surface as normal attachments on read (full round-trip), so two local
agents can hand each other files.

Run:  python tests/test_attachments_send.py
"""

from __future__ import annotations

import json
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-send-"))
os.chdir(_TMP)  # never the repo: a .env there would be loaded into the test
atexit.register(lambda: (os.chdir(tempfile.gettempdir()),
                         shutil.rmtree(_TMP, ignore_errors=True)))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

import discordinator.discord_client as dc  # noqa: E402
from discordinator.discord_client import (  # noqa: E402
    API_BASE,
    DiscordClient,
    DiscordError,
    guess_content_type,
    simplify_message,
)
from discordinator.local_client import LocalClient  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def _mock_client(handler) -> DiscordClient:
    dc.time.sleep = lambda *_a, **_k: None
    client = DiscordClient(token="test-token")
    client._client = httpx.Client(base_url=API_BASE, transport=httpx.MockTransport(handler))
    return client


def _mkfile(name: str, data: bytes = b"hello") -> Path:
    p = _TMP / name
    p.write_bytes(data)
    return p


# -- content-type guessing -------------------------------------------------


def test_guess_content_type() -> None:
    print("guess_content_type (drives is_image + the multipart part header):")
    check(guess_content_type("photo.png") == "image/png", ".png -> image/png")
    check(guess_content_type("a.jpg") == "image/jpeg", ".jpg -> image/jpeg")
    check(guess_content_type("weird.zzz") is None, "unknown extension -> None (not a crash)")


# -- Discord multipart upload ----------------------------------------------


def test_send_single_file_multipart() -> None:
    print("send_files: one file -> a multipart POST carrying payload_json + bytes:")
    seen: dict = {}

    def handler(req):
        seen["content_type"] = req.headers.get("content-type", "")
        seen["body"] = req.content
        return httpx.Response(200, json={"id": "1"})

    client = _mock_client(handler)
    try:
        f = _mkfile("notes.txt", b"file-body-here")
        sent = client.send_files("chan", "see attached", [f], label="HOME")
        check(len(sent) == 1, "a single file is one created message")
        check(seen["content_type"].startswith("multipart/form-data"),
              "the request is multipart/form-data (global application/json default removed)")
        check(b"payload_json" in seen["body"], "payload_json part is present")
        check(b"file-body-here" in seen["body"], "the file bytes are in the body")
        check(b"notes.txt" in seen["body"], "the filename is in the body")
        check(b"[HOME] see attached" in seen["body"],
              "the label prefixes the content (so relay self-filtering still works)")
    finally:
        client.close()


def test_send_files_only_no_text() -> None:
    print("send_files: files with no text is allowed (content may be empty):")
    seen: dict = {}

    def handler(req):
        seen["body"] = req.content
        return httpx.Response(200, json={"id": "1"})

    client = _mock_client(handler)
    try:
        sent = client.send_files("chan", "", [_mkfile("a.png", b"img")], label="HOME")
        check(len(sent) == 1, "a files-only message still sends")
        check(b"a.png" in seen["body"], "the file is attached even with empty text")
    finally:
        client.close()


def test_send_files_with_long_text() -> None:
    print("send_files: text past one message is split, the files ride with its last piece:")
    bodies: list = []

    def handler(req):
        if req.headers.get("content-type", "").startswith("multipart/form-data"):
            body = req.content.split(b"payload_json", 1)[1]
            content = json.loads(body[body.index(b"{"):body.index(b"}\r\n") + 1])["content"]
        else:
            content = json.loads(req.content)["content"]
        check(len(content) <= 2000, f"each message is within Discord's limit ({len(content)})")
        bodies.append(content)
        return httpx.Response(200, json={"id": str(len(bodies))})

    client = _mock_client(handler)
    try:
        text = "word " * 500 + "end"
        sent = client.send_files("chan", text, [_mkfile("long.txt", b"x")], label="HOME")
        check(len(sent) == 2 and all(b.startswith("[HOME] ") for b in bodies),
              "two labeled messages: the text didn't fit in one")
        check(" ".join(b[len("[HOME] "):] for b in bodies).split() == text.split(),
              "all of the text arrived")
    finally:
        client.close()


def test_label_too_long_and_blank_runs() -> None:
    print("a label that leaves no room errors at once; a long blank run sends nothing empty:")
    posted: list = []

    def handler(req):
        posted.append(json.loads(req.content)["content"])
        return httpx.Response(200, json={"id": "1"})

    client = _mock_client(handler)
    try:
        try:
            client.send_message("chan", "hello " * 400, label="L" * 2000)
            check(False, "a 2000-character label should raise")
        except ValueError:
            check(not posted, "it raises before posting anything (no endless split)")
        client.send_message("chan", "start" + " " * 4500 + "end")
        check(posted and all(p.strip() for p in posted),
              f"no whitespace-only message is posted ({len(posted)} message(s))")
    finally:
        client.close()


def test_send_batches_over_ten_files() -> None:
    print("send_files: >10 files are split across messages (Discord caps at 10):")
    posts = {"n": 0, "files_total": 0}

    def handler(req):
        posts["n"] += 1
        posts["files_total"] += req.content.count(b"filename=")  # counts file parts
        return httpx.Response(200, json={"id": str(posts["n"])})

    client = _mock_client(handler)
    try:
        files = [_mkfile(f"f{i}.bin", bytes([i])) for i in range(12)]
        sent = client.send_files("chan", "batch", files)
        check(len(sent) == 2, "12 files -> 2 messages (10 + 2)")
        check(posts["n"] == 2, "exactly two POSTs were made")
    finally:
        client.close()


def test_send_rejects_oversize() -> None:
    print("send_files: an oversize file is refused with a clear error (no 413):")
    orig = dc.MAX_UPLOAD_BYTES
    dc.MAX_UPLOAD_BYTES = 4  # tiny cap for the test
    client = _mock_client(lambda req: httpx.Response(200, json={"id": "1"}))
    try:
        big = _mkfile("big.bin", b"way too big")
        client.send_files("chan", "", [big])
        raise AssertionError("expected DiscordError for an oversize file")
    except DiscordError as e:
        check("limit" in str(e).lower(), "oversize file -> DiscordError mentioning the limit")
    finally:
        dc.MAX_UPLOAD_BYTES = orig
        client.close()


def test_send_missing_file() -> None:
    print("send_files: a missing path is refused before any upload:")
    client = _mock_client(lambda req: httpx.Response(200, json={"id": "1"}))
    try:
        client.send_files("chan", "", [_TMP / "does-not-exist.txt"])
        raise AssertionError("expected DiscordError for a missing file")
    except DiscordError as e:
        check("not found" in str(e).lower(), "missing file -> DiscordError (not a silent skip)")
    finally:
        client.close()


# -- Local transport: store + round-trip -----------------------------------


def test_local_send_roundtrip() -> None:
    print("LocalClient.send_files: copies into the store and reads back as attachments:")
    client = LocalClient(label="A")
    img = _mkfile("chart.png", b"\x89PNG\r\n")
    cfgfile = _mkfile("app.yaml", b"k: v\n")  # 5 bytes
    sent = client.send_files("room-send", "two files", [img, cfgfile], label="A")
    check(len(sent) == 1, "both files land in a single local message")

    raw = client.read_messages("room-send", limit=5)
    msg = simplify_message(raw[0])
    check(msg["content"] == "[A] two files", "content carries the label prefix")
    atts = msg["attachments"]
    check(len(atts) == 2, "both attachments are present on read")
    names = {a["filename"] for a in atts}
    check(names == {"chart.png", "app.yaml"}, "both filenames round-trip")
    png = next(a for a in atts if a["filename"] == "chart.png")
    check(png["is_image"] is True, "the .png is flagged as an image")
    check(Path(png["url"]).exists(), "the stored file actually exists on disk")
    check(Path(png["url"]).read_bytes() == b"\x89PNG\r\n", "stored image bytes match the source")
    yaml = next(a for a in atts if a["filename"] == "app.yaml")
    check(yaml["is_image"] is False and yaml["size"] == 5, "the yaml is a non-image with correct size")


def test_local_send_then_download() -> None:
    print("local send + download: a second agent can fetch the stored file:")
    sender = LocalClient(label="A")
    src = _mkfile("payload.bin", b"\x00\x01\x02\x03")
    sender.send_files("room-dl", "", [src])
    receiver = LocalClient(label="B")
    raw = receiver.read_messages("room-dl", limit=5)
    att = simplify_message(raw[0])["attachments"][0]
    out = receiver.download_attachment(att["url"], _TMP / "got")
    check(out.read_bytes() == b"\x00\x01\x02\x03", "receiver copies the exact stored bytes")


def test_local_send_missing_file() -> None:
    print("LocalClient.send_files: a missing source errors clearly:")
    client = LocalClient(label="A")
    try:
        client.send_files("room-x", "", [_TMP / "ghost.txt"])
        raise AssertionError("expected FileNotFoundError for a missing local file")
    except FileNotFoundError:
        check(True, "missing local source -> FileNotFoundError")


def test_local_send_duplicate_names() -> None:
    print("LocalClient.send_files: same-named files from different dirs don't collide:")
    da, db = _TMP / "da", _TMP / "db"
    da.mkdir(parents=True, exist_ok=True); db.mkdir(parents=True, exist_ok=True)
    (da / "log.txt").write_bytes(b"AAA")
    (db / "log.txt").write_bytes(b"BBB")
    client = LocalClient(label="A")
    sent = client.send_files("room-dup", "", [da / "log.txt", db / "log.txt"])
    atts = simplify_message(sent[0])["attachments"]
    check(len(atts) == 2, "both same-named files are stored")
    check(atts[0]["filename"] == "log.txt" and atts[1]["filename"] == "log.txt",
          "both keep their original filename (Discord-parity)")
    urls = {a["url"] for a in atts}
    check(len(urls) == 2, "they are stored at DISTINCT paths (no overwrite)")
    bodies = {Path(a["url"]).read_bytes() for a in atts}
    check(bodies == {b"AAA", b"BBB"}, "neither file's bytes were clobbered")


def test_send_empty_list_errors() -> None:
    print("send_files with no files is a clear error, not a silent no-op:")
    d = _mock_client(lambda req: httpx.Response(200, json={"id": "1"}))
    try:
        d.send_files("chan", "hi", [])
        raise AssertionError("expected ValueError for empty file list (Discord)")
    except ValueError:
        check(True, "DiscordClient.send_files([]) -> ValueError")
    finally:
        d.close()
    try:
        LocalClient(label="A").send_files("room", "hi", [])
        raise AssertionError("expected ValueError for empty file list (local)")
    except ValueError:
        check(True, "LocalClient.send_files([]) -> ValueError")


def test_surface_parity() -> None:
    print("both transports expose send_files (surface parity):")
    check(callable(getattr(DiscordClient, "send_files", None)), "DiscordClient.send_files exists")
    check(callable(getattr(LocalClient, "send_files", None)), "LocalClient.send_files exists")


def main() -> int:
    _orig_sleep = dc.time.sleep
    try:
        test_guess_content_type()
        test_send_single_file_multipart()
        test_send_files_only_no_text()
        test_send_files_with_long_text()
        test_label_too_long_and_blank_runs()
        test_send_batches_over_ten_files()
        test_send_rejects_oversize()
        test_send_missing_file()
        test_local_send_roundtrip()
        test_local_send_then_download()
        test_local_send_missing_file()
        test_local_send_duplicate_names()
        test_send_empty_list_errors()
        test_surface_parity()
    finally:
        dc.time.sleep = _orig_sleep
    print(f"\nALL {_passed} SEND CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
