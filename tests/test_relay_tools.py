"""Relay tools and human commands that had no tests: MCP purge_messages and its
safe defaults, CLI purge and its durations, CLI relay, MCP read_messages and
get_new_messages options, a failing ack, `watch --state`, and a chat_say
that fails part-way through a long message.
Run:  python tests/test_relay_tools.py
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-relaytools-"))
os.chdir(_TMP)  # never the repo: a .env there would be loaded into the test
atexit.register(lambda: (os.chdir(tempfile.gettempdir()),
                         shutil.rmtree(_TMP, ignore_errors=True)))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_LABEL"] = "me"
for _k in ("DISCORD_BOT_TOKEN", "DISCORDINATOR_CHAT_HANDLE", "DISCORDINATOR_CHAT_CHANNEL",
           "DISCORDINATOR_RELAY_CHANNEL", "DISCORDINATOR_SESSION_ID", "DISCORDINATOR_ACK"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import discordinator.mcp_server as mcp  # noqa: E402
from discordinator import chat, cli, config  # noqa: E402
from discordinator.discord_client import DiscordError  # noqa: E402
from discordinator.local_client import LocalClient, local_dir  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def _age(room: str, contents: set[str], days: float) -> None:
    """Make the messages with these contents ``days`` old."""
    path = local_dir() / f"{room}.jsonl"
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    recs = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    for r in recs:
        if any(r["content"].endswith(c) for c in contents):
            r["timestamp"] = when
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")


def _contents(room: str) -> list[str]:
    return [m["content"] for m in reversed(LocalClient("probe").read_messages(room, limit=100))]


def _run(argv: list[str], stdin: str = "") -> tuple[int, str]:
    out = io.StringIO()
    real = sys.stdin
    sys.stdin = io.StringIO(stdin)  # not a terminal
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = cli.main(argv)
    finally:
        sys.stdin = real
    return rc, out.getvalue()


def test_mcp_purge_defaults() -> None:
    print("purge_messages defaults: a dry run of my own messages older than 7 days:")
    r = "purge-mcp"
    LocalClient("me").send_message(r, "my old")
    LocalClient("me").send_message(r, "my new")
    LocalClient("other").send_message(r, "their old")
    _age(r, {"my old", "their old"}, 10)
    out = mcp.purge_messages(channel=r)
    check(out["dry_run"] and out["would_delete"] == 1, f"only my old message matches: {out}")
    check(len(_contents(r)) == 3, "a dry run deletes nothing")
    out = mcp.purge_messages(channel=r, dry_run=False)
    check(out["deleted"] == 1 and _contents(r) == ["my new", "their old"], "then just that one goes")
    out = mcp.purge_messages(channel=r, dry_run=False, only_mine=False, older_than_days=0)
    check(out["deleted"] == 2 and _contents(r) == [], "older_than_days=0, only_mine=False clears it")


def test_cli_purge() -> None:
    print("CLI purge: durations, preview, refusal without a terminal, --yes:")
    for text, secs in (("7d", 7 * 86400), ("24h", 86400), ("30m", 1800), ("90s", 90), ("2", 2 * 86400)):
        check(cli._parse_duration(text) == secs, f"{text} = {secs}s")
    try:
        cli._parse_duration("7 weeks")
        check(False, "a bad duration should raise")
    except config.ConfigError:
        check(True, "a bad duration is an error")
    r = "purge-cli"
    LocalClient("me").send_message(r, "mine old")
    LocalClient("me").send_message(r, "mine new")
    LocalClient("other").send_message(r, "theirs old")
    _age(r, {"mine old", "theirs old"}, 10)
    rc, out = _run(["purge", "-c", r, "--older-than", "7d", "--dry-run"])
    check(rc == 0 and "would delete 1" in out and len(_contents(r)) == 3, "--dry-run previews only")
    rc, out = _run(["purge", "-c", r, "--older-than", "7d"])
    check(rc == 1 and "--yes" in out and len(_contents(r)) == 3,
          "without a terminal to confirm on, it refuses and deletes nothing")
    rc, out = _run(["purge", "-c", r, "--older-than", "7d", "--yes"])
    check(rc == 0 and _contents(r) == ["mine new", "theirs old"], "--yes deletes just my old one")
    rc, out = _run(["purge", "-c", r, "--yes"])
    check(rc == 0 and _contents(r) == ["theirs old"],
          "with no --older-than it takes all of mine, whatever their age")


def test_cli_relay() -> None:
    print("CLI relay: only what's new, without my own label, --reset re-shows:")
    r = "relay-cli"
    LocalClient("x").send_message(r, "from them", label="them")
    LocalClient("x").send_message(r, "from me", label="me")
    rc, out = _run(["relay", "-c", r, "--json"])
    got = [m["content"] for m in json.loads(out)]
    check(rc == 0 and got == ["[them] from them"], f"first run: theirs, not mine: {got}")
    rc, out = _run(["relay", "-c", r])
    check(rc == 0 and "no new messages" in out, "second run: nothing new")
    LocalClient("x").send_message(r, "again", label="them")
    rc, out = _run(["relay", "-c", r, "--json"])
    check([m["content"] for m in json.loads(out)] == ["[them] again"], "then only the new one")
    rc, out = _run(["relay", "-c", r, "--reset", "--json"])
    check(len(json.loads(out)) == 2, "--reset shows recent ones again")


def test_read_messages_tool() -> None:
    print("read_messages: order, limit, after, and an ack that fails:")
    r = "read-tool"
    ids = [LocalClient("x").send_message(r, f"m{n}")[0]["id"] for n in range(5)]
    got = [m["content"] for m in mcp.read_messages(channel=r, limit=3)]
    check(got == ["m2", "m3", "m4"], f"the latest 3, oldest first: {got}")
    got = [m["content"] for m in mcp.read_messages(channel=r, limit=3, newest_first=True)]
    check(got == ["m4", "m3", "m2"], "newest_first")
    got = [m["content"] for m in mcp.read_messages(channel=r, after=ids[2])]
    check(got == ["m3", "m4"], "after an id")
    real = LocalClient.add_reaction

    def no_perm(self, *a, **k):
        raise DiscordError("Missing Permissions (Add Reactions)")

    LocalClient.add_reaction = no_perm  # type: ignore[assignment]
    try:
        got = mcp.read_messages(channel=r, limit=2, ack=True)
        check(len(got) == 2, "an ack the bot can't add doesn't fail the read")
        got = mcp.get_new_messages(channel=r, ack=True)
        check(len(got) == 5, "nor get_new_messages")
    finally:
        LocalClient.add_reaction = real  # type: ignore[assignment]


def test_get_new_messages_options() -> None:
    print("get_new_messages: include_self, and a backlog over 100:")
    r = "gnm"
    mcp.send_message("mine", channel=r)
    LocalClient("x").send_message(r, "theirs", label="them")
    got = [m["content"] for m in mcp.get_new_messages(channel=r, include_self=True)]
    check(got == ["[me] mine", "[them] theirs"], f"include_self shows my own too: {got}")
    for n in range(150):
        LocalClient("x").send_message(r, f"n{n}", label="them")
    first = mcp.get_new_messages(channel=r)
    second = mcp.get_new_messages(channel=r)
    third = mcp.get_new_messages(channel=r)
    check(len(first) == 100 and len(second) == 50 and third == [],
          f"100 per call, then the rest, then nothing: {len(first)}, {len(second)}, {len(third)}")
    check(first[0]["content"] == "[them] n0" and second[-1]["content"] == "[them] n149",
          "none skipped, none repeated")


def test_watch_state_footer() -> None:
    print("watch --state shows who holds the floor:")
    r = "watch-state"
    chat.send_chat(LocalClient("x"), r, "A", "over", "your go", to="B")
    rc, out = _run(["watch", r, "--state", "--no-events", "--no-color"])
    check(rc == 0 and "your go" in out and "floor: B" in out, "the footer names B")


def test_chat_say_fails_part_way() -> None:
    print("a long chat_say that fails part-way says what went out:")
    r = "partial"
    mcp.chat_begin(chatter="A", channel=r)
    mcp.chat_begin(chatter="B", channel=r)
    real = LocalClient.post
    calls = []

    def flaky(self, channel_id, content, *a, **k):
        calls.append(content)
        if len(calls) == 2:
            raise DiscordError("HTTP 500 from Discord")
        return real(self, channel_id, content, *a, **k)

    LocalClient.post = flaky  # type: ignore[assignment]
    try:
        try:
            mcp.chat_say(text="x" * 5000, chatter="A", channel=r, to="B", wait=False)
            check(False, "it should raise")
        except Exception as e:  # noqa: BLE001
            check("Only the first 1 of 3 pieces" in str(e) and "send just the rest" in str(e),
                  f"the error says the first piece went out: {str(e)[:90]}")
    finally:
        LocalClient.post = real  # type: ignore[assignment]
    sent = [c for c in _contents(r) if c.startswith("[A>B|")]
    check(len(sent) == 1 and sent[0].startswith("[A>B|say]"), "exactly that piece is in the room, as `say`")


def test_deleting_the_last_read_message_replays_nothing() -> None:
    print("deleting the message a session last read doesn't replay its inbox:")
    r = "del-last"
    for n in range(3):
        LocalClient("x").send_message(r, f"old {n}", label="them")
    check(len(mcp.get_new_messages(channel=r)) == 3, "read them")
    last = LocalClient("x").send_message(r, "newest", label="them")[0]["id"]
    check(len(mcp.get_new_messages(channel=r)) == 1, "read the newest")
    LocalClient("x").delete_messages(r, [last])
    check(mcp.get_new_messages(channel=r) == [], "nothing comes back after it's deleted")
    LocalClient("x").send_message(r, "after", label="them")
    check([m["content"] for m in mcp.get_new_messages(channel=r)] == ["[them] after"],
          "and new messages still arrive")


def test_sessions_sharing_a_label_see_each_other() -> None:
    print("two sessions with one label and no chat handle still get each other's messages:")
    from discordinator import handles
    r = "shared-label"
    try:
        handles._SESSION_KEY = "id:session-a"
        mcp._sent_ids.clear()
        mcp.send_message("hello from A", channel=r)
        handles._SESSION_KEY = "id:session-b"  # another Claude Code session
        mcp._sent_ids.clear()
        got = [m["content"] for m in mcp.get_new_messages(channel=r)]
        check(got == ["[me] hello from A"], f"B gets A's message: {got}")
        handles._SESSION_KEY = "id:session-a"  # A again, after a restart
        mcp._sent_ids.clear()
        mcp.send_message("more from A", channel=r)
        mcp._sent_ids.clear()
        check(all("from A" not in m["content"] for m in mcp.get_new_messages(channel=r)),
              "A's restarted server still skips A's own")
    finally:
        handles._SESSION_KEY = None
        mcp._sent_ids.clear()


def test_event_log_open_elsewhere() -> None:
    print("an event is still logged when the log is open elsewhere at rotation time:")
    import time
    from discordinator import events
    real = events.ROTATE_AT
    events.ROTATE_AT = 100
    try:
        events.record("filler", pad="x" * 200)
        with open(events.log_path(), "rb"):  # someone tailing the log (Windows can't rotate it)
            t0 = time.monotonic()
            events.record("while_open")
            took = time.monotonic() - t0
        check(took < 0.5, f"no long stall ({took:.2f}s)")
        check(any(e.get("kind") == "while_open" for e in events.read(0)[0]), "and the event is there")
    finally:
        events.ROTATE_AT = real


def test_stop_needs_a_local_room() -> None:
    print("stop/interject with no such local room say so instead of writing into nowhere:")
    rc, out = _run(["stop", "-c", "no-such-room"])
    check(rc == 1 and "no local chat room" in out, "stop refuses")
    rc, out = _run(["interject", "hi", "-c", "no-such-room"])
    check(rc == 1 and "no local chat room" in out, "interject refuses")
    check(not (local_dir() / "no-such-room.jsonl").exists(), "and no room was created")


def main() -> int:
    test_mcp_purge_defaults()
    test_cli_purge()
    test_cli_relay()
    test_read_messages_tool()
    test_get_new_messages_options()
    test_watch_state_footer()
    test_chat_say_fails_part_way()
    test_deleting_the_last_read_message_replays_nothing()
    test_sessions_sharing_a_label_see_each_other()
    test_event_log_open_elsewhere()
    test_stop_needs_a_local_room()
    print(f"\nALL {_passed} RELAY-TOOL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
