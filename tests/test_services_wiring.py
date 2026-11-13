#!/usr/bin/env python3
"""Service-wiring proof (LLM daemon -> db-search + chat-tunnel, roadmap row 5).

Runs the REAL services offline - db-search on the SQLite fixture DB (same
build as services/db-search/harness.py), chat-tunnel on a temp log + FIFO
(same pattern as services/chat-tunnel/proof.py) - then drives director.py's
wiring: census/masters/sender gates/group lookups (db-search), query digest
with queried_at + caveat, live strategies via inspect, the validated order
pipeline sending through the tunnel, and the presence fact gatherer.

Also asserts the raw paths are GONE: no _mysql, no mysql CLI, no 8888, no
direct FIFO/log access, no `tail` in director.py.

Run: python3 tests/test_services_wiring.py   (no worldserver needed)
"""
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "services", "db-search"))

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
        con.executescript(
            open(os.path.join(REPO, "services", "db-search", "fixtures", "fixture.sql")).read()
            .replace("ATTACH DATABASE 'fixture_",
                     "ATTACH DATABASE '" + dirpath + os.sep + "fixture_"))
        con.commit()
    finally:
        con.close()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_socket(path, tries=100):
    for _ in range(tries):
        if os.path.exists(path):
            try:
                s = socket.socket(socket.AF_UNIX)
                s.connect(path)
                s.close()
                return True
            except OSError:
                pass
        time.sleep(0.05)
    return False


def wait_http(url, tries=100):
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return True
        except Exception:
            time.sleep(0.05)
    return False


def main():
    tmp = tempfile.mkdtemp(prefix="director-wiring-")
    sock_path = os.path.join(tmp, "db-search.sock")
    log_path = os.path.join(tmp, "leo.log")
    fifo_path = os.path.join(tmp, "leo.in")
    tunnel_db = os.path.join(tmp, "tunnel.db")
    port = free_port()
    tunnel_url = "http://127.0.0.1:%d/" % port
    open(log_path, "w").close()
    os.mkfifo(fifo_path)

    # a reader for the FIFO: collects every line the tunnel writes in-game
    fifo_lines = []

    def fifo_reader():
        # a FIFO read returns EOF when the writer closes - reopen and keep
        # reading (each tunnel send is its own open/write/close)
        while True:
            fd = os.open(fifo_path, os.O_RDONLY)
            buf = b""
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    fifo_lines.append(line.decode())
            os.close(fd)

    threading.Thread(target=fifo_reader, daemon=True).start()

    build_fixture(tmp)

    db_env = dict(os.environ,
                  DBSEARCH_ADAPTER="sqlite",
                  DBSEARCH_FIXTURE_DB=os.path.join(tmp, "fixture_acore_characters.db"),
                  DBSEARCH_SOCKET=sock_path)
    db_proc = subprocess.Popen([sys.executable, "dbsearch.py", "serve"],
                               cwd=os.path.join(REPO, "services", "db-search"),
                               env=db_env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ct_env = dict(os.environ,
                  CHAT_TUNNEL_LOG=log_path,
                  CHAT_TUNNEL_FIFO=fifo_path,
                  CHAT_TUNNEL_PORT=str(port),
                  CHAT_TUNNEL_DB=tunnel_db,
                  CHAT_TUNNEL_ALLOW="Leo,Cecil,Rydia,Vivi,Ayla,Gau,Locke,Golbez",
                  CHAT_TUNNEL_PACE="0.05",
                  CHAT_TUNNEL_PURGE_EVERY="3600")
    ct_proc = subprocess.Popen([sys.executable, "chat_tunnel.py"],
                               cwd=os.path.join(REPO, "services", "chat-tunnel"),
                               env=ct_env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        check("db-search socket came up", wait_socket(sock_path))
        check("chat-tunnel HTTP came up", wait_http(tunnel_url + "health"))
        if FAIL:
            return finish()

        # director reads its service endpoints from env at import time
        os.environ["DIRECTOR_DBSEARCH_SOCK"] = sock_path
        os.environ["DIRECTOR_TUNNEL_URL"] = tunnel_url
        import director
        # the AUDIT constant must exist at MODULE level (a live run crashed on
        # this exact gap while the test's reassignment masked it)
        check("AUDIT constant defined at module level",
              isinstance(director.AUDIT, str) and director.AUDIT.endswith("audit.log"),
              getattr(director, "AUDIT", None))
        director.AUDIT = os.path.join(tmp, "audit.log")

        print("== 1. raw paths are gone from director.py ==")
        src = open(director.__file__).read()
        check("no _mysql / mysql CLI", "_mysql" not in src and "defaults-file" not in src)
        check("no 8888 / rtt remnants", "8888" not in src and "rtt" not in src)
        check("no direct FIFO/log access",
              "LEO_FIFO" not in src and "DIRECTOR_LOG" not in src)
        check("no log tailing", 'Popen(["tail"' not in src and '"-F"' not in src)
        check("no botcmd probe", '"states", "verify"' not in src and '"botcmd"' not in src)

        print("== 1b. the recipe library: slim prompt, keyword sweep, exact structures ==")
        rules = director.ORDER_RULES
        check("prompt is slim (the library carries the recipes)",
              len(rules) < 4000 and '"query":"recipes"' in rules, len(rules))
        check("prompt: recipes sweep is the task path",
              "attach query {\"query\":\"recipes\",\"text\":\"<the master's words>\"}" in rules)
        check("validate: go travel refused as a dead command",
              "DEAD" in str(director.validate({"bot": "Gau", "cmd": "go travel Booty Bay"}, {})))
        r1 = director.run_queries(
            [{"query": "recipes", "bot": "Gau", "text": "send golbez to stv"}],
            director.census(online_only=False))
        check("recipes sweep: 'send golbez to stv' loads the travel recipe",
              "RECIPE travel" in r1 and "travel <destination>" in r1
              and "Stranglethorn Vale -> Booty Bay" in r1 and "stv=Stranglethorn Vale" in r1, r1[:400])
        r2 = director.run_queries(
            [{"query": "recipes", "bot": "Gau", "text": "have golbez come to you"}],
            director.census(online_only=False))
        check("recipes sweep: 'come to you' loads the follow recipe",
              "RECIPE come_to_me" in r2 and '"cmd":"follow"' in r2
              and "NO coordinates needed" in r2, r2[:300])
        r3 = director.run_queries(
            [{"query": "recipes", "bot": "Gau", "text": "have gau farm peacebloom"}],
            director.census(online_only=False))
        check("recipes sweep: farming loads the farm order structure",
              "RECIPE farm_mat" in r3 and "STEP 3" in r3 and "farm <destination>" in r3
              and "rpg status do gather" not in r3, r3[:300])
        rt = director.run_queries(
            [{"query": "recipes", "bot": "Gau", "text": "have marle learn new skills from her class trainer"}],
            director.census(online_only=False))
        check("recipes sweep: training loads the wander-npc structure",
              "RECIPE train" in rt and "rpg status wander npc" in rt
              and "travel <destination>" in rt, rt[:300])
        rm = director.run_queries(
            [{"query": "recipes", "bot": "Gau", "text": "check marle's mail and send me the herbs"}],
            director.census(online_only=False))
        check("recipes sweep: mail loads the mailbox structure",
              "RECIPE mail" in rm and "mail ?" in rm and "mail take" in rm
              and "sendmail" in rm, rm[:300])
        r4 = director.run_queries(
            [{"query": "recipes", "bot": "Gau", "text": "who is online"}],
            director.census(online_only=False))
        check("recipes sweep: no match says so (fall back to core rules)",
              "no library match" in r4, r4)
        bots_c = director.census()
        err = director.validate({"bot": "Gau", "cmd": "go 12.3;abc"}, bots_c)
        check("validate: bare go x;y legs are mission-internal (refused)",
              "mission-internal" in str(err) and "travel" in str(err), err)
        err = director.validate({"bot": "Gau", "cmd": "go -14420.0;480.0"}, bots_c)
        check("validate: even well-formed bare go x;y is refused",
              "mission-internal" in str(err), err)
        err = director.validate({"bot": "Gau", "cmd": "go -14420.0;480.0;26.0"}, bots_c)
        check("validate: go x;y;z is refused too (mission-internal)",
              "mission-internal" in str(err), err)
        err = director.validate({"bot": "Gau", "cmd": "go Booty Bay"}, bots_c)
        check("validate: 'go <name>' is refused too (live 2026-11-11: it was "
              "sent as a no-op and the bot never moved)",
              "mission-internal" in str(err) and "farm" in str(err), err)
        o_sw = {"bot": "Ayla", "cmd": "travel SW"}
        err = director.validate(o_sw, bots_c)
        check("acronyms: 'SW' expands to Stormwind at validation (live "
              "2026-11-12: 'summon marle to SW' looped on the raw acronym)",
              err is None and o_sw["_walk"][1] == "Stormwind", (err, o_sw))
        o_if = {"bot": "Ayla", "cmd": "farm IF"}
        err = director.validate(o_if, bots_c)
        check("acronyms: 'IF' expands to Ironforge for farm orders too",
              err is None and o_if["_walk"][1] == "Ironforge", (err, o_if))
        err = director.validate({"bot": "Ayla", "cmd": "travel Nowhereville"}, bots_c)
        check("acronyms: unknown names still refuse (no false expansion)",
              "not a known location" in str(err), err)
        o_d = {"bot": "Ayla", "cmd": "travel Duskwoods"}
        err = director.validate(o_d, bots_c)
        check("resolution: colloquial 'Duskwoods' resolves (live 2026-11-12: "
              "'Have Crono level in Duskwoods' SKIPped on the plural)",
              err is None and o_d["_walk"][1] in ("Duskwood", "Darkshire"), (err, o_d))
        o_z = {"bot": "Ayla", "cmd": "travel dusk wood"}
        err = director.validate(o_z, bots_c)
        check("resolution: spaced zone speak resolves via the zone table",
              err is None and o_z["_walk"][1] in ("Duskwood", "Darkshire"), (err, o_z))
        o_f = {"bot": "Ayla", "cmd": "travel Booty"}
        err = director.validate(o_f, bots_c)
        check("resolution: partial names resolve via fuzzy candidates",
              err is None and o_f["_walk"][1] == "BootyBay", (err, o_f))
        o_fz = {"bot": "Ayla", "cmd": "farm Duskwoods"}
        err = director.validate(o_fz, bots_c)
        check("resolution: the chain covers farm orders too",
              err is None and o_fz["_walk"][1] in ("Duskwood", "Darkshire"), (err, o_fz))
        check("prompt: fewshot teaches gather -> skills query -> farm order",
              any("gather herbs" in m["content"] for m in director.FEWSHOT)
              and any("farm Booty Bay" in m["content"] for m in director.FEWSHOT),
              [m["content"][:60] for m in director.FEWSHOT])
        check("prompt: the gather fewshot mirrors the LIVE query-results turn",
              any(m["role"] == "user" and "DB query results" in m["content"]
                  for m in director.FEWSHOT),
              [m["content"][:40] for m in director.FEWSHOT])
        check("prompt: the Barrens grind fewshot uses travel, not raw go legs",
              any("travel TheCrossroads" in m["content"] for m in director.FEWSHOT)
              and not any("go -443" in m["content"] for m in director.FEWSHOT),
              [m["content"][:60] for m in director.FEWSHOT])
        check("prompt: fewshot teaches stv -> travel Booty Bay",
              any("send golbez to stv" in m["content"] for m in director.FEWSHOT)
              and any("travel Booty Bay" in m["content"] for m in director.FEWSHOT),
              [m["content"][:60] for m in director.FEWSHOT])
        r5 = director.run_queries(
            [{"query": "recipes", "bot": "Gau", "text": "have golbez go level in stv"}],
            director.census(online_only=False))
        check("recipes sweep: leveling loads travel + grind orders",
              "RECIPE level_in_zone" in r5 and "travel <the zone's table destination>" in r5
              and "nc +new rpg,+grind,-follow" in r5, r5[:300])
        dig = director.run_queries(
            [{"query": "dest", "bot": "Gau", "name": "Booty Bay"}],
            director.census(online_only=False))
        check("dest query returns the ready-made go x;y order",
              "go -" in dig and "pathfinding" in dig
              and "queried_at" in dig, dig)
        dig = director.run_queries(
            [{"query": "dest", "bot": "Gau", "name": "Nowhereville"}],
            director.census(online_only=False))
        check("dest query: unknown location says so",
              "no known teleport location" in dig, dig)

        print("== 1c. zone->destination translation table (A25) ==")
        check("translation table covers 30+ zones",
              len(director.ZONE_DESTINATIONS) >= 30,
              len(director.ZONE_DESTINATIONS))
        check("every zone has a destination + bracket",
              all(isinstance(d, tuple) and len(d) == 2 and d[0] and d[1]
                  for d in director.ZONE_DESTINATIONS.values()))
        check("abbreviations resolve to canonical places (zone or game_tele)",
              all(z in director.ZONE_DESTINATIONS
                  or director._tele_coords(z) is not None
                  for z in director.ZONE_ABBREVIATIONS.values()),
              [z for z in director.ZONE_ABBREVIATIONS.values()
               if z not in director.ZONE_DESTINATIONS
               and director._tele_coords(z) is None])
        check("library renders the full table (via the travel recipe)",
              all(d[0] in r1 for d in director.ZONE_DESTINATIONS.values()), r1[:200])
        check("library separates prompt data from prompt rules",
              "stv=Stranglethorn Vale" in r1
              and "Booty Bay" not in director.ORDER_RULES)

        print("== 1d. agent layers as recipes: quest-by-name, silent-bot, zone names ==")
        rq = director.run_queries(
            [{"query": "recipes", "bot": "Gau", "text": "send ayla after the quest claws from the deep"}],
            director.census(online_only=False))
        check("recipes sweep: quest-by-name loads the id-lookup structure",
              "RECIPE quest_by_name" in rq and "rpg do quest <id>" in rq
              and "NEVER invent quest ids" in rq, rq[:300])
        rs = director.run_queries(
            [{"query": "recipes", "bot": "Gau", "text": "cyan is silent and ignoring orders"}],
            director.census(online_only=False))
        check("recipes sweep: silent-bot loads the A20 procedure",
              "RECIPE silent_bot" in rs and "revive" in rs
              and "bot remove" in rs and "bot add" in rs, rs[:300])
        check("zone names sourced table present",
              director.ZONE_NAMES.get(46) == "Burning Steppes"
              and director.ZONE_NAMES.get(33) == "Stranglethorn Vale"
              and director.ZONE_NAMES.get(45) == "Arathi Highlands")
        check("map names table present",
              director.MAP_NAMES.get(0) == "Eastern Kingdoms")
        dig = director.run_queries([{"query": "location", "bot": "Gau"}],
                                   director.census(online_only=False))
        check("location digest names map + zone (Gau: fixture map 1, zone 14)",
              "Kalimdor" in dig and "Durotar" in dig, dig)
        check("roster names the map",
              "Eastern Kingdoms" in director.roster_text(director.census(online_only=False)))
        director._note_bot_error("Ayla", "Unknown spell")
        check("error counter bumps for the presence thread",
              director.BOTFB_SEQ[0] >= 1, director.BOTFB_SEQ)

        print("== 1e. harbormaster: routes, boat/carriage rails, mats + live queries ==")
        check("six shipping routes defined",
              len(director.TRAVEL_ROUTES) == 6, director.TRAVEL_ROUTES)
        harbors = sorted(set(h for r in director.TRAVEL_ROUTES for h in (r[0], r[1])))
        missing = [h for h in harbors if director._tele_coords(h) is None]
        check("every route harbor resolves in game_tele (db-search)",
              not missing, missing)
        rb = director.run_queries(
            [{"query": "recipes", "bot": "Gau", "text": "take the boat from booty bay to ratchet"}],
            director.census(online_only=False))
        check("recipes sweep: the boat recipe carries routes + narration rules",
              "RECIPE harbormaster_boat" in rb and "Booty Bay->Ratchet" in rb
              and "never mention teleporting" in rb, rb[:300])
        check("farming rule lives in the library, not the prompt",
              "RECIPE farm_mat" in r3 and "farm <mat>" not in director.ORDER_RULES)

        bots = director.census()
        # not at a dock (no live position at all -> SKIP, never a blind boat)
        err = director.validate({"bot": "Gau", "cmd": "boat Ratchet"}, bots)
        check("boat refused without a live position (no blind boarding)",
              "SKIP" in str(err) and "live position" in str(err), err)
        err = director.validate({"bot": "Ayla", "cmd": "boat Nowhereville"}, bots)
        check("boat refused to a non-route destination", "not an arrival harbor" in str(err), err)
        err = director.validate({"bot": "Leo", "cmd": "boat Undercity"}, bots)
        check("boat refused for faction (Alliance toon, Horde zeppelin)",
              "route" in str(err) and "SKIP" in str(err), err)
        err = director.validate({"bot": "Ayla", "cmd": "carriage Booty Bay"}, bots)
        check("carriage refused while DIRECTOR_CARRIAGE=0",
              "carriage is disabled" in str(err), err)

        # put Ayla at the Booty Bay dock: the position line must land AFTER
        # validate's inspect goes out (reply-window discipline), so a delayed
        # writer thread plays the fake worldserver mid-probe.
        def fake_dock():
            time.sleep(1.0)
            with open(log_path, "a") as f:
                f.write("2026-11-10 21:00:00.000 | MESSAGES | headless.world.chat.chat:display:124 - "
                        "[System]: Ayla: -14420.0 480.0 26.0 0 0.5 |Stranglethorn Vale|\n")

        threading.Thread(target=fake_dock, daemon=True).start()
        o = {"bot": "Ayla", "cmd": "boat Ratchet"}
        err = director.validate(o, bots)
        check("boat validates when the bot is at the departure dock",
              err is None and o["_route"] == ("Booty Bay", "Ratchet", "boat"), (err, o))
        check("boat order line is a GM .tele name",
              director.order_line("Ayla", "boat Ratchet") == ".tele name Ayla Ratchet",
              director.order_line("Ayla", "boat Ratchet"))
        check("every route harbor resolves against the concatenated fixture rows",
              all(director._tele_canonical(h) for h in harbors),
              [(h, director._tele_canonical(h)) for h in harbors])
        check("_tele_canonical returns the row's own spelling",
              director._tele_canonical("Booty Bay") == "BootyBay"
              and director._tele_canonical("menethil harbor") == "MenethilHarbor",
              director._tele_canonical("Booty Bay"))
        def fake_dock_send():
            time.sleep(1.0)
            with open(log_path, "a") as f:
                f.write("2026-11-10 21:00:30.000 | MESSAGES | headless.world.chat.chat:display:124 - "
                        "[System]: Ayla: -14420.0 480.0 26.0 0 0.5 |Stranglethorn Vale|\n")

        threading.Thread(target=fake_dock_send, daemon=True).start()
        sent = director.execute([{"bot": "Ayla", "cmd": "boat Ratchet"}], bots,
                                pace=0, dry=False)
        time.sleep(0.5)
        check("boat leg sent as .tele name via the tunnel",
              ".tele name Ayla Ratchet" in fifo_lines, fifo_lines[-3:])
        check("audit records it as a BOAT leg",
              "BOAT" in open(director.AUDIT).read()
              and ".tele name Ayla Ratchet" in open(director.AUDIT).read())

        def fake_dock2():
            time.sleep(1.0)
            with open(log_path, "a") as f:
                f.write("2026-11-10 21:01:00.000 | MESSAGES | headless.world.chat.chat:display:124 - "
                        "[System]: Ayla: -14420.0 480.0 26.0 0 0.5 |Stranglethorn Vale|\n")

        threading.Thread(target=fake_dock2, daemon=True).start()
        digest = director.run_queries(
            [{"query": "mats", "bot": "Ayla", "name": "Peacebloom"},
             {"query": "live", "bot": "Ayla"}],
            director.census(online_only=False))
        check("mats query ranks teleport locations by spawn density",
              "Goldshire" in digest and "spawns" in digest, digest)
        check("live query returns the real-time position",
              "live position" in digest and "-14420" in digest, digest)

        print("== 1f. travel mission: stand-down -> tele -> confirm -> restore (offline sim) ==")
        real_hop, real_tick, real_stall, real_max = (director.WALK_HOP,
                                                     director.WALK_TICK,
                                                     director.WALK_STALL,
                                                     director.WALK_MAX)
        director.WALK_HOP, director.WALK_TICK = 100.0, 0.3
        director.WALK_STALL, director.WALK_MAX = 40, 120.0
        # sim world: Ayla starts near Goldshire; the fake worldserver honors
        # Leo's '.tele name Ayla BootyBay' by jumping her to the fixture's
        # BootyBay tele point (-14420, 480); the mission then confirms the
        # arrival from a live position read and restores her.
        sim = {"x": -9446.0, "y": 64.0, "target": None}
        sim_state = {"arrivals": [], "teles": []}

        def sim_world():
            seen = len(fifo_lines)
            while not sim_state.get("done"):
                time.sleep(0.05)
                for line in fifo_lines[seen:]:
                    seen += 1
                    m = re.match(r"/w Ayla go ([-\d.]+);([-\d.]+)", line)
                    if m:
                        sim["target"] = (float(m.group(1)), float(m.group(2)))
                    m = re.match(r"\.tele name Ayla (\S+)$", line.strip())
                    if m and m.group(1) == "BootyBay":
                        sim_state["teles"].append(m.group(1))
                        sim["x"], sim["y"], sim["target"] = -14420.0, 480.0, None

        def sim_replier():
            seen = len(fifo_lines)
            while not sim_state.get("done"):
                time.sleep(0.05)
                for line in fifo_lines[seen:]:
                    seen += 1
                    if ".playerbots inspect Ayla position" not in line:
                        continue
                    if sim["target"]:
                        dx = sim["target"][0] - sim["x"]
                        dy = sim["target"][1] - sim["y"]
                        d = (dx * dx + dy * dy) ** 0.5
                        if d > 1:
                            sim["x"] += dx / d * min(80.0, d)
                            sim["y"] += dy / d * min(80.0, d)
                    with open(log_path, "a") as f:
                        f.write("2026-11-10 21:11:00.000 | MESSAGES | "
                                "headless.world.chat.chat:display:124 - "
                                "[System]: Ayla: %.1f %.1f 56.0 0 0.5 |Elwynn Forest|\n"
                                % (sim["x"], sim["y"]))

        threading.Thread(target=sim_world, daemon=True).start()
        threading.Thread(target=sim_replier, daemon=True).start()
        o = {"bot": "Ayla", "cmd": "travel Booty Bay"}
        err = director.validate(o, director.census())
        check("travel order validates and resolves the destination",
              err is None and o["_travel"] and o["_walk"][1] == "BootyBay", (err, o))
        check("travel order line is a GM .tele name",
              director.order_line("Ayla", "travel BootyBay") == ".tele name Ayla BootyBay",
              director.order_line("Ayla", "travel BootyBay"))
        reported = []
        real_say = director.say
        director.say = lambda who, what: (reported.append(what), fifo_lines.append("(say) %s" % what))
        err2 = director._start_walk(o, "Cecil")
        check("travel mission launches", err2 is None, err2)
        deadline = time.time() + 90
        while time.time() < deadline and not any("arrived" in r for r in reported):
            time.sleep(0.2)
        sim_state["done"] = True
        director.say = real_say
        check("mission sent the GM .tele name leg",
              sim_state["teles"] == ["BootyBay"] and
              ".tele name Ayla BootyBay" in fifo_lines, (sim_state["teles"], fifo_lines[-4:]))
        check("mission reported the arrival",
              any("arrived at BootyBay" in r for r in reported), reported)
        check("mission narrated the journey",
              any("is travelling to BootyBay" in r for r in reported), reported)
        check("mission stood the bot down and restored it",
              any("nc -new rpg,-grind,-gather,-follow" in l for l in fifo_lines) and
              any("nc +new rpg,+grind,-follow" in l for l in fifo_lines), fifo_lines[-6:])
        check("audit records the leg as TRAVEL",
              "TRAVEL" in open(director.AUDIT).read(), None)
        err = director.validate({"bot": "Gau", "cmd": "travel Nowhereville"},
                                director.census())
        check("travel to an unknown location is refused",
              "not a known location" in str(err), err)
        real_pd = director.PATH_DIRECT
        director.PATH_DIRECT, director.WALK_HOP = 290.0, 200.0
        hx, hy, d = director._next_hop(-9000.0, 100.0, -9100.0, 150.0)
        check("final approach within the path budget is ordered DIRECTLY",
              (hx, hy) == (-9100.0, 150.0), (hx, hy))
        hx, hy, d = director._next_hop(-9000.0, 100.0, -9600.0, 100.0)
        check("beyond the budget: straight-line hop capped at WALK_HOP",
              abs((hx - -9000.0) - (-200.0)) < 0.01 and hy == 100.0, (hx, hy))
        director.PATH_DIRECT, director.WALK_HOP = real_pd, real_hop
        ow = {"bot": "Gau", "cmd": "walk Booty Bay"}
        err = director.validate(ow, director.census())
        check("walk (ground-only) order validates without the flight flag",
              err is None and "_walk" in ow and "_travel" not in ow, (err, ow))
        director.WALK_HOP, director.WALK_TICK = real_hop, real_tick
        director.WALK_STALL, director.WALK_MAX = real_stall, real_max

        print("== 1g. farm keeper: tele -> gather -> re-arm -> stand-down cancel ==")
        real_refresh = director.FARM_REFRESH
        director.FARM_REFRESH = 2
        sim2 = {"x": -9446.0, "y": 64.0, "target": None}
        farm_state = {"done": False, "gathers": 0}

        def farm_world():
            seen = len(fifo_lines)
            while not farm_state["done"]:
                time.sleep(0.05)
                for line in fifo_lines[seen:]:
                    seen += 1
                    m = re.match(r"\.tele name Ayla (\S+)$", line.strip())
                    if m and m.group(1) == "BootyBay":
                        sim2["x"], sim2["y"] = -14420.0, 480.0
                    if "/w Ayla rpg status do gather" in line:
                        farm_state["gathers"] += 1

        def farm_replier():
            seen = len(fifo_lines)
            while not farm_state["done"]:
                time.sleep(0.05)
                for line in fifo_lines[seen:]:
                    seen += 1
                    if ".playerbots inspect Ayla position" not in line:
                        continue
                    with open(log_path, "a") as f:
                        f.write("2026-11-11 22:00:00.000 | MESSAGES | "
                                "headless.world.chat.chat:display:124 - "
                                "[System]: Ayla: %.1f %.1f 26.0 0 0.5 |Stranglethorn Vale|\n"
                                % (sim2["x"], sim2["y"]))

        threading.Thread(target=farm_world, daemon=True).start()
        threading.Thread(target=farm_replier, daemon=True).start()
        fo = {"bot": "Ayla", "cmd": "farm Booty Bay"}
        err = director.validate(fo, director.census())
        check("farm order validates and resolves the destination",
              err is None and fo.get("_farm") and fo["_walk"][1] == "BootyBay", (err, fo))
        check("farm order line shows the GM tele",
              director.order_line("Ayla", "farm BootyBay") == ".tele name Ayla BootyBay",
              director.order_line("Ayla", "farm BootyBay"))
        err = director.validate({"bot": "Gau", "cmd": "farm Nowhereville"},
                                director.census())
        check("farm to an unknown location is refused",
              "not a known location" in str(err), err)
        reported2 = []
        real_say2 = director.say
        director.say = lambda who, what: (reported2.append(what), fifo_lines.append("(say) %s" % what))
        err2 = director._start_walk(fo, "Cecil")
        check("farm mission launches", err2 is None, err2)
        err3 = director._start_walk(fo, "Cecil")
        check("second farm order for the same bot is refused",
              "already farming" in str(err3), err3)
        deadline = time.time() + 30
        while time.time() < deadline and farm_state["gathers"] < 3:
            time.sleep(0.2)
        check("farm mission armed and re-armed the gather status",
              farm_state["gathers"] >= 3, farm_state["gathers"])
        check("farm mission narrated the arrival",
              any("arrived at BootyBay and is gathering" in r for r in reported2), reported2)
        check("farm keeper registered the bot",
              director._lease_kind("Ayla") == "farm", director.leases())
        err = director.validate({"bot": "Ayla", "cmd": "rpg status idle"},
                                director.census())
        check("stand-down order cancels the keeper",
              err is None and "Ayla" not in director.leases(),
              (err, director.leases()))
        time.sleep(3)
        g_before = farm_state["gathers"]
        time.sleep(4)
        check("no further re-arms after the stand-down",
              farm_state["gathers"] == g_before, (g_before, farm_state["gathers"]))
        farm_state["done"] = True
        director.FARM_REFRESH = real_refresh
        director.say = real_say2

        print("== 2. db-search facts: census, masters, sender gates ==")
        online = director.census()
        check("census (online): Ayla, Gau, Golbez, Leo",
              sorted(online) == ["Ayla", "Gau", "Golbez", "Leo"], sorted(online))
        all_bots = director.census(online_only=False)
        check("census (all) includes offline Locke/Cecil/Rydia/Vivi",
              {"Locke", "Cecil", "Rydia", "Vivi"} <= set(all_bots), sorted(all_bots))
        check("census account for guild member Leo is the shell account",
              all_bots["Leo"]["account"] == "i_was_cmd", all_bots.get("Leo"))
        check("masters: HUMANS on master account only (no Leo, no bot toons)",
              director.masters() == {"cecil", "rydia", "vivi"}, director.masters())
        check("sender account: Leo -> i_was_cmd",
              director._sender_account("Leo") == "i_was_cmd")
        check("bot sender gate: Ayla (bot account) is a bot sender",
              director._is_bot_sender("Ayla") is True)
        check("bot sender gate: Leo (shell account) is a bot sender",
              director._is_bot_sender("Leo") is True)
        check("bot sender gate: Cecil (master account) is not",
              director._is_bot_sender("Cecil") is False)

        print("== 3. group lookups via db-search ==")
        # Locke is still in the raid rows (A3 async logout) - the fixture models it
        check("Leo's group members", director.group_members("Leo") == ["Leo", "Ayla", "Gau", "Locke"],
              director.group_members("Leo"))
        gt = director.group_text("Leo")
        check("group text: raid led by Leo with Ayla+Gau",
              "raid" in gt and "led by Leo" in gt and "Ayla" in gt and "Gau" in gt, gt)
        check("ungrouped toon reads 'not in any group'",
              director.group_text("Cecil") == "not in any group", director.group_text("Cecil"))

        print("== 4. query digest carries queried_at + caveat (staleness contract) ==")
        digest = director.run_queries(
            [{"query": "quest", "bot": "Locke"},
             {"query": "money", "bot": "Ayla"},
             {"query": "skills", "bot": "Ayla"}],
            all_bots)
        digest2 = director.run_queries(
            [{"query": "bags", "bot": "Ayla"},
             {"query": "played", "bot": "Locke"}],
            all_bots)
        digest += "\n" + digest2
        for ln in digest.splitlines():
            check("queried_at + caveat: %s" % ln.split(":")[0],
                  "queried_at" in ln and "caveat:" in ln, ln)
        check("quest digest names fixture quests",
              "The Fargatome" in digest or "ready to turn in" in digest, digest)
        check("money digest formats gold", "0g 50s 0c" in digest, digest)
        check("skills digest names professions", "herb" in digest, digest)
        check("bags digest counts free slots", "free bag slots" in digest, digest)
        check("unknown query refused", "unknown" in
              director.run_queries([{"query": "nope", "bot": "Ayla"}], all_bots))
        check("out-of-census bot refused", "not in census" in
              director.run_queries([{"query": "money", "bot": "Nobody"}], all_bots))

        print("== 5. validated order pipeline -> sends through chat-tunnel ==")
        bots = director.census()
        check("whitelisted order to an online bot validates",
              director.validate({"bot": "Ayla", "cmd": "nc +grind"}, bots) is None)
        check("FORBIDDEN command refused",
              "FORBIDDEN" in director.validate({"bot": "Ayla", "cmd": "destroy all"}, bots))
        check("unknown bot refused",
              "not online" in director.validate({"bot": "Nobody", "cmd": "nc"}, bots))
        check("cast gated per profession (Ayla is herb/mine, not tailor)",
              "not allowed" in director.validate({"bot": "Ayla", "cmd": "cast Mooncloth"}, bots))
        check("hand-play toon never commanded",
              "hand-play" in director.validate({"bot": "Cecil", "cmd": "nc +grind"}, bots))
        check("group remove of a non-member refused",
              "not a member" in director.validate({"bot": "Golbez", "cmd": "group remove"}, bots))
        o = {"bot": "Gau", "cmd": "group remove"}
        err = director.validate(o, bots)
        check("group remove of a bot member rewrites to 'leave'",
              err is None and o["cmd"] == "leave", (err, o))
        o = {"bot": "Locke", "cmd": "bot add"}
        check("bot add for an OFFLINE unit validates",
              director.validate(o, director.census(online_only=False)) is None)

        sent = director.execute(
            [{"bot": "Ayla", "cmd": "nc ?"},
             {"bot": "Locke", "cmd": "bot add"},
             {"bot": "Nobody", "cmd": "nc"}],
            bots, pace=0, dry=False)
        time.sleep(0.5)
        check("whisper order went out as /w via the tunnel",
              "/w Ayla nc ?" in fifo_lines, fifo_lines)
        check("master cmd went out as .playerbots via the tunnel",
              ".playerbots bot add Locke" in fifo_lines, fifo_lines)
        check("unknown bot was skipped (nothing sent to it)",
              not any("Nobody" in l for l in fifo_lines), fifo_lines)
        check("execute returned the 2-order plan", sent and len(sent) == 2, sent)
        check("audit log recorded the sends",
              "SENT /w Ayla nc ?" in open(director.AUDIT).read())

        print("== 6. replies: say() whispers back through the tunnel ==")
        director.say("Cecil", "aye, moving out")
        time.sleep(0.5)
        check("say() wrote /w Cecil via the tunnel",
              "/w Cecil aye, moving out" in fifo_lines, fifo_lines)
        director.say("Cecil", "moving to Booty Bay. | executed: /w Golbez go travel Booty Bay")
        check("say() strips the WoW escape char (pipes garble/drop the whisper)",
              any("/w Cecil moving to Booty Bay. ; executed:" in l
                  and "|" not in l.split(":", 1)[-1][:2] for l in fifo_lines[-2:]),
              fifo_lines[-2:])

        print("== 6b. human whispers land as tunnel events (the daemon's input feed) ==")
        cursor = director.tunnel("get_events_since", after_id=0, limit=1000)["last_id"]
        with open(log_path, "a") as f:
            f.write("2026-11-10 20:00:30.000 | MESSAGES | headless.world.chat.chat:display:124 - "
                    "[Cecil] [whisper]: leo, status report\n")
        ev = None
        for _ in range(40):
            batch = director.tunnel("get_events_since", after_id=cursor, limit=100)
            for e in batch["events"]:
                if e["direction"] == "reply" and e["sender"] == "Cecil":
                    ev = e
            if ev:
                break
            time.sleep(0.05)
        check("human whisper parsed into a reply event with channel + text",
              ev is not None and ev["channel"] == "whisper"
              and ev["command"] == "leo, status report", ev)

        print("== 7. live read: strategies via chat-tunnel inspect (A23) ==")

        def fake_worldserver():
            time.sleep(1.0)
            with open(log_path, "a") as f:
                f.write("2026-11-10 20:00:00.000 | MESSAGES | headless.world.chat.chat:display:124 - "
                        "[System]: Ayla: Strategies: rpg, grind\n")

        threading.Thread(target=fake_worldserver, daemon=True).start()
        ok, txt = director._probe_strategies("Ayla")
        check("inspect strategy reply correlated back",
              ok and "rpg, grind" in txt, (ok, txt))

        def fake_worldserver2():
            time.sleep(1.0)
            with open(log_path, "a") as f:
                f.write("2026-11-10 20:01:00.000 | MESSAGES | headless.world.chat.chat:display:124 - "
                        "[System]: Ayla: Strategies: rpg, grind\n")

        threading.Thread(target=fake_worldserver2, daemon=True).start()
        check("strategies digest surfaces the live read",
              "rpg, grind" in director.run_queries(
                  [{"query": "strategies", "bot": "Ayla"}], all_bots))

        print("== 8. turn cap + hang indicators (live 2026-11-10 gap) ==")
        import io
        buf = io.StringIO()
        stamped = director._StampedStdout(buf)
        stamped.write("daemon: hello\n")
        check("daemon log lines are timestamped",
              "\n" in buf.getvalue() and "daemon: hello" in buf.getvalue()
              and len(buf.getvalue().split(" ")[0]) == 10, buf.getvalue())

        real_llm = director.llm
        nudged = []

        def slow_llm(messages):
            time.sleep(1.2)          # a turn slower than the test threshold
            return "aye"

        director.STILL_WORKING = 0.5
        director.llm = slow_llm
        out = director.llm_retry([{"role": "user", "content": "x"}],
                                 notify=lambda: nudged.append("nudge"))
        check("slow turn returns its reply", out == "aye", out)
        check("still-working nudge fired once mid-turn", nudged == ["nudge"], nudged)

        def fast_llm(messages):
            return "quick"

        director.llm = fast_llm
        nudged.clear()
        out = director.llm_retry([{"role": "user", "content": "x"}],
                                 notify=lambda: nudged.append("nudge"))
        check("fast turn: no nudge", out == "quick" and nudged == [], (out, nudged))
        director.llm = real_llm
        director.STILL_WORKING = 45.0

        print("== 8b. turn watchdog: stage tracking + hard abort (live 2026-11-10) ==")
        real_say, real_sw, real_tm = director.say, director.STILL_WORKING, director.TURN_MAX
        said = []
        director.say = lambda who, what: said.append(what)
        director.STILL_WORKING, director.TURN_MAX = 0.5, 2.0

        def hang_worker():
            director._stage("queries")
            time.sleep(30)          # a stalled stage: past TURN_MAX, must be abandoned

        director._run_turn(hang_worker, "Cecil")
        check("hard abort fires at TURN_MAX and names the stuck stage",
              any("stuck at 'queries'" in s for s in said), said)
        check("progress whispers fired before the abort and name the stage",
              any(s.startswith("still on it: queries") for s in said)
              and said.index("I'm stuck at 'queries' (2s) - dropping this one; "
                             "whisper me again.") >= 0, said)
        check("stage resets to idle after an aborted turn",
              director._STAGE["name"] == "idle", director._STAGE)

        # token gate: the abandoned worker's late output is dropped by
        # say()/send_order() - they call _turn_aborted(), so prove THAT flips
        # (the test's say monkeypatch bypasses the real gate)
        gate = []

        def gated_check_worker():
            director._stage("reply")
            gate.append(director._turn_aborted())
            time.sleep(2.5)          # outlasts TURN_MAX, then checks the gate
            gate.append(director._turn_aborted())

        said.clear()
        director._run_turn(gated_check_worker, "Cecil")
        time.sleep(1.5)             # let the abandoned worker take its second sample
        check("late whispers from an aborted turn are dropped",
              gate == [False, True], gate)
        check("a fresh turn is never gated by the previous abort",
              not director._turn_aborted(), director._ABORTED)

        # a normal turn: no progress whispers, worker's whisper goes through
        def ok_worker():
            director._stage("llm")
            director.say("Cecil", "all done")

        said.clear()
        director._run_turn(ok_worker, "Cecil")
        check("healthy turn whispers normally (gate only bites aborted turns)",
              said == ["all done"], said)

        director.say, director.STILL_WORKING, director.TURN_MAX = \
            real_say, real_sw, real_tm

        print("== 8c. LLM runaway brake (live 2026-11-10: 16,938-token spiral) ==")
        import io, json as _json, urllib.error
        real_urlopen = urllib.request.urlopen
        bodies = []

        class FakeResp(io.BytesIO):
            pass

        def ok_urlopen(req, timeout=None):
            bodies.append(_json.loads(req.data.decode()))
            return io.BytesIO(_json.dumps(
                {"message": {"content": "aye"}}).encode())

        def think_reject_urlopen(req, timeout=None):
            bodies.append(_json.loads(req.data.decode()))
            if "think" in bodies[-1]:
                raise urllib.error.HTTPError(
                    req.full_url, 400,
                    "think is only supported by thinking models",
                    {}, io.BytesIO(b"think is only supported by thinking models"))
            return io.BytesIO(_json.dumps(
                {"message": {"content": "aye"}}).encode())

        urllib.request.urlopen = ok_urlopen
        out = director.llm([{"role": "user", "content": "x"}])
        check("llm request carries the runaway brake and leaves thinking on",
              out == "aye" and bodies[-1]["options"].get("num_predict") == director.MAX_TOKENS
              and "think" not in bodies[-1], bodies[-1])

        real_thinking, real_max = director.THINKING, director.MAX_TOKENS
        director.THINKING = False
        bodies.clear()
        urllib.request.urlopen = think_reject_urlopen
        out = director.llm([{"role": "user", "content": "x"}])
        check("DIRECTOR_THINKING=0 sends think:false and falls back on rejection",
              out == "aye" and len(bodies) == 2
              and bodies[0]["think"] is False and "think" not in bodies[1], bodies)
        check("num_predict survives the fallback retry",
              bodies[1]["options"].get("num_predict") == director.MAX_TOKENS, bodies[1])

        director.THINKING, director.MAX_TOKENS = True, 0
        bodies.clear()
        urllib.request.urlopen = ok_urlopen
        director.llm([{"role": "user", "content": "x"}])
        check("MAX_TOKENS=0 drops the cap",
              "num_predict" not in bodies[-1]["options"], bodies[-1])
        director.THINKING, director.MAX_TOKENS = real_thinking, real_max
        urllib.request.urlopen = real_urlopen

        # empty content but the thinking channel holds the fenced answer
        def thinking_answer_urlopen(req, timeout=None):
            bodies.append(_json.loads(req.data.decode()))
            return io.BytesIO(_json.dumps({"message": {
                "content": "",
                "thinking": "plan the JSON... ```json\n{\"reply\": \"aye\", "
                             "\"orders\": []}\n``` done",
            }, "done_reason": "stop"}).encode())

        def empty_spiral_urlopen(req, timeout=None):
            bodies.append(_json.loads(req.data.decode()))
            return io.BytesIO(_json.dumps({"message": {
                "content": "", "thinking": "loop loop loop",
            }, "done_reason": "length"}).encode())

        urllib.request.urlopen = thinking_answer_urlopen
        out = director.llm([{"role": "user", "content": "x"}])
        check("empty content recovered from the thinking channel's JSON fence",
              out.strip().startswith('{"reply": "aye"'), out)

        urllib.request.urlopen = empty_spiral_urlopen
        out = director.llm([{"role": "user", "content": "x"}])
        check("capped spiral (done_reason=length) stays empty, not hallucinated",
              out == "", out)
        urllib.request.urlopen = real_urlopen

        print("== 8d. truncated contracts + narration wording (live 2026-11-11) ==")
        out = director._extract_json('"""json\n{"query": "recipes", "text": "have golbez gather"}\n"""')
        check("triple-double-quote fences extract like backtick fences",
              out and json.loads(out)["query"] == "recipes", out)
        out = director._extract_json('```json\n{"reply": "ok", "orders": []}\n```')
        check("backtick fences still extract",
              out and json.loads(out)["reply"] == "ok", out)
        p = director.parse_orders('"""json\n{"query": "recipes", "text": "ha"')
        check("capped mid-contract reply never reaches the tunnel raw",
              "tangled" in p["reply"] and p["orders"] == [] and p["queries"] == [], p)
        p = director.parse_orders("Golbez is heading out.")
        check("plain-text replies pass through unchanged",
              p["reply"] == "Golbez is heading out." and p["orders"] == [], p)
        said = []
        real_send = director.send_order
        director.send_order = lambda who, cmd: said.append(cmd) or {"line": cmd}
        director.say("Cecil", 'multi\nline ""json reply')
        check("say() flattens newlines before the whisper",
              len(said) == 1 and "\n" not in said[0] and " ; " in said[0], said)
        director.send_order = real_send

        print("== 9. presence facts (heartbeat hands, never decides) ==")
        director._svc_state = lambda: "active"
        cfg = director._wd_conf()
        cfg["GROUP"] = "Cecil Rydia"
        leo, group_on, wanted, svc = director._presence_facts(cfg)
        check("Leo online flag from db-search", leo == 1, leo)
        check("hand-play group offline count", group_on == 0, group_on)
        check("service state passed through", svc == "active", svc)
        msg = director._hb_msg((leo, group_on, wanted, svc, 0), "state change", cfg)
        check("heartbeat message lists Leo's group members",
              "Ayla" in msg and "Gau" in msg, msg)

        print("== 10. invariants: fewshots + recipes + LLM-boundary fuzz ==")
        # A SKIP is fine when it is about the WORLD (bot offline, not at a
        # dock, spell not known) - the invariant is about COMMAND SHAPE: our
        # own teaching material must never teach an order validate() rejects
        # as malformed/unknown/forbidden.
        SITUATIONAL = ("not online", "not in census", "already", "not at a "
                       "departure harbor", "could not read a live position",
                       "cast spell not allowed", "not a member",
                       "hand-play", "no route")

        def order_ok(err):
            return err is None or any(s in str(err) for s in SITUATIONAL)

        bots_c = director.census()
        bad = []
        for m in director.FEWSHOT:
            if m["role"] != "assistant":
                continue
            obj = director.parse_orders(m["content"])
            for o in director.expand_orders(obj.get("orders", []),
                                            director.census(online_only=False)):
                err = director.validate(dict(o), bots_c)
                if not order_ok(err):
                    bad.append((m["content"][:40], o, err))
        check("invariant: every fewshot order validates (no drift)",
              not bad, bad)

        INST = {"<destination>": "Booty Bay",
                "<the zone's table destination>": "Booty Bay",
                "<arrival harbor>": "Ratchet",
                "<exact spell name>": "Heal",
                "<id>": "153", "<name>": "Ayla", "<mat>": "Peacebloom",
                "<item>": "Peacebloom", "<word>": "herbs"}
        rec = json.load(open("services/db-search/recipes.json"))["recipes"]
        bad2 = []
        for r in rec:
            cmds = [o.get("cmd", "") for o in r.get("orders", [])]
            for s in r.get("steps", []):
                cmds += re.findall(r'"cmd":\s*"([^"]+)"', s.replace('\\"', '"'))
            for c in cmds:
                for k, v in INST.items():
                    c = c.replace(k, v)
                if "<" in c:
                    bad2.append((r["id"], "uninstantiated placeholder", c))
                    continue
                err = director.validate({"bot": "Ayla", "cmd": c}, bots_c)
                if not order_ok(err):
                    bad2.append((r["id"], c, err))
        check("invariant: every recipe command validates", not bad2, bad2)

        garbage = [
            "",
            '```json\n{"reply": "ok", "orders": []}\n```',
            '"""json\n{"query": "recipes", "text": "ha"',
            'Requesting flight path to STV.\n"json..."',
            "plain prose, no json at all",
            '{"reply": "a|b\nc", "orders": [{"bot": "Gau", "cmd": "go Booty Bay"}]}',
            '```\n{"reply": "x", "orders": [{"bot": "Gau", "cmd": "destroy everything"}]}\n```',
            '{"reply": "dup", "orders": []} and then {"reply": "second", "orders": []}',
            '{"reply": "ok", "orders": [{"bot": "Gau", "cmd": "travel Nowhereville"}]}',
            '{"reply": {"nested": "dict"}, "orders": []}',
            '{"reply": "ok", "orders": [{"bot": "Gau", "cmd": "follow"}, "not a dict", {"bot": 5, "cmd": "follow"}, {"bot": "Gau"}]}',
            '{"reply": "ok", "queries": ["nope", {"query": "recipes", "text": "gather herbs"}, 7]}',
            '{"reply": "ok", "orders": "follow everyone"}',
            '[{"reply": "not an object", "orders": []}]',
            '{"reply": "Ayla and Mog are moving to Ashenvale. ```json\n{"query": "recipes"}\n```", "orders": []}',
            'Checking.\n```json\n{"reply": "Ayla and Mog are moving to Ashenvale. \'\'json\n{"query": "recipes", "text": "x"}", "orders": []}\n```',
        ]
        sent_f = []
        real_send = director.send_order
        director.send_order = lambda who, cmd: sent_f.append((who, cmd)) or {"line": cmd}
        bad3 = []
        for g in garbage:
            try:
                obj = director.parse_orders(g)
                msg = obj.get("reply", "")
                for o in obj.get("orders", []):
                    err = director.validate(dict(o), bots_c)
                    msg += "" if err is None else " | " + str(err)
                director.say("Cecil", msg)
            except Exception as e:
                bad3.append((g[:30], repr(e)))
        director.send_order = real_send
        check("invariant: garbage LLM output never crashes the turn tail",
              not bad3, bad3)
        check("invariant: nothing raw (fences/newlines) reaches the tunnel",
              not any("```" in c or '"""' in c or "\n" in c
                      for _, c in sent_f),
              [c for _, c in sent_f if "```" in c or '"""' in c or "\n" in c])
        check("invariant: refused orders never reach the bot "
              "(only the master hears the SKIP)",
              all(who == "Cecil" for who, _ in sent_f),
              [w for w in sent_f if w[0] != "Cecil"])
        p1 = director.parse_orders('{"reply": {"nested": "dict"}, "orders": []}')
        check("boundary: non-text reply becomes the retry note",
              "tangled" in p1["reply"], p1)
        p2 = director.parse_orders('{"reply": "ok", "orders": [{"bot": "Gau", "cmd": "follow"}, "junk", {"bot": 5, "cmd": "follow"}]}')
        check("boundary: malformed order entries dropped, good ones kept",
              p2["orders"] == [{"bot": "Gau", "cmd": "follow"}], p2)
        p3 = director.parse_orders('{"reply": "ok", "queries": ["nope", {"query": "recipes", "text": "x"}, 7]}')
        check("boundary: malformed query entries dropped, good ones kept",
              p3["queries"] == [{"query": "recipes", "text": "x"}], p3)
        pa = director.parse_orders('{"reply": "Ayla and Mog are moving to Ashenvale. ```json\n{"query": "recipes"}\n```", "orders": []}')
        check("boundary: a leaked fenced fragment in the reply keeps the prose",
              pa["reply"] == "Ayla and Mog are moving to Ashenvale.", pa)
        pb = director.parse_orders('Checking.\n```json\n{"reply": "Ayla and Mog are moving to Ashenvale. \'\'json\n{"query": "recipes", "text": "x"}", "orders": []}\n```')
        check("boundary: a mis-fenced fragment is salvaged from the wrapper",
              pb["reply"] == "Ayla and Mog are moving to Ashenvale.", pb)
        p4 = director.parse_orders('[{"reply": "array not object"}]')
        check("boundary: an array-wrapped contract still yields text + no orders",
              p4["reply"] == "array not object" and p4["orders"] == [], p4)

        assert director._take_lease("Gau", "travel", "Ironforge") is None
        err = director.validate({"bot": "Gau", "cmd": "farm Booty Bay"}, bots_c)
        check("invariant: a leased bot refuses new missions with the live verb",
              "already travelling to Ironforge" in str(err), err)
        err = director.validate({"bot": "Gau", "cmd": "rpg status idle"}, bots_c)
        check("invariant: stand-down cancels only farm leases",
              "Gau" in director.leases(), director.leases())
        director._release_lease("Gau")
        err = director.validate({"bot": "Gau", "cmd": "travel Booty Bay"}, bots_c)
        check("invariant: a released lease accepts new missions",
              err is None, err)
        return finish()
    finally:
        db_proc.terminate()
        ct_proc.terminate()
        for p in (db_proc, ct_proc):
            try:
                p.wait(5)
            except Exception:
                p.kill()


def finish():
    print("service wiring tests: %s" %
          ("ALL PASS" if not FAIL else "FAILURES: %d" % FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
