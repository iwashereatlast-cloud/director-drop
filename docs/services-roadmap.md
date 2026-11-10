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
| 2. db-search | `services/db-search/` | premade read-only query catalog (typed tools with "what it can/can't answer" contracts); every result carries `queried_at` + staleness caveat; hard allowlist, read-only DB user, per-query timeout | SHIPPED (PR #45 + #70, live-verified 2026-11-10) | `services/db-search/harness.py`: 36/36 offline (envelope+caveat, allowlist, injection refusal, single-SELECT audit, timeout, 0600 unix-socket e2e, client-hangup robustness); live run: census/group/character/redirects OK; pending RO grants for `acore_playerbots.*` + `acore_world.quest_template`, `guild_rank.rname` fixed post-live | free from a world-tick perspective; LLM never writes free-form SQL; out-of-scope names answer code=redirect to rtt-engine |
| 3. rtt-engine ("8.8.8.8") | `services/rtt-engine/` | real-time / non-polled queries + commands; stable JSON interface, SOAP backend initially, swap for an in-core command bus later | NOT STARTED | — | only for tick-time-sensitive data; anything the DB answers never routes here |
| 4. web-gui | `services/web-gui/` | read-only dashboard over 1–3: bot status table, live tunnel event feed, query catalog browser | NOT STARTED | — | LAST; trivial once the services expose JSON |

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
- **Send-liveness cross-check (chat-tunnel → rtt-engine, PROPOSED
  2026-11-10):** chat-tunnel's `liveprobe.py --rtt-sock` consumes the
  existing `rtt-engine/1` `pinfo` op as a per-bot pre-flight: a server-side
  online check on a path that bypasses the FIFO whisper transport. This
  separates "reachable but not answering" from "offline" without a new
  op. OPEN for rtt-engine's owner: a read-only chat verb on the command
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
- **rtt-engine interface (rtt-engine owns):** `rtt-engine/1` — unix socket
  (default `/tmp/rtt-engine.sock`), one JSON request per line → one envelope
  per line: `{interface, ok, op, backend, queried_at, latency_ms, result,
  error:{code, message, route_hint?}}`. Ops: `catalog`, `ping`, `pinfo`
  (live position of an ONLINE bot via the 8888 command server). Refused:
  DB-answerable names → `route_hint: "db-search"`; `kick` → `route_hint:
  "chat-tunnel"` (accepted gap). `backend` is always `pbcmd` — callers must
  not depend on it beyond display. Offline truth is db-search (logout
  flushes final position to `characters`).

## SOAP end-of-life decision (2026-11-09)

SOAP is removed completely — even a single command riding on it is one more
layer to break. Consequences, settled at tracker level:

- rtt-engine's real-time backend is the stock mod-playerbots command server
  (`Playerbots.CommandServerPort`, default 8888, read-only line protocol
  `<verb>,<guid>\n`, answers live position/state/HP for ONLINE bots only).
  No in-core command bus will be built.
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
3. rtt-engine ships with the pbcmd backend (stock 8888 command server) and
   the response envelope unchanged from the original `rtt-engine/1` shape —
   callers must not notice the backend swap (harness asserts it).
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
