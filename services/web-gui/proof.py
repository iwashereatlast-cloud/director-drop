#!/usr/bin/env python3
"""web-gui proof harness - runs WITHOUT a live worldserver, DB, or Leo.

Boots the two real sibling services in fixture mode (db-search on a sqlite
fixture built from services/db-search/fixtures/fixture.sql, chat-tunnel
against a temp log + FIFO with a name allowlist so no MySQL is touched),
then drives the REAL web-gui over HTTP and asserts (web-gui/2, resynced to
the post-rtt-engine stack):

  1. the dashboard page serves with all three panels;
  2. /api/overview merges census + tunnel health + Leo's character row +
     Leo's group membership (the group/raid badge source) + the Leo
     session status, with the staleness caveat surfaced;
  3. the live chat window seam: cursor reads (get_events_since) are
     ascending + gapless with a monotonic last_id, a sent command is bound
     to its whisper reply by correlation_id, and wait_for_events actually
     long-polls (blocks ~the timeout with no new events);
  4. /api/catalogs lists the db-search catalog; /api/grammar serves the
     chat-tunnel grammar (inspect queries included);
  5. /api/query executes a catalog read and propagates db-search refusals;
  6. /api/live runs a chat-tunnel inspect (correlation_id issued, the
     [System] reply line matched back through /api/reply) and REFUSES
     non-allowlisted queries (gui_read_only);
  7. the Leo button: /api/leo status is graceful without systemd, a bad
     action is refused, start/stop return a structured answer (never a
     5xx, never a crash);
  8. graceful degradation: with both siblings down, every panel endpoint
     still answers 200 with a per-backend "unreachable" state;
  9. the bind knob + login screen (unchanged from web-gui/1): wildcard
     refused, specific address accepted, lan resolves, no-password LAN
     bind refused, warning proved without ever listening on the LAN, full
     login flow on loopback.

Exit code 0 = all proofs pass. Run: python3 proof.py
"""
import json
import os
import shutil
import threading
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
DBSEARCH = os.path.join(REPO, "services", "db-search")
CHATTUNNEL = os.path.join(REPO, "services", "chat-tunnel")

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (name, detail))


def http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode()), dict(e.headers)


def wait_port(port, tries=80, host="127.0.0.1"):
    for _ in range(tries):
        try:
            with urllib.request.urlopen("http://%s:%d/health" % (host, port),
                                        timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def wait_connect(port, tries=80, host="127.0.0.1"):
    for _ in range(tries):
        try:
            socket.create_connection((host, port), timeout=1).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def start_gui(port, bind, extra_env=None):
    env = dict(os.environ, WEBGUI_PORT=str(port), WEB_GUI_BIND=bind)
    env.update(extra_env or {})
    return subprocess.Popen([sys.executable, os.path.join(HERE, "web_gui.py"),
                             "serve"], env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def wait_sock(path, tries=80):
    for _ in range(tries):
        if os.path.exists(path):
            return True
        time.sleep(0.1)
    return False


def build_fixture(tmp):
    for f in os.listdir(tmp):
        if f.startswith("fixture_") and f.endswith(".db"):
            os.unlink(os.path.join(tmp, f))
    con = sqlite3.connect(os.path.join(tmp, "fixture_acore_characters.db"))
    try:
        sql = open(os.path.join(DBSEARCH, "fixtures", "fixture.sql")).read()
        sql = sql.replace("ATTACH DATABASE 'fixture_",
                          "ATTACH DATABASE '" + tmp + os.sep + "fixture_")
        con.executescript(sql)
        # a fresh worldserver heartbeat row for the world-liveness signal
        # (the fixture ships a static ancient row by design; this one is
        # now-relative so the GUI's verdict computes 'up')
        import time as _t
        con.execute("INSERT OR REPLACE INTO acore_characters.uptime "
                    "VALUES (1, ?, ?, 30)",
                    (int(_t.time()) - 5400, 5400))
        con.commit()
    finally:
        con.close()



def arm_arrival_reply(base, bot, log, deadline=40):
    """Wait for the mission's inspect poll (a PENDING 'inspect position' sent
    event for this bot), then feed the [System] position reply AT the
    game_tele point - the mission's arrival evidence. The pending-check is
    the point: a reply line that lands before any pending inspect gets
    stored unmatched and the mission never sees it."""
    for _ in range(deadline * 2):
        st, ev, _ = http("GET", base + "/api/events?after_id=0&limit=1000")
        evs = (ev.get("result") or {}).get("events", [])
        if any(x.get("direction") == "sent" and x.get("target") == bot
               and x.get("command") == "inspect position"
               and x.get("status") == "pending" for x in evs):
            with open(log, "a") as f:
                f.write(display_line(
                    "[System]: %s: -14420.0 480.0 26.0 0 "
                    "0 |Stranglethorn Vale|" % bot) + "\n")
                f.flush()
            return True
        time.sleep(0.5)
    return False

def drain_pending_probes(base, bot, log, line):
    """Answer every OUTSTANDING pending 'inspect position' probe for this
    bot, until none remain. The tunnel binds replies oldest-pending-first:
    stale probes left by earlier missions sit AHEAD of a new leg's probes
    in the queue and eat its replies (live failure: the vendor leg starved
    while its STV feeds bound to the farm keeper's leftovers). Call this
    BEFORE starting a mission so the leg's probes are the oldest."""
    for _ in range(20):
        st, ev, _ = http("GET", base + "/api/events?after_id=0&limit=1000")
        evs = (ev.get("result") or {}).get("events", [])
        pend = [x for x in evs
                if x.get("direction") == "sent" and x.get("target") == bot
                and x.get("command") == "inspect position"
                and x.get("status") == "pending"]
        if not pend:
            return True
        with open(log, "a") as f:
            for _ in pend:
                f.write(display_line(line) + "\n")
            f.flush()
        time.sleep(1.2)       # let the binds flip
    return False


def status_feeder_thread(base, log, mapping, stop, command="rpg status"):
    """While a status POST is blocking, answer every pending ask of
    `command` for the mapped toons with a scripted reply line."""
    answered = set()
    while not stop["v"]:
        st, ev, _ = http("GET", base + "/api/events?after_id=0&limit=1000")
        evs = (ev.get("result") or {}).get("events", [])
        for x in evs:
            if x.get("direction") == "sent" \
                    and x.get("command") == command \
                    and x.get("status") == "pending" \
                    and x.get("target") in mapping \
                    and x.get("target") not in answered:
                bot = x.get("target")
                with open(log, "a") as f:
                    f.write(display_line("[{}] [whisper]: {}"
                                         .format(bot, mapping[bot])) + "\n")
                    f.flush()
                answered.add(bot)
        if len(answered) >= len(mapping):
            return
        time.sleep(0.4)


def feed_all_pending(base, bot, log, line, deadline=40):
    """Keep feeding ANY pending 'inspect position' probe for this bot with
    the given position line, for the whole deadline. No id-guard: the real
    mission leg fires its first probe BEFORE this helper starts, and the
    tunnel binds a reply to the OLDEST pending - stale probes from earlier
    segments steal single feeds. Feed, wait for the bind to flip the
    pending's status, repeat. Returns the number of feeds."""
    fed = 0
    quiet = 0
    for _ in range(deadline * 2):
        st, ev, _ = http("GET", base + "/api/events?after_id=0&limit=1000")
        evs = (ev.get("result") or {}).get("events", [])
        pend = [x for x in evs
                if x.get("direction") == "sent" and x.get("target") == bot
                and x.get("command") == "inspect position"
                and x.get("status") == "pending"]
        if pend:
            with open(log, "a") as f:
                f.write(display_line(line) + "\n")
                f.flush()
            fed += 1
            quiet = 0
            time.sleep(1.5)      # let the tunnel bind it
        else:
            quiet += 1
            if fed and quiet >= 8:
                return fed
        time.sleep(0.5)
    return fed


def feed_position_reply(base, bot, log, line, deadline=60):
    """Answer a NEW pending 'inspect position' probe for this bot (one that
    APPEARS after this call starts) with an arbitrary position line. The
    id-guard is the point: earlier probes that are still awaiting their
    status flip must not be answered instead (live race: the Stormwind
    answer was consumed by the ARRIVAL probe's pending window and the
    keeper's real probe died unheard)."""
    start = 0
    st, ev, _ = http("GET", base + "/api/events?after_id=0&limit=1000")
    for x in (ev.get("result") or {}).get("events", []):
        start = max(start, int(x.get("id") or 0))
    for _ in range(deadline * 2):
        st, ev, _ = http("GET", base + "/api/events?after_id=%d&limit=500"
                         % start)
        evs = (ev.get("result") or {}).get("events", [])
        if any(x.get("direction") == "sent" and x.get("target") == bot
               and x.get("command") == "inspect position"
               and x.get("status") == "pending" for x in evs):
            with open(log, "a") as f:
                f.write(display_line(line) + "\n")
                f.flush()
            return True
        time.sleep(0.5)
    return False


def nc_feeder_loop(base, log, bots, stop):
    """Answer EVERY pending 'nc ?' ask for the given bots with a strategy
    readback - the verify loop's readback side in the fixture (replies to
    EVERY ask, not one-shot: the loop needs before AND after readbacks)."""
    while not stop["v"]:
        st, ev, _ = http("GET", base + "/api/events?after_id=0&limit=1000")
        for x in (ev.get("result") or {}).get("events", []):
            if x.get("direction") == "sent" \
                    and x.get("command") == "nc ?" \
                    and x.get("status") == "pending" \
                    and x.get("target") in bots:
                with open(log, "a") as f:
                    f.write(display_line(
                        "[{}] [whisper]: Strategies: +new rpg +grind +quest "
                        "+gather -follow".format(x.get("target"))) + "\n")
                    f.flush()
                time.sleep(0.6)
        time.sleep(0.4)


def display_line(inner):
    """A loguru-shaped chat display line, the A14 anchor the parser needs."""
    return ("2026-11-12 10:00:00.123 | INFO | MESSAGES | "
            "headless.world.chat.chat:display:124 - " + inner)


def main():
    tmp = tempfile.mkdtemp(prefix="webgui-proof-")
    procs = []
    try:
        # --- fixture-mode db-search -------------------------------------
        build_fixture(tmp)
        db_sock = os.path.join(tmp, "db-search.sock")
        procs.append(subprocess.Popen(
            [sys.executable, os.path.join(DBSEARCH, "dbsearch.py"), "serve"],
            env=dict(os.environ, DBSEARCH_ADAPTER="sqlite",
                     DBSEARCH_FIXTURE_DB=os.path.join(tmp, "fixture_acore_characters.db"),
                     DBSEARCH_SOCKET=db_sock),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        check("db-search fixture mode came up", wait_sock(db_sock))

        # --- fixture-mode chat-tunnel (name allowlist: no MySQL) ---------
        log = os.path.join(tmp, "leo.log")
        fifo = os.path.join(tmp, "leo.in")
        open(log, "w").close()
        os.mkfifo(fifo)
        fifo_rd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        tunnel_port = 18765
        procs.append(subprocess.Popen(
            [sys.executable, os.path.join(CHATTUNNEL, "chat_tunnel.py")],
            env=dict(os.environ, CHAT_TUNNEL_LOG=log, CHAT_TUNNEL_FIFO=fifo,
                     CHAT_TUNNEL_PORT=str(tunnel_port),
                     CHAT_TUNNEL_DB=os.path.join(tmp, "tunnel.db"),
                     CHAT_TUNNEL_ALLOW="Leo,Ayla,Gau,Locke,Vivi"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        check("chat-tunnel fixture mode came up", wait_port(tunnel_port))

        # --- the real web-gui -------------------------------------------
        port = 18766
        base = "http://127.0.0.1:%d" % port
        leases_path = os.path.join(tmp, "director-leases.json")
        procs.append(start_gui(port, "127.0.0.1", {
            "WEBGUI_TUNNEL": "http://127.0.0.1:%d" % tunnel_port,
            "WEBGUI_DBSEARCH_SOCK": db_sock,
            "WEBGUI_CENSUS": "i_was_here",
            "WEBGUI_LEO": "Leo",
            "DIRECTOR_DBSEARCH_SOCK": db_sock,
            "DIRECTOR_TUNNEL_URL": "http://127.0.0.1:%d/" % tunnel_port,
            "DIRECTOR_LEASES_FILE": leases_path,
            "DIRECTOR_FARM_REFRESH": "20",
            "WEBGUI_AUDIT": os.path.join(tmp, "webgui-audit.jsonl"),
            "WEBGUI_POS_POLL": "3600",   # one cycle at startup, then quiet
            "WEBGUI_HANDPLAY_FILE": os.path.join(tmp, "handplay.json"),
            "WEBGUI_STATES_FILE": os.path.join(tmp, "webgui-states.json"),
            "WEBGUI_REVIEW_FILE": os.path.join(tmp, "webgui-last-review.json"),
        }))
        check("web-gui came up", wait_port(port))

        # --- the live position poller's first cycle (zone names) ----------
        # it inspects every online census bot at startup; feed each probe
        # the [System] position reply (zone name in pipes) and the overview
        # should carry real zone names for the table's zone column
        arm_arrival_reply(base, "Ayla", log)
        arm_arrival_reply(base, "Gau", log)
        # the poller probes EVERY census toon - including DB-offline ones
        # (the A5 trap: a stale offline flag must not hide a live bot)
        arm_arrival_reply(base, "Locke", log)
        pos_ok = False
        for _ in range(60):
            time.sleep(1)
            st, po, _ = http("GET", base + "/api/overview")
            pz = (po.get("positions") or {}).get("positions", {})
            if pz.get("Ayla", {}).get("zone") == "Stranglethorn Vale" \
                    and pz.get("Gau", {}).get("zone") == "Stranglethorn Vale" \
                    and pz.get("Locke", {}).get("zone") == "Stranglethorn Vale":
                pos_ok = True
                break
        check("live position poller fills zone names (not zone numbers)",
              pos_ok, json.dumps(po.get("positions"))[:200])
        check("DB-offline toons are probed too (the A5 staleness trap)",
              pz.get("Locke", {}).get("zone") == "Stranglethorn Vale",
              json.dumps(pz.get("Locke", {}))[:120])
        st, tv, _ = http("GET", base + "/api/overview")
        tt = tv.get("truth") or {}
        check("the reconciler: a probed DB-offline toon is LIVE (Tier 0)",
              tt.get("Ayla", {}).get("state") == "live"
              and tt.get("Locke", {}).get("state") == "live"
              and (tt.get("Ayla", {}).get("why") or "").startswith("answered"),
              json.dumps(tt)[:200])

        # --- 1. dashboard page ------------------------------------------
        req = urllib.request.Request(base + "/")
        with urllib.request.urlopen(req, timeout=10) as r:
            page = r.read().decode()
        check("page serves with the three panels",
              all(x in page for x in
                  ('id="panel-bots"', 'id="panel-feed"', 'id="panel-catalog"')))
        check("page has the toon modal + fleet controls + new columns",
              'id="toon-modal"' in page and "<th>mission</th>" in page
              and "<th>xp/hr</th>" in page and 'id="panic"' in page
              and 'id="fleet-online"' in page and 'id="fleet-offline"' in page
              and 'id="tab-audit"' in page and "Click a toon's name" in page)
        # the SERVED script must parse: Python processes the source's
        # escape sequences before the browser ever sees the bytes, so
        # checking the source is worthless - a raw newline inside a JS
        # string (an escape written one level short) broke EVERY browser
        # while the source still looked fine. Check the real served bytes.
        node = shutil.which("node")
        if node:
            jspath = os.path.join(tmp, "served-page.js")
            with open(jspath, "w") as f:
                f.write(page.split("<script>")[1].split("</script>")[0])
            r = subprocess.run([node, "--check", jspath],
                               capture_output=True, text=True)
            check("the SERVED page script parses (node --check)",
                  r.returncode == 0, (r.stderr or "")[:200])
        else:
            check("the SERVED page script parses (node unavailable - SKIP)",
                  True, "node not installed; run the harness where it is")

        # --- 2. overview: census + leo row + leo group + session ---------
        st, o, _ = http("GET", base + "/api/overview")
        check("overview answers 200 ok", st == 200 and o.get("ok") is True)
        d = o.get("census", {})
        check("overview census: fixture roster + caveat",
              d.get("ok") is True and d.get("row_count", 0) >= 4
              and "A5" in d.get("caveat", ""), json.dumps(d)[:200])
        names = {r[0] for r in d.get("rows", [])}
        check("overview census lists the fixture toons",
              {"Ayla", "Gau", "Locke"} <= names, str(sorted(names)))
        check("overview tunnel health ok",
              o.get("tunnel", {}).get("ok") is True)
        lc = o.get("leo_char", {})
        check("overview carries Leo's own row (get_character)",
              lc.get("ok") is True and (lc.get("rows") or [[""]])[0][0] == "Leo",
              json.dumps(lc)[:200])
        lg = o.get("leo_group", {})
        members = {r[4] for r in lg.get("rows", [])}
        check("overview carries Leo's group (get_group_membership)",
              lg.get("ok") is True
              and {"Leo", "Ayla", "Gau", "Locke"} <= members,
              json.dumps(lg)[:200])
        check("Leo's fixture group is a raid (groupType bit 0x02)",
              lg.get("ok") is True and (lg.get("rows") or [["0"]])[0][0] in
              ("2", "6", "3"), str((lg.get("rows") or [["?"]])[0][0]))
        check("Leo session status is graceful without systemd",
              isinstance(o.get("leo_active"), bool))

        # --- 3. live chat seam: cursor + correlation ----------------------
        st, sc, _ = http("POST", "http://127.0.0.1:%d/" % tunnel_port,
                         {"tool": "send_command",
                          "args": {"bot": "Ayla", "cmd": "who"}})
        check("harness sent a command through chat-tunnel",
              sc.get("ok") is True, json.dumps(sc)[:200])
        cid = sc.get("result", {}).get("correlation_id")
        with open(log, "a") as f:
            f.write(display_line("[Ayla] [whisper]: Ayla here") + "\n")
            f.flush()
        time.sleep(1.0)
        st, e1, _ = http("GET", base + "/api/events?after_id=0&limit=200")
        evs = (e1.get("result") or {}).get("events", [])
        ids = [x["id"] for x in evs]
        check("cursor read is ascending + gapless with a last_id",
              e1.get("ok") is True and ids == sorted(ids)
              and len(ids) >= 2 and ids[-1] == (e1.get("result") or {}).get("last_id"),
              json.dumps(e1)[:200])
        check("feed shows the sent command with its correlation_id",
              any(x["direction"] == "sent" and x["correlation_id"] == cid
                  for x in evs), json.dumps(evs)[:200])
        check("feed shows the reply matched to the right command",
              any(x["direction"] == "reply"
                  and x.get("matched_correlation_id") == cid
                  for x in evs), json.dumps(evs)[:200])
        last = (e1.get("result") or {}).get("last_id")
        t0 = time.time()
        st, w1, _ = http("GET", base + "/api/events/wait?after_id=%d&timeout=3"
                         % last)
        took = time.time() - t0
        check("wait_for_events long-polls (blocks ~timeout, empty batch)",
              w1.get("ok") is True and took >= 2.5 and took < 10
              and not (w1.get("result") or {}).get("events"),
              "took=%.1fs %s" % (took, json.dumps(w1)[:120]))
        # regression (live bug 2026-11-13): an idle wait LONGER than the
        # GUI's default 15s backend timeout used to read as 'timed out';
        # the proxy must outlive the poll it asked for
        t0 = time.time()
        st, w2, _ = http("GET", base + "/api/events/wait?after_id=%d&timeout=16"
                         % last)
        took2 = time.time() - t0
        check("idle 16s wait outlives the proxy timeout (no false 'timed out')",
              w2.get("ok") is True and took2 >= 15
              and not (w2.get("result") or {}).get("events"),
              "took=%.1fs %s" % (took2, json.dumps(w2)[:120]))

        # --- 4. catalogs + grammar ---------------------------------------
        st, c, _ = http("GET", base + "/api/catalogs")
        db_tools = {r["name"] for r in c.get("db_search", {}).get("rows", [])}
        check("catalogs lists the db-search catalog",
              c.get("db_search", {}).get("ok") is True
              and {"get_online_census", "get_character",
                   "get_group_membership", "get_equipped_gear"} <= db_tools,
              str(sorted(db_tools)))
        st, gr, _ = http("GET", base + "/api/grammar")
        queries = ((gr.get("result") or {}).get("live_reads") or {}).get("queries")
        check("grammar serves the chat-tunnel grammar (inspect queries)",
              gr.get("ok") is True and queries and "position" in queries,
              json.dumps(gr)[:200])

        # --- 5. query browser execution ----------------------------------
        st, q, _ = http("POST", base + "/api/query",
                        {"tool": "get_character", "params": {"toon": "Ayla"}})
        check("query browser runs a catalog read (envelope + caveat)",
              q.get("ok") is True and q.get("tool") == "get_character"
              and q.get("row_count") == 1 and q.get("caveat", "").strip()
              and "queried_at" in q, json.dumps(q)[:200])
        st, bad, _ = http("POST", base + "/api/query",
                          {"tool": "no_such_tool", "params": {}})
        check("db-search refusals propagate (unknown_tool)",
              bad.get("ok") is False
              and bad.get("code") == "unknown_tool", json.dumps(bad)[:200])
        st, inj, _ = http("POST", base + "/api/query",
                          {"tool": "get_character",
                           "params": {"toon": "Ayla'; DROP TABLE x; --"}})
        check("injection-shaped params are refused upstream",
              inj.get("ok") is False and inj.get("code") == "bad_param",
              json.dumps(inj)[:200])

        # --- 6. live inspect (the A23 path, through the tunnel) ----------
        st, lv, _ = http("POST", base + "/api/live",
                         {"bot": "Ayla", "query": "position"})
        lv_cid = (lv.get("result") or {}).get("correlation_id")
        check("live inspect issues a correlated command",
              lv.get("ok") is True and lv_cid, json.dumps(lv)[:200])
        with open(log, "a") as f:
            f.write(display_line(
                "[System]: Ayla: Ayla position (live): map 1 at "
                "-8925.06, 133.77, 83.33 in Dun Morogh") + "\n")
            f.flush()
        reply = None
        for _ in range(15):
            time.sleep(0.5)
            st, rp, _ = http("GET", base + "/api/reply?cid=%s" % lv_cid)
            if rp.get("ok") and (rp.get("result") or {}).get("reply"):
                reply = rp["result"]["reply"]
                break
        check("inspect [System] reply is matched back by correlation_id",
              reply is not None and "position" in (reply or {}).get("command", ""),
              json.dumps(rp)[:200])
        st, lvbad, _ = http("POST", base + "/api/live",
                            {"bot": "Ayla", "query": "kick"})
        check("non-allowlisted live queries are refused by the GUI",
              lvbad.get("ok") is False
              and lvbad.get("error", {}).get("code") == "gui_read_only",
              json.dumps(lvbad)[:200])

        # --- 6b. mission drawer: orders through director's rails -----------
        st, ms0, _ = http("GET", base + "/api/missions")
        check("missions endpoint answers (empty registry)",
              st == 200 and ms0.get("ok") is True and ms0.get("missions") == {},
              json.dumps(ms0)[:120])
        st, mb, _ = http("POST", base + "/api/mission",
                         {"bot": "Ayla", "op": "detonate"})
        check("mission endpoint refuses unknown ops",
              st == 200 and mb.get("ok") is False
              and mb.get("error", {}).get("code") == "bad_request",
              json.dumps(mb)[:120])
        st, mbad, _ = http("POST", base + "/api/mission",
                           {"bot": "Ayla", "op": "travel",
                            "dest": "NowhereFake"})
        check("unknown destination is refused by validate (SKIP reason back)",
              mbad.get("ok") is False
              and mbad.get("result", {}).get("status") == "refused"
              and "not a known location" in mbad.get("result", {}).get("detail", ""),
              json.dumps(mbad)[:200])
        st, sbad, _ = http("POST", base + "/api/mission",
                           {"bot": "Ayla", "op": "state", "dest": "party"})
        check("state op refuses unknown presets",
              sbad.get("ok") is False
              and sbad.get("error", {}).get("code") == "bad_request",
              json.dumps(sbad)[:200])
        # the OPERATOR'S VERIFY LOOP needs readbacks: the nc ? answerer
        # runs for the whole state-op section (before AND after asks)
        ncstop = {"v": False}
        threading.Thread(target=nc_feeder_loop,
                         args=(base, log, {"Ayla", "Gau", "Locke"}, ncstop),
                         daemon=True).start()
        st, sgr, _ = http("POST", base + "/api/mission",
                          {"bot": "Ayla", "op": "state", "dest": "grind"})
        check("state grind sends the botcmd preset verbatim",
              sgr.get("ok") is True and len(sgr.get("results", [])) == 1
              and "+grind,-quest,-gather,-follow" in
              sgr.get("results", [{}])[0].get("detail", ""),
              json.dumps(sgr)[:200])
        st, sga, _ = http("POST", base + "/api/mission",
                          {"bot": "Ayla", "op": "state", "dest": "gather"})
        check("state gather sends the wiki workflow (strategy + launcher)",
              sga.get("ok") is True and len(sga.get("results", [])) == 2
              and all(r.get("status") == "sent" for r in sga["results"])
              and any("nc +new rpg,-grind,-quest,-follow,+gather"
                      in (r.get("detail") or "") for r in sga["results"])
              and any(r.get("cmd") == "rpg do gather"
                      for r in sga["results"]),
              json.dumps(sga)[:300])
        st, sid, _ = http("POST", base + "/api/mission",
                          {"bot": "Ayla", "op": "state", "dest": "idle"})
        check("state idle sends the nc stand-down (read-only rpg status never sent)",
              sid.get("ok") is True and len(sid.get("results", [])) == 1
              and "-new rpg" in sid.get("results", [{}])[0].get("detail", ""),
              json.dumps(sid)[:260])
        st, sov, _ = http("GET", base + "/api/overview")
        check("the state registry records the last state set (mission column)",
              ((sov.get("states") or {}).get("ayla") or {}).get("state") == "idle",
              json.dumps(sov.get("states"))[:160])

        st, on0, _ = http("POST", base + "/api/mission",
                          {"bot": "Ayla", "op": "online"})
        check("bring-online refuses a bot that is already online (census)",
              on0.get("ok") is False
              and "already online" in on0.get("result", {}).get("detail", ""),
              json.dumps(on0)[:200])
        st, on1, _ = http("POST", base + "/api/mission",
                          {"bot": "Locke", "op": "online"})
        check("bring-online sends the master command for an offline bot",
              on1.get("ok") is True and on1.get("result", {}).get("status") == "sent",
              json.dumps(on1)[:200])
        st, on2, _ = http("POST", base + "/api/mission",
                          {"bot": "Ayla", "op": "offline"})
        check("take-offline sends the master command for an online bot",
              on2.get("ok") is True and on2.get("result", {}).get("status") == "sent",
              json.dumps(on2)[:200])

        st, mt, _ = http("POST", base + "/api/mission",
                         {"bot": "Ayla", "op": "travel", "dest": "BootyBay"})
        check("travel order starts a mission (lease taken)",
              mt.get("ok") is True and mt.get("result", {}).get("status") == "started"
              and "BootyBay" in mt.get("result", {}).get("detail", ""),
              json.dumps(mt)[:200])
        st, ms1, _ = http("GET", base + "/api/missions")
        check("the lease registry shows Ayla travelling to BootyBay",
              (ms1.get("missions", {}).get("Ayla") or {}).get("kind") == "travel"
              and (ms1.get("missions", {}).get("Ayla") or {}).get("name") == "BootyBay",
              json.dumps(ms1)[:200])
        # the mission's arrival evidence: an inspect [System] reply AT the
        # game_tele point -> the travel mission confirms + releases
        arm_arrival_reply(base, "Ayla", log)
        released = False
        for _ in range(45):
            time.sleep(1)
            st, msx, _ = http("GET", base + "/api/missions")
            if "Ayla" not in msx.get("missions", {}):
                released = True
                break
        check("travel mission confirms arrival and releases the lease",
              released, json.dumps(msx)[:200])

        # dead-pid lease entries are pruned, not blocking: a crashed owner
        # holds nothing
        with open(leases_path, "w") as f:
            json.dump({"Gau": {"kind": "farm", "name": "BootyBay",
                               "pid": 999999, "ts": time.time()}}, f)
        st, mdp, _ = http("POST", base + "/api/mission",
                          {"bot": "Gau", "op": "travel", "dest": "BootyBay"})
        check("a dead holder's lease is reclaimed (crash does not block)",
              mdp.get("ok") is True
              and mdp.get("result", {}).get("status") == "started",
              json.dumps(mdp)[:200])

        # --- 6b1. leave party (the whitelisted no-GM whisper) ------------
        st, lv2, _ = http("POST", base + "/api/mission",
                          {"bot": "Ayla", "op": "leave"})
        check("leave party sends the whitelisted 'leave' whisper",
              lv2.get("ok") is True
              and lv2.get("result", {}).get("status") == "sent"
              and "/w Ayla leave" in lv2.get("result", {}).get("detail", ""),
              json.dumps(lv2)[:200])

        # --- 6b1b. disband party (group remove per raid member) -----------
        st, db_, _ = http("POST", base + "/api/mission",
                          {"bot": "Leo", "op": "disband"})
        left = [r for r in db_.get("results", []) if r.get("status") == "sent"]
        check("disband leaves every raid member (offline-but-verified too)",
              db_.get("ok") is True and len(left) == 3
              and all((r.get("detail") or "").startswith("/w ")
                      and r.get("cmd") == "leave" for r in left),
              json.dumps(db_))

        # --- 6b2. the hand-play lock is a live toggle ----------------------
        st, hl, _ = http("POST", base + "/api/handplay",
                         {"toon": "Gau", "on": True})
        check("hand-play lock engages via the API",
              hl.get("ok") is True and "gau" in hl.get("handplay_list", []),
              json.dumps(hl)[:160])
        st, hr, _ = http("POST", base + "/api/mission",
                         {"bot": "Gau", "op": "state", "dest": "grind"})
        hr0 = (hr.get("results") or [{}])[0]
        check("a locked hand-play toon refuses orders (the director rail)",
              hr.get("ok") is False
              and hr0.get("status") == "refused"
              and "hand-play toon" in (hr0.get("detail") or ""),
              json.dumps(hr)[:260])
        st, hu, _ = http("POST", base + "/api/handplay",
                         {"toon": "Gau", "on": False})
        check("hand-play lock releases via the API",
              hu.get("ok") is True and "gau" not in hu.get("handplay_list", []),
              json.dumps(hu)[:160])
        st, hg, _ = http("POST", base + "/api/mission",
                         {"bot": "Gau", "op": "state", "dest": "grind"})
        check("an unlocked toon takes orders again",
              hg.get("ok") is True
              and hg.get("result", {}).get("status") == "confirmed",
              json.dumps(hg)[:260])
        st, hau, _ = http("GET", base + "/api/audit?limit=200")
        check("hand-play toggles are audited",
              any(e.get("action") == "handplay" for e in hau.get("entries", [])),
              json.dumps(hau.get("entries", [])[-2:])[:160])
        # leave a lock on a third toon: the file must carry it to ANY
        # process that starts later (persistence across restarts)
        st, hp3, _ = http("POST", base + "/api/handplay",
                          {"toon": "Crono", "on": True})
        check("the hand-play registry is written to its file",
              hp3.get("ok") is True and "crono" in hp3.get("handplay_list", []),
              json.dumps(hp3)[:160])

        # --- 6b4b. the doctor sweep (self-diagnosis) ----------------------
        st, doc_, _ = http("GET", base + "/api/doctor")
        st, wv, _ = http("GET", base + "/api/overview")
        check("the world liveness signal reads the heartbeat (verdict up)",
              (wv.get("world") or {}).get("state") == "up"
              and (wv.get("world") or {}).get("uptime_s") == 5400,
              json.dumps(wv.get("world"))[:160])
        check("the doctor sweep passes every subsystem on a healthy stack",
              doc_.get("ok") is True
              and doc_.get("rows") and all(r.get("ok") for r in doc_["rows"])
              and "FAIL" not in doc_.get("text", ""),
              json.dumps(doc_.get("rows"))[:260])

        # --- 6b4. the log review digest (nothing through the cracks) ---
        st, rv0, _ = http("GET", base + "/api/review")
        check("review digest answers with everything before the first mark",
              rv0.get("ok") is True and rv0.get("counts", {}).get("actions", 0) >= 1
              and (rv0.get("since") == 0), json.dumps(rv0.get("counts"))[:160])
        st, mk1, _ = http("POST", base + "/api/review", {"action": "mark"})
        check("marking reviewed stores the cursor",
              mk1.get("ok") is True and mk1.get("review", {}).get("event_id", -1) >= 0,
              json.dumps(mk1)[:160])
        st, lv9, _ = http("POST", base + "/api/mission",
                          {"bot": "Ayla", "op": "leave"})
        st, rv1, _ = http("GET", base + "/api/review")
        check("a new action shows up in the since-last-review digest",
              rv1.get("counts", {}).get("actions") == 1
              and "leave" in ((rv1.get("new_actions") or [{}])[0].get("detail") or ""),
              json.dumps(rv1.get("new_actions"))[:200])
        st, mk2, _ = http("POST", base + "/api/review", {"action": "mark"})
        st, rv2, _ = http("GET", base + "/api/review")
        check("a fresh digest after marking is empty of actions",
              rv2.get("counts", {}).get("actions") == 0,
              json.dumps(rv2.get("counts"))[:160])

        ncstop["v"] = True
        # --- 6b3. Tier 1 gating (strict for missions, permissive whispers)
        st, t1s, _ = http("POST", base + "/api/mission",
                          {"bot": "Cecil", "op": "travel", "dest": "BootyBay"})
        check("Tier 1: a mission to an UNVERIFIED offline toon is refused",
              t1s.get("ok") is False
              and "Tier 1" in ((t1s.get("error") or {}).get("message") or ""),
              json.dumps(t1s)[:200])
        nc2stop = {"v": False}
        threading.Thread(target=nc_feeder_loop,
                         args=(base, log, {"Ayla", "Gau", "Locke"}, nc2stop),
                         daemon=True).start()
        st, t1w, _ = http("POST", base + "/api/mission",
                          {"bot": "Locke", "op": "state", "dest": "grind"})
        check("Tier 1: a whisper to a probe-verified offline toon goes through",
              t1w.get("ok") is True
              and t1w.get("result", {}).get("status") == "confirmed",
              json.dumps(t1w)[:260])
        nc2stop["v"] = True

        st, mf, _ = http("POST", base + "/api/mission",
                         {"bot": "Ayla", "op": "farm", "dest": "BootyBay"})
        check("farm order starts the gather keeper",
              mf.get("ok") is True and mf.get("result", {}).get("status") == "started",
              json.dumps(mf)[:200])
        st, ms2, _ = http("GET", base + "/api/missions")
        check("the lease registry shows Ayla farming in BootyBay",
              (ms2.get("missions", {}).get("Ayla") or {}).get("kind") == "farm",
              json.dumps(ms2)[:200])
        arm_arrival_reply(base, "Ayla", log)
        ARM = "nc +new rpg,-grind,-quest,-follow,+gather"
        asked = False
        for _ in range(40):
            time.sleep(1)
            st, ev, _ = http("GET", base + "/api/events?after_id=0&limit=1000")
            cmds = [x.get("command") for x in
                    (ev.get("result") or {}).get("events", [])]
            if ARM in cmds and "rpg do gather" in cmds:
                # feed the rpg status readback its answer (the [whisper]
                # reply shape) once the ask goes out
                if "rpg status" in cmds and not asked:
                    with open(log, "a") as f:
                        f.write(display_line("[Ayla] [whisper]: doing gather")
                                + "\n")
                        f.flush()
                    asked = True
                continue
            armed = ARM in cmds and "rpg do gather" in cmds
            if armed and asked:
                break
        check("farm arms the wiki workflow: strategies + 'rpg do gather'",
              ARM in cmds and "rpg do gather" in cmds,
              json.dumps(cmds[-10:])[:240])
        narr = False
        for _ in range(30):
            time.sleep(1)
            st, ev, _ = http("GET", base + "/api/events?after_id=0&limit=1000")
            cmds = [x.get("command") for x in
                    (ev.get("result") or {}).get("events", [])]
            if any(c and "arrived at BootyBay and is gathering" in c
                   for c in cmds):
                narr = True
                break
        check("the rpg status readback confirms gathering before narration",
              narr, json.dumps(cmds[-6:])[:200])
        st, msd, _ = http("POST", base + "/api/mission",
                          {"bot": "Ayla", "op": "standdown"})
        check("stand down releases the farm lease",
              msd.get("ok") is True, json.dumps(msd)[:160])
        gone = False
        for _ in range(10):
            time.sleep(1)
            st, msy, _ = http("GET", base + "/api/missions")
            if "Ayla" not in msy.get("missions", {}):
                gone = True
                break
        check("registry shows the farm lease gone after stand down", gone)

        # --- 6c. warning flags: the bag scan ------------------------------
        st, wf, _ = http("GET", base + "/api/warnings")
        check("warnings endpoint answers (bag scan, no flags on the fixture)",
              st == 200 and wf.get("ok") is True and wf.get("warnings") == []
              and wf.get("scan_errors") == []
              and wf.get("census_ok") is True, json.dumps(wf)[:200])
        check("the bag scan exposes per-toon capacity (the fill-up answer)",
              (wf.get("bags") or {}).get("Ayla", {}).get("free", -1) > 0
              and (wf.get("bags") or {}).get("Ayla", {}).get("cap", 0) >= 16,
              json.dumps(wf.get("bags"))[:200])

        # --- 6c2. progress + wallets + audit + fleet actions ----------------
        check("overview carries leveling progress (xp/hr per toon)",
              (o.get("progress") or {}).get("ok") is True
              and any(r[0] == "Ayla" for r in (o.get("progress") or {}).get("rows", [])),
              json.dumps(o.get("progress"))[:160])
        check("overview carries wallets (money per toon)",
              (o.get("wallets") or {}).get("ok") is True
              and any(r[0] == "Ayla" and r[3] == "5000"
                      for r in (o.get("wallets") or {}).get("rows", [])),
              json.dumps(o.get("wallets"))[:160])
        st, au0, _ = http("GET", base + "/api/audit?limit=100")
        check("audit log records the GUI's write actions so far",
              au0.get("ok") is True
              and any(e.get("action") == "mission" for e in au0.get("entries", [])),
              json.dumps(au0)[:200])
        st, fall, _ = http("POST", base + "/api/mission",
                           {"bot": "ALL", "op": "online"})
        sent_cmds = [r for r in fall.get("results", []) if r.get("status") == "sent"]
        check("fleet bring-all-online lands at least one add (skips expected)",
              fall.get("ok") is True and sent_cmds
              and all(r["cmd"] == "bot add" for r in sent_cmds),
              json.dumps(fall)[:200])
        st, fpd, _ = http("POST", base + "/api/mission",
                          {"bot": "ALL", "op": "standdown"})
        check("panic stand-down-all broadcasts the nc stand-down",
              fpd.get("ok") is True
              and any("nc -new rpg" in (r.get("detail") or "")
                      for r in fpd.get("results", []) if r.get("status") == "sent"),
              json.dumps(fpd)[:200])
        st, au1, _ = http("GET", base + "/api/audit?limit=100")
        check("audit log shows the fleet actions too",
              sum(1 for e in au1.get("entries", [])
                  if "ALL" in (e.get("detail") or "")) >= 2,
              json.dumps(au1.get("entries", [])[-2:])[:200])

        # --- 6c3. vendor_all: the bulk bag-alarm answer ------------------
        st, va0, _ = http("POST", base + "/api/mission",
                          {"bot": "ALL", "op": "vendor_all"})
        check("vendor_all without a town is refused",
              va0.get("ok") is False
              and va0.get("error", {}).get("code") == "bad_request",
              json.dumps(va0)[:160])
        st, va1, _ = http("POST", base + "/api/mission",
                          {"bot": "ALL", "op": "vendor_all",
                           "dest": "BootyBay"})
        check("vendor_all with no bags-full toons is a clean no-op",
              va1.get("ok") is True and va1.get("results") == []
              and "no bags-full" in va1.get("note", ""),
              json.dumps(va1)[:160])

        # drain the stale pending probes BEFORE the vendor leg starts
        drain_pending_probes(base, "Ayla", log,
                             "[System]: Ayla: -14420.0 480.0 26.0 0 "
                             "0 |Stranglethorn Vale|")
        # CURSOR: this section runs late; drain to the newest event first
        vcur = 0
        while True:
            st, evx, _ = http("GET", base +
                              "/api/events?after_id=%d&limit=1000" % vcur)
            evr = evx.get("result") or {}
            batch = evr.get("events") or []
            if not batch:
                break
            vcur = evr.get("last_id") or vcur
            if len(batch) < 1000:
                break

        # --- 6d. vendor run mission (the full-bags action) ----------------
        st, mv, _ = http("POST", base + "/api/mission",
                         {"bot": "Ayla", "op": "vendor", "dest": "BootyBay"})
        check("vendor run starts (lease taken, kind vendor)",
              mv.get("ok") is True and mv.get("result", {}).get("status") == "started"
              and "BootyBay" in mv.get("result", {}).get("detail", ""),
              json.dumps(mv)[:200])
        st, mvv, _ = http("POST", base + "/api/mission",
                          {"bot": "Ayla", "op": "travel", "dest": "BootyBay"})
        check("a vendor lease refuses a second mission (one per bot)",
              mvv.get("ok") is False
              and "already selling in" in mvv.get("result", {}).get("detail", ""),
              json.dumps(mvv)[:200])
        feed_all_pending(base, "Ayla", log,
                         "[System]: Ayla: -14420.0 480.0 26.0 0 "
                         "0 |Stranglethorn Vale|", deadline=30)
        # the sell whisper goes out; feed the bot's ANSWER (correlated
        # whisper reply) - the mission must surface it as evidence
        sold = False
        for _ in range(30):
            time.sleep(1)
            st, ev, _ = http("GET", base +
                             "/api/events?after_id=%d&limit=500" % vcur)
            evs = (ev.get("result") or {}).get("events", [])
            if any(x.get("direction") == "sent"
                   and x.get("command") == "s vendor"
                   and x.get("status") == "pending" for x in evs):
                with open(log, "a") as f:
                    f.write(display_line("[Ayla] [whisper]: sold 12 items") + "\n")
                    f.flush()
                sold = True
                break
        check("vendor run sends the sell whisper after arrival",
              sold, "vcur=%s cmds=%s" % (vcur, cmds[:20]))
        narrated = False
        for _ in range(30):
            time.sleep(1)
            st, ev, _ = http("GET", base +
                             "/api/events?after_id=%d&limit=500" % vcur)
            cmds = [x.get("command") for x in
                    (ev.get("result") or {}).get("events", [])]
            if any(c and "sold 12 items" in c for c in cmds):
                narrated = True
                break
        check("the bot's sell answer is captured and narrated (evidence)",
              narrated, "vcur=%s cmds=%s" % (vcur, cmds[:20]))
        vgone = False
        for _ in range(20):
            time.sleep(1)
            st, msz, _ = http("GET", base + "/api/missions")
            if "Ayla" not in msz.get("missions", {}):
                vgone = True
                break
        check("vendor run releases the lease when done", vgone)

        # --- 6d2. the rpg status readback + the echo dedup ----------------
        stop1 = {"v": False}
        th = threading.Thread(target=status_feeder_thread,
            args=(base, log, {"Ayla": "I am doing gather"}, stop1))
        th.start()
        st, rs1, _ = http("POST", base + "/api/mission",
                          {"bot": "Ayla", "op": "status"})
        stop1["v"] = True
        th.join(timeout=2)
        check("the status op asks and returns the bot's rpg status answer",
              rs1.get("ok") is True
              and "doing gather" in rs1.get("result", {}).get("detail", ""),
              json.dumps(rs1)[:200])

        stop2 = {"v": False}
        th2 = threading.Thread(target=status_feeder_thread,
            args=(base, log, {"Ayla": "gathering in Stranglethorn",
                              "Gau": "grinding gnolls"}, stop2))
        th2.start()
        st, rs2, _ = http("POST", base + "/api/mission",
                          {"bot": "ALL", "op": "status"})
        stop2["v"] = True
        th2.join(timeout=6)
        names = {x.get("bot"): x for x in rs2.get("results", [])}
        check("the fleet status sweep answers every online toon",
              rs2.get("ok") is True
              and (names.get("Ayla", {}).get("status") == "answered"
                   and "gathering" in (names.get("Ayla", {}).get("detail") or ""))
              and (names.get("Gau", {}).get("status") == "answered"
                   and "gnolls" in (names.get("Gau", {}).get("detail") or "")),
              json.dumps(rs2)[:240])

        # the master's whisper_inform echo must NOT become an event
        with open(log, "a") as f:
            f.write(display_line(
                "[Leo] [whisper_inform]: ECHOxDEADxBEEF test line") + "\n")
            f.flush()
        time.sleep(3)
        st, ev, _ = http("GET", base + "/api/events?after_id=0&limit=1000")
        check("Leo's own whisper_inform echo is deduped (no event)",
              not any("ECHOxDEADxBEEF" in (x.get("command") or "")
                      for x in (ev.get("result") or {}).get("events", [])),
              "the echo line must never appear in the event stream")

        # --- 6e. the farm drift watchdog (drain the stale probes first - see
        # drain_pending_probes: the tunnel binds oldest-pending-first) (live 2026-11-23: a leased farm
        # bot was found standing in another city; the keeper must verify) --
        drain_pending_probes(base, "Ayla", log,
                             "[System]: Ayla: -14420.0 480.0 26.0 0 "
                             "0 |Stranglethorn Vale|")
        st, fd1, _ = http("POST", base + "/api/mission",
                          {"bot": "Ayla", "op": "farm", "dest": "BootyBay"})
        check("drift test: the farm restarts for the watchdog probe",
              fd1.get("ok") is True
              and fd1.get("result", {}).get("status") == "started",
              json.dumps(fd1)[:160])
        feed_all_pending(base, "Ayla", log,
                         "[System]: Ayla: -14420.0 480.0 26.0 0 "
                         "0 |Stranglethorn Vale|", deadline=30)
        # CURSOR DISCIPLINE: this test runs late in the session and the
        # event stream is long - after_id=0 only returns the OLDEST 1000
        # events. Drain to the newest id first, then read fresh-only.
        cur = 0
        while True:
            st, evx, _ = http("GET", base +
                              "/api/events?after_id=%d&limit=1000" % cur)
            evr = evx.get("result") or {}
            batch = evr.get("events") or []
            if not batch:
                break
            cur = evr.get("last_id") or cur
            if len(batch) < 1000:
                break
        # the keeper's first cycle probes the position: answer with a FAR
        # position (Stormwind) - the watchdog must catch the drift
        fed = feed_all_pending(base, "Ayla", log,
                               "[System]: Ayla: -8800.0 700.0 100.0 0 "
                               "0 |Stormwind City|", deadline=40)
        drift_seen, sold_back = False, False
        for _ in range(60):
            time.sleep(1)
            st, evx, _ = http("GET", base +
                              "/api/events?after_id=%d&limit=500" % cur)
            evr = evx.get("result") or {}
            batch = evr.get("events") or []
            cur = max(cur, evr.get("last_id") or cur)
            for x in batch:
                c = x.get("command") or ""
                if "drifted away from BootyBay" in c:
                    drift_seen = True
                if c.startswith(".tele") and "BootyBay" in c:
                    sold_back = True   # the travel order's wire form
        dtail = ["%s|%s|%s" % (x.get("direction"), x.get("status"),
                                (x.get("command") or "")[:70])
                 for x in batch]
        check("the drift watchdog: a farmed bot caught in another city is "
              "sent back (narrated + travel order)",
              drift_seen and sold_back,
              "fed=%s drift=%s sent_back=%s fresh=%s"
              % (fed, drift_seen, sold_back, dtail[-14:]))
        fled = False
        for _ in range(50):
            time.sleep(1)
            st, msz, _ = http("GET", base + "/api/missions")
            if "Ayla" not in msz.get("missions", {}):
                fled = True
                break
        check("the drift watchdog ends the farm when the bot will not return",
              fled, json.dumps(msz.get("missions", {}))[:160])

        # --- 7. the Leo button --------------------------------------------
        st, ls, _ = http("GET", base + "/api/leo")
        check("Leo session status endpoint answers",
              st == 200 and ls.get("ok") is True
              and isinstance(ls.get("active"), bool), json.dumps(ls)[:120])
        st, lb, _ = http("POST", base + "/api/leo", {"action": "detonate"})
        check("Leo button refuses unknown actions",
              st == 200 and lb.get("ok") is False
              and lb.get("error", {}).get("code") == "bad_request",
              json.dumps(lb)[:120])
        st, lstart, _ = http("POST", base + "/api/leo", {"action": "start"})
        check("Leo start returns a structured answer (never a 5xx/crash)",
              st == 200 and isinstance(lstart.get("ok"), bool)
              and isinstance(lstart.get("active"), bool)
              and "unit" in lstart, json.dumps(lstart)[:200])

        # --- 8. graceful degradation: both siblings down -------------------
        dead_port = 18767
        dead_base = "http://127.0.0.1:%d" % dead_port
        procs.append(start_gui(dead_port, "127.0.0.1", {
            "WEBGUI_TUNNEL": "http://127.0.0.1:18999",
            "WEBGUI_DBSEARCH_SOCK": os.path.join(tmp, "nope-db.sock"),
            "WEBGUI_HANDPLAY_FILE": os.path.join(tmp, "handplay.json")}))
        check("down-siblings web-gui instance came up", wait_port(dead_port))
        st, dhp, _ = http("GET", dead_base + "/api/handplay")
        check("hand-play locks persist into a freshly started process",
              dhp.get("ok") is True and "crono" in dhp.get("handplay", []),
              json.dumps(dhp)[:160])
        st, o2, _ = http("GET", dead_base + "/api/overview")
        check("degraded overview still answers 200 ok",
              st == 200 and o2.get("ok") is True)
        check("degraded census reports unreachable (not a crash)",
              o2.get("census", {}).get("ok") is False
              and o2.get("census", {}).get("error", {}).get("code") == "unreachable",
              json.dumps(o2.get("census", {}))[:200])
        check("degraded tunnel + leo_char + leo_group report unreachable",
              o2.get("tunnel", {}).get("ok") is False
              and o2.get("leo_char", {}).get("ok") is False
              and o2.get("leo_group", {}).get("ok") is False)
        st, e2, _ = http("GET", dead_base + "/api/events")
        check("degraded feed reports unreachable",
              e2.get("ok") is False
              and e2.get("error", {}).get("code") == "unreachable",
              json.dumps(e2)[:200])
        st, q2, _ = http("POST", dead_base + "/api/query",
                         {"tool": "get_character", "params": {"toon": "Ayla"}})
        check("degraded query reports unreachable",
              q2.get("ok") is False
              and q2.get("error", {}).get("code") == "unreachable")
        req = urllib.request.Request(dead_base + "/")
        with urllib.request.urlopen(req, timeout=10) as r:
            check("degraded page still serves", r.status == 200)

        # --- 9. bind guard + LAN knob + login ------------------------------
        g = subprocess.run(
            [sys.executable, os.path.join(HERE, "web_gui.py"), "serve"],
            env=dict(os.environ, WEBGUI_PORT="18900", WEB_GUI_BIND="0.0.0.0"),
            capture_output=True, text=True, timeout=10)
        check("refuses the wildcard bind (a specific address is required)",
              g.returncode != 0 and "SPECIFIC" in (g.stderr + g.stdout),
              (g.stderr + g.stdout).strip()[:120])

        st, h0, _ = http("GET", base + "/health")
        check("loopback default requires no login",
              st == 200 and h0.get("ok") is True)

        p2 = start_gui(18768, "127.0.0.2")
        procs.append(p2)
        check("specific-address bind accepted (127.0.0.2)",
              wait_port(18768, host="127.0.0.2"))
        st, h2, _ = http("GET", "http://127.0.0.2:18768/health")
        check("127.0.0.2 serves without a login (loopback /8)",
              st == 200 and h2.get("ok") is True)

        sys.path.insert(0, HERE)
        import web_gui
        lan_addr = web_gui.resolve_lan_addr()
        check("lan mode resolves a non-loopback host address",
              bool(lan_addr) and not web_gui.is_loopback(lan_addr),
              repr(lan_addr))

        g = subprocess.run(
            [sys.executable, os.path.join(HERE, "web_gui.py"), "serve"],
            env=dict(os.environ, WEBGUI_PORT="18901", WEB_GUI_BIND="lan"),
            capture_output=True, text=True, timeout=10)
        check("lan bind without WEB_GUI_PASS refuses to start",
              g.returncode != 0 and "WEB_GUI_PASS" in (g.stderr + g.stdout),
              (g.stderr + g.stdout).strip()[:120])

        blocker = socket.socket()
        blocker.bind((lan_addr, 18903))
        try:
            g = subprocess.run(
                [sys.executable, os.path.join(HERE, "web_gui.py"), "serve"],
                env=dict(os.environ, WEBGUI_PORT="18903", WEB_GUI_BIND="lan",
                         WEB_GUI_PASS="sekret"),
                capture_output=True, text=True, timeout=15)
        finally:
            blocker.close()
        out = g.stderr + g.stdout
        check("non-loopback bind prints the prominent startup warning",
              "WARNING: web-gui is bound to" in out
              and "CLEARTEXT" in out and "firewalling" in out,
              out.strip()[:160])
        check("warning instance never listened (port collision, exits)",
              g.returncode != 0)

        p3 = start_gui(18769, "127.0.0.2",
                       {"WEB_GUI_AUTH": "on", "WEB_GUI_USER": "director",
                        "WEB_GUI_PASS": "sekret"})
        procs.append(p3)
        authed_base = "http://127.0.0.2:18769"
        check("auth-enabled instance came up",
              wait_connect(18769, host="127.0.0.2"))
        st, r0, _ = http("GET", authed_base + "/api/overview")
        check("unauthenticated API request is refused (401)",
              st == 401 and r0.get("ok") is False, json.dumps(r0)[:120])
        st, r1, _ = http("POST", authed_base + "/login",
                         {"user": "director", "pass": "wrong"})
        check("wrong password is refused (401)",
              st == 401 and r1.get("ok") is False)
        st, r2, hh = http("POST", authed_base + "/login",
                          {"user": "director", "pass": "sekret"})
        cookie = (hh.get("Set-Cookie") or "").split(";", 1)[0]
        check("login issues a session cookie",
              st == 200 and r2.get("ok") is True
              and cookie.startswith("webgui_session="),
              str(hh.get("Set-Cookie"))[:120])
        st, r3, _ = http("GET", authed_base + "/api/overview",
                         headers={"Cookie": cookie})
        check("session cookie unlocks the API",
              st == 200 and r3.get("ok") is True, json.dumps(r3)[:120])
        st, r4, _ = http("POST", authed_base + "/logout",
                         headers={"Cookie": cookie})
        st, r5, _ = http("GET", authed_base + "/api/overview",
                         headers={"Cookie": cookie})
        check("logout invalidates the session",
              r4.get("ok") is True and st == 401)

        g = subprocess.run(
            [sys.executable, os.path.join(HERE, "web_gui.py"), "serve"],
            env=dict(os.environ, WEBGUI_PORT="18902", WEB_GUI_BIND="lan",
                     WEB_GUI_AUTH="off", WEB_GUI_PASS="sekret"),
            capture_output=True, text=True, timeout=10)
        check("WEB_GUI_AUTH=off is refused on a non-loopback bind",
              g.returncode != 0 and "refused" in (g.stderr + g.stdout),
              (g.stderr + g.stdout).strip()[:120])

    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        try:
            os.close(fifo_rd)
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAIL == 0:
        print("web-gui proof harness: %d pass / 0 fail" % PASS)
        return 0
    print("web-gui proof harness: %d pass / %d FAIL" % (PASS, FAIL))
    return 1


if __name__ == "__main__":
    sys.exit(main())
