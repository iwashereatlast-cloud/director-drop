#!/usr/bin/env python3
"""db-search - premade read-only query catalog for the director LLM (service 2/4).

The LLM never writes SQL. It picks a typed tool from the catalog below, the
service runs ONE fixed, pre-reviewed SELECT under the SELECT-only MySQL
identity (~/.my.cnf-director pattern, same as director.py), with a per-query
timeout, and returns a JSON envelope that always carries `queried_at` and a
per-query staleness `caveat` (the roadmap's staleness contract, A3/A5).

Interface: JSON over a local-only socket - one request per line, one response
per line. Default AF_UNIX /tmp/db-search.sock; set DBSEARCH_ADDR=host:port to
listen on 127.0.0.1 instead (never expose it - the socket IS the allowlist).

Request:  {"tool": "<name>", "params": {...}, "id": "optional-correlation"}
Response: {"ok": true, "tool": ..., "id": ..., "queried_at": ..., "caveat": ...,
           "rows": [...], "row_count": N, "duration_ms": M}
          {"ok": false, "tool": ..., "id": ..., "error": "...", "code": "..."}

Adapters:
  mysql    the real one (serverpc): mysql CLI with --defaults-file, SELECT-only
           grants, subprocess timeout = the tool's per-query timeout.
  sqlite   the proof harness one: same fixed SQL against a SQLite fixture DB
           (fixtures/fixture.sql), so every tool is proofable with no
           worldserver running.

Modes:
  python3 dbsearch.py serve                run the socket service (DBSEARCH_ADAPTER)
  python3 dbsearch.py query <tool> [k=v..]   one-shot query, print the envelope
  python3 dbsearch.py catalog             print the tool catalog as JSON
"""
import json, os, re, socket, socketserver, sqlite3, subprocess, sys, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ADAPTER = os.environ.get("DBSEARCH_ADAPTER", "mysql")
SOCKET_PATH = os.environ.get("DBSEARCH_SOCKET", "/tmp/db-search.sock")
ADDR = os.environ.get("DBSEARCH_ADDR", "")          # host:port -> TCP on 127.0.0.1 only
MYCNF = os.path.expanduser(os.environ.get("DBSEARCH_MYCNF", "~/.my.cnf-director"))
FIXTURE_DB = os.environ.get("DBSEARCH_FIXTURE_DB", os.path.join(HERE, "fixtures", "fixture.db"))
DEFAULT_TIMEOUT = float(os.environ.get("DBSEARCH_TIMEOUT", "10"))

# ---------------------------------------------------------------- params ----
# Every interpolable param is allowlisted per tool AND regex-validated here;
# there is no code path that puts client text into SQL unvalidated.
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")

def _p(tool, params, key, required=True):
    v = (params or {}).get(key)
    if v is None or v == "":
        if required:
            raise ToolError("missing param '%s'" % key, "bad_param")
        return None
    v = str(v)
    if not NAME_RE.fullmatch(v):
        raise ToolError("param '%s' must match %s (got %r)" % (key, NAME_RE.pattern, v[:40]), "bad_param")
    return v

# --------------------------------------------------------------- catalog ----
# sql: ONE static SELECT. Placeholders {x} are filled only from _p()-validated
# values. can/cannot feed the catalog docs (README + `catalog` tool).
CATALOG = {

  "get_online_census": {
    "sql": ("SELECT c.name, a.username AS account, c.level, c.class, c.map, "
            "c.zone, c.online, gm.guildid "
            "FROM acore_characters.characters c "
            "JOIN acore_auth.account a ON a.id = c.account "
            "LEFT JOIN acore_characters.guild_member gm ON gm.guid = c.guid "
            "WHERE a.username LIKE '{account_like}' "
            "ORDER BY a.username, c.name"),
    "params": {"account_like": "required - account name prefix, e.g. i_was_here% is not allowed; pass the bare prefix (e.g. i_was_here)"},
    "timeout": 10,
    "can": "which toons exist on a set of accounts, their level/class/map/zone, the DB `online` flag, and guild membership",
    "cannot": "whether a bot actually has AI / a master right now (live nc? probe, service 3); whether an online=1 row is truly logged in (A5)",
    "caveat": "characters.online is reset to 0 at worldserver boot and set asynchronously on login (A5), and bot logins/logouts are queued async (A3): online flags can lag reality by tens of seconds either way. Treat online as a hint; live whisper probes are ground truth for session state.",
    "fill": lambda p: {"account_like": _p("get_online_census", p, "account_like") + "%"},
  },

  "get_group_membership": {
    "sql": ("SELECT g.groupType, g.difficulty, g.raidDifficulty, "
            "lc.name AS leader, c.name AS member, c.level, c.class, "
            "c.online, gm2.subgroup, gm2.memberFlags "
            "FROM acore_characters.group_member gm "
            "JOIN acore_characters.`groups` g ON g.guid = gm.guid "
            "JOIN acore_characters.characters lc ON lc.guid = g.leaderGuid "
            "JOIN acore_characters.group_member gm2 ON gm2.guid = g.guid "
            "JOIN acore_characters.characters c ON c.guid = gm2.memberGuid "
            "WHERE gm.memberGuid = "
            "(SELECT guid FROM acore_characters.characters WHERE name = '{toon}') "
            "ORDER BY gm2.subgroup, c.name"),
    "params": {"toon": "required - exact toon name, letters/digits/underscore"},
    "timeout": 10,
    "can": "the party/raid a toon belongs to: type (party/raid/bg bits), difficulty, leader, every member with level/class/subgroup/flags/DB-online",
    "cannot": "battleground groups (not persisted); whether a member is in whisper range; who the bot's AI master is",
    "caveat": "group rows are written live by the worldserver on group events, BUT fresh bot logins auto-join the leader's group (A6) until cleared, and async login/logout (A3) can leave rows briefly stale around bot add/remove. A row here is not proof a member is online right now (A5).",
    "fill": lambda p: {"toon": _p("get_group_membership", p, "toon")},
  },

  "get_character": {
    "sql": ("SELECT c.name, a.username AS account, c.level, c.race, c.class, "
            "c.map, c.zone, c.money, c.totaltime, c.online "
            "FROM acore_characters.characters c "
            "JOIN acore_auth.account a ON a.id = c.account "
            "WHERE c.name = '{toon}'"),
    "params": {"toon": "required - exact toon name"},
    "timeout": 10,
    "can": "account, level, race, class, last map/zone, money, total played time, DB online flag for one toon",
    "cannot": "live position (this is the last flushed value); gear/quest progress (separate tools); AI/mastership",
    "caveat": "map/zone/money/totaltime are flushed to the DB at logout or periodic save - they lag live state by minutes. online is boot-reset/async (A5). Do not use position data to reason about where a toon is RIGHT NOW.",
    "fill": lambda p: {"toon": _p("get_character", p, "toon")},
  },

  "get_account_links": {
    "sql": ("SELECT accountName, botAccount, createTime "
            "FROM acore_playerbots.playerbots_account_links "
            "WHERE accountName LIKE '{account_like}' OR botAccount LIKE '{account_like}' "
            "ORDER BY accountName, botAccount"),
    "params": {"account": "required - account name prefix to filter by (pass the bare prefix)"},
    "timeout": 10,
    "can": "which bot accounts are linked (cross-account addaccount is refused unless linked, A12); link creation times",
    "cannot": "whether any given toon of a linked account is online or has AI (that is census + live probe)",
    "caveat": "the link table only changes via .playerbots account link/unlink commands, so it is near-real-time stable; but a link existing says nothing about current sessions (A3/A5 staleness applies to the toons, not the rows).",
    "fill": lambda p: {"account_like": _p("get_account_links", p, "account") + "%"},
  },

  "get_quest_log": {
    "sql": ("SELECT cqs.quest, qt.LogTitle, cqs.status, cqs.explored "
            "FROM acore_characters.character_queststatus cqs "
            "LEFT JOIN acore_world.quest_template qt ON qt.ID = cqs.quest "
            "WHERE cqs.guid = (SELECT guid FROM acore_characters.characters WHERE name = '{toon}') "
            "ORDER BY cqs.status, cqs.quest"),
    "params": {"toon": "required - exact toon name"},
    "timeout": 10,
    "can": "the toon's quest log rows with status codes (1=COMPLETE, 3=INCOMPLETE, 5=FAILED, 6=REWARDED) and titles when acore_world is granted",
    "cannot": "live progress within an incomplete quest (mob kill counts live in memory until save); whether the bot's rpg engine will accept it (needs a POI - A8)",
    "caveat": "quest status rows flush with the character save - an in-flight quest can be further along than this shows. REWARDED/FAILED rows are stable; INCOMPLETE progress can lag by minutes.",
    "fill": lambda p: {"toon": _p("get_quest_log", p, "toon")},
  },

  "get_guild_members": {
    "sql": ("SELECT c.name, g_r.rankName AS `rank`, c.level, c.class, c.online "
            "FROM acore_characters.guild g "
            "JOIN acore_characters.guild_member gm ON gm.guildid = g.guildid "
            "JOIN acore_characters.characters c ON c.guid = gm.guid "
            "JOIN acore_characters.`guild_rank` g_r ON g_r.guildid = g.guildid AND g_r.rid = gm.rank "
            "WHERE g.name = '{guild}' ORDER BY c.name"),
    "params": {"guild": "required - exact guild name (single word; quote-free by repo rule)"},
    "timeout": 10,
    "can": "every member of one guild with rank, level, class, DB online flag",
    "cannot": "guilds other than the one named; guild bank contents; online-truth (A5)",
    "caveat": "guild membership/rank rows persist on change and are stable, but the online column is the boot-reset/async flag (A5) - a member shown online may have crashed without a logout write.",
    "fill": lambda p: {"guild": _p("get_guild_members", p, "guild")},
  },
}

META_TOOLS = ("catalog",)

class ToolError(Exception):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code

# ------------------------------------------------------------- adapters ----
class MysqlAdapter:
    """Real adapter: mysql CLI under the SELECT-only identity (director.py pattern)."""
    def __init__(self, mycnf):
        self.mycnf = mycnf
        if not os.path.exists(mycnf):
            raise SystemExit("db-search: no MySQL defaults file at %s (DBSEARCH_MYCNF)" % mycnf)

    def run(self, sql, timeout):
        env = dict(os.environ)
        env.pop("MYSQL_PWD", None)
        try:
            out = subprocess.run(
                ["mysql", "--defaults-file=" + self.mycnf, "-N", "-B", "-e", sql],
                capture_output=True, text=True, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ToolError("query timed out after %ss" % timeout, "timeout")
        except FileNotFoundError:
            raise ToolError("mysql client not on PATH", "backend")
        if out.returncode != 0:
            raise ToolError(out.stderr.strip()[:200] or "mysql failed", "backend")
        return out.stdout.strip()

class SqliteAdapter:
    """Harness adapter: the SAME fixed SQL against SQLite fixture DBs that
    mirror the server's separate databases (acore_characters / acore_auth /
    acore_playerbots / acore_world) via ATTACH, so the catalog SQL runs
    unmodified."""
    SCHEMAS = ("acore_characters", "acore_auth", "acore_playerbots", "acore_world")

    def __init__(self, path):
        self.path = path   # fixture dir: fixture_acore_<schema>..db siblings

    def run(self, sql, timeout):
        deadline = time.monotonic() + timeout
        base = os.path.dirname(self.path)
        con = sqlite3.connect(":memory:")
        try:
            for s in self.SCHEMAS:
                con.execute("ATTACH DATABASE ? AS %s" % s,
                            (os.path.join(base, "fixture_%s.db" % s),))
            cur = con.execute(sql)
            rows = cur.fetchall()
            if time.monotonic() > deadline:
                raise ToolError("query timed out after %ss" % timeout, "timeout")
            return "\n".join("\t".join("" if v is None else str(v) for v in r) for r in rows)
        finally:
            con.close()

def make_adapter():
    if ADAPTER == "sqlite":
        return SqliteAdapter(FIXTURE_DB)
    return MysqlAdapter(MYCNF)

# --------------------------------------------------------------- engine ----
SELECT_ONLY_RE = re.compile(r"^\s*SELECT\b", re.IGNORECASE)

def assert_readonly_catalog():
    """Every registered statement is exactly one SELECT - checked at startup."""
    for name, spec in CATALOG.items():
        sql = spec["sql"]
        if not SELECT_ONLY_RE.match(sql) or ";" in sql[7:]:
            raise SystemExit("db-search: catalog entry %s is not a single SELECT" % name)

def run_tool(adapter, tool, params):
    """Returns the response dict (envelope). The ONLY path to the DB."""
    if tool == "catalog":
        return {"ok": True, "tool": "catalog",
                "queried_at": _now(),
                "caveat": "static catalog - no DB query, no staleness",
                "rows": [{"name": n, "params": s["params"], "timeout": s["timeout"],
                          "can": s["can"], "cannot": s["cannot"], "caveat": s["caveat"]}
                         for n, s in sorted(CATALOG.items())],
                "row_count": len(CATALOG), "duration_ms": 0}
    if tool not in CATALOG:
        raise ToolError("unknown tool %r - allowed: %s" %
                        (tool, ", ".join(sorted(CATALOG) + list(META_TOOLS))), "unknown_tool")
    spec = CATALOG[tool]
    values = spec["fill"](params or {})
    sql = spec["sql"]
    for k, v in values.items():
        sql = sql.replace("{%s}" % k, v)
    if "{" in sql:
        raise SystemExit("db-search: unfilled placeholder in %s" % tool)
    t0 = time.monotonic()
    raw = adapter.run(sql, spec["timeout"])
    ms = int((time.monotonic() - t0) * 1000)
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        rows.append(line.split("\t"))
    return {"ok": True, "tool": tool, "queried_at": _now(), "caveat": spec["caveat"],
            "rows": rows, "row_count": len(rows), "duration_ms": ms}

def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def handle(adapter, request):
    if not isinstance(request, dict) or not isinstance(request.get("tool"), str):
        return {"ok": False, "error": "request must be {\"tool\": ..., \"params\": {...}}", "code": "bad_request"}
    rid = request.get("id")
    try:
        resp = run_tool(adapter, request["tool"], request.get("params"))
    except ToolError as e:
        resp = {"ok": False, "error": str(e), "code": e.code}
    except Exception as e:                                   # noqa: BLE001
        resp = {"ok": False, "error": "internal error: %s" % e, "code": "internal"}
    if rid is not None:
        resp["id"] = rid
    return resp

# --------------------------------------------------------------- server ----
class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        adapter = self.server.adapter
        for line in self.rfile:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                resp = {"ok": False, "error": "request is not JSON", "code": "bad_request"}
            else:
                resp = handle(adapter, req)
            self.wfile.write((json.dumps(resp) + "\n").encode())

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

def serve():
    assert_readonly_catalog()
    adapter = make_adapter()
    if ADDR:
        host, _, port = ADDR.partition(":")
        if host not in ("", "127.0.0.1", "localhost", "::1"):
            raise SystemExit("db-search: refusing to bind %r - local-only by contract" % host)
        srv = Server(("127.0.0.1", int(port or 8765)), Handler)
        where = "127.0.0.1:%s" % srv.server_address[1]
    else:
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        class UnixServer(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
        srv = UnixServer(SOCKET_PATH, Handler)
        os.chmod(SOCKET_PATH, 0o600)
        where = SOCKET_PATH
    srv.adapter = adapter
    sys.stderr.write("db-search: serving %d tools on %s (adapter=%s)\n" %
                     (len(CATALOG) + len(META_TOOLS), where, ADAPTER))
    srv.serve_forever()

def one_shot(tool, kv):
    adapter = make_adapter()
    params = {}
    for item in kv:
        k, _, v = item.partition("=")
        params[k] = v
    print(json.dumps(handle(adapter, {"tool": tool, "params": params}), indent=2))

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if mode == "serve":
        serve()
    elif mode == "query":
        one_shot(sys.argv[2], sys.argv[3:])
    elif mode == "catalog":
        print(json.dumps(run_tool(None, "catalog", {}), indent=2))
    else:
        raise SystemExit(__doc__)

if __name__ == "__main__":
    main()
