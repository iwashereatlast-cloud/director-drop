# Services roadmap — master tracker

**Scope decision (2026-11-08):** the stack stops layering fire-and-forget
whisper commands (the root cause behind #36/#37/#38/#41). Instead we build
four focused services the LLM calls as deterministic tools. One repo, four
services, each independently proofable. This file is the **master tracker**:
every services conversation updates its own row here, and cross-service
contract changes get confirmed here before shipping.

Shared rules for all four conversations:

- One repo (`director-drop`), one service per conversation. Stay in your
  service's directory; do not touch the others' code without a tracker note.
- Every service exposes JSON over a local-only interface (unix socket or
  127.0.0.1 port), with a proof harness that can run without a live
  worldserver (fixtures/replay).
- No behavioral assumption ships unconfirmed — new assumptions go into
  `docs/assumptions.md` per its rule, and `botcmd doctor` bundles are the
  evidence ticket.
- Deploy as `systemctl --user` units, same pattern as leo/director.

## Service table

| svc | dir | purpose | status | proof | notes |
|---|---|---|---|---|---|
| 1. chat-tunnel | `services/chat-tunnel/` | single owner of `/tmp/leo.log`: tailer → SQLite (WAL) with retention purge; sends commands with correlation IDs; matches replies to the command that triggered them (sender + window) — kills the stale-line false positive | SHIPPED (PR) | `services/chat-tunnel/proof.py`: 8/8 replay scenarios green without a live worldserver (basic bind, interleaved out-of-order replies, garbled-line corpus `fixtures/replay-corrupt.log.txt`, late past-window reply, ordered same-bot replies, retention purge, cursor read, long-poll); live gate = `liveprobe.py` (self-contained JSON battery) + `wait_for_events` long-poll seam for the web-gui feed | build FIRST: ground-truth layer everything else verifies against |
| 2. db-search | `services/db-search/` | premade read-only query catalog (typed tools with "what it can/can't answer" contracts); every result carries `queried_at` + staleness caveat; hard allowlist, read-only DB user, per-query timeout | SHIPPED + LIVE-VERIFIED 2026-11-10 (PRs #45/#70/#71/#73/#74/#76/#77) | `services/db-search/harness.py`: 62/62 offline; 15 tools + `catalog` meta + 4 redirects; live-verified: census, group, character, quest log w/ titles, links (deduped), guild w/ space, skills, redirects, socket e2e; leveling catalog (gear/KPIs/XP-penalty/quest-bands/skills/bags/mounts/wallets/talents) offline-proofed, gear+skills live-run pending post-#77; RO grants landed (`acore_playerbots.*`, `acore_world.quest_template`, `acore_world.item_template`); schema drifts fixed from live evidence (`guild_rank.rname`, `account_access.gmlevel`, `playerbots_account_links` id-pair, `character_talent` spell/specMask, no world talent tables) | free from a world-tick perspective; LLM never writes free-form SQL; out-of-scope names answer code=redirect; live-read redirects flipped from retired rtt-engine to chat-tunnel (A23) as route_hints with db_fallback + offline caveat (PR #91/#92); LIVE-PROVEN 2026-11-10: online inspect (Marle/Cyan, 0.0s latency, liveprobe 2/2), pending->poll reply flow, and the offline case (Cecil inspect timeout with the offline-truth note -> get_character fallback with queried_at + last-logout caveat); harness 76/76 on serverpc; A5 seen live both directions (Locke answered while flagged online=0, Cecil silent at online=0); ADDED 2026-11-10 (operator-approved, PR #101, harbormaster/farming seam): `get_game_tele` (named teleport locations - the only names legal in `.tele name`) + `get_mat_spawns` (mat -> ranked teleport locations by nearby herb/vein spawn density, GO type 3, 1000-yard radius); harness 85/85 offline; live gate: SELECT grants on `acore_world.gameobject`, `gameobject_template`, `game_tele` + schema spot-check |
| 3. rtt-engine ("8.8.8.8") | ~~`services/rtt-engine/`~~ (REMOVED) | was: real-time / non-polled queries over the stock 8888 command server | **RETIRED 2026-11-10 (operator decision)** — the 8888 registry serves RANDOM bots only (A19), while `.playerbots inspect`/`exec` (A23, live-proven, zero module changes) serves EVERY online bot with AI through the chat channel — the engine was one extra moving part with no unique reach. Service, harness, fixtures, unit file deleted (git history preserves them); live reads fold into chat-tunnel | retired live-verified: harness was 23/23; live battery ALL PASSED (ping, watch-list gate, routing rails, reach NOTEs) at retirement | all live reads (playerbot + RNDBOT) route through chat-tunnel via `.playerbots inspect <bot> <query>`; the 8888 port can be closed entirely (Playerbots.CommandServerPort=0) — its no-auth 0.0.0.0 bind was the stack's last unauthenticated listener |
| 4. web-gui | `services/web-gui/` | read-only dashboard over 1–3: bot status table, live tunnel event feed, query catalog browser | NOT STARTED | — | LAST; trivial once the services expose JSON |
| 5. LLM wiring (the brain) | `director.py` (rebuild in place) | wire the LLM daemon to the two services so it stops gathering facts itself: db-search for DB facts (staleness-marked), chat-tunnel for sends/replies/live reads (incl. `inspect`). The LLM is the master controller — every scheduled or prompted action goes through it; director.py executes its orders and relays service results. Old raw paths (`_mysql`, `_cmdserver_position`, direct census SQL) are DELETED in the same pass, not kept as fallback. Backend stays Ollama. Heartbeat + validated order pipeline contracts are preserved. | NOT STARTED (2026-11-10) | — | settled by owner: LLM holds tool decisions via JSON orders (no backend change); free hand to restructure director.py around the two services; behavior contracts (presence heartbeat, validated orders, service-control allowlist) must survive; proof must run against service fixtures/mocks without a live worldserver |

## Cross-service contracts

Contracts live here so a change is visible to every conversation:

- **Single-brain rule (all, IMPLEMENTED for presence 2026-11-10, operator's
  take):** the LLM is the ONLY decision-maker about game state — which bots
  exist, who is in what group, what gets whispered and when, AND Leo's own
  presence. Everything else is hands: they may gather facts, execute orders,
  and journal — never decide. Implemented: the leo watchdog is INFO-ONLY
  (journals state changes + alarms, never acts; if the daemon is down the
  body is down BY DESIGN and the watchdog journal says so). The daemon's
  presence thread gathers facts every ~30s and, when attention is needed
  (state change fires immediately; standing alarms re-notify at most every
  5 min), sends the LLM a PRESENCE HEARTBEAT: facts + the operator's standing
  policy + the ritual recipe + current group members. The LLM's orders flow
  through the SAME validated pipeline as human orders, plus a strict service
  order ({"service":"leo","cmd":"start|stop|restart"}). `bot addaccount` joined
  the LLM's master-cmd vocabulary so the ritual is expressible as orders.
  The old watchdog auto-ritual/group-clear (which re-added bots the LLM may
  have removed — the #38/#39 remaster ping-pong — and dissolved LLM-assembled
  groups) is gone. First catch: the acore_auth.logs transcript showed a
  10-bot `leave` batch (watchdog clear) — the chat-log audit trail is the
  instrument that detects brain-fights. Verified by tests/test_presence.py
  (heartbeat conditions + service-order allowlist, 12/12).
- **Event schema (chat-tunnel owns):** a tunnel event is
  `{id, ts, direction (sent|reply), sender, target, command, correlation_id, raw_line}`.
  `correlation_id` is unique per sent command; a reply is matched only within
  its window, never by "next line seen".
- **Staleness contract (db-search owns):** every query result embeds
  `queried_at` and a per-query `caveat` string stating what can have drifted
  since (e.g. group membership vs async login/logout, A3/A5).
- **Routing rule (all):** DB-answerable → db-search; tick-sensitive or
  non-polled → rtt-engine; never whisper-probe to learn something the DB
  already knows.
- **Live reads (chat-tunnel owns, CONFIRMED 2026-11-10, A23; rtt-engine
  retired the same day):** ALL live bot reads — playerbot AND random bot —
  route through chat-tunnel via `.playerbots inspect <bot> <query>` from
  Leo's session (SEC_GAMEMASTER, Console::Yes — in the module already,
  zero changes): state, position, tpos, movement, target, hp, strategy,
  action, values, travel, budget; the reply lands as
  `[System]: <bot>: <reply>` in leo.log, same raw shape the 8888 port
  used. `.playerbots exec <bot> <command>` is the state-changing sibling
  (replies go to the bot's master = Leo). ABSORPTION COMPLETE + LIVE-VERIFIED
  2026-11-10 (chat-tunnel, live run: liveprobe --inspect on Marle+Cyan, 2/2
  OK at 0.0s, positions bound to their own inspect commands, parse truth
  clean, S:delivered with system replies n/a per A21): (1) `inspect(bot, query)` tool with correlation
  IDs and `[System]` reply matching — only pending inspect commands are
  eligible, so whisper/system channels never cross-steal; `exec` rides the
  raw-dot passthrough and correlates as a whisper; (2) the watch-list gate =
  the roster allowlist (guild 'Special FXc' + bot accounts); (3) a timed-out
  inspect carries `note: offline truth is db-search` — never a guess;
  (4) 8888 fully retired — liveprobe's rtt pre-flight removed, and liveprobe
  gains `--inspect` (live-read battery through the inspect tool). Proof
  harness 12/12 incl. cross-channel isolation + the offline-fallback case.
  CONSUMER NOTES (owners, not this pass): db-search should flip its
  `get_current_position`/`get_live_status`/`get_bot_master`/
  `is_bot_responding` redirects from rtt-engine to chat-tunnel; web-gui
  should drop its rtt panel (catalog/ping/pinfo buttons, `/api/overview`
  rtt ping); chat-tunnel's liveprobe rtt pre-flight is REMOVED (absorbed
  2026-11-10 — replaced by `--inspect`, the live-read battery).
- **Send-liveness cross-check (OBSOLETE 2026-11-10 — rtt-engine retired;
  superseded by the confirmed chat-log delivery receipts):** the proposed
  `pinfo` pre-flight is gone with the service; `verify(correlation_id)`
  already separates "delivered but silent" (S:delivered, no reply — bot
  not answering) from "never sent" (S:not_found — transport down) against
  the server's own chat record. The rtt-sock idea is dead. OPEN for rtt-engine's owner: a read-only chat verb on the command
  server (see recent incoming whispers per GUID) would give chat-tunnel a
  true delivery receipt — but it needs a mod-playerbots change (new verb
  in `PlayerbotCommandServer.cpp`), so it needs an assumptions-row +
  evidence before anyone builds it. Not agreed yet; nothing depends on it.
- **Chat log in DB (db-search owns, CONFIRMED 2026-11-10):** AC core logs
  whispers natively into `acore_auth.logs` (AppenderDB writes via
  LoginDatabase — verified in core source `AppenderDB.cpp`). Live paste
  shows clean text, both directions, human and bot traffic, including Leo's
  OUTGOING whispers → chat-tunnel delivery receipts are now available.
  Required worldserver.conf recipe (all four, no module change):
  `ChatLog.Enable = 1` (the emitter gate — `chat_log.cpp` returns early
  without it; this was the silent blocker), `Appender.DB=3,5,0`,
  `Logger.chat.whisper=4,DB`, `Log.Async.Enable = 1` (writes off the world
  tick). Only whisper is routed to DB, so say/guild/yell never land there.
  Retention is core-provided: `LogDB.Opt.ClearTime` purges rows older than
  14 days every 10 min (tighten if needed). Still POLLED — a db-search
  catalog query with the staleness caveat, never a replacement for the
  tunnel's real-time feed. Encoding issue azerothcore#17519 did NOT
  reproduce (clean UTF-8 in the live rows). Caveat (A21, live-proven): NOT
  every mod-generated reply type passes the chat logger — `who` replies are
  logged, the strategy-listing reply is not; consumers must treat the
  transcript as partial for bot-originated traffic, never as proof a reply
  did not happen.
- **rtt-engine interface (RETIRED 2026-11-10 — service deleted; kept here as history):** `rtt-engine/1` — unix socket
  (default `/tmp/rtt-engine.sock`), one JSON request per line → one envelope
  per line: `{interface, ok, op, backend, queried_at, latency_ms, result,
  error:{code, message, route_hint?}}`. Ops: `catalog`, `ping`, `pinfo`
  (live position), `get_current_position` (alias — the name db-search
  redirects here), `get_live_status` (live state+position+HP). Refused:
  DB-answerable names → `route_hint: "db-search"`; `kick`,
  `get_bot_master`, `is_bot_responding` → `route_hint: "chat-tunnel"`
  (accepted gap / live-session facts the command server has no verb for —
  correlated whisper probes own them). `backend` is always `pbcmd` —
  callers must not depend on it beyond display. Offline truth is
  db-search (logout flushes final position to `characters`). Brain-facing
  outline: `services/rtt-engine/TOOLS.md`.

## SOAP end-of-life decision (2026-11-09)

SOAP is removed completely — even a single command riding on it is one more
layer to break. Consequences, settled at tracker level:

- rtt-engine's real-time backend is the stock mod-playerbots command server
  (`Playerbots.CommandServerPort`, default 8888, read-only line protocol
  `<verb>,<guid>\n`). REACH CORRECTED (live + source-pinned 2026-11-10,
  A19): the port forwards into `RandomPlayerbotMgr::HandleRemoteCommand`,
  so it serves ONLINE RANDOM bots only — master-owned playerbots answer
  `invalid guid` even with live AI. Guild-invited RNDBOTs are served (both
  on the watch list and in the registry); OUR playerbots fall back to
  db-search's last-flushed position. Extending the reach to playerbots is
  A22 (a mod change, OPEN). No in-core command bus will be built.
- `server_info` and generic `console` passthrough are deleted (nothing in
  the stack consumes them).
- `kick` is refused with `route_hint: "chat-tunnel"` — `.playerbots bot
  remove` covers registered bots; force-kicking a non-bot plain session is
  an accepted gap (DB logout flush covers offline truth).
- Live position of an online bot is a nice-to-have query, never load-bearing;
  offline truth is db-search (logout flushes final position to `characters`).
- Legacy SOAP paths outside rtt-engine (director.py soap_command, the
  location query's SOAP layer, lib/botcmd common.sh/system.sh/roster.sh,
  `botcmd system arm/soap/cmd`) are deleted in the same removal pass.

## Order & gates

1. chat-tunnel ships with a proof that a reply is matched to the right
   command under replay (including a late/garbled line fixture).
2. db-search ships with the query catalog documented, each query proofed
   against a DB fixture.
3. rtt-engine shipped (pbcmd backend, stable `rtt-engine/1` envelope,
   harness 23/23, live battery green) and was then RETIRED 2026-11-10 —
   its live-read lane folds into chat-tunnel via `.playerbots inspect`
   (A23). Gate met and closed.
4. web-gui starts only after 1–3 answer JSON.

## Evidence protocol update (2026-11-09)

`botcmd doctor` is RETIRED as the evidence protocol. It is a layer cake
(one command that silently runs probes, whispers, greps) — the exact
pattern this rebuild removes. Evidence now comes from the services
directly, one thing per call:

- bot behavior / replies: chat-tunnel `get_reply(correlation_id)`
- DB facts: db-search (staleness-marked)
- live position: rtt-engine `pinfo`

No session should suggest running doctor again. The confirm-with column in
`docs/assumptions.md` that still points at doctor is historical record,
not guidance.
