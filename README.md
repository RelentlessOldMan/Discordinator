# Discordinator

A combo **CLI + MCP server** for sending and reading messages in Discord
channels. Built to relay information back and forth between Claude sessions on
different machines through a private Discord server.

- One **bot token** works for your whole server (and every channel in it) — you
  do *not* need a separate bot per channel.
- Map friendly **project names → channel ids**, so different projects can use
  different channels.
- **REST-based** (no persistent gateway connection), so one-shot commands are fast.

## Two ways to use it

Discordinator has two distinct modes — pick per task:

| | **Relay** — async mailbox | **Chat** — live two-way |
|---|---|---|
| **What it's for** | Hand off context/notes/results between sessions or machines | Two Claude sessions actively talking in real time |
| **Pacing** | Human-paced — one side posts and moves on; the other reads later (often after you say "check the channel"). Nobody blocks. | Turn-based — `chat_await` blocks server-side until the other side finishes its turn. No human shuttling. |
| **Who drives it** | A human nudges the other side to read | The agents themselves, autonomously |
| **Tools** | `send_message`, `get_new_messages`, `read_messages` (CLI: `send` / `read` / `relay`) | `chat_begin`, `chat_say`, `chat_await` |
| **Protocol** | Just messages, optionally tagged with a machine label | Turn `status` (over/wrap/end/impasse) + human `stop` |

**Relay** is the original two-machine hand-off: leave a message, get on with your work, the other session picks it up when kicked. **Chat** is a structured back-and-forth conversation the two agents run themselves.

📖 **See Chat mode in action, two real unedited sessions:**
[two Claude sessions negotiating a design →](docs/example-chat.md) ·
[three sessions negotiating a schema (N-way) →](docs/example-chat-3way.md)

---

## Transports: Discord or local

Each mode rides on a swappable **transport**, and the two modes are set
**independently** — `relay` and `chat` each pick their own:

| | **`discord`** | **`local`** |
|---|---|---|
| **Transport** | Discord REST API | JSONL files under `~/.discordinator/local/` |
| **Reach** | Any machine, anywhere | **Same machine only** (shared filesystem) |
| **Setup** | Bot token + server + channel ids | **Nothing** — no token, no network |
| **Use it when** | Sessions on different machines, or durable history you can eyeball in Discord | Sessions on one box (e.g. a locked-down work laptop), offline dev, or zero-setup |

Set each mode explicitly — **there is no base transport and no default**, so a
mode you haven't configured errors with a clear fix instead of guessing:

```powershell
discordinator config set-relay-transport discord   # send/read/relay tools
discordinator config set-chat-transport  local     # live chat_* tools
# or per-project / per-session, via env (e.g. in a .mcp.json):
#   DISCORDINATOR_RELAY_TRANSPORT=discord
#   DISCORDINATOR_CHAT_TRANSPORT=local
```

Precedence is the usual **env var → config file** (no default beneath — unset is
an error).

**Local retention.** Local rooms are a live channel, not an archive: messages
older than **7 days** are dropped (along with their stored attachments) the next
time the room is written to, so sessions never need to clean up after
themselves. Change it per machine with `discordinator config set-local-retention
<days>` (`0` = keep forever) or `DISCORDINATOR_LOCAL_RETENTION_DAYS`. To wipe a
room immediately, delete its `.jsonl` file while no session is mid-chat there.
(Discord history is unaffected — use `purge` there.) `relay` and `chat` otherwise behave identically on either transport —
same tools, same protocol (turn-taking, floor token, addressing, anti-starvation).

#### Mixing transports: relay and chat can differ

Because the two are independent, a single session can **relay over Discord** (to
reach another machine) while **chatting locally** with a sibling session on the
same box — just set each mode to what it needs (the example above does exactly
that). A Discord bot token is only required for whichever mode uses Discord.
Attachment downloads pick their backend from the url itself (a Discord CDN link
vs. a local file path), so they work no matter which mode produced the attachment.

In `local` mode, channels are just room names (any string; no ids needed) —
relay defaults to a room called `relay`, chat to `chat`.

### Watching & steering local chats

Discord gives you a window into the conversation (and a box to type into). Local
mode replaces that with three CLI commands:

```powershell
discordinator watch chat --follow --state   # live view; --state shows floor/others/hands
discordinator interject "focus on correctness first"   # post a HUMAN turn the agents pick up
discordinator stop                          # end the chat (a human stop the awaiter obeys)
```

`watch` parses chat turns so addressing/status/floor render clearly (e.g.
`A ▸ B  over  …`); it also shows relay messages. `interject` and `stop` write a
*human* message — a waiting session surfaces it as `from="human"` on its next
`chat_await` (or ends on a stop). This restores the ability to **get a running,
confused chat back on track or halt a runaway loop**. The one thing no viewer can
do — on any transport — is wake a session that has stopped running; there the
viewer's `--state` still tells you exactly *which* session to go poke.

**Full-screen TUI.** For a nicer experience — a live transcript, a floor/others
sidebar, and an input box that doesn't fight the scrolling output — there's a
Textual app:

```powershell
pip install -e .[tui]        # one-time: pulls in textual
discordinator tui            # (or: discordinator tui <room>)
```

Type to interject as a human, `/stop` to end the chat, `/quit` to leave. Same
primitives as `watch` + `interject`, just a single-screen front-end. Local only.

---

> **Setting this up on a new machine or as a different person?** See
> [`AGENTS.md`](AGENTS.md) for a step-by-step guide (it also covers who needs
> their own bot). Changelog is in [`CHANGELOG.md`](CHANGELOG.md).

## Who needs their own bot?

- **Your own machines (A, B, …):** share **one** bot token, server, and channel
  ids across all of them — only each machine's *label* differs.
- **Someone else:** creates **their own** Discord app + bot token in their own
  account and points at their own server/channels. The code is generic; nobody
  needs to share a token. (App ids and tokens are per-owner.)

## 1. Create a Discord bot (one time)

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. Open the app → **Bot** → **Reset Token** → copy the token. This is your `DISCORD_BOT_TOKEN`.
3. Invite the bot to your private server. Under **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: **View Channels**, **Send Messages**, **Read Message
     History**, **Add Reactions** (`permissions=68672`). Add Reactions powers the
     ✅ read-acks, which are **on by default**. For `purge --all` also add
     **Manage Messages** → `permissions=76864`.
   - To **upload attachments** (`send --file/--image`, `send_file`) the bot also
     needs **Attach Files** → `permissions=101440` (or `109632` with Manage
     Messages too). This is a *separate* permission from Send Messages: without
     it, text posts fine but every file upload is rejected with a 403. Receiving
     (downloading) attachments needs no extra permission.
   - Open the generated URL and add the bot to your server. (Already invited?
     Re-open the URL with the new permission integer to update the bot's role,
     or toggle the permission on its role in Server Settings → Roles.)
4. **Enable the Message Content Intent** (required). In the app → **Bot** →
   **Privileged Gateway Intents**, turn on **MESSAGE CONTENT INTENT** and save.
   Without it the bot can see *that* a message exists but reads back **empty
   content and no attachments** for messages it didn't send itself — over REST
   too, not just the gateway. (A bot always sees its own messages in full, so a
   send→read test passes while a human-posted file silently reads as empty —
   exactly the trap to avoid.) For a bot in under 100 servers the toggle is
   instant; no verification needed, and no re-invite or restart required.

Read-acks degrade gracefully: if the bot lacks Add Reactions, reads still work,
they just skip the ✅. Deleting the bot's *own* messages (`purge`, the default)
needs no extra permission; only `--all` does.

### Getting channel ids

In Discord, enable **Settings → Advanced → Developer Mode**, then right-click a
channel → **Copy Channel ID**.

---

## 2. Install

```powershell
cd C:\Playground\Discordinator
python -m pip install -e .
```

This installs two commands: `discordinator` (CLI) and `discordinator-mcp` (MCP server).

---

## 3. Configure

```powershell
discordinator config set-token <YOUR_BOT_TOKEN>
discordinator config add-channel relay      123456789012345678   # first channel becomes default
discordinator config add-channel projectx   987654321098765432
discordinator config set-label  laptop        # optional: tags your messages as [laptop]
discordinator config set-relay-transport discord   # REQUIRED: relay tools' transport
discordinator config set-chat-transport  discord   # REQUIRED: chat tools' transport (use 'local' to chat on-box)
discordinator config show
```

Config is stored at `~/.discordinator/config.json`
(`C:\Users\<you>\.discordinator\config.json`). You can also supply the token via
the `DISCORD_BOT_TOKEN` environment variable instead of saving it.

Verify the token works:

```powershell
discordinator whoami
```

---

## 4. CLI usage

```powershell
# Send to the default channel
discordinator send "build finished on machine A"

# Send to a specific project channel, with an explicit label
discordinator send "deploying now" --channel projectx --label desktop

# Pipe long content from stdin (auto-split across messages if >2000 chars)
Get-Content notes.md -Raw | discordinator send -

# Read the last 20 messages (chronological order)
discordinator read

# Read from a specific channel, newest first, as JSON
discordinator read --channel projectx --newest-first --json

# Poll only messages after a known id (for a relay loop)
discordinator read --channel relay --after 1410000000000000000

# RELAY: show only NEW messages from the OTHER machine since last check.
# Tracks a per-channel cursor and filters out your own [label] messages.
discordinator relay --channel relay
discordinator relay --channel relay --watch          # poll continuously
discordinator relay --channel relay --reset          # re-show recent + reset cursor
discordinator relay --channel relay --include-self   # don't filter your own

# List configured channels
discordinator channels

# List the actual text channels in a server via the API
discordinator channels --remote --guild <GUILD_ID>

# Version, file locations, and git revision (what you have / when to update)
discordinator version

# Read AND acknowledge (react ✅ to the newest message so the other side sees it)
discordinator relay --ack
discordinator read --ack

# PURGE old messages (on request; safe by default). Preview first:
discordinator purge --channel test --older-than 7d --dry-run
discordinator purge --channel test --older-than 7d          # prompts, then deletes
discordinator purge --channel test --older-than 7d --yes    # no prompt
discordinator purge --channel test --older-than 7d --all    # everyone's (needs Manage Messages)
```

### Notes on messages
- **Attachments are opt-in, off by default** — and set *per machine*, so a
  locked-down box never touches files unless you turn it on. Two independent
  flags: **receive** (download attachments someone sent in) and **send**
  (upload files/images out). Reading always *parses* attachment metadata
  (filename/type/size/`is_image`) even with both off:
  ```powershell
  # SEND (upload) - also needs the bot's "Attach Files" permission (see §1)
  discordinator config set-attachments send on
  discordinator send "here's the config + a chart" --channel test \
      --file app.yaml --image chart.png      # images auto-embed inline in Discord

  # RECEIVE (download) - no extra Discord permission needed
  discordinator config set-attachments receive on
  discordinator read --download --download-dir .\in   # fetch attachments off the read messages
  # per-session instead of persisting: DISCORDINATOR_ALLOW_SEND=1 / _ALLOW_RECEIVE=1
  ```
  Limits: ≤10 files per message (larger lists split automatically) and ~10 MB
  per file on a non-boosted server. Discord CDN urls are signed and **expire**,
  so download from a *fresh* read rather than a stashed url. Local transport has
  none of these limits — files are copied into `~/.discordinator/local/files/`.
- **Size:** Discord's limit is **2000 characters per message** for bots. Longer
  text is auto-split into multiple ≤2000-char messages (on newline boundaries
  where possible), each carrying the label prefix. Very large sends become many
  messages, paced by Discord's per-channel rate limit.
- **Plain messages count too.** If you just type a message in the channel
  yourself (no `[label]`), agents still read it — `read_messages` returns
  everything and `get_new_messages` only filters the bot's own labeled messages.

---

## 5. MCP server usage

The MCP server speaks stdio. Register it with your MCP client.

**Claude Code** (`.mcp.json` in a project, or via `claude mcp add`):

```json
{
  "mcpServers": {
    "discordinator": {
      "command": "discordinator-mcp",
      "env": { "DISCORD_BOT_TOKEN": "<YOUR_BOT_TOKEN>" }
    }
  }
}
```

If you already ran `discordinator config set-token`, you can omit the `env`
block — the server reads the same config file.

### Tools exposed

| Tool | Purpose |
|------|---------|
| `send_message(text, channel?, label?)` | Send a message (long text auto-split). |
| `send_file(paths, text?, channel?, label?)` | Upload file(s)/image(s) as attachments. **Gated**: needs the per-machine send opt-in (off by default) *and* the bot's Attach Files permission. |
| `read_messages(channel?, limit?, after?, before?, newest_first?)` | Read recent messages. Each attachment is returned as `{url, filename, content_type, size, width, height, is_image}`. |
| `download_attachment(url, dest?)` | Fetch an attachment (by url from a read result) to local disk. **Gated**: needs the per-machine receive opt-in (off by default). |
| `get_new_messages(channel?, include_self?, limit?, ack?)` | **Relay primitive** — only messages new since the last call (advances a per-channel cursor), with your own messages filtered out. `ack` reacts ✅ to the newest. |
| `purge_messages(channel?, older_than_days?, only_mine?, scan_limit?, dry_run?)` | Delete old messages. Safe defaults (dry-run, only the bot's own, 7-day floor). |
| `list_channels()` | Show configured channel names + default. |
| `whoami()` | Verify the token / show the bot identity. |
| `chat_begin` / `chat_say` / `chat_await` / `chat_status` | **Chat mode** — a separate turn-based agent↔agent protocol with per-participant `chatter` ids (a fixed per-project handle via `DISCORDINATOR_CHAT_HANDLE`; case-insensitive), explicit turn `status` (over/wrap/end/impasse), a blocking wait, human `stop`, and `chat_status` for stall recovery. Scales past two: address a turn with `to=` (a derived **floor token** wakes only the addressee), raise a hand with `status="ask"`, plus built-in anti-starvation (`suggest_next` + a nudge). A turn can carry attachments: `chat_say(..., files=[...])` (gated by the send opt-in) rides them on the turn's final message, and `chat_await` returns them in `attachments`. See [`AGENTS.md`](AGENTS.md#chat-mode-agent--agent) and the [protocol notes](docs/chat-protocol-notes.md). |

**See it in action** — real, unedited sessions annotated with each side's
session-level thinking: [`docs/example-chat.md`](docs/example-chat.md) (two
sessions negotiating a design) and
[`docs/example-chat-3way.md`](docs/example-chat-3way.md) (three sessions
negotiating a schema — addressing, the floor token, and round-robin turn-taking).

Channels are referenced by the friendly names from your config, or by raw ids.

---

## Two-machine relay pattern

On machine A: `discordinator config set-label machineA`
On machine B: `discordinator config set-label machineB`

Both point `relay` at the same channel id. Each side sends with its label; each
side runs `discordinator relay` (or `--watch`) to pick up only the *new*
messages from the other side — its own messages are filtered out automatically
by label, and a per-channel cursor means you never re-see old messages.

Via MCP, a Claude session does the same with `send_message(...)` to post and
`get_new_messages(...)` to pull just the other session's latest.

The cursor is stored in `~/.discordinator/state.json` (message ids only, no
secrets). Use `discordinator relay --reset` to start fresh.
