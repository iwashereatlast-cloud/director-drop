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
- Local-only by default: `WEB_GUI_BIND` defaults to 127.0.0.1 (no login
  needed). Opt-in LAN exposure exists but is fenced — see
  **LAN bind & login** below.

## LAN bind & login

The GUI is reachable from the LAN only as a deliberate, fenced choice:

- `WEB_GUI_BIND=192.168.88.2` binds exactly that address;
  `WEB_GUI_BIND=lan` resolves the host's **current** LAN address at
  startup (UDP-connect trick, no packets sent), so moving the server to
  a new network keeps the GUI working with no config edit. The wildcard
  `0.0.0.0` is always refused — a specific address is required, so an
  accidental wide bind is impossible.
- Any non-loopback bind **requires** a login (`WEB_GUI_USER` /
  `WEB_GUI_PASS` — the service refuses to start without a password) and
  prints a prominent startup warning. `WEB_GUI_AUTH=off` on a
  non-loopback bind is refused.
- **The warning is honest: there is no TLS.** The username, password and
  session cookie cross the LAN in cleartext, and behind the login the
  query browser executes real DB reads. Anyone on the segment who can
  reach the port and get the password can read the roster data —
  the operator is responsible for firewalling the address/port
  (e.g. `sudo ufw allow from 192.168.88.0/24 to any port 8766` and deny
  the rest).
- **Recommended default:** keep `127.0.0.1` and reach the GUI from the
  laptop through an SSH tunnel (`ssh -N -L 8766:127.0.0.1:8766
  <user>@<server>`, then open http://127.0.0.1:8766/ locally) — same
  browser experience, encrypted, no extra attack surface.

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

`WEBGUI_PORT` (8766), `WEB_GUI_BIND` (127.0.0.1 — a specific address,
`lan` to auto-resolve the host's current LAN address, or the loopback
default; the wildcard `0.0.0.0` is ALWAYS refused), `WEB_GUI_USER`
(director) / `WEB_GUI_PASS` (required for any non-loopback bind),
`WEB_GUI_AUTH` (auto | on | off — `off` is refused on non-loopback
binds), `WEBGUI_TUNNEL` (http://127.0.0.1:8765), `WEBGUI_DBSEARCH_SOCK`
(/tmp/db-search.sock), `WEBGUI_RTT_SOCK` (/tmp/rtt-engine.sock),
`WEBGUI_CENSUS` (acct_ — account prefix for the bot table),
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
real GUI over HTTP. 44 checks: page + panels render; overview merges
census (with caveat) + both healths; the feed shows a sent command and
its reply **matched by correlation_id**; both catalogs list; a catalog
read executes and refusals (`unknown_tool`, injection-shaped params)
propagate; `pinfo` answers and offline is a soft answer; non-read rtt
ops are refused by the GUI (`gui_read_only`); with all three siblings
down every endpoint still answers 200 with `unreachable` states; the
wildcard bind is refused while a specific address (127.0.0.2) is
accepted; `lan` resolves the host's address and refuses to start
without a password; the startup warning fires (proved WITHOUT ever
listening on the LAN interface — the warning precedes the bind, so a
held address forces the exit); and the login flow is exercised
end-to-end on 127.0.0.2 (401 without a session, 401 on a wrong
password, cookie issued, cookie unlocks the API, logout invalidates,
`WEB_GUI_AUTH=off` refused on LAN binds). Exit 0 = all PASS.

## Deploy

Same `systemctl --user` pattern as the siblings:

```bash
ln -s $HOME/downloads/director-drop/services/web-gui/web-gui.service \
      $HOME/.config/systemd/user/web-gui.service
systemctl --user daemon-reload
systemctl --user enable --now web-gui
```

The unit ships loopback-only (the recommended default — reach it from a
laptop with `ssh -N -L 8766:127.0.0.1:8766 <user>@<server>`). To expose
it on the LAN instead, edit the unit's environment:

```ini
Environment=WEB_GUI_BIND=lan        # or a specific address, e.g. 192.168.88.2
Environment=WEB_GUI_USER=director
Environment=WEB_GUI_PASS=<password> # REQUIRED: the service refuses to start without it
```

and firewall the port to the trusted segment (see **LAN bind & login**).

Write actions (send a command from the GUI, bot add/remove, etc.) come
later — this service ships read-only.
