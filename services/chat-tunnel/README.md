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
| `get_reply` | `correlation_id` | `{sent: {...}, reply: {...}\|null}` |

`GET /health` returns the running config. Errors come back as
`{"ok": false, "error": "..."}`. If the FIFO write fails (no reader — Leo's
shell down), `send_command` returns `ok: false` and nothing is recorded.

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

## Deploy

Same `systemctl --user` pattern as leo/director:

```
ln -s $HOME/downloads/director-drop/services/chat-tunnel/chat-tunnel.service \
      $HOME/.config/systemd/user/chat-tunnel.service
systemctl --user daemon-reload
systemctl --user enable --now chat-tunnel
```
