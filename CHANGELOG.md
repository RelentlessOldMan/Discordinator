# Changelog

All notable changes to Discordinator are recorded here. The version is the
single source of truth in `src/discordinator/__init__.py` (`__version__`);
`discordinator version` prints it along with the git revision so you can tell
exactly what a machine has and whether it needs updating.

Versioning: `1.0.x` — the patch number bumps with each release. (The `0.x`
entries below are the pre-1.0 development history.)

## [1.0.34] - 2026-10-07
Loose ends from the fourth review: guard notifications, plain chat replies, same-label relay, relay chunking, stalled locks, local deletes
- Stop-hook guard: a background-task notification or a compacted session continuing no longer counts as the human speaking, so a session woken by one mid-chat can't drop out
- send_message to a chat room where you owe a reply posts it as your chat turn to that peer
- Relay: each session skips only the messages it sent, and each project has its own read position - same-label sessions relay to each other
- Long relay messages keep blank lines and indentation where they're cut
- Locks record their holder: an exited holder's lock is taken at once; a waiter that times out errors instead of writing unlocked
- Local deletes never fail while sessions read the room (a deletion record is appended instead)

## [1.0.33] - 2026-10-07
Fourth review fixes: one conversation per session in a shared room, per-session relay, config and CLI room
- Another chat's end, unaddressed turns, plain replies and members no longer reach your conversation in a shared room; an unaddressed end goes to your peer
- An unaddressed opener reaches a late responder; a turn owed to an idle session survives 100+ messages of other traffic
- chat_status answers for a second session's own name; two answers at the same instant: one gets through
- Split turns survive Discord trimming whitespace
- Relay read positions are per label (set DISCORDINATOR_LABEL per project); config set-* never saves env overrides; stop/interject/watch use the room the sessions last used; local purges retry while Windows refuses

## [1.0.32] - 2026-10-06
Third review fixes: sessions sharing a machine or a room, posted-then-failed sends, plain replies, stop words
- state.json is updated under a cross-process lock, so one session can't erase another's read position or crash with Access is denied
- chat_say that fails after posting says posted=true instead of raising; Discord posts carry a nonce so a retry can't post twice
- Replies go back to whoever handed over the turn, and turns are worked out per session, so separate chats can share one room
- A reply is refused if something for you arrived unread (two sessions answering a human don't both go out)
- A plain send_message reply goes to the side that asked; only a bare stop word stops; a renamed session keeps its name; long turns survive read errors

## [1.0.31] - 2026-10-06
Second review fixes: human stop and remarks, whole split turns, exact long text, no phantoms, stable names
- A human stop really ends the chat - rejoining doesn't restart it; a human remark doesn't give the turn to both sides
- Long split turns always arrive whole (across remarks, other peers' turns, timeouts, or joining mid-turn) and byte-for-byte exact
- A corrected typo in to= no longer leaves a phantom participant
- Omitting chatter keeps the session's role; chat_status never claims a name
- Local transport: reads retry while the room file is being swapped

## [1.0.30] - 2026-10-06
Independent review fixes: 3-way stalls, split turns, typo'd addresses, stable handles, guard gaps
- 3+ party chats: a raised hand no longer hides that the floor holder owes its turn
- Long (split) turns arrive whole after a rejoin, a hand-back, or a timeout mid-turn
- chat_say warns at once (with 'did you mean') when to= names nobody known, instead of waiting
- Handles: a live session keeps its name however long it's idle; recycled process ids can't hold a name; renames reported on every chat result
- Stop-hook guard: works alongside other Stop hooks; a failed end/impasse is caught
- Local transport: crashed-writer locks recovered safely before anyone writes unlocked; a partly-sent long message says what to resend

## [1.0.29] - 2026-10-06
Review fixes: 3-way floor handling, Stop hook catches failed chat_say; docs caught up
- 3+ party chats: a say/working from someone who doesn't hold the floor no longer counts as holding it (no false 'send over', no misleading wait notes, no suppressed reminder)
- Stop-hook guard: blocks once when the last chat_say errored (the turn probably never went out); reminder tracking no longer depends on how Claude Code logs the hook's feedback
- Docs: README, protocol notes, example transcripts, AGENTS and chat_await description updated for v1.0.26-1.0.28

## [1.0.28] - 2026-10-06
A failed chat send can't strand a turn; local chats share file paths
- If chat_say fails, the error says nothing was posted and that it is still your turn (works in 3+ party chats: only the floor holder is owed the turn)
- Local chats: chat_say(files=...) adds the files' full paths to the message instead of copying them; no attachment opt-in needed

## [1.0.27] - 2026-10-06
No more turns stranded on 'say'
- chat_await called while your own turn is unfinished (last message was say/working, never yielded) returns immediately with unfinished_turn and a note to send status='over' — instead of both sides waiting forever; a newer message from someone else is still delivered first
- the waiting side's channel reminder now names a stalled 'say' ("[B] sent status 'say' but never finished the turn…") so a human knows exactly which session to poke
- tool docs: almost always use 'over'; 'say' only splits one long turn and must never end one
- test_chat_flow.py +9 checks (42); all 19 suites pass

## [1.0.26] - 2026-10-06
Chats that don't stall: say+wait in one call, next steps, working status, Stop-hook guard
- chat_say(over/wrap) now WAITS for the reply and returns it as reply (wait=True default, timeout=120) — a turn is one call, removing the step models forgot (posting, saying 'I'll wait', then ending their turn)
- every chat_say/chat_await result carries next: one imperative line (reply now / keep waiting / you may stop); timeout notes explain that stopping strands the chat
- new status working: 'hold on, I'm doing a 20-minute task' — keeps the floor, the waiting side keeps waiting and sees the note (progress in results and chat_status), and the 'it's your turn' channel reminder is held back for up to an hour
- chat_await called when it's already your turn hands that turn straight back (already_received) instead of blocking on yourself; newer human/plain messages still win
- new discordinator chat-guard: a Claude Code Stop hook that blocks a session from ending its turn mid-chat (with the exact next step); allows a repeat stop with no further chat activity, reminds again if the session keeps chatting and drops out again; reads the session's own transcript so two sessions in one directory are never confused; any error = allow
- new test_chat_flow.py (33) + test_chat_guard.py (30); all 19 suites pass

## [1.0.25] - 2026-10-06
Session handles: project/role names, no silent collisions + review fixes
- chatter is now a ROLE appended to the project's DISCORDINATOR_CHAT_HANDLE (chatter='ui' -> CodeCarver/ui), so two sessions in the same project can chat while staying recognizable; omit chatter when it's the project's only session; with no project handle, chatter is used as-is
- collision guard: each MCP session claims its handle in ~/.discordinator/handles.json; if another LIVE session on this machine holds it you get <handle>-2 and chat_begin returns a note — sessions can never silently share a name and ignore each other. Claims are released at exit; dead (pid gone) or day-old claims are reclaimed. Liveness is queried via the Win32 API, never os.kill (which terminates on Windows)
- across machines, give the project a distinct handle per machine in .mcp.json (e.g. CodeCarverWork)
- FIX (v1.0.24 regression): a 2-party reply taking 30+ minutes dropped the other party from the state, leaving no floor holder — the speaker an unaddressed owed turn replied to is now always kept
- FIX: retention no longer switches off for a room whose first record is undated/damaged (falls back to a full scan); FIX: addressing and from_whom use the same case-insensitive matching as everything else
- new test_handles.py (28, incl. real-process liveness) + 11 review checks; all 17 suites pass; handles.py 91%, chat.py 99%, local_client.py 94%

## [1.0.24] - 2026-10-06
Chat identity: one name per project, state scoped to the current chat
- chat state (chat_status, watch --state, TUI) now covers only the CURRENT chat — after the last end/impasse or human stop — and drops participants silent 30+ minutes (both ends of an owed turn are kept), so stale handles from earlier chats no longer linger in the rotation
- handles are case-insensitive (Convex = convex) everywhere: participants, floor, your_turn, self-filtering in chat_await, and the per-handle cursor slot
- new DISCORDINATOR_CHAT_HANDLE (set per project in .mcp.json): chat_* calls may omit chatter and get the project's fixed handle, so a project never appears under several names; an explicit chatter still overrides (needed when two sessions of the same project chat)
- viewers label the non-floor ranking 'others' instead of 'waiting' — it's a fairness order, not a list of sessions blocked in chat_await
- UPGRADE: per-handle chat cursors are now keyed case-insensitively, so a chat in flight during the upgrade re-reads from the last 20 messages once; add DISCORDINATOR_CHAT_HANDLE to each project's .mcp.json and /mcp reconnect
- new test_chat_identity.py (23 checks); all 16 suites pass; .mcp.json.example also fixed (stale DISCORDINATOR_TRANSPORT hint)

## [1.0.23] - 2026-10-05
Local retention: local rooms keep 7 days by default
- local-room messages older than local_retention_days (default 7) are dropped, with their stored attachments, the next time the room is written to — sessions never need to clean up; 0 = keep forever
- set with config set-local-retention <days> or DISCORDINATOR_LOCAL_RETENTION_DAYS; invalid values raise a ConfigError naming the fix; version shows it when a mode is local
- pruning reads only the first line in the common case and rewrites only once the oldest record is 10% past the window, so a busy room rewrites ~once per tenth of the window, not per message; best-effort on I/O errors
- new test_local_retention.py (25 checks); all 15 suites pass

## [1.0.22] - 2026-10-05
Local viewers: blank line between messages
- watch (single room, --all, --follow) and the TUI now put a blank line before each timestamped message for readability; multi-line messages stay grouped

## [1.0.21] - 2026-10-02
Transport is now explicit per mode — no base, no fallback (BREAKING)
- removed the single `transport` config key, the `DISCORDINATOR_TRANSPORT` env var, and the `config set-transport` command: there is no longer a shared base that silently sets both modes
- `relay_transport` and `chat_transport` must each be set explicitly (config or DISCORDINATOR_RELAY_TRANSPORT / DISCORDINATOR_CHAT_TRANSPORT); an unset mode raises a ConfigError naming the exact `config set-<mode>-transport` fix instead of defaulting
- MIGRATION: a config that only had `transport: X` must now set `relay_transport: X` and `chat_transport: X`. `version` prints each mode's transport (or "(unset)")
- rationale: three transport keys with a hidden fallback was muddy, and a key that sets both modes at once was surprising; now each mode is exactly what the config says
- tests updated across the suite (local-mode tests set both per-mode env vars); test_split_transport (unset=error, explicit combos) + test_cli_split_transport (set-transport gone, unset errors) rewritten; all 14 suites pass; client_factory 100%, config 91% branch

## [1.0.20] - 2026-10-02
Per-mode transport: relay and chat can use different backends
- one session can now relay over Discord (to reach another machine) while chatting LOCALLY with a sibling session on the same box — previously transport was a single global switch for the whole process
- config relay_transport / chat_transport override the base transport per mode (env: DISCORDINATOR_RELAY_TRANSPORT / DISCORDINATOR_CHAT_TRANSPORT), each falling back to transport when unset; a token is only needed for whichever mode runs on Discord
- make_client(cfg, mode) builds the right backend per tool; relay tools pass "relay", chat tools "chat"; download_attachment picks its backend from the url shape (CDN link vs local path) so it works regardless of which mode delivered the file
- new CLI: config set-relay-transport / set-chat-transport; version prints the split when the two differ
- fully backward compatible (set only transport → both modes follow it); new test_split_transport.py (32) + test_cli_split_transport.py (12); client_factory 100%, config 91% branch; all 14 suites pass

## [1.0.19] - 2026-10-01
Attachment hardening (review follow-up) + CLI/MCP glue tests
- fix: same-named files in one message no longer collide — local store and the download dest de-duplicate (`name`, `name-1`, …) instead of silently overwriting (Discord already keyed by index)
- fix: the bot token is no longer sent to the Discord CDN host on attachment downloads
- fix: send_files([]) with no files is now a clear error instead of a silent success
- tests: new test_cli_attachments.py (14) + test_mcp_attachments.py (11) exercising the opt-in gates, send --file / read --download, send_file/download_attachment/chat_say(files) and config set-attachments; plus collision / empty-list / no-token-leak edge tests
- all 12 suites pass; chat.py 98% branch, core transports 88-93%

## [1.0.18] - 2026-10-01
Chat-turn attachments + local image dimensions
- chat_say(files=...) attaches files/images to a live chat turn (gated by send opt-in); they ride the turn's final message so floor/turn/addressing are untouched; ≤10 files/turn
- chat_await surfaces them in a new `attachments` field (and per-message in `messages`); human/out-of-band turns carry attachments too
- local transport fills in image width/height via Pillow when available (parity with Discord's server-side dims; Pillow stays optional)
- verified live on Discord (chat header + multipart in one message) and locally; chat.py 98% branch coverage, 92% overall

## [1.0.17] - 2026-10-01
Attachments: send/receive files and images (opt-in, off by default)
- send --file/--image + MCP send_file: multipart upload, images auto-embed inline; ≤10 files/msg (larger lists auto-batched), ~10MB/file, oversize/missing rejected up front
- read --download + MCP download_attachment: fetch attachments to disk; reads now parse rich attachment metadata {url,filename,content_type,size,width,height,is_image} (clean swap from bare url strings)
- two independent per-machine opt-ins, OFF by default: config set-attachments send|receive on (or DISCORDINATOR_ALLOW_SEND/RECEIVE); sending also needs the bot's Attach Files permission (permissions=101440)
- local transport parity: files copied under ~/.discordinator/local/files/<msg-id>/, same attachment shape on read, cleaned up on delete

## [1.0.16] - 2026-09-29
Fix TUI transcript truncating long turns (now wraps)
- RichLog min_width lowered so long chat turns wrap at the panel width instead of being clipped

## [1.0.15] - 2026-09-29
Textual TUI for local chats: discordinator tui
- full-screen live transcript + floor/waiting sidebar + input box (type=interject, /stop, /quit)
- optional extra: pip install -e .[tui]; command lazy-imports textual with a helpful hint if absent
- local-only front-end over watch + post_human; headless run_test() coverage

## [1.0.14] - 2026-09-29
Local-mode viewer: watch/interject/stop for observing and steering local chats
- watch [room|--all] --follow --state: live view with parsed chat turns (addressing/status) + derived floor/waiting/hands/suggest
- interject/stop write a human turn so a person can steer or halt a running local chat with no Discord UI (local-only)
- perf: append reads only the file tail for the next id; follow loops flush for piping

## [1.0.13] - 2026-09-29
Local (no-Discord) transport: same-machine relay + chat over files
- transport: discord|local (config set-transport / DISCORDINATOR_TRANSPORT); env > config > default 'discord'
- local mode needs no token/network/channels — rooms are JSONL under ~/.discordinator/local/ (relay->'relay', chat->'chat')
- both relay and chat work over either transport; same tools/protocol. Same-machine only; no human-in-channel affordances locally

## [1.0.12] - 2026-09-29
N-way chat: addressing, floor token, anti-starvation
- chat_say to= addresses a turn to one peer; a history-derived floor token wakes only the addressee
- status=ask raises a hand without taking the floor; chat_status/await/say surface floor, floor_requests, waiting, suggest_next
- auto-nudge names a starved hand-raiser and asks the holder to yield; 2-party behavior unchanged (omit to)

## [1.0.11] - 2026-09-28
Separate relay vs live-chat default channels
- chat_* tools default to DISCORDINATOR_CHAT_CHANNEL (a shared room); relay uses DISCORDINATOR_RELAY_CHANNEL
- stops live chats from landing on a per-project relay mailbox; both sides meet in the shared room with no channel arg
- renamed DISCORDINATOR_CHANNEL -> DISCORDINATOR_RELAY_CHANNEL; added config set-chat-channel

## [1.0.10] - 2026-09-27
chat_await auto-nudge for behavior-independent stall recovery
- waiting side posts one channel reminder after nudge_after (default 240s)
- names who is waited on; deduped; awaiters skip nudge messages

## [1.0.9] - 2026-09-27
Harden chat vs stuck-chat deadlock; add chat_status + recovery
- chat_await surfaces plain out-of-band replies (no more stranded awaiter)
- chat_status query + chat_begin recovery repositions onto an owed turn
- docs/chat-protocol-notes.md incl. 3+ chatter analysis

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
