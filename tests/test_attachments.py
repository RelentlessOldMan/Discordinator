"""Tests for attachment RECEIVE + PARSE (Phase 1).

Covers the transport-agnostic pieces that make received attachments usable:
the normalized attachment shape produced on every read (`attachment_info` /
`simplify_message`), image detection, filename recovery from a CDN url, the
per-machine opt-in gate (off by default), and the download path on BOTH
transports (Discord = HTTP GET via a mock; local = a filesystem copy).

No network, no real Discord: an httpx.MockTransport feeds canned bytes and each
test isolates its own config/storage under a throwaway dir.
Run:  python tests/test_attachments.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-attach-"))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

import discordinator.discord_client as dc  # noqa: E402
from discordinator import config  # noqa: E402
from discordinator.config import ConfigError  # noqa: E402
from discordinator.discord_client import (  # noqa: E402
    API_BASE,
    DiscordClient,
    DiscordError,
    attachment_info,
    simplify_message,
)
from discordinator.local_client import LocalClient  # noqa: E402

_passed = 0

# Env vars config.load() consults for the attachment gate — cleared per test so
# a stray value in the real environment can't flip a result.
_ENV_KEYS = ("DISCORDINATOR_ALLOW_SEND", "DISCORDINATOR_ALLOW_RECEIVE")


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def _clear_env() -> None:
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


def _rm_config() -> None:
    Path(os.environ["DISCORDINATOR_CONFIG"]).unlink(missing_ok=True)


def _mock_client(handler) -> DiscordClient:
    client = DiscordClient(token="test-token")
    client._client = httpx.Client(base_url=API_BASE, transport=httpx.MockTransport(handler))
    return client


# -- parse / shape ---------------------------------------------------------


def test_attachment_info_image_by_content_type() -> None:
    print("attachment_info: image detected via content_type, metadata carried:")
    a = attachment_info({
        "url": "http://x/pic.png", "filename": "pic.png",
        "content_type": "image/png", "size": 2048, "width": 640, "height": 480,
    })
    check(a["is_image"] is True, "content_type image/* -> is_image True")
    check(a["width"] == 640 and a["height"] == 480, "image dimensions carried through")
    check(a["size"] == 2048 and a["filename"] == "pic.png", "size and filename carried")
    check(a["content_type"] == "image/png", "content_type carried")


def test_attachment_info_image_by_extension() -> None:
    print("attachment_info: image detected via filename extension (no content_type):")
    a = attachment_info({"url": "http://x/PHOTO.JPG", "filename": "PHOTO.JPG"})
    check(a["is_image"] is True, "image extension is detected case-insensitively")
    check(a["content_type"] is None, "missing content_type -> None (not a crash)")
    check(a["width"] is None and a["height"] is None, "missing dimensions -> None")


def test_attachment_info_non_image() -> None:
    print("attachment_info: non-image files are not flagged as images:")
    yaml = attachment_info({"url": "http://x/config.yaml", "filename": "config.yaml",
                            "content_type": "text/yaml", "size": 120})
    check(yaml["is_image"] is False, "a .yaml config is not an image")
    txt = attachment_info({"url": "http://x/notes.txt", "filename": "notes.txt"})
    check(txt["is_image"] is False, "a .txt with no content_type is not an image")


def test_simplify_message_attachments_shape() -> None:
    print("simplify_message: attachments become rich dicts (clean swap from url strings):")
    raw = {
        "id": "9", "author": {"username": "u"}, "timestamp": "t", "content": "c",
        "attachments": [
            {"url": "http://x/a.png", "filename": "a.png", "content_type": "image/png"},
            {"filename": "no-url.txt"},  # dropped: no url
            {"url": "http://x/b.pdf", "filename": "b.pdf", "content_type": "application/pdf"},
        ],
    }
    s = simplify_message(raw)
    atts = s["attachments"]
    check(isinstance(atts, list) and all(isinstance(a, dict) for a in atts),
          "attachments is now a list of dicts, not url strings")
    check(len(atts) == 2, "an attachment without a url is still dropped")
    check([a["filename"] for a in atts] == ["a.png", "b.pdf"], "order is preserved")
    check(atts[0]["is_image"] is True and atts[1]["is_image"] is False,
          "the png is flagged image, the pdf is not")
    check(simplify_message({})["attachments"] == [], "no attachments -> empty list (safe default)")


def test_filename_from_url() -> None:
    print("_filename_from_url: recovers a name from a (possibly signed) CDN url:")
    check(dc._filename_from_url("https://cdn.x/attachments/1/2/config.yaml?ex=ab&is=cd")
          == "config.yaml", "query string is stripped, last path segment kept")
    check(dc._filename_from_url("https://cdn.x/a/my%20file.txt") == "my file.txt",
          "percent-encoding is decoded")
    check(dc._filename_from_url("https://cdn.x/") == "attachment",
          "no filename in url -> 'attachment' fallback")


# -- the opt-in gate (off by default) --------------------------------------


def test_gate_defaults_off() -> None:
    print("attachment gate: OFF by default (no willy-nilly sending/receiving):")
    _clear_env()
    _rm_config()
    cfg = config.load()
    check(cfg["allow_send_attachments"] is False, "send attachments default False")
    check(cfg["allow_receive_attachments"] is False, "receive attachments default False")
    check(config.can_send_attachments(cfg) is False, "can_send_attachments False by default")
    check(config.can_receive_attachments(cfg) is False, "can_receive_attachments False by default")


def test_gate_env_override() -> None:
    print("attachment gate: env vars can enable each direction independently:")
    _clear_env()
    _rm_config()
    os.environ["DISCORDINATOR_ALLOW_RECEIVE"] = "1"
    cfg = config.load()
    check(config.can_receive_attachments(cfg) is True, "DISCORDINATOR_ALLOW_RECEIVE=1 enables receive")
    check(config.can_send_attachments(cfg) is False, "send stays off (independent flag)")
    _clear_env()
    os.environ["DISCORDINATOR_ALLOW_SEND"] = "yes"
    os.environ["DISCORDINATOR_ALLOW_RECEIVE"] = "off"
    cfg = config.load()
    check(config.can_send_attachments(cfg) is True, "DISCORDINATOR_ALLOW_SEND=yes enables send")
    check(config.can_receive_attachments(cfg) is False, "ALLOW_RECEIVE=off parses to False")
    _clear_env()


def test_gate_require_raises() -> None:
    print("require_* helpers raise a helpful ConfigError when disabled:")
    disabled = {"allow_send_attachments": False, "allow_receive_attachments": False}
    try:
        config.require_receive_attachments(disabled)
        raise AssertionError("expected ConfigError when receive is disabled")
    except ConfigError as e:
        check("disabled" in str(e).lower(), "receive disabled -> ConfigError explaining how to enable")
    try:
        config.require_send_attachments(disabled)
        raise AssertionError("expected ConfigError when send is disabled")
    except ConfigError as e:
        check("disabled" in str(e).lower(), "send disabled -> ConfigError explaining how to enable")
    # Enabled -> no raise.
    config.require_receive_attachments({"allow_receive_attachments": True})
    config.require_send_attachments({"allow_send_attachments": True})
    check(True, "require_* is a no-op when the direction is enabled")


# -- download: Discord transport (HTTP GET, mocked) ------------------------


def test_discord_download_to_file() -> None:
    print("DiscordClient.download_attachment: GET bytes -> written file:")
    payload = b"name: value\nfoo: bar\n"
    client = _mock_client(lambda req: httpx.Response(200, content=payload))
    try:
        dest = _TMP / "out" / "config.yaml"
        got = client.download_attachment("https://cdn.x/a/config.yaml?ex=1", dest)
        check(got == dest, "returns the written path")
        check(dest.read_bytes() == payload, "bytes are written verbatim")
    finally:
        client.close()


def test_discord_download_to_dir_uses_url_name() -> None:
    print("DiscordClient.download_attachment: dest dir -> filename from url:")
    client = _mock_client(lambda req: httpx.Response(200, content=b"img"))
    try:
        d = _TMP / "downloads"
        d.mkdir(parents=True, exist_ok=True)
        got = client.download_attachment("https://cdn.x/a/chart.png?ex=9", d)
        check(got == d / "chart.png", "filename is derived from the url into the directory")
        check(got.read_bytes() == b"img", "content written into the directory")
    finally:
        client.close()


def test_discord_download_no_token_leak() -> None:
    print("DiscordClient.download_attachment: the bot token is not sent to the CDN:")
    seen: dict = {}

    def handler(req):
        seen["auth"] = req.headers.get("authorization", "")
        return httpx.Response(200, content=b"x")

    client = _mock_client(handler)
    try:
        client.download_attachment("https://cdn.x/a/f.png", _TMP / "noleak.png")
        check("test-token" not in seen["auth"], "the bot token is blanked on the CDN GET")
    finally:
        client.close()


def test_discord_download_dir_collision() -> None:
    print("DiscordClient.download_attachment: same-named downloads don't overwrite:")
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        return httpx.Response(200, content=f"body{state['n']}".encode())

    client = _mock_client(handler)
    try:
        d = _TMP / "coll"
        d.mkdir(parents=True, exist_ok=True)
        p1 = client.download_attachment("https://cdn.x/a/report.pdf?ex=1", d)
        p2 = client.download_attachment("https://cdn.x/b/report.pdf?ex=2", d)
        check(p1 != p2, "a second same-named download gets a distinct path")
        check(p1.read_bytes() == b"body1" and p2.read_bytes() == b"body2",
              "neither download clobbered the other")
    finally:
        client.close()


def test_discord_download_error_maps() -> None:
    print("DiscordClient.download_attachment: HTTP error -> DiscordError:")
    _orig = dc.time.sleep
    dc.time.sleep = lambda *_a, **_k: None
    client = _mock_client(lambda req: httpx.Response(404, json={}))
    try:
        client.download_attachment("https://cdn.x/a/gone.png", _TMP / "gone.png")
        raise AssertionError("expected DiscordError for a 404 download")
    except DiscordError as e:
        check("Not found" in str(e), "a 404 download surfaces as a DiscordError")
    finally:
        dc.time.sleep = _orig
        client.close()


# -- download: local transport (filesystem copy) ---------------------------


def test_local_download_copies_file() -> None:
    print("LocalClient.download_attachment: copies the stored file (no CDN):")
    src = _TMP / "store" / "data.bin"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"\x00\x01\x02local")
    client = LocalClient(label="demo")
    out_dir = _TMP / "recv"
    out_dir.mkdir(parents=True, exist_ok=True)
    got = client.download_attachment(str(src), out_dir)
    check(got == out_dir / "data.bin", "filename is preserved when dest is a directory")
    check(got.read_bytes() == b"\x00\x01\x02local", "bytes match the stored file")
    # Explicit destination file path is honored too.
    explicit = _TMP / "recv" / "renamed.bin"
    got2 = client.download_attachment(str(src), explicit)
    check(got2 == explicit and explicit.read_bytes() == b"\x00\x01\x02local",
          "an explicit dest path is written as-is")


def test_local_download_missing_source() -> None:
    print("LocalClient.download_attachment: a missing source errors clearly:")
    client = LocalClient(label="demo")
    try:
        client.download_attachment(str(_TMP / "nope" / "ghost.txt"), _TMP / "recv")
        raise AssertionError("expected FileNotFoundError for a missing local attachment")
    except FileNotFoundError:
        check(True, "missing local attachment -> FileNotFoundError (not a silent empty file)")


def test_surface_parity() -> None:
    print("both transports expose download_attachment (surface parity):")
    check(callable(getattr(DiscordClient, "download_attachment", None)),
          "DiscordClient has download_attachment")
    check(callable(getattr(LocalClient, "download_attachment", None)),
          "LocalClient has download_attachment")


def main() -> int:
    test_attachment_info_image_by_content_type()
    test_attachment_info_image_by_extension()
    test_attachment_info_non_image()
    test_simplify_message_attachments_shape()
    test_filename_from_url()
    test_gate_defaults_off()
    test_gate_env_override()
    test_gate_require_raises()
    test_discord_download_to_file()
    test_discord_download_to_dir_uses_url_name()
    test_discord_download_no_token_leak()
    test_discord_download_dir_collision()
    test_discord_download_error_maps()
    test_local_download_copies_file()
    test_local_download_missing_source()
    test_surface_parity()
    _clear_env()
    print(f"\nALL {_passed} ATTACHMENT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
