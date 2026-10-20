"""Dump what a stuck local chat room looks like to discordinator (read-only).

Run on the machine the room lives on, with the same Python that runs the MCP
server:  python tools/chat_diag.py <room> > chat-diag.txt
It prints the room's messages (tag and first 100 characters of each), the
chat state kept for the room, the session events about it, and the state
compute_state works out for every name that has posted there.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discordinator import __version__, chat, config  # noqa: E402
from discordinator.discord_client import simplify_message  # noqa: E402
from discordinator.local_client import LocalClient, local_dir  # noqa: E402


def main() -> int:
    room = sys.argv[1] if len(sys.argv) > 1 else "chat"
    c = LocalClient()
    cid = room
    print(f"discordinator {__version__}, room {room!r} ({cid}), dir {local_dir()}")
    print("\n== messages (oldest first) ==")
    msgs = list(reversed(c.read_messages(cid, limit=100)))
    names = []
    for m in msgs:
        s = simplify_message(m)
        text = (s.get("content") or "").replace("\n", " ")
        print(f"{s.get('timestamp', '')[:19]} {s.get('id')} bot={s.get('bot')} {text[:100]}")
        p = chat.parse_msg(s)
        who = (p or {}).get("participant")
        if who and who not in names:
            names.append(who)
    print("\n== chat state kept for the room ==")
    st = config.load_state()
    print(json.dumps((st.get("chat") or {}).get(cid), indent=1, default=str))
    print("\n== session events about the room (last 60) ==")
    ev = config.config_path().parent / "events.jsonl"
    if ev.exists():
        lines = [x for x in ev.read_text(encoding="utf-8", errors="replace").splitlines()
                 if room in x or cid in x]
        for x in lines[-60:]:
            print(x[:300])
    print("\n== compute_state per name ==")
    for who in names:
        try:
            s = chat.compute_state(c, cid, who)
        except Exception as e:  # noqa: BLE001
            print(f"{who}: error {e!r}")
            continue
        keep = {k: s.get(k) for k in ("your_turn", "ended", "participants", "floor",
                                      "pending_turn", "progress")}
        keep["_owed"] = [{k: t.get(k) for k in ("from", "to", "status", "id", "ts")}
                         for t in s.get("_owed") or []]
        keep["cursor"] = chat.get_cursor(cid, who)
        keep["reply_to"] = chat.get_reply_to(cid, who)
        print(who, json.dumps(keep, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
