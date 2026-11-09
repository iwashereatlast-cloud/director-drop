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
| 1. chat-tunnel | `services/chat-tunnel/` | single owner of `/tmp/leo.log`: tailer → SQLite (WAL) with retention purge; sends commands with correlation IDs; matches replies to the command that triggered them (sender + window) — kills the stale-line false positive | SHIPPED (PR) | `services/chat-tunnel/proof.py`: 6/6 replay scenarios green without a live worldserver (basic bind, interleaved out-of-order replies, garbled-line corpus `fixtures/replay-corrupt.log.txt`, late past-window reply, ordered same-bot replies, retention purge) | build FIRST: ground-truth layer everything else verifies against |
| 2. db-search | `services/db-search/` | premade read-only query catalog (typed tools with "what it can/can't answer" contracts); every result carries `queried_at` + staleness caveat; hard allowlist, read-only DB user, per-query timeout | SHIPPED (PR #45) | `services/db-search/harness.py`: 30 pass / 0 fail offline (allowlist, static-SQL assertions, SELECT-only identity, per-tool timeout, staleness envelope, unix-socket e2e) | free from a world-tick perspective; LLM never writes free-form SQL |
| 3. rtt-engine ("8.8.8.8") | `services/rtt-engine/` | real-time / non-polled queries; stable JSON interface; backend = the stock mod-playerbots command server (8888, read-only `<verb>,<guid>\n`) | **SHIPPED (pbcmd)** — `rtt-engine/1` over unix socket, backend `pbcmd` (name→GUID via the RO DB identity, cached; `<verb>,<guid>\n` per PlayerbotCommandServer.cpp); ops `catalog`/`ping`/`pinfo`; `kick` refused → chat-tunnel (accepted gap); SOAP gone from the repo (this pass) | `bash services/rtt-engine/prove.sh` — 15 checks offline vs a mock 8888 server (envelope key-for-key unchanged, live position, offline/invalid-guid → db-search fallback, kick → chat-tunnel, GUID cache, backend-down envelope) | only for tick-time-sensitive ONLINE-bot data; anything the DB answers never routes here — refused with a `route_hint` (db-search / chat-tunnel), structural in the op catalog |
| 4. web-gui | `services/web-gui/` | read-only dashboard over 1–3: bot status table, live tunnel event feed, query catalog browser | SHIPPED (PR) | `services/web-gui/proof.py`: 30 pass / 0 fail offline — boots the real 1–3 in fixture mode (sqlite fixture, temp log+FIFO, mock 8888 + GUID fixture), then drives the real GUI over HTTP: page + panels, overview merge (census caveat surfaced, tunnel health, rtt ping), feed shows a sent command and its reply matched by correlation_id, both catalogs list, catalog read executes and refusals (unknown_tool, injection-shaped params) propagate, pinfo live/offline, non-read rtt ops refused by the GUI (gui_read_only), all-siblings-down → every endpoint answers 200 with unreachable states, non-loopback bind refused | read-only by construction: `send_command` never proxied, rtt ops allowlisted to {catalog,ping,pinfo} in-process, `web-gui/1` on 127.0.0.1:8766; tracker note: this pass also removed a dead `adapter = adapter` class-attr line in `dbsearch.py` serve() — it crashed unix-socket serve with NameError (class bodies don't close over function locals); `srv.adapter = adapter` after construction was already the only wiring the handler uses; db-search harness re-run 30/30 |

## Cross-service contracts

Contracts live here so a change is visible to every conversation:

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
