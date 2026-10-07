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
   still ends the chat.)

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
- **No auto-`chat_say` on plain replies.** A plain reply is surfaced as `plain`;
  the model decides whether to treat it as the other side's turn. We don't silently
  convert `send_message` into a chat turn.

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
| A session answers with plain `send_message` | It counts as the floor holder's turn: delivered to the side that handed them the floor, not echoed back to its sender. |
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
