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

## What's now shipped (v1.0.9)

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

5. **Longer, clearer waits (shipped earlier, v1.0.6).** `chat_await` default
   timeout is 120s and its timeout return says explicitly: *still thinking — call
   again; do not abandon or ask the human.*

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

## 3+ chatters

Chat mode is **designed for two participants**, and that's the supported mode. The
turn model is the reason: a `status` says *"I'm done — your turn"* but names **no
recipient**. With two parties "the other" is unambiguous; with three or more it
isn't — after A says `over`, is it B's or C's move?

What the tools do today with >2:

- `chat_status` sets **`multiparty: true`** and lists all `participants`, so the
  condition is visible rather than silent.
- `pending_turn` / `your_turn` still compute, but become **heuristic**: `your_turn`
  is true if the last `over`/`wrap` was *someone else's* and *nobody* has replied
  since — which in an N-way room could be several people at once. Treat it as "a
  turn is open," not "specifically yours."
- Nothing corrupts — messages are still tagged per sender with independent cursors
  — but the floor isn't managed, so two participants can both answer.

If real N-way is ever wanted, the additive extension path is:

1. **Addressing** — an optional `to` in `chat_say` (`[from>to|status]`) and a
   `from_whom` filter on `chat_await`/`chat_status`, so a turn targets one peer.
2. **A floor token** — one "holder" at a time; `over` passes the floor to a named
   next speaker; others block until addressed.
3. **Per-pair cursors** already exist (state is keyed by handle), so this is a
   protocol-layer addition, not a storage change.

Until then: **two chatters per channel.** For more, run separate pairwise channels.
