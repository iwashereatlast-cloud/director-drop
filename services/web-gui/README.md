# web-gui (service 4 of 4)

Read-only, Cockpit-style dashboard over the three sibling services. One
stdlib-only HTTP server on 127.0.0.1:8766, no dependencies, no build step.

Panels (roadmap row 4):

| panel | source | what you see |
|---|---|---|
| **Bots** | db-search `get_online_census` | toon / account / level / class / map / zone / DB-online / guild for the census account prefix, with the per-query staleness caveat shown under the table; a **live** button per row asks rtt-engine `pinfo` for the current position of an online bot (offline truth stays db-search) |
| **Tunnel feed** | chat-tunnel `get_recent_events` | sent commands and matched replies, newest first, with correlation ids, status badges (pending/replied/timeout, matched/unmatched) and the raw line; polls every 5 s |
| **Query catalog browser** | db-search `catalog` + rtt-engine `catalog` | both catalogs side by side; pick a tool, fill its params, run it read-only, see the full envelope (rows, `queried_at`, staleness `caveat`) |

## Read-only by construction

- chat-tunnel's `send_command` is **never proxied** — command sends belong
  to chat-tunnel callers (director, botcmd), not the dashboard.
- rtt ops are allowlisted to `{catalog, ping, pinfo}` **inside the GUI**
  before the request leaves the process; anything else gets
  `{"code": "gui_read_only"}` (rtt-engine's own refusals are a second wall).
- db-search is read-only upstream (static SELECTs, SELECT-only identity);
  the GUI just passes tool + params through and renders the envelope.
- Local-only: `WEBGUI_ADDR` must be loopback, anything else refuses to
  start (same contract as db-search's TCP mode).

## Interface — `web-gui/1`

`GET /` serves the dashboard (self-contained HTML+JS, no external assets).
JSON API (all degrade to a per-backend `{"code": "unreachable"}` state —
HTTP 200, never a 5xx — when a sibling is down):

| route | method | body / query | returns |
|---|---|---|---|
| `/health` | GET | — | running config |
| `/api/overview` | GET | — | census rows + tunnel health + rtt ping, one snapshot |
| `/api/events` | GET | `limit`, `direction` (`sent`\|`reply`) | tunnel events, newest first |
| `/api/catalogs` | GET | — | db-search catalog + rtt-engine catalog |
| `/api/query` | POST | `{"tool", "params"}` | the db-search envelope (or its refusal) |
| `/api/rtt` | POST | `{"op", "name"?}` | the rtt-engine envelope, or `gui_read_only` |

## Env knobs (all optional)

`WEBGUI_PORT` (8766), `WEBGUI_ADDR` (127.0.0.1, loopback enforced),
`WEBGUI_TUNNEL` (http://127.0.0.1:8765), `WEBGUI_DBSEARCH_SOCK`
(/tmp/db-search.sock), `WEBGUI_RTT_SOCK` (/tmp/rtt-engine.sock),
`WEBGUI_CENSUS` (i_was_here — account prefix for the bot table),
`WEBGUI_TIMEOUT` (15 s per backend call).

## Run

```bash
python3 services/web-gui/web_gui.py serve
# then open http://127.0.0.1:8766/ on the host (local-only by contract)
```

## Proof (no worldserver, no DB, no 8888 server)

```bash
python3 services/web-gui/proof.py
```

Boots the three REAL sibling services in fixture mode (sqlite fixture,
temp log + FIFO, mock command server + GUID fixture), then drives the
real GUI over HTTP. 22 checks: page + panels render; overview merges
census (with caveat) + both healths; the feed shows a sent command and
its reply **matched by correlation_id**; both catalogs list; a catalog
read executes and refusals (`unknown_tool`, injection-shaped params)
propagate; `pinfo` answers and offline is a soft answer; non-read rtt
ops are refused by the GUI (`gui_read_only`); with all three siblings
down every endpoint still answers 200 with `unreachable` states; and a
non-loopback bind is refused at startup. Exit 0 = all PASS.

## Deploy

Same `systemctl --user` pattern as the siblings:

```bash
ln -s $HOME/downloads/director-drop/services/web-gui/web-gui.service \
      $HOME/.config/systemd/user/web-gui.service
systemctl --user daemon-reload
systemctl --user enable --now web-gui
```

Write actions (send a command from the GUI, bot add/remove, etc.) come
later — this service ships read-only.
