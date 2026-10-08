"""End-to-end tests for the attachment CLI glue (previously only smoke-tested).

Drives `cli.main([...])` over the LOCAL transport (no network) to exercise the
wiring that unit tests skip: the send/receive opt-in GATES, `send --file`,
`read --download`, and `config set-attachments` persistence. Isolated config +
storage under a throwaway dir.  Run:  python tests/test_cli_attachments.py
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-cli-att-"))
os.chdir(_TMP)  # never the repo: a .env there would be loaded into the test
atexit.register(lambda: (os.chdir(tempfile.gettempdir()),
                         shutil.rmtree(_TMP, ignore_errors=True)))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
for _k in ("DISCORDINATOR_ALLOW_SEND", "DISCORDINATOR_ALLOW_RECEIVE"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discordinator import cli, config  # noqa: E402
from discordinator.discord_client import simplify_message  # noqa: E402
from discordinator.local_client import LocalClient  # noqa: E402

_passed = 0
ROOM = "cli-att-room"


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def test_send_file_gated_then_succeeds() -> None:
    print("send --file: blocked until the send opt-in is on, then uploads:")
    src = _TMP / "cfg.yaml"
    src.write_text("k: v\n", encoding="utf-8")

    rc = cli.main(["send", "hello", "--channel", ROOM, "--file", str(src)])
    check(rc == 1, "send --file is refused while send opt-in is OFF (exit 1)")

    rc = cli.main(["config", "set-attachments", "send", "on"])
    check(rc == 0, "config set-attachments send on succeeds")

    rc = cli.main(["send", "see file", "--channel", ROOM, "--file", str(src), "--label", "T"])
    check(rc == 0, "send --file succeeds once enabled")

    msgs = LocalClient("probe").read_messages(ROOM, limit=5)
    atts = simplify_message(msgs[0])["attachments"]
    check(len(atts) == 1 and atts[0]["filename"] == "cfg.yaml",
          "the file was attached and stored in the room")


def test_read_download_gated_then_succeeds() -> None:
    print("read --download: blocked until the receive opt-in is on, then fetches:")
    outdir = _TMP / "dl"
    rc = cli.main(["read", "--channel", ROOM, "--download", "--download-dir", str(outdir)])
    check(rc == 1, "read --download is refused while receive opt-in is OFF (exit 1)")

    rc = cli.main(["config", "set-attachments", "receive", "on"])
    check(rc == 0, "config set-attachments receive on succeeds")

    rc = cli.main(["read", "--channel", ROOM, "--download", "--download-dir", str(outdir)])
    check(rc == 0, "read --download succeeds once enabled")
    check((outdir / "cfg.yaml").exists(), "the attachment was downloaded to the target dir")


def test_send_files_only_no_text() -> None:
    print("send --file with NO text body is allowed (files-only message):")
    f = _TMP / "only.txt"
    f.write_text("just a file", encoding="utf-8")
    rc = cli.main(["send", "--channel", ROOM, "--file", str(f)])  # no text positional
    check(rc == 0, "a files-only send (no text) succeeds, doesn't block on stdin")
    atts = simplify_message(LocalClient("probe").read_messages(ROOM, limit=1)[0])["attachments"]
    check(any(a["filename"] == "only.txt" for a in atts), "the files-only attachment is stored")


def test_read_download_json() -> None:
    print("read --download --json reports downloaded paths as JSON:")
    outdir = _TMP / "dl-json"
    rc = cli.main(["read", "--channel", ROOM, "--download", "--download-dir", str(outdir), "--json"])
    check(rc == 0, "read --download --json succeeds")
    check(any(outdir.iterdir()), "attachments were written to the json-mode download dir")


def test_flags_persisted() -> None:
    print("the opt-in flags are persisted to config (not just session env):")
    cfg = config.load()
    check(config.can_send_attachments(cfg) is True, "send flag persisted to disk")
    check(config.can_receive_attachments(cfg) is True, "receive flag persisted to disk")


def test_image_alias_shares_file_list() -> None:
    print("--image and --file accumulate into one attachment list:")
    img = _TMP / "pic.png"
    img.write_bytes(b"\x89PNG\r\n")
    doc = _TMP / "a.txt"
    doc.write_text("hi", encoding="utf-8")
    rc = cli.main(["send", "both", "--channel", ROOM, "--image", str(img), "--file", str(doc)])
    check(rc == 0, "send with both --image and --file succeeds")
    msgs = LocalClient("probe").read_messages(ROOM, limit=1)
    names = {a["filename"] for a in simplify_message(msgs[0])["attachments"]}
    check(names == {"pic.png", "a.txt"}, "both --image and --file items are attached")


def main() -> int:
    test_send_file_gated_then_succeeds()
    test_read_download_gated_then_succeeds()
    test_send_files_only_no_text()
    test_read_download_json()
    test_flags_persisted()
    test_image_alias_shares_file_list()
    print(f"\nALL {_passed} CLI-ATTACHMENT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
