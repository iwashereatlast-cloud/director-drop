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
        check("recipes sweep: farming loads the 4-step structure",
              "RECIPE farm_mat" in r3 and "STEP 4" in r3 and "rpg status do gather" in r3, r3[:300])
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
        check("abbreviations resolve to canonical zones",
              all(z in director.ZONE_DESTINATIONS
                  for z in director.ZONE_ABBREVIATIONS.values()))
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

        print("== 1f. travel mission: hop -> taxi -> fly -> last mile (offline sim) ==")
        real_hop, real_tick, real_stall, real_max = (director.WALK_HOP,
                                                     director.WALK_TICK,
                                                     director.WALK_STALL,
                                                     director.WALK_MAX)
        director.WALK_HOP, director.WALK_TICK = 100.0, 0.3
        director.WALK_STALL, director.WALK_MAX = 40, 120.0
        # sim world: Ayla (an orc, horde) starts near Goldshire; her FM is the
        # fixture Horde FM at (-9800, 60); the taxi list offers Booty Bay; taxi 1 lands her near
        # the BootyBay tele point (-14420, 480); then she walks the last mile.
        sim = {"x": -9446.0, "y": 64.0, "target": None}
        sim_state = {"arrivals": [], "taxis": []}

        def sim_world():
            seen = len(fifo_lines)
            while not sim_state.get("done"):
                time.sleep(0.05)
                for line in fifo_lines[seen:]:
                    seen += 1
                    m = re.match(r"/w Ayla go ([-\d.]+);([-\d.]+)", line)
                    if m:
                        sim["target"] = (float(m.group(1)), float(m.group(2)))
                    if re.match(r"/w Ayla taxi \?$", line.strip()):
                        for i, nm in [(1, "Booty Bay"), (2, "Stormwind")]:
                            with open(log_path, "a") as f:
                                f.write("2026-11-10 21:10:%02d.000 | MESSAGES | "
                                        "headless.world.chat.chat:display:124 - "
                                        "[Ayla] [whisper]: %d: %s\n" % (i * 2, i, nm))
                    m = re.match(r"/w Ayla taxi (\d+)$", line.strip())
                    if m and m.group(1) == "1":
                        sim_state["taxis"].append("taxi 1")
                        sim["x"], sim["y"], sim["target"] = -14405.0, 495.0, None

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
        check("mission flew the long leg (taxi 1 ordered)",
              sim_state["taxis"] == ["taxi 1"], sim_state["taxis"])
        check("mission reported the arrival",
              any("arrived at BootyBay" in r for r in reported), reported)
        check("mission narrated the flight",
              any("flying to Booty Bay" in r for r in reported), reported)
        hops = [l for l in fifo_lines if re.match(r"/w Ayla go ", l)]
        check("mission hopped to the flight master first",
              len(hops) >= 1 and "taxi" in " ".join(fifo_lines), len(hops))
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
