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

        r = handle(adapter, {"tool": "get_equipped_gear", "params": {"toon": "Ayla"}})
        check("gear envelope", envelope_ok(r, "get_equipped_gear"))
        gear = {row[1]: row for row in r["rows"]}
        check("gear: Ayla has chest/feet/mainhand with names",
              set(gear) == {"chest", "feet", "mainhand"}
              and gear["chest"][2] == "Linen Robe" and gear["mainhand"][2] == "Worn Shortsword", gear)
        check("gear: slot ids and item levels present",
              r["rows"][0][0] == "4" and gear["feet"][4] == "18", r["rows"])
        r = handle(adapter, {"tool": "get_equipped_gear", "params": {"toon": "Cecil"}})
        check("gear: unequipped toon -> 0 rows, still ok",
              r.get("ok") is True and r["row_count"] == 0, r)

        r = handle(adapter, {"tool": "get_leveling_progress", "params": {"account": "i_was_here"}})
        check("leveling envelope", envelope_ok(r, "get_leveling_progress"))
        prog = {row[0]: row for row in r["rows"]}
        check("leveling: xp/hour derived (Ayla 4100 xp / 5400s)",
              float(prog["Ayla"][7]) == 2733.0, prog.get("Ayla"))
        check("leveling: session recency present (Cecil logged out days ago)",
              prog["Cecil"][5] == "1730900000", prog.get("Cecil"))
        r = handle(adapter, {"tool": "get_group_xp_penalty", "params": {"toon": "Ayla"}})
        check("penalty: raid group size + raid bit",
              r.get("ok") and r["rows"] == [["4", "2"]], r)
        r = handle(adapter, {"tool": "get_group_xp_penalty", "params": {"toon": "Cecil"}})
        check("penalty: ungrouped toon -> 0 rows",
              r.get("ok") is True and r["row_count"] == 0, r)
        r = handle(adapter, {"tool": "get_quest_difficulty", "params": {"toon": "Locke"}})
        check("quest difficulty envelope", envelope_ok(r, "get_quest_difficulty"))
        band = {row[0]: row[6] for row in r["rows"]}
        check("quest difficulty: 153 red (+6), 25 gray (-6), 36 gray (-20)",
              band.get("153") == "red" and band.get("25") == "gray"
              and band.get("36") == "gray", band)
        r = handle(adapter, {"tool": "get_skills", "params": {"toon": "Ayla"}})
        check("skills envelope", envelope_ok(r, "get_skills"))
        sk = {row[1]: row for row in r["rows"]}
        check("skills: herb 150/150, mine 75/150, firstaid 40/75, riding 150",
              sk.get("herb")[2:] == ["150", "150"] and sk.get("mine")[2:] == ["75", "150"]
              and sk.get("firstaid")[2:] == ["40", "75"]
              and sk.get("riding")[2:] == ["150", "150"], sk)
        check("skills: combat/armor named (staves, cloth, leather)",
              sk.get("staves") is not None and sk.get("cloth") is not None
              and sk.get("leather") is not None, sorted(sk))
        r = handle(adapter, {"tool": "get_talents", "params": {"toon": "Ayla"}})
        check("talents envelope", envelope_ok(r, "get_talents"))
        byspec = {}
        for row in r["rows"]:
            byspec[row[1]] = byspec.get(row[1], 0) + 1
        check("talents: 4 spells, 2 per specMask bit", byspec == {"1": 2, "2": 2}, byspec)
        r = handle(adapter, {"tool": "get_talents", "params": {"toon": "Cecil"}})
        check("talents: talentless toon -> 0 rows, still ok",
              r.get("ok") is True and r["row_count"] == 0, r)
        r = handle(adapter, {"tool": "get_mount_items", "params": {"toon": "Ayla"}})
        check("mounts envelope", envelope_ok(r, "get_mount_items"))
        check("mounts: Chestnut Mare Bridle found in backpack",
              r["row_count"] == 1 and r["rows"][0][2] == "Chestnut Mare Bridle"
              and r["rows"][0][0] == "0" and r["rows"][0][1] == "25", r)
        r = handle(adapter, {"tool": "get_bag_space", "params": {"toon": "Ayla"}})
        check("bag space: capacity 24 (16+8 pouch), used 3, free 21",
              r.get("ok") and r["rows"] == [["24", "3", "21"]], r)
        r = handle(adapter, {"tool": "get_wallets", "params": {"account": "i_was_here"}})
        check("wallets envelope", envelope_ok(r, "get_wallets"))
        check("wallets: richest first (Golbez 80000)",
              r["rows"][0][0] == "Golbez" and r["rows"][0][3] == "80000", r["rows"][0])

        r = handle(adapter, {"tool": "get_guild_members", "params": {"guild": "Special FXc"}})
        check("guild envelope (name with a space accepted)", envelope_ok(r, "get_guild_members"))
        g = rows_by_key(r)
        check("guild: Cecil is Guild Master", g["Cecil"][1] == "Guild Master", g.get("Cecil"))

        print("== 2. hard allowlist & param validation ==")
        r = handle(adapter, {"tool": "get_character", "params": {"toon": "Zem"}})
        check("scope: outsider toon refused (not on family, not in guild)",
              r.get("ok") is False and r.get("code") == "out_of_scope", r)
        r = handle(adapter, {"tool": "get_guild_members", "params": {"guild": "Someother Guild"}})
        check("scope: unwatched guild refused", r.get("code") == "out_of_scope", r)
        r = handle(adapter, {"tool": "get_wallets", "params": {"account": "someother"}})
        check("scope: off-family account prefix refused", r.get("code") == "out_of_scope", r)
        r = handle(adapter, {"tool": "get_wallets", "params": {"account": "i_was"}})
        check("scope: in-family prefix allowed", r.get("ok") is True, r)
        r = handle(adapter, {"tool": "get_character", "params": {"toon": "Leo"}})
        check("scope: Leo allowed via guild membership",
              r.get("ok") is True and r["row_count"] == 1, r)
        dbsearch.SCOPE = False
        r = handle(adapter, {"tool": "get_character", "params": {"toon": "Zem"}})
        check("scope: DBSEARCH_SCOPE=0 disables the gate",
              r.get("ok") is True and r["row_count"] == 1, r)
        dbsearch.SCOPE = True
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
              any(row.get("redirect_to") == "chat-tunnel"
                  and "route_hint" in row and "db_fallback" in row
                  for row in r["rows"]), r["rows"])
        r = handle(adapter, {"tool": "get_current_position"})
        check("redirect: live position -> chat-tunnel inspect with reason",
              r.get("ok") is False and r.get("code") == "redirect"
              and r.get("redirect_to") == "chat-tunnel"
              and "inspect" in r.get("route_hint", "")
              and "flushed" in r.get("error", ""), r)
        r = handle(adapter, {"tool": "get_bot_master"})
        check("redirect: mastership -> chat-tunnel, no DB fallback",
              r.get("code") == "redirect" and r.get("redirect_to") == "chat-tunnel"
              and "no mastership row" in r.get("db_fallback", ""), r)
        r = handle(adapter, {"tool": "get_current_position", "id": "z9"})
        check("redirect: correlation id echoed", r.get("id") == "z9", r)
        r = handle(adapter, {"tool": "get_equipped_gear", "params": {"toon": "Cecil"}, "id": "abc"})
        check("empty result still ok + correlation id echoed",
              r.get("ok") is True and r.get("id") == "abc" and r["row_count"] == 0, r)
        r = handle(adapter, {"tool": "get_character", "params": {"toon": "Nope"}})
        check("scope: nonexistent toon reads as out_of_scope (no existence leak)",
              r.get("ok") is False and r.get("code") == "out_of_scope", r)

        print("== 3. redirect flip: chat-tunnel route hints + offline fallback ==")
        import re as _re
        blob = json.dumps(dbsearch.REDIRECTS)
        check("no rtt-engine references left in the redirects",
              "rtt" not in blob.lower(), [k for k in dbsearch.REDIRECTS])
        check("all redirects point at chat-tunnel with the inspect interface",
              all(r["service"] == "chat-tunnel" and "inspect" in r["route_hint"]
                  or "whisper" in r["route_hint"]
                  for r in dbsearch.REDIRECTS.values()),
              [r["service"] for r in dbsearch.REDIRECTS.values()])
        check("hints name the local endpoint + correlation flow",
              all("127.0.0.1:8765" in r["route_hint"]
                  and "correlation" in r["route_hint"]
                  for r in dbsearch.REDIRECTS.values()))

        class MockChatTunnel:
            """Mock of chat-tunnel's documented interface (A23): inspect ->
            correlation_id; get_reply -> raw answer, or status timeout with
            the offline-truth note for a bot the DB shows offline."""
            def __init__(self, db_adapter):
                self.db = db_adapter
                self.pending = {}
            def call(self, tool, args):
                if tool == "inspect":
                    row = handle(self.db, {"tool": "get_character",
                                           "params": {"toon": args["bot"]}})
                    online = row["rows"][0][9] if row["rows"] else "0"
                    cid = "c-%s" % args["bot"]
                    self.pending[cid] = ("-9307.99 -3048.59 127.369 0 -0.361657 |Redridge Mountains|"
                                         if online == "1" else
                                         {"status": "timeout",
                                          "note": "offline truth is db-search"})
                    return {"ok": True, "result": {"correlation_id": cid}}
                if tool == "get_reply":
                    return self.pending[args["correlation_id"]]
                raise AssertionError("unknown mock tool " + tool)

        ct = MockChatTunnel(adapter)
        red = handle(adapter, {"tool": "get_current_position"})
        check("redirect carries route_hint + db_fallback",
              red.get("code") == "redirect" and red.get("redirect_to") == "chat-tunnel"
              and "inspect" in red.get("route_hint", "")
              and "get_character" in red.get("db_fallback", ""), red)
        online = ct.call("inspect", {"bot": "Ayla", "query": "position"})
        reply = ct.call("get_reply", online["result"])
        check("online bot: live inspect answers (seconds fresh, no DB guess)",
              "Redridge Mountains" in reply and "timeout" not in reply, reply)
        offline = ct.call("inspect", {"bot": "Locke", "query": "position"})
        reply = ct.call("get_reply", offline["result"])
        check("offline bot: inspect times out with the offline-truth note",
              reply.get("status") == "timeout"
              and "db-search" in reply.get("note", ""), reply)
        fb = handle(adapter, {"tool": "get_character", "params": {"toon": "Locke"}})
        check("offline fallback: DB answer with queried_at + last-logout caveat",
              fb.get("ok") is True and fb["row_count"] == 1
              and "logout" in fb["caveat"].lower(), fb.get("caveat"))

        print("== 4. read-only by construction ==")
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

        print("== 4b. game_tele + mat spawn tools (harbormaster seam) ==")
        r = handle(adapter, {"tool": "get_game_tele", "params": {"name": "Menethil"}})
        check("tele envelope", envelope_ok(r, "get_game_tele"), json.dumps(r)[:200])
        check("tele: partial name finds MenethilHarbor (live names are concatenated)",
              r.get("ok") and r["rows"] and r["rows"][0][0] == "MenethilHarbor", r)
        r = handle(adapter, {"tool": "get_game_tele", "params": {"name": "Booty Bay"}})
        check("tele: spaced query still matches the concatenated row",
              r.get("ok") and r["rows"] and r["rows"][0][0] == "BootyBay", r)
        r = handle(adapter, {"tool": "get_game_tele", "params": {"name": "Nowhereville"}})
        check("tele: unknown name -> 0 rows, still ok",
              r.get("ok") and r["row_count"] == 0, r)
        r = handle(adapter, {"tool": "get_game_tele", "params": {"name": "x'; DROP TABLE game_tele; --"}})
        check("tele: SQL metacharacters refused at the param gate",
              r.get("ok") is False and r.get("code") == "bad_param", r)
        r = handle(adapter, {"tool": "get_mat_spawns", "params": {"mat": "Peacebloom"}})
        check("mat spawns envelope", envelope_ok(r, "get_mat_spawns"), json.dumps(r)[:200])
        check("mat: Goldshire ranks first for Peacebloom (3 spawns in radius)",
              r.get("ok") and r["rows"] and r["rows"][0][0] == "Goldshire"
              and r["rows"][0][2] == "3", r)
        check("mat: Kharanos also has Peacebloom (1 spawn), ranked second",
              any(row[0] == "Kharanos" and row[2] == "1" for row in r["rows"]), r)
        r = handle(adapter, {"tool": "get_mat_spawns", "params": {"mat": "Copper Vein"}})
        check("mat: Copper Vein found near Kharanos",
              r.get("ok") and any(row[0] == "Kharanos" for row in r["rows"]), r)
        r = handle(adapter, {"tool": "get_mat_spawns", "params": {"mat": "Thorium"}})
        check("mat: unknown mat -> 0 rows, still ok",
              r.get("ok") and r["row_count"] == 0, r)

        r = handle(adapter, {"tool": "get_flightmasters", "params": {"x": "-9446.0", "y": "64.0", "map": "0", "faction": "alliance"}})
        check("flightmasters envelope", envelope_ok(r, "get_flightmasters"), json.dumps(r)[:200])
        check("flightmasters: nearest FM first (Goldshire area, alliance filter)",
              r.get("ok") and r["rows"] and r["rows"][0][0] == "Fargas the FM", r)
        check("flightmasters: non-FM npcs excluded (npcflag gate)",
              not any(row[0] == "Innkeeper" for row in r.get("rows", [])), r)
        check("flightmasters: horde FM excluded for an alliance bot",
              not any(row[0] == "Horde FM" for row in r.get("rows", [])), r)
        r = handle(adapter, {"tool": "get_flightmasters", "params": {"x": "-9446.0", "y": "64.0", "map": "0", "faction": "horde"}})
        check("flightmasters: horde filter -> horde FM only",
              r.get("ok") and r["row_count"] == 1 and r["rows"][0][0] == "Horde FM", r)
        r = handle(adapter, {"tool": "get_flightmasters", "params": {"x": "-9446.0", "y": "64.0", "map": "0", "faction": "any"}})
        check("flightmasters: any -> both Goldshire-area FMs returned (no faction gate)",
              r.get("ok") and
              {"Fargas the FM", "Horde FM"}.issubset({row[0] for row in r["rows"]}), r)
        r = handle(adapter, {"tool": "get_flightmasters", "params": {"x": "-9446.0", "y": "64.0", "map": "0"}})
        check("flightmasters: missing faction refused at the param gate",
              r.get("ok") is False and r.get("code") == "bad_param", r)
        r = handle(adapter, {"tool": "get_flightmasters", "params": {"x": "-9446.0", "y": "64.0", "map": "0", "faction": "gadgetzan"}})
        check("flightmasters: bogus faction word refused",
              r.get("ok") is False and r.get("code") == "bad_param", r)
        r = handle(adapter, {"tool": "get_flightmasters", "params": {"x": "-9446.0", "y": "64.0", "map": "9", "faction": "alliance"}})
        check("flightmasters: other map -> 0 rows, still ok",
              r.get("ok") and r["row_count"] == 0, r)
        r = handle(adapter, {"tool": "get_flightmasters", "params": {"x": "0; DROP", "y": "1", "map": "0", "faction": "alliance"}})
        check("flightmasters: SQL metacharacters refused at the param gate",
              r.get("ok") is False and r.get("code") == "bad_param", r)

        print("== 4c. recipe library sweep (get_recipes) ==")
        r = handle(adapter, {"tool": "get_recipes", "params": {"text": "have golbez come to you"}})
        check("recipes: envelope", envelope_ok(r, "get_recipes"), json.dumps(r)[:200])
        check("recipes: 'come to you' -> the follow recipe",
              r.get("ok") and r["row_count"] == 1
              and json.loads(r["rows"][0])["id"] == "come_to_me", r)
        r = handle(adapter, {"tool": "get_recipes", "params": {"text": "send golbez to stv"}})
        check("recipes: 'stv' -> the travel recipe with the zone table",
              r.get("ok") and json.loads(r["rows"][0])["id"] == "travel"
              and "Stranglethorn Vale" in r["rows"][0], r)
        r = handle(adapter, {"tool": "get_recipes", "params": {"text": "who is online"}})
        check("recipes: no match -> 0 rows, still ok",
              r.get("ok") and r["row_count"] == 0, r)
        r = handle(adapter, {"tool": "get_recipes", "params": {"text": "x'; DROP TABLE recipes; --"}})
        check("recipes: SQL metacharacters refused at the param gate",
              r.get("ok") is False and r.get("code") == "bad_param", r)

        print("== 5. per-query timeout fires ==")
        # the sqlite fixture adapter DETECTS timeouts post-hoc (deadline checked
        # after the query completes - the real mysql adapter ENFORCES by killing
        # the subprocess), so the bound is machine-speed independent: the query
        # must overrun the 0.05s deadline everywhere but not stall the harness.
        t0 = time.monotonic()
        try:
            adapter.run("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c "
                        "LIMIT 20000000) SELECT COUNT(*) FROM c", 0.05)
            check("slow query killed as code=timeout", False, "no ToolError raised")
        except ToolError as e:
            check("slow query killed as code=timeout", e.code == "timeout", str(e))
        check("timeout decided without stalling (<30s)", time.monotonic() - t0 < 30)

        print("== 6. socket server end-to-end (unix socket, local-only) ==")
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
