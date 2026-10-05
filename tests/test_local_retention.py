"""Local-room retention: messages older than ``local_retention_days`` are pruned
when the room is next written to, along with their stored attachments.

Covers the config surface (default 7, env override, validation, the
``config set-local-retention`` command), the pruning rules (cutoff, the 10%
slack that keeps a steady stream from rewriting every append, 0 = forever,
unparseable timestamps kept), attachment cleanup, and factory wiring.
Run:  python tests/test_local_retention.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-ret-"))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
for _k in ("DISCORD_BOT_TOKEN", "DISCORDINATOR_LABEL", "DISCORDINATOR_LOCAL_RETENTION_DAYS"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discordinator import cli, config  # noqa: E402
from discordinator.client_factory import make_client  # noqa: E402
from discordinator.local_client import LocalClient  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def _backdate(c: LocalClient, room: str, ages_days: list[float]) -> list[str]:
    """Seed ``room`` with one message per age (oldest first) by rewriting the
    stored timestamps. Returns the ids in order."""
    ids = [c.post(room, f"msg aged {a}d")["id"] for a in ages_days]
    path = c._room_path(room)
    now = datetime.now(timezone.utc)
    recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    for rec, age in zip(recs, ages_days):
        rec["timestamp"] = (now - timedelta(days=age)).isoformat()
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    return ids


def _contents(c: LocalClient, room: str) -> list[str]:
    return [m["content"] for m in reversed(c.read_messages(room, limit=100))]


def test_config_surface() -> None:
    print("config: default 7, env override, validation:")
    cfg = config.load()
    check(config.local_retention_days(cfg) == 7, "default retention is 7 days")
    os.environ["DISCORDINATOR_LOCAL_RETENTION_DAYS"] = "2.5"
    try:
        check(config.local_retention_days(config.load()) == 2.5, "env var overrides (2.5)")
    finally:
        os.environ.pop("DISCORDINATOR_LOCAL_RETENTION_DAYS")
    check(config.local_retention_days({"local_retention_days": 0}) == 0, "0 accepted (keep forever)")
    for bad in ("abc", -1, "inf", "nan"):
        try:
            config.local_retention_days({"local_retention_days": bad})
            raise AssertionError(f"{bad!r} should be rejected")
        except config.ConfigError as exc:
            check("set-local-retention" in str(exc), f"{bad!r} rejected with the fix named")


def test_cli_set_local_retention() -> None:
    print("config set-local-retention persists and validates:")
    check(cli.main(["config", "set-local-retention", "3"]) == 0, "set-local-retention 3 ok")
    check(config.load()["local_retention_days"] == 3, "persisted as 3 (int)")
    check(cli.main(["config", "set-local-retention", "0.5"]) == 0, "fractional days ok")
    check(config.load()["local_retention_days"] == 0.5, "persisted as 0.5")
    check(cli.main(["config", "set-local-retention", "-2"]) == 1, "negative -> exit 1")
    check(config.load()["local_retention_days"] == 0.5, "bad value left config unchanged")
    check(cli.main(["version"]) == 0, "version runs with retention shown")
    check(cli.main(["config", "set-local-retention", "7"]) == 0, "reset to 7")


def test_prunes_old_on_write() -> None:
    print("a write drops messages older than the window, keeps the rest:")
    c = LocalClient(label="r", retention_days=7)
    _backdate(c, "prune", [10, 9, 3, 1])
    c.post("prune", "fresh")
    got = _contents(c, "prune")
    check(got == ["msg aged 3d", "msg aged 1d", "fresh"], f"old dropped, recent kept: {got}")


def test_slack_avoids_rewrite_churn() -> None:
    print("oldest only just past the window (inside 10% slack) -> no rewrite yet:")
    c = LocalClient(label="r", retention_days=10)
    _backdate(c, "slack", [10.5, 2])  # past 10d, but under 11d (10% slack)
    c.post("slack", "fresh")
    check(len(_contents(c, "slack")) == 3, "nothing pruned while within the slack")
    _backdate(c, "slack2", [11.5, 10.2, 2])  # oldest past slack -> prune to the window
    c.post("slack2", "fresh")
    check(_contents(c, "slack2") == ["msg aged 2d", "fresh"],
          "once triggered, prunes everything past the window (not just past the slack)")


def test_zero_keeps_forever() -> None:
    print("retention 0 = keep forever:")
    c = LocalClient(label="r", retention_days=0)
    _backdate(c, "forever", [400, 30])
    c.post("forever", "fresh")
    check(len(_contents(c, "forever")) == 3, "nothing pruned at 0")


def test_unparseable_timestamp_kept() -> None:
    print("records without a usable timestamp are never pruned:")
    c = LocalClient(label="r", retention_days=7)
    _backdate(c, "oddts", [30, 20])
    path = c._room_path("oddts")
    recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    recs[1]["timestamp"] = "not a date"
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    c.post("oddts", "fresh")
    check(_contents(c, "oddts") == ["msg aged 20d", "fresh"],
          "dated old record pruned; unparseable one kept")


def test_attachments_removed_with_message() -> None:
    print("a pruned message's stored attachment files are deleted too:")
    c = LocalClient(label="r", retention_days=7)
    src = _TMP / "pic.txt"
    src.write_text("hello", encoding="utf-8")
    old = c.send_files("att", "with file", [src])[0]
    keep = c.send_files("att", "recent file", [src])[0]
    old_dir, keep_dir = c._files_dir(old["id"]), c._files_dir(keep["id"])
    check(old_dir.exists() and keep_dir.exists(), "both attachment dirs stored")
    path = c._room_path("att")
    recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    recs[0]["timestamp"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    c.post("att", "fresh")
    check(not old_dir.exists(), "pruned message's attachment dir removed")
    check(keep_dir.exists(), "kept message's attachment dir untouched")


def test_factory_passes_config() -> None:
    print("make_client hands the configured retention to LocalClient:")
    os.environ["DISCORDINATOR_LOCAL_RETENTION_DAYS"] = "4"
    try:
        client = make_client(config.load(), "chat")
        check(client._retention_days == 4, "factory -> retention 4 from config")
    finally:
        os.environ.pop("DISCORDINATOR_LOCAL_RETENTION_DAYS")
    check(LocalClient()._retention_days == 7, "direct LocalClient() defaults to 7")


def main() -> int:
    test_config_surface()
    test_cli_set_local_retention()
    test_prunes_old_on_write()
    test_slack_avoids_rewrite_churn()
    test_zero_keeps_forever()
    test_unparseable_timestamp_kept()
    test_attachments_removed_with_message()
    test_factory_passes_config()
    print(f"\nALL {_passed} LOCAL-RETENTION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
