# rtt-engine ("8.8.8.8") — service 3 of 4

Real-time / **non-polled** query service. Owns the tick-time-sensitive
surface: LIVE reads of ONLINE bots through the **stock mod-playerbots
command server** (`Playerbots.CommandServerPort`, default 8888, read-only
line protocol `<verb>,<guid>\n` per `PlayerbotCommandServer.cpp` — zero
module changes). Everything the DB can answer belongs to
`services/db-search/` (service 2) and is **refused here** with a
`route_hint` — the routing rule is structural, not a convention.

**The old console-credential path is end-of-life** (decision 2026-11-09,
`docs/services-roadmap.md`): no console credentials anywhere, no
`executeCommand` envelope, no credential side-file. The command server is
the only real-time backend; no in-core command bus will be built.

## ⚠ Security

The module's command server has **no authentication and binds 0.0.0.0**.
This service must talk to **127.0.0.1:8888 only** (`RTT_PBHOST` — do not
point it at anything else), and the operator **must firewall port 8888
from the outside** (e.g. `sudo ufw deny 8888/tcp` or an equivalent
firewall rule on the host's external interface).

## Stable interface — `rtt-engine/1`

Transport: **unix socket** (default `/tmp/rtt-engine.sock`, mode 0600,
local-only). One JSON request per line → one JSON response per line.

```json
// request
{"op": "pinfo", "name": "Vivi"}
// response envelope — identical keys for every op
{"interface": "rtt-engine/1", "ok": true, "op": "pinfo", "backend": "pbcmd",
 "queried_at": "2026-11-09T06:01:06Z", "latency_ms": 12,
 "result": {"name": "Vivi", "online": true, "guid": 9, "map": 1,
            "position": [-8925.06, 133.77, 83.33], "zone": "Dun Morogh",
            "reply": "Vivi position (live): map 1 at ... in Dun Morogh"},
 "error": null}
```

`error` is `null` on success, else `{"code", "message", "route_hint"?}`.
Codes: `unknown_op`, `wrong_service`, `bad_params`, `backend_unavailable`,
`backend_error`.

### Op catalog (tick-time-sensitive only)

| op | params | answers | why it routes HERE (not db-search) |
|---|---|---|---|
| `catalog` | — | this list + the refused set | — |
| `ping` | — | command-server liveness + latency | health of the live path |
| `pinfo` | `name` | LIVE position of an ONLINE bot (map/zone/xyz) | the DB only has the LAST LOGOUT position; `online` resets at worldserver boot (assumption A5) |

**Refused:**

- DB-answerable names (`location-static`, `quest`, `money`, `skills`,
  `bags`, `played`, `raid`, `census`) → `route_hint: "db-search"`.
- `kick` → `route_hint: "chat-tunnel"` — `.playerbots bot remove` covers
  registered bots (chat-tunnel owns command sends); force-kicking a non-bot
  plain session is an **accepted gap** (DB logout flush covers offline
  truth).

Unknown ops get the same `db-search` hint. Never live-probe for something
the DB already knows.

### Offline fallback

The command server answers **online bots only**. An offline bot, a
non-bot toon, or a name with no GUID resolves to
`{"online": false, "caveat": "... offline truth is db-search"}` — never an
error, and never a guess: db-search owns the offline truth (logout flushes
the final position to `characters`).

## Backend: pbcmd

`PbCmdBackend` resolves **name → GUID** through the read-only DB identity
(`RTT_MYCNF`, default `~/.my.cnf-director`; cached `RTT_GUID_TTL` seconds,
default 300), then sends `<verb>,<guid>\n` to the command server and reads
the reply line(s). Read-only verbs only (`position` today; `state`/`hp`
are available the same way if a caller ever needs them).

## Run / install

```bash
python3 services/rtt-engine/rtt_engine.py serve            # foreground
python3 services/rtt-engine/rtt_engine.py query '{"op":"ping"}'   # client
bash   services/rtt-engine/prove.sh                        # proof harness
```

Env knobs: `RTT_SOCK`, `RTT_PBHOST` (127.0.0.1 — keep it that way),
`RTT_PBPORT` (8888), `RTT_TIMEOUT`, `RTT_MYCNF`, `RTT_GUID_TTL`,
`RTT_GUID_SOURCE` (`mysql` | `fixture`), `RTT_GUID_FIXTURE`.

systemd (same pattern as `leo/`):

```bash
mkdir -p ~/.config/systemd/user
ln -sf ~/downloads/director-drop/services/rtt-engine/rtt-engine.service \
    ~/.config/systemd/user/rtt-engine.service
systemctl --user daemon-reload && systemctl --user enable --now rtt-engine
```

## Proof

`prove.sh` (no worldserver, no DB): a mock of the 8888 command server
(`fixtures/mock-cmdserver.py`, scripted replies) plus the REAL pbcmd
backend with fixture GUID lookups — envelope key-for-key unchanged from
`rtt-engine/1`, live position reads, offline/invalid-guid/no-GUID
fallback to db-search, `kick` → chat-tunnel with the accepted-gap message,
routing refusals, bad params, backend-down envelope, and the GUID-lookup
cache (second lookup served from cache; a fresh resolver sees the change).
