# chat-tunnel (service 1 of 4)

Single owner of the Leo client chat log. Tails `/tmp/leo.log`, parses whisper
traffic (`[Name] [whisper]: text`, assumption A14) into structured events in
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
| `stats` | — | line counters + DB status tally (parse truth) |

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
`CHAT_TUNNEL_MAX_ROWS` (100000 hard cap), `CHAT_TUNNEL_DB`
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
`stats` parse truth (dropped-line count vs the raw log = the A14 check), and
an evidence-based `CHAT_TUNNEL_WINDOW` figure from measured reply latencies.
Exit 0 = every probe OK.

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
