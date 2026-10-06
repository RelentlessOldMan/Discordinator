"""Full-screen terminal viewer for local-mode chats (optional ``tui`` extra).

``discordinator tui [room]`` opens a live transcript with a floor/others
sidebar and an input box: type to interject as a human, ``/stop`` to end the
chat, ``/quit`` to leave the viewer. It's the ergonomic front-end over the same
primitives as ``watch``/``interject``/``stop`` — a live view and a human-turn
writer — so input and output no longer fight over one stdout.

Built on Textual (install with ``pip install -e .[tui]``). Local transport only.
"""

from __future__ import annotations

from typing import Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Input, RichLog, Static

from . import chat
from .discord_client import simplify_message
from .local_client import LocalClient

_PALETTE = ("cyan", "green", "magenta", "yellow", "blue", "red",
            "bright_green", "bright_magenta", "bright_yellow", "bright_blue")


def _color_for(name: str, cache: dict[str, str]) -> str:
    if name not in cache:
        cache[name] = _PALETTE[len(cache) % len(_PALETTE)]
    return cache[name]


class ChatTUI(App):
    CSS = """
    #body { height: 1fr; }
    #transcript { width: 3fr; border: round $primary; padding: 0 1; }
    #sidebar { width: 30; border: round $accent; padding: 0 1; }
    """

    BINDINGS = [("ctrl+c", "quit", "Quit")]

    def __init__(self, room: str, poll: float, limit: int, label: Optional[str],
                 retention_days: Optional[float] = None):
        super().__init__()
        self.room = room
        self._poll = poll
        self._limit = limit
        self._client = LocalClient(label=label, retention_days=retention_days)
        self._cursor: str = "0"
        self._colors: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            # min_width low so wrapping happens at the actual panel width
            # (RichLog defaults to 78, which would crop long turns instead).
            yield RichLog(id="transcript", wrap=True, markup=False, highlight=False, min_width=20)
            yield Static("", id="sidebar")
        yield Input(placeholder="type to interject as human · /stop to end · /quit to leave", id="input")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"discordinator · #{self.room}"
        self.sub_title = "local chat"
        log = self.query_one("#transcript", RichLog)
        msgs = self._read(limit=self._limit)
        for m in msgs:
            log.write(self._line(m))
        self._cursor = msgs[-1]["id"] if msgs else "0"
        self._update_sidebar()
        self.query_one("#input", Input).focus()
        self.set_interval(self._poll, self._tick)

    # -- data ---------------------------------------------------------------
    def _read(self, *, limit: int, after: Optional[str] = None) -> list[dict]:
        raw = (self._client.read_messages(self.room, limit=100, after=after)
               if after is not None else self._client.read_messages(self.room, limit=limit))
        msgs = [simplify_message(m) for m in raw]
        msgs.reverse()  # chronological
        return msgs

    def _tick(self) -> None:
        log = self.query_one("#transcript", RichLog)
        new = self._read(limit=self._limit, after=self._cursor or "0")
        for m in new:
            log.write(self._line(m))
        if new:
            self._cursor = new[-1]["id"]
        self._update_sidebar()

    # -- rendering ----------------------------------------------------------
    def _line(self, m: dict) -> Text:
        ts = (m.get("timestamp") or "")[11:19]
        t = Text("\n")  # blank line before each timestamped message
        t.append(f"{ts} ", style="dim")
        parsed = chat.parse(m["content"])
        if parsed:  # a chat turn
            handle, to = parsed["participant"], parsed["to"]
            status, body = parsed["status"], parsed["body"] or ""
            addr = f"{handle} ▸ {to}" if to else handle
            t.append(f"{addr:<16}", style=f"bold {_color_for(handle, self._colors)}")
            t.append(f" {status:<7} ", style="dim")
            t.append(body)
        else:  # relay / plain / human / nudge
            content = m["content"]
            if content.lstrip().startswith("⏳"):
                t.append(content, style="dim italic")
                return t
            if not m.get("bot"):  # human interjection / stop
                t.append(f"{'human':<16}", style="bold black on bright_white")
            else:
                label = m.get("author") or "?"
                t.append(f"{label:<16}", style=f"bold {_color_for(label, self._colors)}")
            t.append("         ")
            t.append(content)
        return t

    def _ended(self, st: dict) -> bool:
        if st.get("ended"):
            return True
        newest = self._client.read_messages(self.room, limit=1)
        if newest:
            nm = simplify_message(newest[0])
            return (not nm.get("bot")) and chat.is_human_stop(nm["content"])
        return False

    def _update_sidebar(self) -> None:
        side = self.query_one("#sidebar", Static)
        try:
            st = chat.compute_state(self._client, self.room, None)
        except Exception:
            side.update("(no state)")
            return
        if not st.get("participants"):
            side.update("[dim]no chat yet[/dim]")
            return
        lines: list[str] = []
        if self._ended(st):
            lines.append("[b red]● ENDED[/b red]\n")
        lines.append(f"[b]floor[/b]   {st.get('floor') or '-'}")
        waiting = st.get("waiting") or []
        lines.append("[b]others[/b] " + (", ".join(waiting) if waiting else "-"))
        hands = [r["from"] for r in (st.get("floor_requests") or [])]
        if hands:
            lines.append("[b]hands[/b]   " + ", ".join(hands))
        if st.get("suggest_next"):
            lines.append(f"[b]suggest[/b] [green]{st['suggest_next']}[/green]")
        lines.append("\n[dim]participants[/dim]")
        for p in st.get("participants", []):
            marker = " ◀ floor" if p == st.get("floor") else ""
            lines.append(f"  {p}{marker}")
        side.update("\n".join(lines))

    # -- input --------------------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.query_one("#input", Input).value = ""
        if not text:
            return
        low = text.lower()
        if low in ("/quit", "/exit", "/q"):
            self.exit()
            return
        if low in ("/stop", "/end", "/halt"):
            self._client.post_human(self.room, "[[STOP]]")
            self._tick()
            return
        if low == "/help":
            self.query_one("#transcript", RichLog).write(
                Text("type to interject · /stop end chat · /quit leave", style="dim italic"))
            return
        # plain text → a human interjection the agents pick up on next chat_await
        self._client.post_human(self.room, text)
        self._tick()


def run_tui(room: str, poll: float, limit: int, label: Optional[str],
            retention_days: Optional[float] = None) -> None:
    ChatTUI(room=room, poll=poll, limit=limit, label=label, retention_days=retention_days).run()
