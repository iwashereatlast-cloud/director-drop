# rtt-engine ("8.8.8.8") — service 3 of 4

Real-time / **non-polled** query-and-command engine. This is the ONLY thing
in the stack that talks to the worldserver's live console surface; everything
the DB can answer belongs to `services/db-search/` (service 2) and is
**refused here** with a `route_hint` — the roadmap's routing rule is
structural, not a convention.

## Stable interface — `rtt-engine/1`

Transport: **unix socket** (default `/tmp/rtt-engine.sock`, mode 0600,
local-only). One JSON request per line → one JSON response per line.

```json
// request
{"op": "pinfo", "name": "Vivi"}
// response envelope — identical keys for every op and every backend
{"interface": "rtt-engine/1", "ok": true, "op": "pinfo", "backend": "soap",
 "queried_at": "2026-11-08T23:11:04Z", "latency_ms": 41,
 "result": {"name": "Vivi", "online": true, "map": 1, "zone": "Dun Morogh",
            "position": [-8925.06, 133.77, 83.33], "detail": "..."},
 "error": null}
```

`error` is `null` on success, else `{"code", "message", "route_hint"?}`.
Codes: `unknown_op`, `wrong_service`, `bad_params`, `backend_unavailable`,
`backend_error`, `refused_console`.

### Op catalog (tick-time-sensitive only)

| op | params | answers | why it routes HERE (not db-search) |
|---|---|---|---|
| `catalog` | — | this list + the refused set | — |
| `ping` | — | backend liveness + latency | health of the live path |
| `server_info` | — | online players, uptime, revision | changes every tick |
| `pinfo` | `name` | live online/map/zone/position of a toon | the DB only has the LAST LOGOUT position; `online` resets at worldserver boot (assumption A5) |
| `console` | `command` | allowlisted console passthrough | escape hatch for tick-sensitive console verbs |

**Refused** (→ `route_hint: "db-search"`): `location-static`, `quest`,
`money`, `skills`, `bags`, `played`, `raid`, `census` — the DB-answerable
names, mirroring director.py's `DB_QUERIES` catalog. Unknown ops get the same
treatment. Never whisper-probe or console-probe for something the DB knows.

`console` is prefix-gated by `RTT_CONSOLE_ALLOW` (default
`.pinfo .server info .group list .group revive` — the Console::Yes read verbs
we already use). Widen it only with a tracker note in
`docs/services-roadmap.md`.

## The backend seam

Every op funnels through one seam:

```python
class Backend:
    def execute(self, command, timeout) -> (ok, text)
    def health(self)                       -> (ok, detail)
```

- **`soap`** (default) — wraps the EXISTING SOAP path verbatim: same envelope,
  same `~/.soap-director` 3-line conf, same Basic-auth shape as
  `director.py soap_command` / `lib/botcmd/common.sh _soap_post`. Nothing
  else in the stack should touch SOAP after this — call rtt-engine instead.
- **`cmdbus`** — stub for the future **in-core command bus**. Same signature;
  answers `backend_unavailable: not wired yet`. When the core endpoint lands,
  only this class changes — the interface, the op catalog and every caller
  stay untouched.
- **`mock`** — scripted fixture backend (`fixtures/mock-responses.json`) so
  the harness runs without a worldserver.

Select with `RTT_BACKEND=soap|cmdbus|mock`.

## Run / install

```bash
python3 services/rtt-engine/rtt_engine.py serve            # foreground
python3 services/rtt-engine/rtt_engine.py query '{"op":"ping"}'   # client
bash   services/rtt-engine/prove.sh                        # proof harness
```

Env knobs: `RTT_SOCK`, `RTT_BACKEND`, `RTT_SOAPCNF` (default
`~/.soap-director`), `RTT_TIMEOUT`, `RTT_CONSOLE_ALLOW`,
`RTT_MOCK_FIXTURE`.

systemd (same pattern as `leo/`):

```bash
mkdir -p ~/.config/systemd/user
ln -sf ~/downloads/director-drop/services/rtt-engine/rtt-engine.service \
    ~/.config/systemd/user/rtt-engine.service
systemctl --user daemon-reload && systemctl --user enable --now rtt-engine
```

## Proof

`prove.sh` (no worldserver needed): mock-backend battery over the real
socket+client — envelope shape, catalog, tick-sensitive reads (`server_info`,
`pinfo` online/offline), console allowlist refusal (`.kick`), routing-rule
refusals (`raid`/`money` → `db-search`), bad params — then the **swap seam**:
`cmdbus` stub and unconfigured `soap` answer through the SAME envelope keys,
proving the backend swaps without touching the interface.
