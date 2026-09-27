# Changelog

All notable changes to Discordinator are recorded here. The version is the
single source of truth in `src/discordinator/__init__.py` (`__version__`);
`discordinator version` prints it along with the git revision so you can tell
exactly what a machine has and whether it needs updating.

Versioning: `1.0.x` — the patch number bumps with each release. (The `0.x`
entries below are the pre-1.0 development history.)

## [1.0.8] - 2026-09-27
Clarify the two modes: relay (async mailbox) vs chat (live two-way)
- README comparison table + AGENTS mode-picker framing

## [1.0.7] - 2026-09-27
Example is a clean linked text page; drop raw chat_pngs dump
- docs/example-chat.md self-contained, linked at top of README
- chat_pngs/ untracked + gitignored (local scratch only)

## [1.0.6] - 2026-09-27
Chat example doc + screenshots; chat_await timeout 50s->120s
- docs/example-chat.md: annotated real two-session negotiation
- chat_await default 120s + explicit re-call-on-timeout guidance

## [1.0.5] - 2026-09-27
Document syncing a machine to latest (pull + MCP reconnect)
- AGENTS.md: git pull, when to reinstall, /mcp reconnect vs CLI, version check

## [1.0.4] - 2026-09-27
chat_await messages[] is now metadata-only
- text holds the words; messages[] keeps id/from/status/timestamp (no duplication on long turns)

## [1.0.3] - 2026-09-27
Add chat-mode kickoff prompt snippets
- paste-ready initiator (A) and responder (B) prompts in AGENTS.md

## [1.0.2] - 2026-09-27
Chat mode for two agents (chat_begin/say/await)
- per-participant chatter ids; works same-machine
- explicit turn status + blocking chat_await + mutual end + human stop

## [1.0.1] - 2026-09-26
Add release helper and git version tags
- release.py bumps version, updates CHANGELOG, commits, tags vX.Y.Z, pushes
- v1.0.0 tagged retroactively

## [1.0.0] - 2026-09-26
First stable release; adopts the `1.0.x` scheme. Same code as the final `0.x`.
### Changed
- TLS is now verified against the **OS trust store** (via `truststore`, injected
  once at each entry point's `main()`). This lets corporate TLS-inspection CAs
  (Netskope/Zscaler etc., already trusted by the OS/browser) validate without
  setting `SSL_CERT_FILE`. Full verification is kept — this is NOT `verify=False`.
  Falls back to Python's bundled CA list if `truststore` is unavailable, and
  `SSL_CERT_FILE` still works as an override.
- `truststore>=0.9` is now a direct dependency (was only transitive via `mcp`).

## [0.6.0] - 2026-09-26
### Changed
- `ack_on_read` now defaults to **True** — a fresh install auto-posts ✅ read-acks
  with no extra config. Disable with `config set-ack off` / `DISCORDINATOR_ACK=0`.
- Recommended invite bumped to `permissions=68672` (adds Add Reactions).
### Fixed
- MCP read-ack is now best-effort: a missing Add Reactions permission no longer
  fails the read (it just skips the ✅). Important now that acks are default-on.

## [0.5.0] - 2026-09-25
### Added
- `ack_on_read` config setting (+ `DISCORDINATOR_ACK` env) to auto-react ✅ to the
  newest message on EVERY read, so acks happen without passing a flag each time.
  Set with `discordinator config set-ack on`.
- `read_messages` MCP tool now supports `ack`; `read`/`relay` CLI gained `--no-ack`.

### Changed
- `--ack` / MCP `ack` are now tri-state: explicit flag wins, otherwise the
  `ack_on_read` config decides. This is why plain reads weren't posting a ✅.

## [0.4.0] - 2026-09-25
### Added
- `DISCORDINATOR_CHANNEL` env override for the default channel, so a per-project
  `.mcp.json` can pin each project to its own channel without code changes.

## [0.3.0] - 2026-09-25
### Added
- `purge` CLI command and `purge_messages` MCP tool: on-request cleanup of old
  messages with safe defaults (dry-run, only the bot's own messages, age floor).
  Deleting others' messages needs the Manage Messages permission.
- Read acknowledgements: `--ack` on `read`/`relay` and `ack` on
  `get_new_messages` react ✅ to the newest message read (needs Add Reactions).
- Discord client gained `delete_message` and `add_reaction`.

### Notes
- These features need extra bot permissions. Re-invite with `permissions=76864`
  (adds Add Reactions + Manage Messages) to enable reactions and full purge.

## [0.2.0] - 2026-09-25
### Added
- `relay` CLI command and `get_new_messages` MCP tool: return only messages
  new since the last check (per-channel cursor), filtering out your own machine's
  messages. The relay primitive for two-machine handoff.
- `discordinator version` command (version + config/state paths + git revision).
- `.env` auto-loading (searches the working dir and parents) for a git-ignored,
  project-local token file.
- `.mcp.json.example` template and agent setup guide.

### Fixed
- CLI crashed printing Unicode (emoji/non-latin/code) on the Windows cp1252
  console — output is now forced to UTF-8.
- Long messages: the `[label]` tag is now applied to EVERY chunk, so multi-part
  messages stay self-identifying and relay self-filtering works on them.

### Changed
- Config and relay-state writes are now atomic (temp file + replace) for safety
  when multiple sessions run concurrently.
- MCP server silences per-request httpx INFO logging.
- Version is now sourced dynamically from the package.

## [0.1.0] - 2026-09-25
### Added
- Initial release: REST-based Discord client, `send` / `read` / `channels` /
  `whoami` / `config` CLI commands, and an MCP server exposing `send_message`,
  `read_messages`, `list_channels`, and `whoami`.
- Named channel config (project → channel id) with a default, plus per-machine
  message labels.
