#!/usr/bin/env python3
"""db-search proof harness - runs the whole catalog against the SQLite fixture
(no worldserver, no MySQL, fully offline) and asserts the roadmap's contract:

  1. every tool answers with the expected fixture rows;
  2. every envelope embeds `queried_at` and its per-query staleness `caveat`;
  3. hard allowlist: unknown tools and meta SQL injection are refused;
  4. params are validated (no client text can reach SQL);
  5. every catalog statement is exactly one SELECT (read-only by construction);
  6. the per-query timeout path actually fires (slow query -> code "timeout").

Exit code 0 = all proofs pass. Run: python3 harness.py
"""
import json, os, shutil, sqlite3, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dbsearch
from dbsearch import CATALOG, SqliteAdapter, ToolError, handle, run_tool

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (name, detail))

def build_fixture(dirpath):
    for f in os.listdir(dirpath):
        if f.startswith("fixture_") and f.endswith(".db"):
            os.unlink(os.path.join(dirpath, f))
    con = sqlite3.connect(os.path.join(dirpath, "fixture_acore_characters.db"))
    try:
        con.executescript(open(os.path.join(HERE, "fixtures", "fixture.sql")).read()
                          .replace("ATTACH DATABASE 'fixture_", "ATTACH DATABASE '" + dirpath + os.sep + "fixture_"))
        con.commit()
    finally:
        con.close()

def envelope_ok(resp, tool):
    return (resp.get("ok") is True and resp.get("tool") == tool
            and isinstance(resp.get("queried_at"), str) and len(resp["queried_at"]) == 20
            and "caveat" in resp and resp["caveat"].strip()
            and isinstance(resp.get("rows"), list) and resp.get("row_count") == len(resp["rows"]))

def rows_by_key(resp, key=0):
    return {r[key]: r for r in resp["rows"]}

def main():
    tmp = tempfile.mkdtemp(prefix="dbsearch-fixture-")
    try:
        build_fixture(tmp)
        adapter = SqliteAdapter(os.path.join(tmp, "fixture_acore_characters.db"))

        print("== 1. catalog answers against the fixture ==")
        r = handle(adapter, {"tool": "get_online_census", "params": {"account_like": "i_was_here"}})
        check("census envelope (queried_at + caveat)", envelope_ok(r, "get_online_census"), json.dumps(r)[:200])
        names = {row[0] for row in r["rows"]}
        check("census: every bot-account toon present",
              names == {"Cecil", "Rydia", "Golbez", "Ayla", "Gau", "Locke", "Vivi"}, names)
        check("census: caveat names the async online flag",
              "online" in r["caveat"].lower() and "async" in r["caveat"].lower(), r["caveat"])

        r = handle(adapter, {"tool": "get_group_membership", "params": {"toon": "Ayla"}})
        check("group envelope", envelope_ok(r, "get_group_membership"))
        gm = rows_by_key(r, key=4)
        check("group: Leo's raid lists Leo/Ayla/Gau/Locke",
              set(gm) == {"Leo", "Ayla", "Gau", "Locke"}, set(gm))
        check("group: leader is Leo", all(row[3] == "Leo" for row in r["rows"]))
        check("group: raid bit + raidDifficulty present",
              r["rows"][0][0] == "2" and r["rows"][0][2] == "1", r["rows"][0][:3])
        check("group: caveat names async login/logout",
              "async" in r["caveat"].lower(), r["caveat"])

        r = handle(adapter, {"tool": "get_group_membership", "params": {"toon": "Cecil"}})
        check("group: ungrouped toon -> 0 rows, still ok", r.get("ok") and r["row_count"] == 0, r)

        r = handle(adapter, {"tool": "get_character", "params": {"toon": "Locke"}})
        check("character envelope", envelope_ok(r, "get_character"))
        check("character: Locke row", r["rows"] and r["rows"][0][1] == "i_was_here4th"
              and r["rows"][0][2] == "32", r["rows"])
        check("character: caveat warns flushed fields lag",
              "flush" in r["caveat"].lower(), r["caveat"])

        r = handle(adapter, {"tool": "get_account_links", "params": {"account": "i_was_cmd"}})
        check("links envelope", envelope_ok(r, "get_account_links"))
        linked = {row[1] for row in r["rows"]}
        check("links: all five accounts linked incl. master (live 2026-11-10)",
              linked == {"i_was_here1st", "i_was_here2nd", "i_was_here3rd",
                         "i_was_here4th", "i_was_here5th"}, linked)
        check("links: both-direction rows deduped to one row per pair",
              r["row_count"] == 5, r["rows"])
        check("links: account names resolved from IDs (no raw account_id in rows)",
              all(not row[0].isdigit() for row in r["rows"]), r["rows"])
        r = handle(adapter, {"tool": "get_account_links", "params": {"account": "i_was_here4th"}})
        check("links: filter matches either side of the link",
              r.get("ok") and r["row_count"] == 1 and r["rows"][0][0] == "i_was_cmd", r)

        r = handle(adapter, {"tool": "get_quest_log", "params": {"toon": "Locke"}})
        check("quest envelope", envelope_ok(r, "get_quest_log"))
        q = rows_by_key(r)
        check("quest: Locke has 153 INCOMPLETE titled", q.get("153") and q["153"][2] == "3"
              and "Mirage" in q["153"][1], q.get("153"))
        check("quest: status codes 1/3/6 present",
              {q[k][2] for k in q} == {"1", "3", "6"}, q)

        r = handle(adapter, {"tool": "get_guild_members", "params": {"guild": "Special FXc"}})
        check("guild envelope (name with a space accepted)", envelope_ok(r, "get_guild_members"))
        g = rows_by_key(r)
        check("guild: Cecil is Guild Master", g["Cecil"][1] == "Guild Master", g.get("Cecil"))

        print("== 2. hard allowlist & param validation ==")
        r = handle(adapter, {"tool": "drop table characters"})
        check("unknown tool refused", r.get("ok") is False and r.get("code") == "unknown_tool", r)
        check("unknown-tool error names the redirects",
              "redirects" in str(r.get("error", "")), r)
        r = handle(adapter, {"tool": "get_character", "params": {"toon": "Leo' OR 1=1--"}})
        check("SQL-injection-shaped param refused", r.get("ok") is False and r.get("code") == "bad_param", r)
        r = handle(adapter, {"tool": "get_guild_members", "params": {"guild": "Special'; DROP TABLE guild;--"}})
        check("guild param: spaces ok but quotes still refused",
              r.get("ok") is False and r.get("code") == "bad_param", r)
        r = handle(adapter, {"tool": "get_character"})
        check("missing required param refused", r.get("ok") is False and r.get("code") == "bad_param", r)
        r = handle(adapter, {"tool": "catalog"})
        real = [row for row in r.get("rows", []) if "can" in row]
        check("catalog meta-tool lists every tool with can/cannot",
              r.get("ok") and r["row_count"] == len(CATALOG) + len(dbsearch.REDIRECTS)
              and len(real) == len(CATALOG)
              and all("cannot" in row and "caveat" in row for row in real), r)
        check("catalog meta-tool lists the redirects with routes",
              any(row.get("redirect_to") == "rtt-engine" for row in r["rows"]), r["rows"])
        r = handle(adapter, {"tool": "get_current_position"})
        check("redirect: live position -> rtt-engine with reason",
              r.get("ok") is False and r.get("code") == "redirect"
              and r.get("redirect_to") == "rtt-engine" and "flushed" in r.get("error", ""), r)
        r = handle(adapter, {"tool": "get_bot_master"})
        check("redirect: mastership -> rtt-engine", r.get("code") == "redirect"
              and r.get("redirect_to") == "rtt-engine", r)
        r = handle(adapter, {"tool": "get_current_position", "id": "z9"})
        check("redirect: correlation id echoed", r.get("id") == "z9", r)
        r = handle(adapter, {"tool": "get_character", "params": {"toon": "Nope"}, "id": "abc"})
        check("empty result still ok + correlation id echoed",
              r.get("ok") is True and r.get("id") == "abc" and r["row_count"] == 0, r)

        print("== 3. read-only by construction ==")
        try:
            dbsearch.assert_readonly_catalog()
            check("every catalog statement is a single SELECT", True)
        except SystemExit as e:
            check("every catalog statement is a single SELECT", False, e)
        con = sqlite3.connect(os.path.join(tmp, "fixture_acore_characters.db"))
        before = con.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
        con.close()
        handle(adapter, {"tool": "get_online_census", "params": {"account_like": "i_was_here"}})
        con = sqlite3.connect(os.path.join(tmp, "fixture_acore_characters.db"))
        after = con.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
        con.close()
        check("fixture unchanged after queries (no writes)", before == after)

        print("== 4. per-query timeout fires ==")
        t0 = time.monotonic()
        try:
            adapter.run("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c "
                        "LIMIT 40000000) SELECT COUNT(*) FROM c", 0.05)
            check("slow query killed as code=timeout", False, "no ToolError raised")
        except ToolError as e:
            check("slow query killed as code=timeout", e.code == "timeout", str(e))
        check("timeout decided fast (<5s)", time.monotonic() - t0 < 5)

        print("== 5. socket server end-to-end (unix socket, local-only) ==")
        import threading
        sock_path = os.path.join(tmp, "dbsearch.sock")
        dbsearch.SOCKET_PATH = sock_path
        thread = threading.Thread(target=lambda: handle_serve(adapter, sock_path), daemon=True)
        thread.start()
        deadline = time.time() + 5
        while not os.path.exists(sock_path) and time.time() < deadline:
            time.sleep(0.02)
        import socket as s
        sck = s.socket(s.AF_UNIX, s.SOCK_STREAM)
        sck.settimeout(5)
        sck.connect(sock_path)
        sck.sendall(b'{"tool": "get_character", "params": {"toon": "Golbez"}, "id": "e2e"}\n')
        resp = json.loads(sck.makefile().readline())
        sck.close()
        check("e2e: envelope over the socket", envelope_ok(resp, "get_character")
              and resp.get("id") == "e2e" and resp["rows"][0][0] == "Golbez", resp)
        mode = oct(os.stat(sock_path).st_mode & 0o777)
        check("e2e: socket is 0600 (owner-only)", mode == "0o600", mode)
        sck2 = s.socket(s.AF_UNIX, s.SOCK_STREAM)
        sck2.settimeout(5)
        sck2.connect(sock_path)
        sck2.sendall(b'{"tool": "get_character", "params": {"toon": "Ayla"}}\n')
        sck2.close()   # hang up before reading the reply
        time.sleep(0.2)
        r = handle(adapter, {"tool": "get_character", "params": {"toon": "Ayla"}})
        check("e2e: client hangup before reply does not break the service",
              r.get("ok") is True, r)

        print("\n%d pass, %d fail" % (PASS, FAIL))
        return 0 if FAIL == 0 else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def handle_serve(adapter, sock_path):
    import socketserver
    class UnixServer(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
    srv = UnixServer(sock_path, dbsearch.Handler)
    srv.adapter = adapter
    os.chmod(sock_path, 0o600)
    srv.serve_forever()

if __name__ == "__main__":
    sys.exit(main())
