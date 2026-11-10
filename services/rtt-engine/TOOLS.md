# rtt-engine — function outline for the director brain

You are the brain; rtt-engine is your hands for **live, tick-time-sensitive
facts about ONLINE bots**. This file is the complete, prompt-ready catalog of
what it exposes. Anything not listed here is not yours to ask it for — the
service refuses with a `route_hint` telling you which service owns it.

Transport: unix socket `/tmp/rtt-engine.sock`, one JSON request per line →
one JSON response per line:

```
{"op": "get_live_status", "name": "Vivi"}
→ {"interface": "rtt-engine/1", "ok": true, "op": "get_live_status",
   "backend": "pbcmd", "queried_at": "2026-11-10T12:00:00Z", "latency_ms": 12,
   "result": {...}, "error": null}
```

`error` is `null` on success, else `{"code", "message", "route_hint"?}`.
Codes: `unknown_op`, `wrong_service` (with `route_hint`), `bad_params`,
`backend_unavailable` (command server down), `backend_error`.

## Functions

### `ping` — is the live path up?
- params: none
- returns: `{alive: bool, detail}`
- use first if live answers look wrong; `backend_unavailable` means the
  mod's command server (8888) is unreachable — fall back to db-search facts
  and say so, never guess.

### `pinfo` — live position of one bot
- params: `name` (toon name)
- returns ONLINE: `{name, online: true, guid, map, position: [x, y, z],
  zone, reply}`
- returns OFFLINE: `{name, online: false, caveat}` — never an error. The
  caveat names db-search as the offline truth (logout flushes the final
  position to `characters`).
- does NOT answer: anything about an offline toon (db-search
  `get_character` has the last flushed map/zone); gear, quests, money
  (db-search).

### `get_current_position` — alias of `pinfo`
- db-search redirects this name here (`get_character`'s map/zone is the
  last flushed position, minutes stale). Same params, same result.

### `get_live_status` — live state + position + HP of one bot
- params: `name`
- returns ONLINE: `{name, online: true, guid, state (e.g. "combat" /
  "non-combat"), hp_pct, map, position, zone, reply}`
- returns OFFLINE: same offline shape as `pinfo`.
- use for "is this bot fighting? hurt?" — HP is live, the DB never has it.

## Names this service REFUSES (learn the routing, don't retry)

| name you might reach for | route_hint | why |
|---|---|---|
| `quest`, `money`, `skills`, `bags`, `played`, `raid`, `census`, `location-static` | `db-search` | DB-answerable — never live-probe for what the DB knows |
| `kick` | `chat-tunnel` | `.playerbots bot remove` covers registered bots; force-kicking a non-bot plain session is an accepted gap |
| `get_bot_master` | `chat-tunnel` | mastership is live session state with no command-server verb; proven by a correlated whisper probe (an `nc ?` reply only goes to the bot's master) |
| `is_bot_responding` | `chat-tunnel` | whisper responsiveness needs a correlated probe; `pinfo` online is only the server-side pre-flight |

## Rules of use

1. Live reads are for ONLINE bots only. If `online` is false, stop — the
   offline truth is db-search.
2. Live position/status is a nice-to-have, never load-bearing for orders.
3. Never whisper-probe a bot to learn something this service or db-search
   can answer (the routing rule).
4. `backend` in the envelope is display metadata; never branch on it.
