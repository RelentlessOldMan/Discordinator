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
3. No privileged intents needed — reading history over REST only needs the
   channel permissions above.

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

To make a project default to its own channel, set `DISCORDINATOR_CHANNEL` in that
`.mcp.json`'s `env` block (no token needed there — it comes from the home config):
```json
{ "mcpServers": { "discordinator": {
    "command": "C:\\Program Files\\Python312\\python.exe",
    "args": ["-m", "discordinator.mcp_server"],
    "env": { "DISCORDINATOR_CHANNEL": "code-compass" }
}}}
```

**Or via CLI** (`-s user` = all projects; omit for current project only):
```powershell
claude mcp add discordinator -- "<python.exe>" -m discordinator.mcp_server
```
Confirm inside Claude Code with `/mcp`.

---

## MCP tools
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

## Attachments
Discordinator sends **text only** — it never uploads files. When reading, if a
message happens to carry an attachment, its URL is shown for reference; nothing
is downloaded.

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

## Source layout
`src/discordinator/`: `config.py` (config + relay cursor state + atomic writes),
`discord_client.py` (REST client + chunking), `cli.py`, `mcp_server.py`.
Version lives in `__init__.py`.
