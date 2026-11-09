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
| 3. rtt-engine ("8.8.8.8") | `services/rtt-engine/` | real-time / non-polled queries + commands; stable JSON interface, SOAP backend initially, swap for an in-core command bus later | **SHIPPED (mock-proofed)** — `rtt-engine/1` over unix socket; SOAP wrapped verbatim, seam ready for the in-core bus; live SOAP run pending on serverpc | `bash services/rtt-engine/prove.sh` (16 checks, no worldserver needed) | only for tick-time-sensitive data; anything the DB answers never routes here — refused with a `route_hint` to db-search (structural, in the op catalog) |
| 4. web-gui | `services/web-gui/` | read-only dashboard over 1–3: bot status table, live tunnel event feed, query catalog browser | NOT STARTED | — | LAST; trivial once the services expose JSON |

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
  error:{code, message, route_hint?}}`. Ops: `catalog`, `ping`,
  `server_info`, `pinfo`, `console` (allowlist-gated). DB-answerable op names
  are refused with `route_hint: "db-search"`. The envelope is identical for
  every backend (`soap` now, `cmdbus` later) — callers must not depend on
  `backend` beyond display.

## Order & gates

1. chat-tunnel ships with a proof that a reply is matched to the right
   command under replay (including a late/garbled line fixture).
2. db-search ships with the query catalog documented, each query proofed
   against a DB fixture.
3. rtt-engine ships with the SOAP backend wrapped and a backend-swap seam
   (interface unchanged).
4. web-gui starts only after 1–3 answer JSON.
