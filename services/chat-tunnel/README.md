# chat-tunnel (service 1 of 4)

**Scope contract (operator, 2026-11-10): this tool's job is JUST
communication.** Five functions, nothing else:

1. **Talk to the bots** — `send_command` (paced, correlation IDs)
2. **Tell the LLM how to talk** — `describe` returns the command grammar
   (send shapes, channels, constraints, matching rule); the daemon bakes it
   into the LLM's system prompt at startup, so the grammar lives HERE, next
   to the code that enforces it — no hand-maintained prompt to drift
3. **Listen to the bots** — the tailer parsing the log into events
4. **Verify the conversation is not garbled, both directions** —
   `verify(correlation_id)` compares what the tunnel sent/parsed against
   `acore_auth.logs` (the server's own record, read-only, same MySQL
   identity as the stack): `delivered` / `garbled` (caught, with the
   expected line) / `not_found`. The one DB function that lives here —
   communication-only.
5. **Serve the WEB GUI table in real time** — `get_events_since` /
   `wait_for_events` (live-verified ~instant; well under the 10s budget)

**It never initiates.** The single world-affecting path is the FIFO write
inside `send_command`, reachable only from an explicit caller request. The
ONE background thread is the log sensor; retention is folded into the write
path — there is no timer thread. If the tunnel did something, a caller
asked it to.

Single owner of the Leo client chat log. Tails `/tmp/leo.log`, parses whisper
traffic (A14, confirmed live: chat rides a loguru envelope —
`... | MESSAGES | headless.world.chat.chat:display:124 - [Name] [whisper]: text` —
the parser anchors on the `display:` wrapper so DEBUG noise can never
false-match) into structured events in
SQLite (WAL, retention-purged so the DB never grows unbounded), sends commands
through the Leo FIFO (`/tmp/leo.in`) with a unique `correlation_id` per sent
command, and binds each bot reply to the exact command that triggered it.

## Reply matching (the point of this service)

A reply from bot B is bound to the OLDEST still-pending sent command whose
target is B and whose reply window has not lapsed — sender + time window,
never "the next line seen":

- a garbled/unparseable line is ignored and never consumes a pending match
- a reply past the window leaves the command `timeout` and is stored
  unmatched (no false binding)
- two commands to the same bot get their replies bound oldest-pending-first
- a reply never rebinds an already-`replied` command

Known, accepted limitation (by design of the sender+window contract): any
in-window line from bot B — including stale chatter — can claim B's pending
command. The window bounds the damage; the caller treats a mismatched reply
as a failed probe.

## Event schema (roadmap cross-service contract)

Every event carries `{id, ts, direction (sent|reply), sender, target, command,
correlation_id, raw_line}` plus service columns `channel`, `status`
(`pending|replied|timeout`, sent side) and `matched_correlation_id`
(reply side: the sent event it answered, `null` if unmatched).

## Local-only JSON interface

`POST http://127.0.0.1:8765/` with `{"tool": ..., "args": {...}}`:

| tool | args | result |
|---|---|---|
| `send_command` | `bot`, `cmd` | `{correlation_id, line, bot, cmd}` |
| `get_recent_events` | `limit` (<=1000), `direction` (`sent`\|`reply`) | `{events: [...]}` |
| `get_events_since` | `after_id`, `limit` | `{events: [...], last_id}` — cursor read |
| `wait_for_events` | `after_id`, `timeout` (<=25), `limit` | long-poll; blocks until new events or timeout |
| `get_reply` | `correlation_id` | `{sent: {...}, reply: {...}\|null}` |
| `verify` | `correlation_id` | both-direction garble check vs `acore_auth.logs`: `delivered` / `garbled` / `not_found` per side |
| `describe` | — | the command grammar (send shapes, channels, constraints, tools) |
| `stats` | — | line counters + DB status tally (parse truth) |

**Known limitation (A21, live-proven):** not every mod reply type passes the
server's chat logger — `who` replies land in `acore_auth.logs` (R:delivered),
but the strategy-listing reply to the `nc ?` probe never does (R:not_found is
EXPECTED for it; the mod delivers that reply type through a path the
chat_log hook never sees). The tunnel's own parse of the reply remains the
binding truth for matching; the DB check is corroboration where the mod lets
it exist.

`GET /grammar` serves the same grammar JSON to the web GUI. `verify` uses
`CHAT_TUNNEL_MYCNF` (default `~/.my.cnf-director`, read-only) and accepts a
`CHAT_TUNNEL_LOGDB_FIXTURE` JSON fixture so the proof harness runs the same
code path without a worldserver.

`GET /health` returns the running config. Errors come back as
`{"ok": false, "error": "..."}`. If the FIFO write fails (no reader — Leo's
shell down), `send_command` returns `ok: false` and nothing is recorded.

## Real-time reading (the web-GUI seam)

Service 4 (web-gui) gets a live "what Leo sees" feed with one connection,
no hot-polling, using the event id as a monotonic cursor:

1. `get_events_since(after_id=0)` — initial backlog, remember `last_id`
2. `wait_for_events(after_id=last_id, timeout=25)` — blocks server-side;
   returns the next batch ascending (or empty on timeout), then repeat

Events are ascending and gapless per cursor; retention-purged rows are
gone (a GUI that was away longer than the retention re-syncs from 0).
`stats()` gives parse health and pending counts for a status widget.

Master commands (`bot add`, `bot remove`) go out as `.playerbots ...` lines,
anything starting with `.` goes out verbatim, everything else as `/w <bot> <cmd>`
(same split director.py's `order_line()` makes).

## Env knobs (all optional)

`CHAT_TUNNEL_LOG` (/tmp/leo.log), `CHAT_TUNNEL_FIFO` (/tmp/leo.in),
`CHAT_TUNNEL_PORT` (8765), `CHAT_TUNNEL_WINDOW` (30 s),
`CHAT_TUNNEL_RETENTION` (24 h), `CHAT_TUNNEL_PURGE_EVERY` (60 s),
`CHAT_TUNNEL_PACE` (1.0 s min between FIFO writes — the client/server
throttles whispers, so bursts get dropped in-game; sends are serialized
and spaced), `CHAT_TUNNEL_MAX_ROWS` (100000 hard cap), `CHAT_TUNNEL_DB`
(`<repo>/services/chat-tunnel/tunnel.db`).

Single-owner: a `flock` on `<db>.lock` refuses a second instance (two tailers
would double-match every reply).

## Proof (no live worldserver)

```
python3 services/chat-tunnel/proof.py
```

Starts the real service against a temp fixture log + FIFO and drives it over
the JSON interface. 6 scenarios: basic binding, interleaved out-of-order
replies, the `fixtures/replay-corrupt.log.txt` garbled corpus, a late
past-window reply, ordered same-bot replies, and the retention purge.
Exit 0 = all PASS.

## Live acceptance (serverpc, leo online)

`liveprobe.py` is the self-contained live battery — it talks only to the
service's JSON interface (no botcmd, no daemon):

```
python3 services/chat-tunnel/liveprobe.py --bots Ayla,Locke --rounds 2
```

It tabulates every probe as OK / WRONG-SENDER / SILENT / PENDING, prints the
`stats` parse truth (`lines_garbled` = display lines that failed to parse,
the A14 check; `lines_noise` = DEBUG/loguru lines with no chat, expected),
and an evidence-based `CHAT_TUNNEL_WINDOW` figure from measured reply
latencies. Exit 0 = every probe OK.

Probe bots that are actually online and whispering (check the raw log or the
roster first) — a SILENT verdict for a bot that never appears in the log means
the bot never answered, not that the tunnel lost it. `nc ?` needs mastership
(A1); to separate a mastership problem from a transport problem, cross-check
with an unsecured command (A10) that every online bot answers regardless of
master: `liveprobe.py --cmd "who"`.

### Delivery receipts (the known blind spot)

The headless client does not log outgoing whispers, so a successful FIFO
write proves a reader existed — not that the whisper was spoken in-game.
To narrow that gap, `liveprobe.py --rtt-sock /tmp/rtt-engine.sock` runs a
pre-flight `pinfo` per bot through rtt-engine (service 3): a server-side
online check on a path that bypasses the FIFO entirely. A SILENT verdict
then reads either `server: online - not answering` (reachable, silent —
bot-side question) or `server: offline` (silence explained). Full send
confirmation (seeing the whisper arrive server-side) would need a chat
verb on the mod-playerbots command server — proposed in the roadmap's
cross-service contracts, rtt-engine's call to make.

### When we call it stable

1. **Parse truth:** a soak with bots active where `stats().lines_dropped`
   stays 0 while real whisper lines exist in the log (A14 PASS, pasted).
2. **Correlation:** liveprobe green (all OK) on at least 3 runs, including
   one run with a known-offline bot (its probe must come back SILENT/
   timeout, never falsely replied).
3. **Send path:** `send_command` succeeds with leo up and returns `ok: false`
   with leo stopped — no silent loss either way.
4. **Bounded growth:** after a full retention period, `db_rows` matches
   what retention predicts and the DB file size is stable.
5. **Real-time seam:** a `wait_for_events` loop runs for the soak duration
   without a missed or duplicated event (cursor never rewinds).

All five green across a multi-hour soak = stable; the roadmap row flips to
verified with the pasted evidence.

## Deploy

Same `systemctl --user` pattern as leo/director:

```
ln -s $HOME/downloads/director-drop/services/chat-tunnel/chat-tunnel.service \
      $HOME/.config/systemd/user/chat-tunnel.service
systemctl --user daemon-reload
systemctl --user enable --now chat-tunnel
```
