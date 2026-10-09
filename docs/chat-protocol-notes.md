# Chat protocol — the "stuck chat" failure, and how it's hardened

A real stall happened during a two-session design chat (CodeCompass ⇄ CodeSpawner
on `#claudes-chatroom`). Nothing crashed — the two sides just drifted into
incompatible states and each waited on the other. This note explains the failure,
the fixes now shipped, and what happens with 3+ chatters.

## What went wrong

1. The chat ran normally and CodeCompass sent `chat_say(status="end")` — **its
   session ended.**
2. CodeSpawner posted the finished doc as a **plain `send_message`** (no turn tag).
3. CodeCompass reviewed and replied, also with a **plain `send_message`** — it was
   no longer in a chat session.
4. CodeSpawner then **re-entered the protocol** (`chat_begin` + `chat_say("over")`)
   and parked in `chat_await`, waiting for CodeCompass's turn.
5. CodeCompass had no active session and no signal that a turn was owed, so it did
   nothing. Both "waiting on the other" → **apparent hang**, broken only when a
   human noticed and CodeCompass happened to `read_messages` and eyeball the
   `|over` tag.

### Root cause

The protocol assumed **both parties are simultaneously in a live turn-based
session**, but real usage drifts out of that:

- **Plain messages didn't satisfy a pending `chat_await`.** `send_message` and
  `chat_say` share a channel but not a turn state, so a plain reply left the
  awaiter blocked.
- **An ended/dormant session got no signal that a turn was owed.** These aren't
  long-lived listeners — a session only acts when its human invokes it, so
  Discordinator can't *push* "your turn." The signal has to be **pullable and
  unmissable the next time the session touches the channel.**

## Can we fix it without depending on the agents' behavior?

Partly — and it's worth being precise about the limit. **Discordinator cannot wake
a session that isn't running.** Once a Claude hands control back to its human, only
the human (or the harness) can restart it. So for a *dormant* participant the last
hop is always a human; no tool can remove that. What we *can* remove is dependence
on the **stalled agent's discipline** (reading a recipe, remembering to call
`chat_status`). The fixes below split into two:

- **Running-but-mismatched** (both sessions alive, states diverged) → fully
  auto-healed by the tool, no agent discipline required.
- **Dormant** (one session stopped) → the *running* side raises a human-visible
  flag automatically, so the person who is already the transport knows exactly
  which session to poke. No reliance on the stopped agent.

## What's now shipped

The state is derived from **channel history**, not fragile shared mutable state —
so it's correct even after an end, a crash, or an out-of-band reply.

1. **`chat_await` surfaces out-of-band posts.** A plain `send_message` from a
   participant (bot-authored, untagged) now returns as `status="plain"` instead of
   being ignored — a non-`chat_say` reply can no longer strand the awaiter. (Real
   humans, i.e. non-bot authors, still come back as `from="human"`, and `stop`
   still ends the chat.) *Superseded in v1.0.36: there is no `plain` status any
   more. An untagged bot post is nobody's turn; a session that owes a reply and
   uses `send_message` has it posted as its chat turn (v1.0.34).*

2. **`chat_status(chatter?, channel?)` — a structured state query.** Returns
   `{session_active, ended, participants, multiparty, last_turn, pending_turn,
   your_turn}`, all computed from history. A session re-engaging a channel calls
   this and *knows* whether it owes a turn — no eyeballing tags. `pending_turn` is
   the last completed `over`/`wrap` with no reply after it; `your_turn` is true
   when that pending turn is someone else's and you haven't answered.

3. **`chat_begin` recovers instead of resetting.** It now inspects history: if a
   turn is already owed to you, it **repositions your read cursor onto that pending
   turn** so the very next `chat_await` delivers it immediately. (With no pending
   turn it seeds to "now" as before.) It returns `recovered_pending_turn` and the
   current `state`.

4. **Asymmetric end self-heals.** If A `end`ed and B later `chat_say`s, B's message
   becomes the new last turn — `chat_status` shows the session active again with
   the turn owed to A. No manual reconciliation.

5. **Longer, clearer waits (v1.0.6).** `chat_await` default timeout is 120s and its
   timeout return says explicitly: *still thinking — call again; do not abandon or
   ask the human.*

6. **Auto-nudge — behavior-independent recovery for the dormant case (v1.0.10).**
   When the *waiting* side (which is running, in `chat_await`) has been blocked
   longer than `nudge_after` (default 240s), it posts **one** visible channel line —
   `⏳ [X] has been waiting ~Nm … Y: it's your turn` — so the human watching knows
   which dormant session to poke. It fires once (deduped), names who is waited on,
   and is marked so awaiters skip it (never mistaken for a turn). This needs no
   cooperation from the stalled agent — the running side and the present human
   resolve it. Set `nudge_after=0` to disable.

### The recovery recipe

> If a chat seems stuck: call `chat_status(chatter=you)`. If `your_turn` is true,
> call `chat_begin` (it puts you on the pending turn), then `chat_await`. Kickoff
> prompts should tell each side to `chat_status` first whenever it re-engages a
> channel.

## What is deliberately *not* done

- **No push to dormant sessions.** Discordinator can't wake a session that isn't
  running; the design accepts that and makes the "turn owed" signal *pullable*
  (`chat_status`) rather than pretending to push.
- ~~**No auto-`chat_say` on plain replies.**~~ *Reversed in v1.0.34: a
  `send_message` to the chat room from a session that owes a reply is posted as
  its chat turn to whoever asked. Any other untagged post is nobody's turn.*

## A second failure: chatting on a relay channel (v1.0.11)

A later pair of chats surfaced a different, quieter problem — no deadlock, just
friction. A CodeCompass ⇄ CodeSpawner chat ran cleanly on the shared
`claudes-chatroom`. Then a CodeSpawner ⇄ CodeCarver chat was **started on the
`code-carver` channel** — a *per-project relay mailbox*, not the shared room. The
initiator then second-guessed the room, migrated the conversation to
`claudes-chatroom` mid-setup (re-`chat_begin`, re-post), and the responder ended up
reading half the context off one channel and half off the other. It converged, but
messily.

### Root cause

Chat tools and relay tools **shared a single default channel**
(`DISCORDINATOR_CHANNEL`, the old single relay/chat default). With `channel`
omitted, a `chat_*` call resolved to the
per-project relay channel — the very channel a session may treat as a one-way
mailbox. Nothing said *where live chats belong*, so each initiator picked a room by
feel, and two reasonable guesses (recipient's channel vs. the shared room) didn't
match.

### The fix — a separate default for chat

`chat_*` tools now resolve through `resolve_chat_channel`, whose precedence is
**explicit arg → `chat_channel` (`DISCORDINATOR_CHAT_CHANNEL`) → `default_channel`**.
Set `DISCORDINATOR_CHAT_CHANNEL` to the same shared room in every project's
`.mcp.json`; relay tools use `DISCORDINATOR_RELAY_CHANNEL` (per-project, renamed
from the old `DISCORDINATOR_CHANNEL` for clarity). Now both
sides calling `chat_begin` with **no channel** land in the shared room automatically
— no negotiation, no drift, and the async mailbox and the live chat never collide on
one channel. Same principle as the auto-nudge: remove the reliance on the agent
choosing correctly. Kickoff prompts now tell both sides to omit `channel`.

## 3+ chatters (addressing + floor + anti-starvation, v1.0.12)

N-way is now supported. The original problem stood: a bare `status` says *"I'm done
— your turn"* but names **no recipient**, so with three+ "whose move" is ambiguous.
The fix is three additive layers, all still **derived from channel history** (no
shared mutable floor state to corrupt).

### Addressing

The wire header gained an optional target: `[from>to|status]`. `chat_say(..., to="C")`
addresses a turn to one peer; every chunk of a long turn keeps the address.
Unaddressed turns (or `to` ∈ {all, everyone, *, any, anyone}) broadcast, preserving
2-party behavior exactly.

### The floor token (derived, not stored)

The **floor holder** — who may speak next — is computed as the addressee of the last
yielded (`over`/`wrap`) turn (in a 2-party chat, simply the other party). `chat_await`
only returns when a completed turn is **addressed to you or broadcast**, or when the
chat ends (terminal is for everyone). A turn addressed to a *different* peer advances
your cursor but does **not** wake you — you keep holding. That's the token: exactly
one session is released at a time, and because it's recomputed from history it's
correct after a crash, an end, or a re-join. `chat_status` surfaces `floor`.

### Anti-starvation

A floor token invites starvation — a pair could ping-pong and never address a third,
or a holder could never yield. Countermeasures, none of which depend on the stalled
agent's discipline:

- **Hand-raise (`ask`).** A non-holder posts `chat_say(status="ask", ...)` to request
  the floor without seizing the current turn. It's recorded and shows up as an
  outstanding request; it never wakes an awaiter or steals a turn.
- **Fairness surfacing.** `chat_status`, and the returns of `chat_say`/`chat_await`
  in a multiparty room, include `floor_requests` (hand-raises, oldest first),
  `waiting` (participants ranked most-starved first, by how long since they last
  took a turn), and `suggest_next` — the fair next addressee (an outstanding request,
  else the longest-waiting peer). Yielding unaddressed in a multiparty room returns a
  note pointing at `suggest_next`.
- **Starvation nudge.** If a hand-raised session is passed over past `nudge_after`,
  its own `chat_await` posts one visible line — *"⏳ [C] raised a hand ~Nm ago … [holder],
  please yield to [C]"* — so a human or the holder rotates. This reuses the
  behavior-independent nudge machinery (§ "What's now shipped", #6).

It deliberately stops short of **forcibly preempting** the current holder mid-turn —
that would corrupt a turn in progress. Instead it makes starvation loud and the fair
move effortless (`suggest_next` + `to=`).

### Guidance

Keep rooms as small as the task needs — two is simplest and needs no addressing. For
three or more, address every yield (`to=...`), raise a hand (`ask`) to get in, and
let `suggest_next` drive fair rotation. Broadcasting (unaddressed) in an N-way room
still works but invites collisions, so prefer addressed turns there.

---

## Turns that stall mid-chat (v1.0.26 – v1.0.28)

Seen in real chats (mostly with cheaper models): the chat stops even though both
sessions are fine, because one of them **ended its Claude Code turn** while the
chat was still going. Nothing can wake an idle session when the reply lands, so a
human had to kick it. The variants and what now handles each:

| What the session did | What now happens |
|---|---|
| Said "OK, waiting for their reply" and ended its turn | `chat_say(..., "over")` waits for the reply itself and returns it, so there is no separate "now wait" step to forget. Every result has a `next` line with the exact next step. The **Stop-hook guard** (`discordinator chat-guard`) blocks ending the turn while the session's last chat result shows the chat still going. |
| Needed 10–20 minutes of real work before answering | `status="working"` keeps the floor and tells the others it's busy. Their timeouts say what it's working on, and the reminder post is held back for up to an hour. |
| Ended its turn on `say` instead of `over` | `say` keeps the floor, so the others wait on it forever. Its own `chat_await` now refuses (`unfinished_turn`) and says to send `over`; the waiting side's reminder names the stalled `say`. |
| Called `chat_await` when the turn was already its own | Returns at once with `already_received` and the turn's text instead of blocking on itself. Uses the floor rules, so in a 3+ party chat only the addressee gets it back. |
| A `chat_say` failed (e.g. a file attachment refused) and it assumed the turn went out | The error says nothing was posted and that, if it was its turn, it still is. Combined with the row above, a follow-up `chat_await` hands the turn back. On a **local** chat, `files=` just adds the files' full paths to the message (same disk; no copy, no opt-in), so that case doesn't fail any more. |

Hard limit, unchanged: if a session has truly stopped (the human said stop, or it
stopped again after the guard's
one block), only a human can restart it. `watch --state` / the TUI
shows which one to poke.

## Several sessions, one machine, one room (v1.0.32)

An independent review found ways sessions could trip over each other rather than
over the protocol:

| Situation | What now happens |
|---|---|
| Two sessions on one machine save their read positions at the same moment | `state.json` is updated under a cross-process lock, and a file Windows briefly refuses to open or replace is retried, so one session can't erase another's position (which used to hand it old turns) or crash with "Access is denied". |
| Two separate chats share the room | An unaddressed reply goes to whoever handed you the turn. Whose turn it is gets worked out per session, so one pair's turn doesn't hide another's. A newcomer is told another chat is going on and to address its opener. An unaddressed turn that would talk over someone else's floor is refused before posting. |
| Two sessions both answer a human's kickoff | The first reply goes out; the second `chat_say` is refused ("A posted something you haven't read") and the session reads that reply instead. |
| A session answers with plain `send_message` | It counts as the floor holder's turn: delivered to the side that handed them the floor, not echoed back to its sender. *(Changed in v1.0.34/v1.0.36: such a reply is posted as a proper chat turn; an untagged post is nobody's turn.)* |
| `chat_say` fails after posting (or a Discord response is lost) | It returns `posted: true` with the error instead of raising, so it isn't sent twice. Discord posts carry a nonce, so a retried request returns the first message instead of posting a copy. |
| A human remark starts with "Stop ..." or "End ..." | Only a message that is just a stop word ends the chat. |

## One conversation at a time per session, many per room (v1.0.33)

The fourth review found that chat state was still worked out for the whole room,
though the room is shared. Now each session sees only its own conversation: the
sessions linked by addressed turns (A>B joins A and B). An `end` or `impasse`
closes the conversation it was sent in, and a human stop closes all of them.

| Situation | What now happens |
|---|---|
| C ends its chat with D while A and B are mid-chat in the same room | Only C and D's chat ends. B keeps waiting for A; the Stop hook doesn't let B drop out. An unaddressed `end` is addressed to the peer, like a reply. |
| Two separate pairs chat in one room | Each is a two-person chat: no `multiparty`, no `suggest_next` naming someone from the other pair, and a group's `to="all"` turn wakes only that group. |
| A responder joins after another chat's turn landed on top of the opener | An unaddressed opener stays open to any session not yet in a conversation, so `chat_begin` still finds it. |
| More than 100 messages from other chats pass while a turn is owed to an idle session | State is read back as far as the session's read position (up to 500 messages), so `chat_status` and `chat_begin` still find the turn. |
| A second session of a project calls `chat_status` before `chat_begin` | It answers for the name `chat_begin` would give it (`CodeCarver-2`), not its sibling's. |
| Two sessions answer the same message at the same instant | Checking for unread messages and posting happen under a per-room lock, so the second is refused. |
| A long turn is split exactly at a blank line or trailing spaces | No piece ends in whitespace (Discord trims it), so the turn still reassembles exactly. |
| The human runs `discordinator stop` with no room | It goes to the room this machine's chat sessions last used, not the shell's default. |
| Two sessions relay to each other on one machine | Each label has its own read position. Give each project its own label with `DISCORDINATOR_LABEL` in `.mcp.json`. |
| `discordinator config set-...` in a shell with env settings | Only the setting being changed is saved; env overrides like `DISCORDINATOR_ALLOW_SEND=1` stay in that shell. |
| A purge on the local transport while sessions read the room | The room is rewritten once for the whole purge, retrying for a few seconds while Windows refuses. |

## Loose ends from the fourth review (v1.0.34)

| Situation | What now happens |
|---|---|
| A background task finishes (or a compacted session continues) mid-chat | Claude Code writes these as user messages, so the Stop-hook guard took them for the human speaking and let the session drop out. Lines whose `origin` isn't the human, `<task-notification>` lines and compaction summaries no longer start a new turn. |
| A session owes a chat reply and sends it with `send_message` to the chat room | It's posted as that session's chat turn to the peer it owes, instead of an untagged message the room had to guess about (which could be credited to another chat). |
| Two sessions on one machine relay with the same label | Each skips only the messages it sent itself, and each project keeps its own read position. |
| A long relay message is cut | Each cut drops at most the one newline it falls on, and never leaves whitespace Discord would trim; blank lines and indentation survive. |
| A session holding a shared file's lock stalls | Waiters no longer go ahead unlocked after 15s (which could lose an update); a waiter that runs out of time gets an error ("nothing was posted - try again"). (Superseded in v1.0.44: locks are now OS locks, released when the holder exits.) |
| A local delete while sessions read the room nonstop | If Windows won't allow the room to be rewritten, a deletion record is appended instead; reads skip those messages and the next rewrite drops them. A delete no longer fails. |

## A session is its Claude Code process (v1.0.36)

Real stalls kept coming from one cause: a session's identity lived only in its
MCP server's memory. Claude Code was found leaving the old server running after
an `/mcp` reconnect (servers from two days earlier were still alive next to
their replacements), holding the session's name, so the new server became
`Name-2` and never saw the turns sent to `Name`. Tests hadn't caught it because
they ran every "session" inside one Python process; `tests/test_real_sessions.py`
now drives real server processes over stdio.

| What happened | Now |
|---|---|
| A server restarts (reconnect) | It picks up its session's role and name (recorded per Claude Code process) and takes over a claim its old server still holds. |
| The client disconnects mid-`chat_await` | The server exits at once, without reading the reply meant for its successor. |
| A brand-new session (quit and resumed) whose role-name has a chat going | It takes the name back if exactly one unheld `<project>/<role>` is owed a turn or waiting on its own; otherwise `next` (not just a note) says how. |
| An untagged bot post (relay note, CLI send) lands in the chat room | It's nobody's turn and wakes nobody. It used to be credited as the floor holder's reply, leaving the other side "waiting for a reply" it never sent. |
| A conversation is dropped without `end` | After 30 quiet minutes it no longer blocks its members from hearing a new opener; a turn owed to a session expires after 4 hours. |
| Two sessions of one project relay | Each chatting session has its own read position (by handle), and a position never moves backwards. |
| A peer proposes `wrap` / others chat without you | `next` says to confirm with `end` / to raise a hand with `ask`. |
| Something goes wrong out of sight | `watch` shows session events from `~/.discordinator/events.jsonl`: connects, disconnects, vanished servers, joins, renames, failed calls with the reason. |

## A broad review of the whole codebase (v1.0.42)

| What happened | Now |
|---|---|
| A session waiting on a long job while 100+ messages from other chats pass | State reads back to the session's own latest post too, so its conversation (and the peer's `working`) stays in view: no reminder despite `working`, no turn handed over by a human remark, no pull into a newcomer's opener. |
| A peer said `working` 35 minutes ago | Its conversation is kept for the 60 minutes reminders hold off for it, not dropped at 30. |
| `chat_await(from_whom=B)` while C sends you a turn | C's turn is kept and your next `chat_await` returns it, instead of being skipped for good. |
| The server stops mid-`chat_await` with part of a long turn read | The pieces are kept, and the next server delivers the turn whole. |
| A session in no chat sends an unaddressed `end` | It ends nothing for sessions that haven't said anything yet (still waiting for an opener). |
| A wait reads many messages in a busy room | Its read position is written once per batch, not once per message. |
| A reminder fired during one wait | A new turn starts a fresh wait: no stale "a reminder was posted", and the next reminder can fire. |
| A human types something like `[URGENT\|fyi] prod is down` | Only the bot posts chat turns, with a real status: it's a human remark. |
| A session asks to be called `human` or `all` | Refused - nobody could address it, or its turns would read as the human's. |
| Addressing a peer whose last post is 100+ messages back | No "nobody called that" warning. |
| The client disconnects while a long `chat_say` is posting | The server lets calls in progress finish (up to 20s) before it exits. |
| `watch` / `tui` / `interject` / `stop` with this shell's chat on Discord | They always act on local rooms (a Discord chat is watched in Discord), defaulting to the local room this machine's sessions last used. |

## Locks the operating system releases (v1.0.44)

Shared files (`state.json`, local rooms, the event log, the per-room post lock)
used to be locked by creating a `.lock` file, with waiters taking over the file
of a holder that had exited. On Linux and macOS, three or more waiters in a
precise order could move a live lock aside and let two sessions write at once.
Locks are now `flock` (Linux/macOS) or `LockFileEx` (Windows) on a `.flock`
file: the operating system releases one the moment its holder exits, so there
is no takeover step left to race. A holder that hangs while holding one (it
never has, holds are a file read and write) keeps others waiting; each waiter
errors after its timeout, naming the pid, instead of taking the lock away.
