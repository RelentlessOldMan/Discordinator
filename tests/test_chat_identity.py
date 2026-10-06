"""Chat identity + scoping: who counts as a participant.

Regression for a real muddle where the state footer showed
``waiting: Convex, CodeCarver, convex`` — the same session under two spellings,
a project under two names, and handles left over from an earlier chat. Covers:
case-insensitive handles, scoping state to the current chat (after the last
end / human stop), aging out participants silent for 30+ minutes, and the
per-project fixed handle (DISCORDINATOR_CHAT_HANDLE) used when `chatter` is
omitted.
Run:  python tests/test_chat_identity.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="discordinator-ident-"))
os.environ["DISCORDINATOR_CONFIG"] = str(_TMP / "config.json")
os.environ["DISCORDINATOR_RELAY_TRANSPORT"] = "local"
os.environ["DISCORDINATOR_CHAT_TRANSPORT"] = "local"
for _k in ("DISCORD_BOT_TOKEN", "DISCORDINATOR_LABEL", "DISCORDINATOR_CHAT_HANDLE",
           "DISCORDINATOR_CHAT_CHANNEL", "DISCORDINATOR_RELAY_CHANNEL"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import discordinator.mcp_server as mcp  # noqa: E402
from discordinator import chat, cli  # noqa: E402
from discordinator.config import ConfigError  # noqa: E402
from discordinator.local_client import LocalClient  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def _say(c: LocalClient, room: str, who: str, status: str, text: str, to: str = None) -> None:
    chat.send_chat(c, room, who, status, text, to=to)


def _age(c: LocalClient, room: str, minutes_ago: list[float]) -> None:
    """Rewrite the room's timestamps: record i gets ``minutes_ago[i]``."""
    path = c._room_path(room)
    recs = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
    now = datetime.now(timezone.utc)
    for rec, mins in zip(recs, minutes_ago):
        rec["timestamp"] = (now - timedelta(minutes=mins)).isoformat()
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")


def test_case_insensitive_handles() -> None:
    print("handles differing only in case are ONE participant:")
    c = LocalClient()
    room = "case"
    _say(c, room, "Convex", "over", "hi")
    _say(c, room, "Carver", "over", "hello")
    _say(c, room, "convex", "over", "me again, lowercase")
    st = chat.compute_state(c, room, "CARVER")
    check(st["participants"] == ["Convex", "Carver"],
          f"Convex/convex merged, first spelling kept: {st['participants']}")
    check(st["multiparty"] is False, "still a 2-party chat")
    check(st["floor"] == "Carver", "floor resolves to the other party")
    check(st["your_turn"] is True, "your_turn matches across case (CARVER == Carver)")
    st2 = chat.compute_state(c, room, "CONVEX")
    check(st2["your_turn"] is False, "own turn under another case is not 'owed to me'")


def test_await_skips_own_message_any_case() -> None:
    print("await ignores my own message even if posted in a different case:")
    c = LocalClient()
    room = "selfcase"
    _say(c, room, "Opener", "over", "start")
    chat.reset(room, "Mixer", c.read_messages(room, limit=1)[0]["id"], 20)
    _say(c, room, "MIXER", "over", "my own turn, shouted")
    res = chat.await_turn(c, room, "mixer", timeout=0.3, poll=0.05, nudge_after=0)
    check(res["timed_out"] is True, "own message (other case) did not wake me")
    check(chat.get_cursor(room, "MiXeR") == chat.get_cursor(room, "mixer"),
          "cursor slot is shared across case")


def test_scoped_to_current_chat() -> None:
    print("state covers only the CURRENT chat (after the last end):")
    c = LocalClient()
    room = "scoped"
    _say(c, room, "Old1", "over", "previous chat")
    _say(c, room, "Old2", "end", "done")
    st = chat.compute_state(c, room)
    check(st["ended"] is True and "Old1" in st["participants"],
          "a just-ended chat still reports ended with its participants")
    _say(c, room, "CodeCarver", "over", "new chat", to="Convex")
    _say(c, room, "Convex", "over", "hi back", to="CodeCarver")
    st = chat.compute_state(c, room)
    check(st["participants"] == ["CodeCarver", "Convex"],
          f"previous chat's handles gone: {st['participants']}")
    check(st["ended"] is False and st["multiparty"] is False, "new chat active, 2-party")


def test_human_stop_is_a_boundary() -> None:
    print("a human stop followed by a new chat also starts fresh:")
    c = LocalClient()
    room = "hstop"
    _say(c, room, "Ghost", "over", "old")
    c.post_human(room, "stop")
    _say(c, room, "A", "over", "fresh")
    st = chat.compute_state(c, room, "B")
    check(st["participants"] == ["A", "B"], f"Ghost scoped out: {st['participants']}")


def test_silent_participants_age_out() -> None:
    print("participants silent 30+ min drop out (floor holder kept):")
    c = LocalClient()
    room = "aging"
    _say(c, room, "Drifter", "over", "long ago")          # 90 min ago
    _say(c, room, "A", "over", "hey B", to="B")            # 50 min ago, floor -> B
    _say(c, room, "C", "ask", "can I jump in?")            # 1 min ago
    _age(c, room, [90, 50, 1])
    st = chat.compute_state(c, room)
    check("Drifter" not in st["participants"], "Drifter (90m silent) aged out")
    check({"A", "B", "C"} <= set(st["participants"]),
          "both ends of the owed turn kept even though 50m old")
    check(st["floor"] == "B", "floor still B")
    check(st["waiting"] == [p for p in st["waiting"] if p != "Drifter"]
          and "Drifter" not in st["waiting"], "aged-out handle not in the rotation")
    check(st["floor_requests"] == [{"from": "C"}], "fresh hand-raise kept")


def test_slow_reply_keeps_other_party() -> None:
    print("a 2-party reply that took 30+ min still owes the turn to the other side:")
    c = LocalClient()
    room = "slow"
    _say(c, room, "B", "over", "question")
    _say(c, room, "A", "over", "answer after a long think")
    _age(c, room, [35, 0])
    st = chat.compute_state(c, room)
    check(st["participants"] == ["B", "A"], f"B kept despite 35m: {st['participants']}")
    check(st["floor"] == "B", "floor goes to B (regression: was None)")


def test_unparseable_timestamps_tolerated() -> None:
    print("records with unusable timestamps never break state or age anyone out:")
    c = LocalClient()
    room = "badts"
    _say(c, room, "A", "over", "one")
    _say(c, room, "B", "over", "two")
    path = c._room_path(room)
    recs = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
    for r in recs:
        r["timestamp"] = "garbage"
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    st = chat.compute_state(c, room)
    check(st["participants"] == ["A", "B"] and st["floor"] == "A",
          "no timestamps -> nobody aged out, floor still derived")


def test_from_whom_case_insensitive() -> None:
    print("from_whom matches the sender regardless of case:")
    c = LocalClient()
    room = "fromwhom"
    _say(c, room, "Zed", "over", "start")
    chat.reset(room, "me", c.read_messages(room, limit=1)[0]["id"], 20)
    _say(c, room, "Peer", "over", "for you")
    res = chat.await_turn(c, room, "me", timeout=1, poll=0.05, nudge_after=0, from_whom="PEER")
    check(res["from"] == "Peer" and res["your_turn"], "from_whom='PEER' woke on Peer's turn")


def test_configured_handle() -> None:
    print("chatter defaults to the project's fixed DISCORDINATOR_CHAT_HANDLE:")
    room = "handled"
    try:
        mcp.chat_begin(channel=room)
        raise AssertionError("no handle anywhere should be an error")
    except ConfigError as exc:
        check("DISCORDINATOR_CHAT_HANDLE" in str(exc), "no handle -> error naming the fix")
    os.environ["DISCORDINATOR_CHAT_HANDLE"] = "CodeCarver"
    try:
        out = mcp.chat_begin(channel=room)
        check(out["chatter"] == "CodeCarver", "chat_begin uses the configured handle")
        mcp.chat_say(text="hello", status="over", channel=room)
        last = LocalClient().read_messages(room, limit=1)[0]["content"]
        check(last.startswith("[CodeCarver|over]"), f"chat_say tagged with it: {last[:30]}")
        out = mcp.chat_begin(chatter="ui", channel=room)
        check(out["chatter"] == "CodeCarver/ui", "an explicit chatter becomes a role suffix")
        st = mcp.chat_status(channel=room)
        check("your_turn" in st, "chat_status computes your_turn from the configured handle")
        res = mcp.chat_await(channel=room, timeout=0.2, poll=0.05, nudge_after=0)
        check(res["timed_out"] is True, "chat_await works with the configured handle")
    finally:
        os.environ.pop("DISCORDINATOR_CHAT_HANDLE")


def test_footer_label() -> None:
    print("watch --state labels the rotation 'others', not 'waiting':")
    c = LocalClient()
    room = "footer"
    _say(c, room, "A", "over", "to B", to="B")
    _say(c, room, "C", "ask", "me too")
    footer = cli._state_footer(c, room)
    check("others:" in footer and "waiting" not in footer, f"footer: {footer}")


def main() -> int:
    test_case_insensitive_handles()
    test_await_skips_own_message_any_case()
    test_scoped_to_current_chat()
    test_human_stop_is_a_boundary()
    test_silent_participants_age_out()
    test_slow_reply_keeps_other_party()
    test_unparseable_timestamps_tolerated()
    test_from_whom_case_insensitive()
    test_configured_handle()
    test_footer_label()
    print(f"\nALL {_passed} CHAT-IDENTITY CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
