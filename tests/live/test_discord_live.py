"""Live checks against real Discord - what the mock-server suites can't show.

Not part of the normal run (or CI): it posts to, reads from and PURGES a real
channel. Point it at a channel kept for testing - its name must contain "test",
or the suite refuses to start:

    DISCORDINATOR_LIVE_CHANNEL=<channel id> python tests/live/test_discord_live.py

It uses the bot token from your config (or DISCORD_BOT_TOKEN), never printed,
and a throwaway config and home folder, so your real read positions, handles
and sessions are untouched. The channel is purged at the start and the end.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHANNEL = os.environ.get("DISCORDINATOR_LIVE_CHANNEL", "").strip()
if not CHANNEL.isdigit():
    print("Set DISCORDINATOR_LIVE_CHANNEL to the id of a Discord channel kept for testing.")
    raise SystemExit(2)


def _token() -> str:
    if os.environ.get("DISCORD_BOT_TOKEN"):
        return os.environ["DISCORD_BOT_TOKEN"]
    path = Path(os.environ.get("DISCORDINATOR_CONFIG")
                or Path.home() / ".discordinator" / "config.json").expanduser()
    try:
        token = json.loads(path.read_text(encoding="utf-8")).get("token")
    except (OSError, ValueError):
        token = None
    if not token:
        print(f"No bot token: none in {path}, and DISCORD_BOT_TOKEN isn't set.")
        raise SystemExit(2)
    return str(token)


_TOKEN = _token()
_TMP = Path(tempfile.mkdtemp(prefix="discordinator-live-"))
os.chdir(_TMP)  # never the repo: its .env would be loaded
atexit.register(lambda: (os.chdir(tempfile.gettempdir()),
                         shutil.rmtree(_TMP, ignore_errors=True)))
for _k in ("DISCORDINATOR_CHAT_HANDLE", "DISCORDINATOR_SESSION_ID", "DISCORDINATOR_LABEL"):
    os.environ.pop(_k, None)
os.environ.update({
    "HOME": str(_TMP), "USERPROFILE": str(_TMP),
    "DISCORDINATOR_CONFIG": str(_TMP / "config.json"),
    "DISCORD_BOT_TOKEN": _TOKEN,
    "DISCORDINATOR_RELAY_TRANSPORT": "discord", "DISCORDINATOR_CHAT_TRANSPORT": "discord",
    "DISCORDINATOR_RELAY_CHANNEL": CHANNEL, "DISCORDINATOR_CHAT_CHANNEL": CHANNEL,
    "DISCORDINATOR_LABEL": "live-a",
    "DISCORDINATOR_ALLOW_SEND": "1", "DISCORDINATOR_ALLOW_RECEIVE": "1",
})
sys.path.insert(0, str(ROOT / "src"))

import discordinator.mcp_server as mcp  # noqa: E402
from discordinator import client_factory, config  # noqa: E402
from discordinator.discord_client import DiscordClient, DiscordError  # noqa: E402

_passed = 0
_notes: list[str] = []


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def other() -> DiscordClient:
    """Another session (or machine) on the same bot - not this server's posts."""
    return DiscordClient(_TOKEN)


def contents() -> list[str]:
    """The channel, oldest first."""
    return [m["content"] for m in reversed(mcp.read_messages(limit=100, newest_first=True,
                                                             ack=False))]


def wait_for(cond, what: str, timeout: float = 15.0) -> None:
    """Discord can take a moment to show a change to every read."""
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for: {what}")
        time.sleep(1.0)


def purge_all() -> dict:
    return mcp.purge_messages(dry_run=False)


def test_channel_is_for_testing() -> None:
    print("the channel is one kept for testing, and the bot can see it:")
    with other() as c:
        name = c._request("GET", f"/channels/{CHANNEL}").json().get("name", "")
        me = c.whoami()
    if "test" not in name.lower():
        print(f"Refusing: channel #{name} doesn't have 'test' in its name, and this suite "
              "purges the channel it runs in.")
        raise SystemExit(2)
    check(bool(me.get("id")), f"bot {me.get('username')} can reach #{name}")
    out = purge_all()
    check(not out.get("not_deleted"), f"start clean ({out.get('deleted', 0)} old message(s) purged)")


def test_relay() -> None:
    print("relay: send, read, inbox skips only my own:")
    out = mcp.send_message("hello from live-a")
    check(out.startswith("Sent 1 message"), "send_message posts")
    check(contents()[-1] == "[live-a] hello from live-a", "and it reads back, labelled")
    mcp.get_new_messages()  # position the inbox at now
    with other() as c:
        c.send_message(CHANNEL, "hello from live-b", label="live-b")
    mcp.send_message("me again")
    new = [m["content"] for m in mcp.get_new_messages()]
    check(new == ["[live-b] hello from live-b"], f"the inbox has the other's, not mine: {new}")
    check(mcp.get_new_messages() == [], "and nothing twice")


def test_long_message() -> None:
    print("a long message is split under the limit and loses nothing:")
    text = "\n".join(f"line {i}: " + "x" * (i % 70) for i in range(160))
    out = mcp.send_message(text)
    n = int(out.split()[1])
    check(n >= 3, f"{len(text)} characters go out as {n} messages")
    pieces = contents()[-n:]
    check(all(len(p) <= 2000 for p in pieces), "each within Discord's 2000")
    bodies = [p[len("[live-a] "):] for p in pieces]
    check("".join(bodies).replace("\n", "") == text.replace("\n", ""), "every character is there")


def test_burst_is_paced() -> None:
    print("a burst of sends is paced to Discord's limit, not refused:")
    t0 = time.monotonic()
    for i in range(8):
        mcp.send_message(f"burst {i}")
    took = time.monotonic() - t0
    check(contents()[-8:] == [f"[live-a] burst {i}" for i in range(8)], f"all 8 posted in order ({took:.1f}s)")


def test_delete_own() -> None:
    print("delete_messages: my latest post (every piece), never someone else's:")
    mcp.send_message("keep me")
    long = "y" * 4500
    mcp.send_message(long)
    before = len(contents())
    res = mcp.delete_messages()
    check(res["deleted"] == 3, f"the 3 pieces of my long post go ({res['deleted']})")
    wait_for(lambda: len(contents()) == before - 3, "the pieces to disappear")
    check(contents()[-1] == "[live-a] keep me", "the post before it stays")
    with other() as c:
        theirs = c.send_message(CHANNEL, "not yours", label="live-b")[0]["id"]
    try:
        mcp.delete_messages(message_ids=theirs)
        check(False, "someone else's message should be refused")
    except ValueError as e:
        check("Not your message" in str(e), "another session's message is refused")
    check(contents()[-1] == "[live-b] not yours", "and left alone")


def _await(who: str, t: float = 20.0, **kw) -> dict:
    return mcp.chat_await(chatter=who, timeout=t, poll=1.0, nudge_after=0, **kw)


def test_chat() -> None:
    print("chat over Discord: turns, a long reply, wrap and end:")
    for h in ("alpha", "beta"):
        mcp.chat_begin(chatter=h)
    mcp.chat_say(text="beta, are you there?", chatter="alpha", to="beta", wait=False)
    got = _await("beta")
    check(got["your_turn"] and got["from"] == "alpha" and got["text"] == "beta, are you there?",
          "beta gets alpha's turn")
    reply = "\n".join(f"point {i}: " + "z" * 60 for i in range(60))
    mcp.chat_say(text=reply, chatter="beta", wait=False)  # back to alpha
    got = _await("alpha")
    check(got["your_turn"] and got["text"] == reply,
          f"a {len(reply)}-character reply arrives whole and exact")
    mcp.chat_say(text="that's all, wrap?", chatter="alpha", status="wrap", wait=False)
    got = _await("beta")
    check(got["status"] == "wrap", "beta gets the wrap")
    mcp.chat_say(text="agreed", chatter="beta", status="end", wait=False)
    got = _await("alpha")
    check(got["ended"], "alpha sees the chat end")


def test_chat_set_aside_and_deleted() -> None:
    print("a turn set aside while waiting for someone else; deleted, it never arrives:")
    for h in ("gamma", "delta", "eps"):
        mcp.chat_begin(chatter=h)
    out = mcp.chat_say(text="a typo", chatter="gamma", to="delta", wait=False)
    got = _await("delta", 4, from_whom="eps")
    check(got["timed_out"], "delta, waiting for eps, sets gamma's turn aside")
    mcp.delete_messages(message_ids=out["message_ids"])
    got = _await("delta", 4)
    check(not got["your_turn"], "deleted, it isn't handed to delta")
    mcp.chat_say(text="the fixed one", chatter="gamma", to="delta", wait=False)
    got = _await("delta", 4, from_whom="eps")
    check(got["timed_out"], "the corrected turn is set aside too")
    got = _await("delta", 4)
    check(got["your_turn"] and got["text"] == "the fixed one", "and that one is handed over")
    mcp.chat_say(text="done", chatter="delta", status="end", wait=False)


def test_attachment() -> None:
    print("a file goes up and comes back byte for byte:")
    src = _TMP / "hello.txt"
    src.write_bytes(b"live attachment \x00\x01\x02 check\n")
    mcp.send_file(str(src), text="a file")
    msg = mcp.read_messages(limit=1, newest_first=True, ack=False)[0]
    atts = msg.get("attachments") or []
    check(len(atts) == 1 and atts[0]["filename"] == "hello.txt", "it's attached")
    out = mcp.download_attachment(atts[0]["url"], dest=str(_TMP / "back.txt"))
    check(Path(out["saved"]).read_bytes() == src.read_bytes(), "and downloads identical")


def test_purge() -> None:
    print("purge: preview, then everything - bulk where Discord allows, else one by one:")
    with other() as c:
        c.send_message(CHANNEL, "another session's", label="live-b")
    n = len(contents())
    out = mcp.purge_messages()
    check(out["dry_run"] and out["would_delete"] == n, f"the preview counts all {n}")
    bulk: list[int] = []
    real = DiscordClient.bulk_delete

    def counting(self, channel_id, ids):
        real(self, channel_id, ids)
        bulk.append(len(ids))

    DiscordClient.bulk_delete = counting
    try:
        out = mcp.purge_messages(dry_run=False)
    finally:
        DiscordClient.bulk_delete = real
    check(out["deleted"] == n and not out.get("not_deleted"), f"all {n} deleted")
    _notes.append("bulk delete worked (the bot has Manage Messages)" if bulk else
                  "bulk delete was refused - purge went one by one (no Manage Messages?)")
    wait_for(lambda: contents() == [], "the channel to be empty")
    check(True, "the channel is empty")
    for i in range(3):
        mcp.send_message(f"old-style {i}")
    real_age = client_factory.BULK_MAX_AGE
    client_factory.BULK_MAX_AGE = 0  # as if they were over 14 days old
    try:
        out = mcp.purge_messages(dry_run=False)
    finally:
        client_factory.BULK_MAX_AGE = real_age
    check(out["deleted"] == 3, "messages too old for bulk go one by one")


def main() -> int:
    try:
        test_channel_is_for_testing()
        test_relay()
        test_long_message()
        test_burst_is_paced()
        test_delete_own()
        test_chat()
        test_chat_set_aside_and_deleted()
        test_attachment()
        test_purge()
    finally:
        try:
            purge_all()
        except (DiscordError, config.ConfigError, OSError) as e:
            print(f"(cleanup purge failed: {e})")
    for n in _notes:
        print(f"note: {n}")
    print(f"\nALL {_passed} LIVE DISCORD CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
