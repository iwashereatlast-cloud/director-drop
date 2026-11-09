#!/usr/bin/env python3
"""web-gui proof harness - runs WITHOUT a live worldserver, DB, or the real
8888 command server.

Boots the three real sibling services in fixture mode (db-search on a
sqlite fixture built from services/db-search/fixtures/fixture.sql,
chat-tunnel against a temp log + FIFO, rtt-engine against the mock
command server + GUID fixture from services/rtt-engine/fixtures/), then
drives the REAL web-gui over HTTP and asserts:

  1. the dashboard page serves with all three panels;
  2. /api/overview merges census + tunnel health + rtt ping, with the
     staleness caveat surfaced;
  3. /api/events streams real tunnel events (a sent command bound to its
     reply by correlation_id);
  4. /api/catalogs lists both catalogs;
  5. /api/query executes a catalog read and propagates db-search refusals;
  6. /api/rtt runs the read-only rtt ops and REFUSES non-read ops
     (gui_read_only) before anything leaves the process;
  7. graceful degradation: with all three siblings down, every panel
     endpoint still answers 200 with a per-backend "unreachable" state;
  8. the bind knob: the wildcard is always refused, a specific address
     (127.0.0.2) is accepted, WEB_GUI_BIND=lan resolves the host's current
     non-loopback address, refuses to start without WEB_GUI_PASS, and
     prints the prominent startup warning (proved WITHOUT ever listening
     on the LAN - the warning fires before the bind, so a port collision
     proves it and the process exits);
  9. the login screen: the loopback default needs no login; with auth on,
     unauthenticated API requests get 401, wrong passwords get 401, a
     correct login issues a session cookie that unlocks the API, logout
     invalidates it, and WEB_GUI_AUTH=off is refused on non-loopback binds
     (the whole flow is exercised on 127.0.0.2 - the harness itself never
     binds a non-loopback interface).

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
RTTENGINE = os.path.join(REPO, "services", "rtt-engine")

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
        with urllib.request.urlopen(req, timeout=20) as r:
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


def start_gui(port, bind, extra_env=None, errfile=None):
    env = dict(os.environ, WEBGUI_PORT=str(port), WEB_GUI_BIND=bind)
    env.update(extra_env or {})
    err = open(errfile, "w+") if errfile else subprocess.DEVNULL
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "web_gui.py"),
                          "serve"], env=env,
                         stdout=subprocess.DEVNULL, stderr=err)
    if errfile:
        err.close()
    return p


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

        # --- fixture-mode chat-tunnel -----------------------------------
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
                     CHAT_TUNNEL_DB=os.path.join(tmp, "tunnel.db")),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        check("chat-tunnel fixture mode came up",
              wait_port(tunnel_port))

        # --- fixture-mode rtt-engine (mock 8888 + GUID fixture) ---------
        rtt_sock = os.path.join(tmp, "rtt-engine.sock")
        pb_port = 18888
        procs.append(subprocess.Popen(
            [sys.executable, os.path.join(RTTENGINE, "fixtures",
                                          "mock-cmdserver.py"), str(pb_port),
             os.path.join(RTTENGINE, "fixtures", "cmdserver-responses.json")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(0.3)
        procs.append(subprocess.Popen(
            [sys.executable, os.path.join(RTTENGINE, "rtt_engine.py"), "serve"],
            env=dict(os.environ, RTT_SOCK=rtt_sock, RTT_PBHOST="127.0.0.1",
                     RTT_PBPORT=str(pb_port), RTT_GUID_SOURCE="fixture",
                     RTT_GUID_FIXTURE=os.path.join(RTTENGINE, "fixtures",
                                                   "guid-fixture.json")),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        check("rtt-engine fixture mode came up", wait_sock(rtt_sock))

        # --- the real web-gui -------------------------------------------
        port = 18766
        base = "http://127.0.0.1:%d" % port
        procs.append(subprocess.Popen(
            [sys.executable, os.path.join(HERE, "web_gui.py"), "serve"],
            env=dict(os.environ, WEBGUI_PORT=str(port),
                     WEBGUI_TUNNEL="http://127.0.0.1:%d" % tunnel_port,
                     WEBGUI_DBSEARCH_SOCK=db_sock,
                     WEBGUI_RTT_SOCK=rtt_sock,
                     WEBGUI_CENSUS="i_was_here"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        check("web-gui came up", wait_port(port))

        # --- 1. dashboard page ------------------------------------------
        req = urllib.request.Request(base + "/")
        with urllib.request.urlopen(req, timeout=10) as r:
            page = r.read().decode()
        check("page serves with the three panels",
              all(x in page for x in
                  ('id="panel-bots"', 'id="panel-feed"', 'id="panel-catalog"')))

        # --- 2. overview: census + health merge -------------------------
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
        check("overview rtt ping ok",
              o.get("rtt_ping", {}).get("ok") is True)

        # --- 3. tunnel feed: send a command, append its reply -----------
        st, sc, _ = http("POST", "http://127.0.0.1:%d/" % tunnel_port,
                      {"tool": "send_command",
                       "args": {"bot": "Ayla", "cmd": "who"}})
        check("harness sent a command through chat-tunnel",
              sc.get("ok") is True, json.dumps(sc)[:200])
        cid = sc.get("result", {}).get("correlation_id")
        with open(log, "a") as f:
            f.write("[Ayla] [whisper]: Ayla here\n")
            f.flush()
        time.sleep(1.0)
        st, e, _ = http("GET", base + "/api/events?limit=50")
        evs = e.get("events", [])
        sents = [x for x in evs if x["direction"] == "sent"]
        replies = [x for x in evs if x["direction"] == "reply"]
        check("feed streams sent events with correlation_id",
              e.get("ok") is True and sents
              and all(x.get("correlation_id") for x in sents),
              json.dumps(e)[:200])
        check("feed shows the reply matched to the right command",
              any(x.get("matched_correlation_id") == cid for x in replies),
              json.dumps(replies)[:200])
        st, er, _ = http("GET", base + "/api/events?limit=50&direction=reply")
        check("feed direction filter works",
              er.get("ok") is True
              and all(x["direction"] == "reply" for x in er.get("events", [])))

        # --- 4. catalogs -------------------------------------------------
        st, c, _ = http("GET", base + "/api/catalogs")
        db_tools = {r["name"] for r in c.get("db_search", {}).get("rows", [])}
        rtt_ops = {x["op"] for x in
                   (c.get("rtt_engine", {}).get("result", {}) or {}).get("ops", [])}
        check("catalogs lists the db-search catalog",
              c.get("db_search", {}).get("ok") is True
              and {"get_online_census", "get_character",
                   "get_group_membership"} <= db_tools, str(db_tools))
        check("catalogs lists the rtt-engine op catalog",
              c.get("rtt_engine", {}).get("ok") is True
              and {"catalog", "ping", "pinfo"} <= rtt_ops, str(rtt_ops))

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

        # --- 6. rtt proxy: read ops only -----------------------------------
        st, p, _ = http("POST", base + "/api/rtt",
                     {"op": "pinfo", "name": "Vivi"})
        check("rtt pinfo answers live fixture position",
              p.get("ok") is True and p.get("result", {}).get("online") is True
              and len(p.get("result", {}).get("position", [])) == 3,
              json.dumps(p)[:200])
        st, k, _ = http("POST", base + "/api/rtt", {"op": "kick", "name": "Vivi"})
        check("non-read rtt ops are refused by the GUI itself",
              k.get("ok") is False and k.get("error", {}).get("code") == "gui_read_only",
              json.dumps(k)[:200])
        st, off, _ = http("POST", base + "/api/rtt", {"op": "pinfo", "name": "Cecil"})
        check("rtt offline fallback is a soft answer, not an error",
              off.get("ok") is True
              and off.get("result", {}).get("online") is False,
              json.dumps(off)[:200])

        # --- 7. graceful degradation: all siblings down -------------------
        dead_port = 18767
        dead_base = "http://127.0.0.1:%d" % dead_port
        procs.append(subprocess.Popen(
            [sys.executable, os.path.join(HERE, "web_gui.py"), "serve"],
            env=dict(os.environ, WEBGUI_PORT=str(dead_port),
                     WEBGUI_TUNNEL="http://127.0.0.1:18999",
                     WEBGUI_DBSEARCH_SOCK=os.path.join(tmp, "nope-db.sock"),
                     WEBGUI_RTT_SOCK=os.path.join(tmp, "nope-rtt.sock")),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        check("down-siblings web-gui instance came up", wait_port(dead_port))
        st, o2, _ = http("GET", dead_base + "/api/overview")
        check("degraded overview still answers 200 ok",
              st == 200 and o2.get("ok") is True)
        check("degraded census reports unreachable (not a crash)",
              o2.get("census", {}).get("ok") is False
              and o2.get("census", {}).get("error", {}).get("code") == "unreachable",
              json.dumps(o2.get("census", {}))[:200])
        check("degraded tunnel + rtt report unreachable",
              o2.get("tunnel", {}).get("ok") is False
              and o2.get("rtt_ping", {}).get("ok") is False)
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

        # --- 8. bind guard + LAN knob + login ----------------------------
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

        # specific-address bind accepted (loopback /8, login stays off)
        p2 = start_gui(18768, "127.0.0.2")
        procs.append(p2)
        check("specific-address bind accepted (127.0.0.2)",
              wait_port(18768, host="127.0.0.2"))
        st, h2, _ = http("GET", "http://127.0.0.2:18768/health")
        check("127.0.0.2 serves without a login (loopback /8)",
              st == 200 and h2.get("ok") is True)

        # lan mode: resolves the host's current address (unit, no bind)
        sys.path.insert(0, HERE)
        import web_gui
        lan_addr = web_gui.resolve_lan_addr()
        check("lan mode resolves a non-loopback host address",
              bool(lan_addr) and not web_gui.is_loopback(lan_addr),
              repr(lan_addr))

        # lan mode without a password refuses to start
        g = subprocess.run(
            [sys.executable, os.path.join(HERE, "web_gui.py"), "serve"],
            env=dict(os.environ, WEBGUI_PORT="18901", WEB_GUI_BIND="lan"),
            capture_output=True, text=True, timeout=10)
        check("lan bind without WEB_GUI_PASS refuses to start",
              g.returncode != 0 and "WEB_GUI_PASS" in (g.stderr + g.stdout),
              (g.stderr + g.stdout).strip()[:120])

        # lan mode with a password: warning fires before the listener binds.
        # A dummy socket (bound, never listening) holds the resolved LAN
        # address + port, so the GUI's bind fails right AFTER the warning -
        # proving the warning without ever listening on the LAN interface
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

        # login flow, exercised on 127.0.0.2 with WEB_GUI_AUTH=on (loopback,
        # so the harness itself never touches a non-loopback interface)
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
              st == 200 and r2.get("ok") is True and cookie.startswith("webgui_session="),
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

        # WEB_GUI_AUTH=off is refused on non-loopback binds
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
