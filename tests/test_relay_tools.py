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


def test_mcp_purge_takes_everything() -> None:
    print("purge_messages purges the whole channel - every author, any age - after a dry run:")
    r = "purge-mcp"
    LocalClient("me").send_message(r, "mine")
    LocalClient("other").send_message(r, "another machine's")
    chat.send_chat(LocalClient("me"), r, "A", "over", "a chat turn", to="B")
    LocalClient("me").post_human(r, "an interjection")
    for n in range(130):
        LocalClient("other").send_message(r, f"n{n}")
    out = mcp.purge_messages(channel=r)
    check(out["dry_run"] and out["would_delete"] == 134, f"the dry run counts all of them: {out['would_delete']}")
    check(len(LocalClient("p").read_messages(r, limit=100)) == 100, "and deletes nothing")
    out = mcp.purge_messages(channel=r, dry_run=False)
    check(out["deleted"] == 134 and _contents(r) == [] and "not_deleted" not in out,
          "then every one goes, past the first 100 too")
    LocalClient("me").send_message(r, "old")
    LocalClient("me").send_message(r, "new")
    _age(r, {"old"}, 10)
    out = mcp.purge_messages(channel=r, dry_run=False, older_than_days=7)
    check(_contents(r) == ["new"], "older_than_days narrows it when asked")


def test_discord_purge_without_manage_messages() -> None:
    print("on Discord without Manage Messages: the bot's own go, people's are reported once:")
    import httpx
    from discordinator import client_factory, discord_client as dc
    from discordinator.discord_client import API_BASE, DiscordClient
    msgs = [{"id": str(10 - i), "content": f"m{i}", "timestamp": "2026-01-01T00:00:00+00:00",
             "author": {"id": "bot" if i % 2 == 0 else "person", "bot": i % 2 == 0}}
            for i in range(6)]
    deletes: list = []

    def handler(req):
        if req.url.path.endswith("/users/@me"):
            return httpx.Response(200, json={"id": "bot"})
        if req.method == "GET":
            return httpx.Response(200, json=msgs if "before" not in req.url.params else [])
        mid = req.url.path.rsplit("/", 1)[-1]
        deletes.append(mid)
        author = next(m["author"]["id"] for m in msgs if m["id"] == mid)
        return httpx.Response(204) if author == "bot" else httpx.Response(403, json={})

    real_sleep = dc.time.sleep
    dc.time.sleep = lambda *_a, **_k: None
    client = DiscordClient(token="test-token")
    client._client = httpx.Client(base_url=API_BASE, transport=httpx.MockTransport(handler))
    try:
        targets = client_factory.purge_targets(client, "chan")
        check(len(targets) == 6, "every message is a target, the people's too")
        deleted, problems = client_factory.purge(client, "chan", targets)
        check(deleted == 3, f"the bot's own 3 were deleted ({deleted})")
        check(len(problems) == 1 and "Manage Messages" in problems[0] and "3 message" in problems[0],
              f"one note says why 3 were left: {problems}")
        check(len(deletes) == 4, f"only one refused delete was tried, not three ({len(deletes)})")
    finally:
        client.close()
        dc.time.sleep = real_sleep


def test_delete_own_messages() -> None:
    print("delete_messages: a session deletes its own posts (and only its own), then reposts:")
    from discordinator import handles
    handles._SESSION_KEY = "id:deleter"
    try:
        r = "del-own"
        out = mcp.send_message("tpyo here", channel=r)
        check("ids:" in out, f"send_message reports the ids: {out}")
        LocalClient("x").send_message(r, "someone else's", label="them")
        LocalClient("x").post_human(r, "a human's")
        res = mcp.delete_messages(channel=r)
        check(res["deleted"] == 1 and _contents(r) == ["[them] someone else's", "a human's"],
              "no ids: my latest post goes, nothing else")
        others = [m["id"] for m in LocalClient("p").read_messages(r, limit=10)]
        try:
            mcp.delete_messages(message_ids=others[0], channel=r)
            check(False, "someone else's message should be refused")
        except ValueError as e:
            check("Not your message" in str(e) and "purge" in str(e) and len(_contents(r)) == 2,
                  "another's message (or a human's) is refused, nothing deleted")
        mcp.send_message("first", channel=r)
        mid = mcp.send_message("second", channel=r).split("ids: ")[1].rstrip(").")
        mcp._my_posts.clear()  # the session's server restarted
        res = mcp.delete_messages(message_ids=[mid])
        check(res["deleted"] == 1 and "[me] first" in _contents(r) and "[me] second" not in _contents(r),
              "after a restart it can still delete a post it names")
        try:
            mcp.delete_messages(message_ids=[mid])
            check(False, "deleting it twice should be refused")
        except ValueError:
            check(True, "once deleted it's no longer listed as its own")

        rc = "del-chat"
        mcp.chat_begin(chatter="A", channel=rc)
        mcp.chat_begin(chatter="B", channel=rc)
        out = mcp.chat_say(text="x" * 4500, chatter="A", channel=rc, to="B", wait=False)
        check(len(out["message_ids"]) == 3, "chat_say returns the ids of all its pieces")
        res = mcp.delete_messages(message_ids=out["message_ids"][1])
        check(res["deleted"] == 3 and "Nobody had replied" in res["note"],
              "naming one piece deletes the whole turn; nobody had answered it")
        b = mcp.chat_await(chatter="B", channel=rc, timeout=0.3, poll=0.02, nudge_after=0)
        check(b["timed_out"] and not b["your_turn"], "the deleted turn never reaches B")
        out = mcp.chat_say(text="the right question", chatter="A", channel=rc, to="B", wait=False)
        b = mcp.chat_await(chatter="B", channel=rc, timeout=1, poll=0.02, nudge_after=0)
        check(b["text"] == "the right question", "the reposted turn does")
        mcp.chat_say(text="an answer", chatter="B", channel=rc, wait=False)
        res = mcp.delete_messages(message_ids=out["message_ids"])
        check("B posted since" in res["note"], "deleting after a reply says B may have read it")
    finally:
        handles._SESSION_KEY = None


def test_deleted_turn_never_delivered() -> None:
    print("a turn deleted (or purged) after a session set it aside never reaches it:")
    for how in ("delete", "purge", "keep"):
        r = f"held-{how}"
        a, b, c = (f"{how}-{x}" for x in "abc")  # names no earlier test claimed
        for h in (a, b, c):
            mcp.chat_begin(chatter=h, channel=r)
        out = mcp.chat_say(text="SECRET typo", chatter=a, channel=r, to=b, wait=False)
        got = mcp.chat_await(chatter=b, channel=r, timeout=0.3, poll=0.02, nudge_after=0,
                             from_whom=c)
        check(got["timed_out"] and "handle_note" not in got,
              f"{how}: {b}, waiting for {c}, sets {a}'s turn aside")
        if how == "delete":
            mcp.delete_messages(message_ids=out["message_ids"])
        elif how == "purge":
            mcp.purge_messages(channel=r, dry_run=False)
        got = mcp.chat_await(chatter=b, channel=r, timeout=0.3, poll=0.02, nudge_after=0)
        if how == "keep":
            check(got["your_turn"] and got["text"] == "SECRET typo",
                  "a turn set aside and not deleted still is handed over")
        else:
            check(not got["your_turn"] and "SECRET" not in (got.get("text") or ""),
                  f"{how}: {b}'s next wait doesn't hand it over")


def test_partly_failed_send_can_be_deleted() -> None:
    print("a long send that fails partway: the pieces that went out are still deletable:")
    from discordinator import handles
    handles._SESSION_KEY = "id:partial"
    real = LocalClient._append
    n = [0]

    def flaky(self, *a, **k):
        n[0] += 1
        if n[0] == 3:
            raise OSError("disk hiccup")
        return real(self, *a, **k)

    LocalClient._append = flaky
    try:
        mcp.send_message("y" * 4500, channel="part-sent")
        check(False, "the send should fail")
    except OSError as e:
        check("2 message(s) of it were already posted" in str(e) and "delete_messages" in str(e),
              f"the error says what went out and how to remove it: {e}")
    finally:
        LocalClient._append = real
    check(len(_contents("part-sent")) == 2, "two pieces are in the room")
    res = mcp.delete_messages(channel="part-sent")
    check(res["deleted"] == 2 and _contents("part-sent") == [], "and delete_messages removes both")


def test_purge_reaches_the_chat_room() -> None:
    print("with relay and chat on different transports, purge finds the chat room:")
    from discordinator.client_factory import purge_room
    cfg = {"relay_transport": "discord", "chat_transport": "local",
           "channels": {"ops": "123"}, "default_channel": "ops", "chat_channel": "lounge"}
    check(purge_room(cfg, None) == ("relay", "123"), "no channel: the relay default")
    check(purge_room(cfg, "ops") == ("relay", "123"), "a relay channel: relay")
    check(purge_room(cfg, "lounge") == ("chat", "lounge"), "the chat room: chat (local)")
    check(purge_room(cfg, "scratch") == ("chat", "scratch"),
          "a name only the local chat transport knows: chat")
    flip = {**cfg, "relay_transport": "local", "chat_transport": "discord",
            "channels": {"lounge": "456"}}
    check(purge_room(flip, "lounge") == ("chat", "456"), "reversed: the chat room on Discord")
    check(purge_room(flip, "notes") == ("relay", "notes"), "and other names are local relay rooms")
    same = {**cfg, "relay_transport": "local", "chat_transport": "local"}
    check(purge_room(same, "lounge") == ("relay", "lounge"), "one transport: just the room")
    os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "discord"
    os.environ["DISCORD_BOT_TOKEN"] = "unused"
    try:
        chat.send_chat(LocalClient(), "mixed-chat", "A", "over", "hello", to="B")
        out = mcp.purge_messages(channel="mixed-chat", dry_run=False)
        check(out["deleted"] == 1 and _contents("mixed-chat") == [],
              "the MCP tool purges the local chat room, never touching Discord")
    finally:
        os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
        os.environ.pop("DISCORD_BOT_TOKEN", None)


def test_discord_purge_bulk() -> None:
    print("on Discord, recent messages go 100 at a time; old ones (and leftovers) one by one:")
    import httpx
    from discordinator import client_factory, discord_client as dc
    from discordinator.discord_client import API_BASE, DiscordClient
    now = datetime.now(timezone.utc)
    msgs = [{"id": str(1000 - i), "content": f"m{i}",
             "timestamp": (now - timedelta(days=1 if i < 201 else 20)).isoformat(),
             "author": {"id": "bot" if i % 2 else "person"}} for i in range(210)]
    calls: list = []

    def handler(req):
        if req.url.path.endswith("/users/@me"):
            return httpx.Response(200, json={"id": "bot"})
        if req.method == "GET":
            before = req.url.params.get("before")
            older = [m for m in msgs if before is None or int(m["id"]) < int(before)]
            return httpx.Response(200, json=older[:int(req.url.params.get("limit", 50))])
        if req.url.path.endswith("/bulk-delete"):
            ids = json.loads(req.content)["messages"]
            calls.append(("bulk", len(ids)))
            return httpx.Response(204)
        calls.append(("one", req.url.path.rsplit("/", 1)[-1]))
        return httpx.Response(204)

    real_sleep = dc.time.sleep
    dc.time.sleep = lambda *_a, **_k: None
    client = DiscordClient(token="test-token")
    client._client = httpx.Client(base_url=API_BASE, transport=httpx.MockTransport(handler))
    try:
        targets = client_factory.purge_targets(client, "chan")
        deleted, problems = client_factory.purge(client, "chan", targets)
        bulk = [n for kind, n in calls if kind == "bulk"]
        ones = [x for kind, x in calls if kind == "one"]
        check(deleted == 210 and not problems, f"all 210 deleted ({deleted}, {problems})")
        check(bulk == [100, 100], f"200 of the 201 recent ones in two bulk deletes ({bulk})")
        check(len(ones) == 10, f"the odd recent one and the 9 old ones go one by one ({len(ones)})")
    finally:
        client.close()
        dc.time.sleep = real_sleep


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
    LocalClient("me").post_human(r, "a human")
    _age(r, {"mine old", "theirs old"}, 10)
    rc, out = _run(["purge", "-c", r, "--dry-run"])
    check(rc == 0 and "would delete all 4" in out and len(_contents(r)) == 4, "--dry-run previews only")
    rc, out = _run(["purge", "-c", r])
    check(rc == 1 and "--yes" in out and len(_contents(r)) == 4,
          "without a terminal to confirm on, it refuses and deletes nothing")
    rc, out = _run(["purge", "-c", r, "--older-than", "7d", "--yes"])
    check(rc == 0 and _contents(r) == ["mine new", "a human"], "--older-than takes only the old ones, anyone's")
    rc, out = _run(["purge", "-c", r, "--yes"])
    check(rc == 0 and _contents(r) == [], "plain purge takes everything left")
    rc, out = _run(["purge", "-c", r, "--all", "--yes"])
    check(rc == 0 and "nothing to delete" in out, "--all is still accepted")


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
    test_mcp_purge_takes_everything()
    test_discord_purge_without_manage_messages()
    test_delete_own_messages()
    test_deleted_turn_never_delivered()
    test_partly_failed_send_can_be_deleted()
    test_purge_reaches_the_chat_room()
    test_discord_purge_bulk()
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
