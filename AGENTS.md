# Discordinator — agent setup guide

This repo is a **CLI + MCP server** that sends/reads Discord channel messages.
Purpose: **relay information between AI/coding sessions (or machines)** through a
Discord server. If you are an agent (or person) setting this up on a new machine,
follow this guide.

> Note: this file is intentionally **not** named `CLAUDE.md`. That filename is
> auto-loaded by Claude Code as always-on project memory; this is a setup doc you
> read on demand. If you *want* it auto-loaded, copy it to `CLAUDE.md`.

## What it is
- `discordinator` — command-line tool (setup, humans, quick checks).
- `python -m discordinator.mcp_server` — MCP server exposing tools a session calls.
- Talks to Discord over the **REST API** with **one bot token**. One token works
  for the whole server and every channel in it.

---

## Who owns the bot? (important)
The bot/token is **per owner**, not shared with strangers:

- **Your own machines (A, B, …):** all use the **same** token, **same** server,
  **same** channel ids. Only each machine's *label* differs. They relay to each
  other. This is the primary use case.
- **Someone else entirely:** they create **their own** Discord application + bot
  (their own token) in **their own** account, invite it to **their own** server,
  and use **their own** channel ids. They do **not** use your token or app id.
  The code here is fully generic — nothing is hard-coded — so anyone can run it
  with their own credentials.
- (Only reason two people would share one bot: they deliberately share the token
  and both point at the same server. Not recommended — the token is a secret and
  both would post as the same bot.)

---

## Setup on a new machine (in order)

### 0. Choose a transport per mode
Discordinator has two transports, and **`relay` and `chat` each pick one
explicitly** — there is no shared base and no default, so an unconfigured mode
errors (with the exact fix) rather than guessing:
- **`discord`** — the Discord REST API. Works across machines. Needs a bot token
  + channels (steps 2–3 below).
- **`local`** — **no Discord at all.** Messages live in JSONL files under
  `~/.discordinator/local/`. Two sessions on the **same machine** relay/chat with
  **no token, no network, no channel setup**. Great for a locked-down work laptop
  or offline dev; it cannot reach another machine. Local rooms are for sessions
  chatting *now*, not an archive: messages older than **7 days** are dropped
  (with their stored attachments) the next time the room is written to —
  sessions never need to clean up. Change it with
  `discordinator config set-local-retention <days>` (`0` = keep forever; env
  `DISCORDINATOR_LOCAL_RETENTION_DAYS`). To wipe a room immediately, delete its
  `.jsonl` file while no session is mid-chat there.

Set each mode (env: `DISCORDINATOR_RELAY_TRANSPORT` / `DISCORDINATOR_CHAT_TRANSPORT`):
```powershell
discordinator config set-relay-transport discord   # send/read/relay
discordinator config set-chat-transport  local     # live chat_*
discordinator config set-label <SESSION_LABEL>      # distinct per session for relay self-filtering
```
For a mode on `local`, skip steps 2–3 for it (no bot/token/channels): relay
defaults to room `relay`, chat to room `chat`; pass any `channel="..."` for
another room. The rest of this doc's tool usage is identical on both transports.

**The two modes are independent**, so one session can relay over Discord to
reach another machine while chatting locally with a sibling session on the same
box — exactly the config above. A token is required only for whichever mode runs
on Discord. `download_attachment` picks its backend from the url shape (Discord
CDN link vs. local file path), so it works regardless of which mode delivered
the attachment.

**Watching/steering a local chat** (there's no Discord UI): a human can
`discordinator watch <room> --follow --state` to see it live (chat turns parsed,
plus floor/waiting; `watch --all` interleaves every room), `discordinator
interject "<text>"` to drop a human turn the agents pick up on their next
`chat_await`, and `discordinator stop` to end a runaway chat. For a full-screen
view + input box, `pip install -e .[tui]` then `discordinator tui` (type to
interject, `/stop`, `/quit`). Same hard limit as everywhere: none of this can wake
a session that has stopped running — `--state` just shows you which one to poke.

### 1. Install (Python 3.10+)
```powershell
python -m pip install -e .
```

### 2. Create your own bot (skip if reusing an existing token)
1. https://discord.com/developers/applications → New Application → open **Bot** →
   **Reset Token** → copy it.
2. Invite it to your server (OAuth2 → URL Generator → scope `bot` → permissions:
   View Channels, Send Messages, Read Message History), or use:
   `https://discord.com/api/oauth2/authorize?client_id=<APP_ID>&scope=bot&permissions=68672`
3. **Enable the Message Content Intent** (Bot → Privileged Gateway Intents →
   MESSAGE CONTENT INTENT → on). It's required: without it, reads of messages the
   bot didn't author come back with empty content and no attachments — over REST
   too. The bot sees its own messages regardless, so a send→read test misleadingly
   passes. Instant toggle for a bot in under 100 servers; no re-invite needed.

### 3. Store token + label + channels
Store config in the **home-dir config file** (`~/.discordinator/config.json`),
which is read regardless of the directory the MCP server is launched from — this
is why MCP works across projects.
```powershell
discordinator config set-token <BOT_TOKEN>       # secret; never commit
discordinator config set-label  <MACHINE_LABEL>  # DIFFERENT per machine, e.g. machineB
discordinator config add-channel <name> <channel_id>   # repeat per channel
discordinator config set-default <name>
```
Get channel ids: Discord → Settings → Advanced → Developer Mode on →
right-click a channel → Copy Channel ID.

### 4. Verify
```powershell
discordinator whoami                # prints the bot identity
discordinator send "hello from <label>"
discordinator relay                 # shows messages from the OTHER side only
discordinator version               # version + file paths + git revision
```

### 5. Register the MCP server
**Per-project (for enabling it in only some projects):** copy the template into
each project root where you want the tools:
```powershell
Copy-Item .mcp.json.example <project-root>\.mcp.json
```
It pins a fixed Python interpreter so it works regardless of the project's venv.
Confirm the interpreter path in that file matches this machine
(`python -c "import sys; print(sys.executable)"`).

To make a project default to its own channel, set `DISCORDINATOR_RELAY_CHANNEL` in
that `.mcp.json`'s `env` block (no token needed there — it comes from the home config).
Also set `DISCORDINATOR_CHAT_CHANNEL` to the **shared** room every project uses for
live chats (the same value everywhere), so `chat_*` calls meet there automatically
instead of landing on a per-project relay channel:
```json
{ "mcpServers": { "discordinator": {
    "command": "C:\\Program Files\\Python312\\python.exe",
    "args": ["-m", "discordinator.mcp_server"],
    "env": {
      "DISCORDINATOR_RELAY_CHANNEL": "code-compass",
      "DISCORDINATOR_CHAT_CHANNEL": "claudes-chatroom",
      "DISCORDINATOR_CHAT_HANDLE": "CodeCompass"
    }
}}}
```
`DISCORDINATOR_CHAT_HANDLE` is the project's **fixed name in live chats**, so a
project never shows up under several names (`CodeCarver` one day, `carver` the
next). Set it per project, never in the shared home config. On a second machine,
give the same project a distinct handle in that machine's `.mcp.json` (e.g.
`CodeCarverWork`) so the two machines' sessions can chat with each other.
Relay tools (`send_message`, `get_new_messages`, `read_messages`) default to
`DISCORDINATOR_RELAY_CHANNEL`; chat tools (`chat_*`) default to `DISCORDINATOR_CHAT_CHANNEL`
— two separate defaults so the async mailbox and the live chat never collide on one
channel. (CLI equivalents: `config set-default` and `config set-chat-channel`.)

**Or via CLI** (`-s user` = all projects; omit for current project only):
```powershell
claude mcp add discordinator -- "<python.exe>" -m discordinator.mcp_server
```
Confirm inside Claude Code with `/mcp`.

---

## Two modes — pick the right tools
- **Relay (async mailbox):** hand off context between sessions/machines. One side
  posts and moves on; the other reads later (usually after a human nudge). Nobody
  blocks. Tools: `send_message`, `get_new_messages`, `read_messages`.
- **Chat (live two-way):** two agents talk in real time; `chat_await` blocks until
  the other finishes a turn (no human shuttling). Tools: `chat_begin`, `chat_say`,
  `chat_await`. See [Chat mode](#chat-mode-agent--agent) and the worked
  [example](../docs/example-chat.md).

## MCP tools (relay + utilities)
- `send_message(text, channel?, label?)` — post (long text auto-split; label on
  every chunk).
- `read_messages(channel?, limit?, after?, before?, newest_first?)` — read recent
  (includes plain, untagged messages a human typed directly in the channel).
- `get_new_messages(channel?, include_self?, limit?, ack?)` — **relay primitive**:
  only messages new since the last call (advances a per-channel cursor), your own
  machine's messages filtered out. `ack=true` reacts ✅ to the newest.
- `purge_messages(channel?, older_than_days?, only_mine?, scan_limit?, dry_run?)` —
  delete old messages. Safe defaults: dry_run=True, only_mine=True, 7-day floor.
- `list_channels()` — configured channel names + default.
- `whoami()` — verify token / bot identity.

## Chat mode (agent ↔ agent)
A separate, turn-based protocol — distinct tools so it's never conflated with the
relay above. Every call is made as a `chatter` handle (yours), because both sides
may be on the SAME machine and must stay distinguishable; each handle has its own
read cursor. **Your handle is the project's `DISCORDINATOR_CHAT_HANDLE`**, and
`chatter` is an optional **role** appended to it:
- only session of the project in the chat → omit `chatter` → `CodeCarver`
- two sessions of the same project → each passes its own role, e.g.
  `chatter="ui"` / `chatter="api"` → `CodeCarver/ui`, `CodeCarver/api`
- pass the same `chatter` on every `chat_*` call of that session (if a call
  omits it, the session keeps the role it last used)
- no project handle configured → `chatter` is used as-is (required)

Safety net: each session claims its handle machine-wide. If another **live**
session on this machine already holds it, you get `CodeCarver-2` and `chat_begin`
returns a `note` saying so (repeated as `handle_note` on every chat result) —
two sessions can never silently share a name and ignore each other's turns. A
session keeps its name for as long as it runs; claims free up when it exits. Handles are
case-insensitive (`Convex` = `convex`).

**Who counts as a participant.** Chat state (`chat_status`, the `watch --state` /
TUI sidebar) covers only the **current** chat — everything after the last
`end`/`impasse` or human stop — and drops anyone silent for **30+ minutes**
(except both ends of a turn still owed). The ranked list of non-floor
participants (`waiting` in results, shown as **others** in the viewers) is a
fairness order for `suggest_next`, not a list of sessions actually blocked in
`chat_await`.

> **Which channel?** Live chats always happen on the **shared chat channel** —
> configured once per project as `DISCORDINATOR_CHAT_CHANNEL` (e.g.
> `claudes-chatroom`) and identical across projects. **Never start a chat on a
> per-project relay channel** (`code-compass`, `code-carver`, …): those are
> one-way async mailboxes (a session may even treat them as read-only) and a live
> chat there splits context and strands the other side. Because the chat channel
> is a configured default, just **omit `channel`** in every `chat_*` call — both
> sides land in the same room with nothing to negotiate. Only pass `channel`
> explicitly to override for a one-off.
>
> Because the room is shared, other chats may be going on in it. Replies are
> addressed automatically (below), so only the **opener** needs care: if you know
> your peer's handle, address it (`to="<peer>"`). `chat_begin` adds a `note` when
> another chat is in progress in the room, and an unaddressed turn that would talk
> over someone else's floor is refused (nothing posted) with what to do instead.

- `chat_begin(chatter?, channel?, turn_cap=20)` — both sides call first; each
  resolves to a DISTINCT handle (project handle, `project/role`, or e.g. "A"/"B"
  when no project handle is set). The result's `chatter` is your handle. Seeds read position to now, resets turn count.
- `chat_say(text, chatter?, status="over", channel?, to?, wait=True, timeout=120)` —
  send with an explicit status — **almost always `over`** (the default). `say` (only to split one long turn; never end on it — if you call `chat_await` with a turn left on `say`, it refuses and tells you to send `over`), `working` ("hold on, I'm going
  to go do something" — keeps the floor, tells the others you're busy; post the
  results with `over` when done), `ask` (raise a hand — request the floor without
  taking the turn), `over` (your turn), `wrap` (propose ending — agree?), `end`
  (ending now), `impasse` (stuck — get the human). `to="handle"` addresses the turn
  to one peer (see 3+ chatters). If you omit `to`, your turn is addressed to whoever
  handed you the turn (so a reply always goes back to its asker); `to="all"`
  broadcasts on purpose. If `to` names nobody known
  (no one by that name has posted, and no live session here has it), the result
  warns at once - with a "did you mean" - instead of waiting. **On `over`/`wrap` it
  also waits for the reply and returns it as `reply`** (same shape as
  `chat_await`), so a turn is one call: post, get the answer, respond.
  If `chat_say` raises, the message was **not** posted - if it was your turn, it
  still is; fix the problem and send again. That includes two deliberate refusals:
  something for you arrived that you haven't read yet (call `chat_await` first -
  e.g. when two sessions both answer a human, only the first reply goes out), or
  the turn would talk over someone else's floor. If instead it returns
  `posted: true` with an `error`, the message **was** posted (something failed
  afterwards) - don't send it again; follow `next`.
- `chat_await(chatter?, channel?, timeout=120, poll=3, from_whom?)` — BLOCKS until a
  turn comes to YOU / a human interjects / a participant posts out-of-band / timeout.
  Returns `{from, to, status, text, your_turn, ended, stop_reason, timed_out,
  cap_reached, next}` (plus `floor, pending_requests, waiting, suggest_next` when the
  floor comes to you in a multiparty room). A turn addressed to a *different* peer
  doesn't wake you — you hold until the floor is yours. `from_whom` narrows waking to
  one peer. **If `timed_out` and not `ended`, call it again** — the other side is
  still busy, and waiting a long time is fine; the timeout `note` shows what they
  said they're working on. (A plain `send_message` reply counts as the floor
  holder's turn: it comes back as `status="plain"` to the side that handed them the
  floor, so a non-`chat_say` reply can't strand you - and isn't echoed back to its
  sender.) Called when it's
  already your turn, it hands that turn straight back (`already_received`) instead
  of blocking on yourself. After a long wait (`nudge_after`, default 240s) it posts
  one visible channel reminder so a human knows which session to poke — held back
  for up to an hour while the other side has said it's `working`.
- `chat_status(chatter?, channel?)` — read the current state from history:
  `{session_active, ended, participants, multiparty, last_turn, pending_turn, floor,
  floor_requests, waiting, suggest_next, your_turn}`. **Call this whenever you
  (re)engage a chat channel** to learn if a turn is owed to you — don't eyeball
  message tags.

Flow: both `chat_begin` → initiator `chat_say(..., "over")` (which waits and
returns the reply), other `chat_await`; then each side just keeps calling
`chat_say(..., "over")` with its answer. **Every result has a `next` line — the
exact next step; follow it.** **Don't** have both `chat_await` first (deadlock).
End is mutual: one `wrap`, the other `end`. **The human can type `stop` (or
`[[STOP]]`) in the channel to halt** - a message that is just a stop word ("stop",
"halt!", "end chat now"); a remark that merely starts with one ("Stop arguing and
look at the test") is an ordinary remark. `chat_await` returns `ended` with
`stop_reason="human"`, and a session that rejoins afterwards is told the chat was
stopped (it isn't handed a turn). Any other human message comes back as
`from="human"` so the agents can react; it doesn't move the turn — only the side
that already had it gets `your_turn`, the other is told to keep waiting. Long
messages are split into pieces and always reassemble exactly. A soft `turn_cap` surfaces `cap_reached` to nudge wrapping up.

**Need time mid-chat?** Reply `chat_say("hold on — running the tests",
status="working")`, do the work, then `chat_say(<results>, status="over")`. The
other side keeps waiting and sees your note; no false "it's your turn" reminder.

**Never end your turn mid-chat.** Nothing can wake an idle session when the reply
lands — a human would have to kick it. Install the Stop-hook guard (below) to
enforce this.

### Stop-hook guard (recommended)
`discordinator chat-guard` is a Claude Code **Stop hook**: if a session made chat
calls this turn and its last chat result shows the chat still going, ending the
turn is blocked once with the exact next step ("call chat_await again", "it's your
turn — reply"). Stopping again with no further chat activity is allowed (for when
a session genuinely needs the human); a session that keeps chatting and drops out
again is reminded again. A `chat_await` that failed once (a transient error) is
not a reason to drop out: it's blocked with "call chat_await again". It reads the
session's own transcript, so two sessions in
one directory are never confused, and any error means "allow". Install once per
machine in `~/.claude/settings.json`:
```json
{ "hooks": { "Stop": [ { "hooks": [ {
    "type": "command",
    "command": "\"C:/Users/<you>/AppData/Roaming/Python/Python312/Scripts/discordinator.exe\" chat-guard"
} ] } ] } }
```
(Use the path of your `discordinator` executable: `where discordinator`.)

**Recovering from a stall.** If a chat seems stuck, it usually means one side
`end`ed (or dropped) and the other spoke again, or someone replied with a plain
`send_message`. Call `chat_status(chatter=you)`: if `your_turn` is true, call
`chat_begin` (it repositions you onto the pending turn) then `chat_await`. See
[`docs/chat-protocol-notes.md`](docs/chat-protocol-notes.md) for the full analysis.

**3+ chatters (addressing + floor + anti-starvation).** N-way is supported. Because
a bare "your turn" is ambiguous with three or more, discipline is: **address every
yielding turn** with `chat_say(..., status="over", to="handle")`. The addressee is
the **floor holder** — only they wake from `chat_await`; everyone else keeps
holding. The floor is derived from history (the addressee of the last yielded turn),
so it survives a crash or re-join.

- **Want in while someone else holds the floor?** `chat_say(status="ask", ...)` —
  a hand-raise that's recorded without interrupting the current turn.
  (`say`/`working` only count as holding things up when the floor holder sends
  them; from anyone else they're just a note.)
- **Not starving anyone:** after you yield in a multiparty room, `chat_say` and
  `chat_await` return `pending_requests` (who raised a hand), `waiting` (ranked
  most-starved first), and `suggest_next` (the fair next addressee — an outstanding
  request, else the longest-waiting peer). Address `suggest_next` to rotate fairly.
- **If you're passed over:** your `chat_await` keeps blocking; past `nudge_after` it
  posts one visible line naming you and asking the floor holder to yield to you, so
  a human or the holder rotates. No agent has to remember a recipe.
- An unaddressed turn goes back to whoever handed you the floor. To let anyone
  answer, broadcast on purpose with `to="all"` (which can collide).

Still, keep rooms as small as the task needs — two is simplest; use addressing when
you genuinely need three or more in one conversation.

### Kickoff prompts (paste one to each session)

Both sides **omit `channel`** so they meet on the configured shared chat channel
automatically — don't name a room (that's what caused a real cross-channel mix-up).
Each project's fixed handle (`DISCORDINATOR_CHAT_HANDLE`) is used automatically;
if both sessions are in the **same project**, give each a role with `chatter`
(e.g. `chatter="ui"` / `chatter="api"`) and use it on every chat call.

**Session A — initiator** (fill in TOPIC):
```
You're in a turn-based chat with another AI via the `discordinator` MCP. Do NOT
pass a channel (the shared chat room is the default). Do this:
1. chat_begin().
2. Open with chat_say(text=<your message>, status="over") - add to="<their handle>"
   if you know it (the room is shared). It waits for the reply and returns it in
   `reply`.
3. Keep going: read the reply, think, answer with chat_say(..., status="over").
   EVERY result has a `next` field — always do exactly what it says. If a result
   timed out, call chat_await again; keep waiting as long as it takes.
NEVER end your turn while the chat is going — nothing can wake you when the reply
lands. Need time to do real work first? Send status="working" ("hold on, running
the tests"), do it, then post the results with status="over". Propose ending with
status="wrap"; confirm the other's wrap with status="end"; status="impasse" if a
human is needed. Stop when a result has ended=true. Topic: <TOPIC>
```

**Session B — responder** (same default channel):
```
You're in a turn-based chat with another AI via the `discordinator` MCP. Do NOT
pass a channel (the shared chat room is the default). Do this:
1. chat_begin().
2. Wait for the opener: chat_await(). If it times out, call it again.
3. Answer with chat_say(..., status="over") — it waits for and returns the next
   reply. EVERY result has a `next` field — always do exactly what it says.
NEVER end your turn while the chat is going — nothing can wake you when the reply
lands. Need time to do real work first? Send status="working", do it, then post
the results with status="over". status="wrap" proposes ending; status="end"
confirms the other's wrap; status="impasse" if a human is needed. Stop when a
result has ended=true.
```

(To halt them at any time, type `stop` in the channel yourself.)

**N-way — paste to each of 3+ sessions** (same-project sessions: add distinct roles):
```
You're in a turn-based GROUP chat with other AIs via the `discordinator` MCP. Do
NOT pass a channel (shared default). Rules for 3+:
1. chat_begin(). Note your handle (the result's `chatter`).
2. To speak: ALWAYS address your yield — chat_say(text=..., status="over",
   to="<the peer you want to answer>"). Only that peer wakes. It then waits for
   the floor to come back to you.
3. EVERY result has a `next` field — always do what it says. If a result timed
   out, call chat_await again. When the floor comes to you the result includes
   suggest_next — address your next turn to it so nobody is starved.
4. To get a word in while another holds the floor: chat_say(status="ask", ...).
5. Need time for real work? status="working", do it, then status="over".
6. Propose ending with status="wrap"; confirm with status="end" (ends for all).
   status="impasse" if stuck. NEVER end your turn mid-chat. Stop when ended=true.
Topic: <TOPIC>
```

## Permissions
Recommended invite: `permissions=68672` (View + Send + Read History + Add
Reactions). Add Reactions powers the ✅ read-acks, which are ON by default
(`ack_on_read`); without it, reads still work but skip the ✅. For `purge --all`
(others' messages) also add Manage Messages → `permissions=76864`. Deleting the
bot's OWN messages needs nothing extra.

## Reading human messages
If a person just types a message directly in the channel (no `[label]` tag),
agents still see it: `read_messages` returns everything, and `get_new_messages`
only filters out the *bot's own machine label* — a plain human message has no
such prefix, so it passes through. Human messages also come from the person's
Discord account, not the bot.

**Requires the Message Content Intent.** To read the *content and attachments* of
messages the bot didn't send (i.e. any human or other-bot message), the app must
have the privileged **Message Content Intent** enabled (Developer Portal → Bot →
Privileged Gateway Intents → MESSAGE CONTENT INTENT). This applies to REST reads,
not just the gateway. Without it, a human message reads back with empty content
and an empty attachment list even though its id/author/timestamp are visible —
and because the bot always sees its *own* messages in full, a send→read self-test
will pass and hide the problem. Instant toggle for a bot in under 100 servers.

## Attachments
Discordinator can send and receive files/images, but **both directions are
opt-in and OFF by default** (so a locked-down machine never moves files unless
told to). Enable per machine with `config set-attachments send on` /
`... receive on` (or `DISCORDINATOR_ALLOW_SEND` / `DISCORDINATOR_ALLOW_RECEIVE=1`).
- **Send** (`send --file/--image`, MCP `send_file`, `chat_say(files=...)`): needs
  the send opt-in AND the bot's **Attach Files** permission (`permissions=101440`).
  Images auto-embed; ≤10 files/message, ~10MB/file.
- **Local chats** (`chat_transport: local`): `chat_say(files=...)` copies
  nothing and needs no opt-in. Both sessions share the disk, so the files' full
  paths are added to the message for the other side to open directly.
- **Receive** (`read --download`, MCP `download_attachment`): needs the receive
  opt-in. Reads surface each attachment as `{url, filename, content_type, size,
  width, height, is_image}`; pass the `url` to `download_attachment` to fetch the
  bytes. Discord CDN urls are signed and **expire**, so download from a FRESH
  read, not a stashed url.
- **Reading a human's attachment requires the Message Content Intent** (see
  below) — without it the attachment list reads empty even though the message is
  visible.

## Message size
Discord's limit is **2000 characters per message** for bots. Longer text is
automatically split into multiple ≤2000-char messages (preferring newline
boundaries), each carrying the label prefix. There is no practical cap on total
size, but very large sends become many messages and are paced by Discord's
per-channel rate limit (~5 messages / 5s; the client auto-retries on 429).

---

## Updating
- Code is installed editable (`pip install -e .`), so pulling new code takes
  effect immediately — no reinstall needed unless dependencies changed.
- `discordinator version` shows the local version + git commit; compare against
  the repo to know if a machine is behind. See `CHANGELOG.md` for what changed.

### Syncing a machine to latest (e.g. Machine B)
1. Pull (and reinstall only if a release added a dependency):
   ```powershell
   git -C C:\Playground\Discordinator pull
   # only if CHANGELOG says deps changed:
   python -m pip install -e C:\Playground\Discordinator -q
   ```
2. Pick up the new code where it runs:
   - **CLI** — nothing to do; each command is a fresh process.
   - **MCP** — a running server is on old code until restarted. In Claude Code,
     `/mcp` → reconnect the `discordinator` server, or start a fresh session.
3. Confirm: `discordinator version` should match the repo's latest tag
   (`git -C C:\Playground\Discordinator describe --tags`).

## Tests
Each `tests/test_*.py` is a standalone script (no pytest) that exits non-zero on
its first failed check. They use temp configs and the local transport or a mock
server, so they need no token or network. Run them all:
```bash
for t in tests/test_*.py; do python "$t" > /dev/null || echo "FAIL $t"; done
```
GitHub Actions (`.github/workflows/tests.yml`) runs every suite on each push and
pull request, on Windows and Linux with Python 3.10 and 3.12. Check it's green
before cutting a release.

## Releasing
Versioning is `1.0.x` (bump the patch each release). Once your code changes are
committed, cut a release with the helper — it bumps `__version__`, prepends a
CHANGELOG entry, commits, tags `vX.Y.Z`, and pushes:
```powershell
python release.py "one-line summary" --bullet "detail" --bullet "detail"
# --version X.Y.Z to set explicitly; --no-push to hold the push
```

## Gotchas
- **Token stays local** — home config / git-ignored `.env` / MCP `env` block.
  Never commit it. `.env`, `config.json`, `state.json` are git-ignored.
- **`.env` auto-load is cwd-based** — only helps when run from inside this repo.
  For MCP launched from other projects, rely on the home config file.
- **Python env** — `python -m discordinator.mcp_server` needs the package
  installed for that interpreter; pin a fixed path in `.mcp.json` for per-project
  venvs.
- **UTF-8** — the CLI forces UTF-8 output so Unicode doesn't crash the Windows
  console.
- **Corporate TLS proxy** — TLS is verified against the OS trust store (via
  `truststore`, injected at startup), so TLS-inspection CAs (Netskope/Zscaler)
  that the OS/browser already trusts work with no CA-bundle path. No
  `SSL_CERT_FILE` needed; it still overrides if set. Full verification is kept
  (not `verify=False`). If you still see `CERTIFICATE_VERIFY_FAILED`, the
  inspecting CA isn't in the OS store — have IT install it there.

## Source layout
`src/discordinator/`: `config.py` (config + relay cursor state + atomic writes),
`discord_client.py` (REST client + chunking), `cli.py`, `mcp_server.py`.
Version lives in `__init__.py`.
