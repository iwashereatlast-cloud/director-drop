# db-search (service 2/4)

Premade, read-only query catalog the director LLM calls as **typed tools**.
The LLM never writes SQL: it picks a tool name and passes validated params;
the service runs **one fixed, pre-reviewed SELECT** under the SELECT-only
MySQL identity and returns a JSON envelope that always carries `queried_at`
and a per-query staleness `caveat` (the roadmap's staleness contract).

## Guarantees

| guarantee | how |
|---|---|
| hard allowlist | the only callable names are the catalog + `catalog` (meta); anything else → `{"ok": false, "code": "unknown_tool"}` |
| no free-form SQL | client text never reaches the DB: every SQL statement is a static string in `dbsearch.py`; params are regex-validated (`^[A-Za-z][A-Za-z0-9_]{0,31}$`) before interpolation |
| read-only DB user | the mysql adapter runs under the same SELECT-only identity as director.py (`~/.my.cnf-director`); the harness additionally asserts every catalog statement is a single SELECT |
| per-query timeout | each tool declares its own; the mysql subprocess is killed at the deadline → `{"ok": false, "code": "timeout"}` |
| local-only | AF_UNIX socket `/tmp/db-search.sock` (mode `0600`); TCP mode refuses to bind anything but 127.0.0.1/::1 |

## Interface

```
{"tool": "get_group_membership", "params": {"toon": "Ayla"}, "id": "corr-42"}
→ {"ok": true, "tool": "get_group_membership", "id": "corr-42",
   "queried_at": "2026-11-09T10:11:12Z",
   "caveat": "group rows are written live ... async login/logout (A3) ...",
   "rows": [["2","0","1","Leo","Leo","80","5","1","0","0"], ...],
   "row_count": 4, "duration_ms": 12}
```

One JSON request per line, one JSON response per line. Rows are tab-split
arrays (same wire shape the mysql CLI produces); column order per tool below.

## Query catalog

| tool | params | answers | does NOT answer | staleness caveat (summary) |
|---|---|---|---|---|
| `get_online_census` | `account_like` (bare prefix, `%` auto-appended) | toons on a set of accounts: name, account, level, class, map, zone, DB `online`, guildid | AI/mastership (live `nc ?` probe, service 3); true login state | `online` is boot-reset and set async on login (A5); logins/logouts are queued (A3) — flags lag by tens of seconds |
| `get_group_membership` | `toon` | the party/raid of one toon: type bits, difficulty, leader, members with level/class/subgroup/flags/DB-online | battleground groups (not persisted); whisper range; AI master | rows are live-written on group events, but bot logins auto-join the leader's group (A6) and async add/remove (A3) leave brief staleness; online column is A5-stale |
| `get_character` | `toon` | account, level, race, class, last map/zone, money, totaltime, DB online | live position; gear; quests; AI | map/zone/money/totaltime are last-flushed values (logout/periodic save) — minutes stale; online per A5 |
| `get_account_links` | `account` (bare prefix) | which bot accounts are linked to a shell account (cross-account addaccount gate, A12) + creation times | whether any toon of those accounts is online/has AI | link rows only change via `.playerbots account link/unlink` — near-stable; staleness applies to the toons, not the rows |
| `get_quest_log` | `toon` | quest log rows: quest id, title (needs `acore_world.quest_template` SELECT grant), status (1=COMPLETE 3=INCOMPLETE 5=FAILED 6=REWARDED), explored | live progress inside an incomplete quest; whether rpg accepts it (needs a POI, A8) | status rows flush with the character save; INCOMPLETE progress can lag by minutes |
| `get_guild_members` | `guild` (exact, quote-free name) | one guild's members with rank, level, class, DB online | other guilds; guild bank; online-truth | membership/rank stable on change; online column is A5-stale |
### Redirects — names that deliberately do NOT go here

Reaching for one of these does not return `unknown_tool`; it answers
`{"ok": false, "code": "redirect", "redirect_to": ...}` with the owning
service and the reason, so the caller learns the routing rule instead of
guessing again (or falling back to a whisper-probe). They are also listed by
the `catalog` meta-tool.

| name | routed to | because |
|---|---|---|
| `get_current_position` | rtt-engine | live position is tick-time state; the DB only has the last flushed map/zone (`get_character`) |
| `get_live_status` | rtt-engine | live HP/mana/combat never lives in the DB |
| `get_bot_master` | rtt-engine | AI mastership is live session state (SetMaster at bot login); no DB row |
| `is_bot_responding` | rtt-engine | session liveness is proven by a live probe with correlation, never by DB flags (A5) |

Routing rule reminder (roadmap): anything this catalog answers must NEVER be
learned by whisper-probing; tick-sensitive things (live position, "does this
bot answer right now") belong to service 3 (rtt-engine).

## Run

```bash
# serverpc: real MySQL via the SELECT-only identity
DBSEARCH_ADAPTER=mysql python3 dbsearch.py serve      # /tmp/db-search.sock

# offline / fixture mode
DBSEARCH_ADAPTER=sqlite DBSEARCH_FIXTURE_DB=.../fixture_acore_characters.db \
  python3 dbsearch.py serve

python3 dbsearch.py query get_group_membership toon=Ayla
python3 dbsearch.py catalog
```

Env: `DBSEARCH_ADAPTER` (mysql|sqlite), `DBSEARCH_SOCKET`, `DBSEARCH_ADDR`
(host:port, loopback only), `DBSEARCH_MYCNF`, `DBSEARCH_FIXTURE_DB`,
`DBSEARCH_TIMEOUT` (fallback).

## Proof harness (no worldserver needed)

```bash
python3 harness.py     # 30 checks: 30 pass, 0 fail
```

Proves: every tool returns the expected fixture rows; every envelope carries
`queried_at` + its caveat; unknown tools / SQL-injection-shaped params /
missing params are refused; out-of-scope names redirect with a route + reason;
every catalog statement is a single SELECT; the fixture is byte-identical after
queries (no writes); the per-query timeout fires (`code=timeout`); the
unix-socket path works end-to-end at mode 0600.
The fixture (`fixtures/fixture.sql`) is a miniature of the four DBs with the
repo's roster lore — including deliberate staleness traps: Locke is
DB-offline but still in Leo's raid rows (A3); all four ritual accounts are
linked including `acct_bot5` (matching live 2026-11-10).

## Deploy (serverpc)

`db-search.service` is a `systemctl --user` unit in the leo/ pattern:

```bash
cp services/db-search/db-search.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now db-search
```
