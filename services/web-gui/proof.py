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
        con.commit()
    finally:
        con.close()


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
        procs.append(start_gui(port, "127.0.0.1", {
            "WEBGUI_TUNNEL": "http://127.0.0.1:%d" % tunnel_port,
            "WEBGUI_DBSEARCH_SOCK": db_sock,
            "WEBGUI_CENSUS": "i_was_here",
            "WEBGUI_LEO": "Leo",
        }))
        check("web-gui came up", wait_port(port))

        # --- 1. dashboard page ------------------------------------------
        req = urllib.request.Request(base + "/")
        with urllib.request.urlopen(req, timeout=10) as r:
            page = r.read().decode()
        check("page serves with the three panels",
              all(x in page for x in
                  ('id="panel-bots"', 'id="panel-feed"', 'id="panel-catalog"')))

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
            "WEBGUI_DBSEARCH_SOCK": os.path.join(tmp, "nope-db.sock")}))
        check("down-siblings web-gui instance came up", wait_port(dead_port))
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
