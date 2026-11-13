# web-gui (service 4 of 4)

Read-mostly, Cockpit-style dashboard over the sibling services. One
stdlib-only HTTP server on `127.0.0.1:8766`, no dependencies, no build
step. Interface `web-gui/2` (resynced 2026-11-13 to the post-rtt-engine
stack: live reads route through chat-tunnel `inspect`, A23).

Panels (roadmap row 4):

| panel | source | what you see |
|---|---|---|
| **Leo's chat — live** | chat-tunnel `get_events_since` + `wait_for_events` | exactly what Leo sees in chat — whisper, guild and inspect-system events with their raw display lines, long-polled (one request per batch, no hot-polling), newest at the bottom |
| **Bots** | db-search `get_online_census` + `get_character` + `get_group_membership`; chat-tunnel `inspect` | census table with the staleness caveat, **Leo's own row on top**, a group/raid badge per toon (Leo's group auto-filled; any toon on demand via the **group** button), and a **live** button per row that asks chat-tunnel `inspect(bot, "position")` for the position right now (offline truth stays db-search) |
| **Query catalog browser** | db-search `catalog` + chat-tunnel `describe` | the db-search catalog with read-only execution (full envelope incl. `queried_at` + staleness `caveat`), plus the chat-tunnel grammar as a display-only tab |
| **Mission drawer** | director.py `submit_orders` + the shared lease registry | click a toon's name: current mission, destination search (db-search `get_game_tele`) or mat search (`get_mat_spawns` → best teleport spots), then **travel there** / **farm there** / **level (grind cycle)** / **stand down** — every order goes through the mission engine's own validate/lease rails, two-step confirm |

Header: live status chips for chat-tunnel, db-search and **Leo's session**
(`systemctl --user is-active leo.service`) — with the Leo on/off button.

## Read-mostly by construction

- chat-tunnel's `send_command` is **never proxied** — command sends belong
  to chat-tunnel callers (director, botcmd), not the dashboard.
- Live reads go through chat-tunnel `inspect` with the GUI-side allowlist
  mirroring the tunnel's `INSPECT_QUERIES`; anything else gets
  `{"code": "gui_read_only"}` (the tunnel re-validates — second wall).
- db-search is read-only upstream (static SELECTs, SELECT-only identity,
  subject scope gate); the GUI just passes tool + params through and
  renders the envelope.
- **Write actions (deliberate, minimal, railed):**
  - the **Leo button**: `POST /api/leo {"action": "start"|"stop"}` runs
    `systemctl --user start|stop leo.service` (`WEBGUI_LEO_UNIT`).
  - the **mission drawer**: `POST /api/mission` submits ONE templated
    order (`travel/farm/walk <dest>`, `level` = the grind cycle,
    `standdown`) through director.py's `submit_orders` seam — the SAME
    expand/validate/lease/mission rails the LLM daemon uses. The GUI
    never builds a free-form cmd, and validate() re-resolves every
    destination (game_tele), re-checks census/online, and takes the
    shared lease. On a LAN bind both sit behind the login screen.
- The old single write action line:
  `POST /api/leo {"action": "start"|"stop"}` runs
  `systemctl --user start|stop leo.service` (`WEBGUI_LEO_UNIT`). On a LAN
  bind it sits behind the login screen; on loopback it is local-only by
  definition. `inspect` also sends a live-read command through the tunnel
  (correlated, allowlisted). Nothing else in the GUI writes anything.
- Local-only by default: `WEB_GUI_BIND` defaults to 127.0.0.1 (no login
  needed). Opt-in LAN exposure exists but is fenced — see
  **LAN bind & login** below.

## Interface — `web-gui/2`

`GET /` serves the dashboard (self-contained HTML+JS, no external assets).
JSON API (all degrade to a per-backend `{"code": "unreachable"}` state —
HTTP 200, never a 5xx — when a sibling is down):

| route | method | body / query | returns |
|---|---|---|---|
| `/health` | GET | — | running config |
| `/api/overview` | GET | — | census + Leo's row + Leo's group + tunnel health + Leo session status |
| `/api/events` | GET | `after_id`, `limit` | cursor read: events with id > after_id, ascending, plus `last_id` |
| `/api/events/wait` | GET | `after_id`, `timeout` (≤25), `limit` | long-poll: blocks until new events or timeout |
| `/api/reply` | GET | `cid` | the sent event + its matched reply (chat-tunnel `get_reply`) |
| `/api/live` | POST | `{"bot", "query"}` | chat-tunnel `inspect` (correlation_id; the reply lands in the feed / via `/api/reply`) |
| `/api/grammar` | GET | — | the chat-tunnel grammar (`describe`) |
| `/api/catalogs` | GET | — | the db-search catalog |
| `/api/query` | POST | `{"tool", "params"}` | the db-search envelope (or its refusal) |
| `/api/leo` | GET | — | Leo session status (`systemctl --user is-active`) |
| `/api/leo` | POST | `{"action": "start"\|"stop"}` | start/stop the Leo unit (the write action) |
| `/api/missions` | GET | — | the shared lease registry: what every bot is doing (kind, name, pid) |
| `/api/mission` | POST | `{"bot", "op", "dest"?}` | submit one mission order through director's rails (op: travel/farm/walk/level/standdown) |

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
  query browser executes real DB reads and the Leo button can stop the
  world session. Anyone on the segment who can reach the port and get
  the password can read the roster data — the operator is responsible
  for firewalling the address/port (e.g. `sudo ufw allow from
  192.168.88.0/24 to any port 8766` and deny the rest).
- **Recommended default:** keep `127.0.0.1` and reach the GUI from the
  laptop through an SSH tunnel (`ssh -N -L 8766:127.0.0.1:8766
  <user>@<server>`, then open http://127.0.0.1:8766/ locally) — same
  browser experience, encrypted, no extra attack surface.

## Env knobs (all optional)

`WEBGUI_PORT` (8766), `WEB_GUI_BIND` (127.0.0.1 — a specific address,
`lan` to auto-resolve the host's current LAN address, or the loopback
default; the wildcard `0.0.0.0` is ALWAYS refused), `WEB_GUI_USER`
(director) / `WEB_GUI_PASS` (required for any non-loopback bind),
`WEB_GUI_AUTH` (auto | on | off — `off` is refused on non-loopback
binds), `WEBGUI_TUNNEL` (http://127.0.0.1:8765), `WEBGUI_DBSEARCH_SOCK`
(/tmp/db-search.sock), `WEBGUI_LEO` (Leo — the shell toon: own table row
+ group), `WEBGUI_LEO_UNIT` (leo.service — the unit the Leo button
starts/stops), `WEBGUI_CENSUS` (i_was_here — account prefix for the bot
table), `WEBGUI_TIMEOUT` (15 s per backend call).

The mission drawer imports `director.py` (the mission owner) in-process
and uses the SAME env defaults as the daemon (`DIRECTOR_DBSEARCH_SOCK`,
`DIRECTOR_TUNNEL_URL`, `DIRECTOR_LEASES_FILE` — the shared lease
registry at `~/.director-leases.json`). Missions launched from the GUI
run as threads in the GUI process and send through the chat-tunnel
service; the shared lease file is what keeps the daemon and the GUI from
double-missioning a bot. If the GUI restarts mid-mission, the keeper
thread dies with it, the lease's dead pid is pruned on the next read, and
the bot's gather status lapses on its own (≤15 min) — nothing is
orphaned.

## Run

```bash
python3 services/web-gui/web_gui.py serve
# then open http://127.0.0.1:8766/ on the host (local-only by contract)
```

## Proof (no worldserver, no DB, no Leo session)

```bash
python3 services/web-gui/proof.py
```

Boots the REAL sibling services in fixture mode (sqlite fixture, temp
log + FIFO with a name allowlist so no MySQL is touched) and drives the
real GUI over HTTP. **65 checks**: page + panels render; overview merges
census (with caveat), tunnel health, Leo's character row, Leo's group
(the group/raid badge source) and the Leo session status; the live chat
seam — cursor reads ascending + gapless with a monotonic `last_id`, a
sent command bound to its whisper reply by correlation_id, and
`wait_for_events` actually long-polls; both catalogs list; a catalog
read executes and refusals (`unknown_tool`, injection-shaped params)
propagate; a live `inspect` issues a correlated command whose
`[System]` reply is matched back through `/api/reply`; non-allowlisted
live queries refused by the GUI (`gui_read_only`); the Leo button —
status graceful without systemd, unknown actions refused, start returns
a structured answer; with both siblings down every endpoint still
answers 200 with `unreachable` states; the mission drawer end-to-end — unknown ops refused, unknown destinations refused by validate with the SKIP reason back, the level grind cycle sent, a travel mission that takes the lease, confirms arrival from a live inspect reply AT the game_tele point and releases (and a dead-pid holder's lease is reclaimed, not blocking), a farm keeper that arms `rpg status do gather` and stands down releasing the lease; and the bind knob + login flow
(wildcard refused, 127.0.0.2 accepted, `lan` resolves, no-password LAN
bind refused, startup warning proved without ever listening on the LAN
interface, full login lifecycle on loopback). Exit 0 = all PASS.

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
