#!/usr/bin/env python3
"""rtt-engine ("8.8.8.8") - real-time / non-polled query service.

Service 3 of 4 (docs/services-roadmap.md, console end-of-life decision
2026-11-09). Owns the tick-time-sensitive surface: LIVE reads of ONLINE
bots via the stock mod-playerbots command server
(`Playerbots.CommandServerPort`, default 8888, read-only line protocol
`<verb>,<guid>\\n` per PlayerbotCommandServer.cpp - zero module changes).
It answers live position/state/HP for ONLINE bots ONLY; offline truth is
db-search (logout flushes the final position to `characters`).

Interface (STABLE - "rtt-engine/1"):
  transport : unix socket (default /tmp/rtt-engine.sock), one JSON request
              per line, one JSON response per line, connection per request.
  request   : {"op": "<name>", ...params}
  response  : {"interface": "rtt-engine/1", "ok": bool, "op": str,
               "backend": "pbcmd", "queried_at": iso8601,
               "latency_ms": int, "result": {...}|null,
               "error": {"code": str, "message": str,
                         "route_hint": str|omitted}|null}

Ops: catalog, ping, pinfo. Refused: DB-answerable names -> db-search;
`kick` -> chat-tunnel (`.playerbots bot remove` covers registered bots;
force-kicking a non-bot plain session is an accepted gap).

SECURITY: the module's command server has NO auth and binds 0.0.0.0 - this
service must talk to 127.0.0.1:8888 ONLY (RTT_PBHOST), and the operator
must firewall port 8888 from the outside (see README.md).

Stdlib only. Modes:
  rtt_engine.py serve [--socket P]
  rtt_engine.py query '<json>' [--socket P]     one-shot client
"""
import argparse
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import time

INTERFACE = "rtt-engine/1"
SOCK = os.environ.get("RTT_SOCK", "/tmp/rtt-engine.sock")
PBHOST = os.environ.get("RTT_PBHOST", "127.0.0.1")
PBPORT = int(os.environ.get("RTT_PBPORT", "8888"))
TIMEOUT = float(os.environ.get("RTT_TIMEOUT", "5"))
MYCNF = os.environ.get("RTT_MYCNF",
                       os.path.expanduser(os.environ.get("DIRECTOR_MYCNF",
                                                          "~/.my.cnf-director")))
# name -> GUID resolution: "mysql" (read-only identity, default) or "fixture"
# (JSON file, for the offline proof harness).
GUID_SOURCE = os.environ.get("RTT_GUID_SOURCE", "mysql")
GUID_FIXTURE = os.environ.get(
    "RTT_GUID_FIXTURE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "fixtures", "guid-fixture.json"))
GUID_TTL = float(os.environ.get("RTT_GUID_TTL", "300"))  # cache seconds

# The routing rule, made structural. DB-answerable asks are refused with a
# route_hint to db-search (names mirror director.py's DB_QUERIES catalog);
# `kick` is refused with a route_hint to chat-tunnel - `.playerbots bot
# remove` covers registered bots, and force-kicking a non-bot plain session
# is an accepted gap (end-of-life decision, docs/services-roadmap.md).
DB_ANSWERABLE = ("location-static", "quest", "money", "skills", "bags",
                 "played", "raid", "census")
CHAT_TUNNEL = ("kick",)

ERR_UNKNOWN_OP = "unknown_op"
ERR_WRONG_SERVICE = "wrong_service"
ERR_BAD_PARAMS = "bad_params"
ERR_BACKEND_DOWN = "backend_unavailable"
ERR_BACKEND_ERROR = "backend_error"


# ---------------------------------------------------------------------------
# backend: the stock mod-playerbots command server (8888 line protocol)
# ---------------------------------------------------------------------------

class PbCmdBackend:
    """Talks to Playerbots.CommandServerPort (default 8888): send
    '<verb>,<guid>\\n', read the reply lines (PlayerbotCommandServer.cpp).
    Read-only, no auth, ONLINE bots only. name -> GUID goes through the
    read-only DB identity (or a fixture, offline) and is cached GUID_TTL."""
    name = "pbcmd"

    def __init__(self):
        self._guid_cache = {}  # name.lower() -> (guid, fetched_at)

    # ---- name -> GUID (read-only, cached) ----

    def _guid_from_db(self, name):
        try:
            out = subprocess.run(
                ["mysql", "--defaults-file=" + MYCNF, "-N", "-B", "-e",
                 "SELECT guid FROM acore_characters.characters "
                 "WHERE name='%s'" % name.replace("'", "")],
                capture_output=True, text=True, timeout=TIMEOUT)
            row = (out.stdout or "").strip().splitlines()
            return int(row[0]) if row and row[0].isdigit() else None
        except Exception:
            return None

    def _guid_from_fixture(self, name):
        try:
            with open(GUID_FIXTURE) as f:
                table = json.load(f)
        except Exception:
            return None
        for k, v in table.items():
            if k.lower() == name.lower():
                return int(v)
        return None

    def guid(self, name):
        """name -> GUID or None, cached GUID_TTL seconds."""
        key = name.lower()
        hit = self._guid_cache.get(key)
        now = time.monotonic()
        if hit and now - hit[1] < GUID_TTL:
            return hit[0]
        if GUID_SOURCE == "fixture":
            g = self._guid_from_fixture(name)
        else:
            g = self._guid_from_db(name)
        self._guid_cache[key] = (g, now)
        return g

    # ---- line protocol ----

    def ask(self, verb, guid, timeout=TIMEOUT):
        """Send '<verb>,<guid>\\n', return (ok, text). ok False = transport
        problem (server down / timeout). An EMPTY reply is ok=True, text="":
        the connection worked but the bot is not live (offline / not a bot),
        exactly like the real server closing on a dead guid."""
        try:
            s = socket.create_connection((PBHOST, PBPORT), timeout=timeout)
        except Exception as e:
            return False, "command server %s:%d unreachable (%s)" % (PBHOST,
                                                                     PBPORT,
                                                                     type(e).__name__)
        try:
            s.settimeout(timeout)
            s.sendall(("%s,%d\n" % (verb, guid)).encode("utf-8"))
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            text = buf.decode("utf-8", "replace").strip()
            return True, text
        except Exception as e:
            return False, type(e).__name__ + ": " + str(e)
        finally:
            s.close()

    def health(self):
        try:
            s = socket.create_connection((PBHOST, PBPORT), timeout=TIMEOUT)
        except Exception as e:
            return False, "command server %s:%d unreachable (%s)" % (PBHOST,
                                                                     PBPORT,
                                                                     type(e).__name__)
        s.close()
        return True, "command server %s:%d reachable" % (PBHOST, PBPORT)


BACKENDS = {"pbcmd": PbCmdBackend}


# ---------------------------------------------------------------------------
# op catalog (tick-time-sensitive / non-polled ONLY - see the refused sets)
# ---------------------------------------------------------------------------

def op_catalog(backend):
    refused = {"ops": sorted(list(DB_ANSWERABLE) + list(CHAT_TUNNEL)),
               "route_hints": {**{o: "db-search" for o in DB_ANSWERABLE},
                               **{o: "chat-tunnel" for o in CHAT_TUNNEL}}}
    return {"ok": True, "result": {"ops": [
        {"op": "catalog", "answers": "this list (what routes here and why)"},
        {"op": "ping", "answers": "command-server liveness + latency"},
        {"op": "pinfo", "answers": "LIVE position of an ONLINE bot via the "
         "mod's command server (8888). Offline truth is db-search: logout "
         "flushes the final position to characters (A5: online resets at "
         "worldserver boot)"},
    ], "refused": refused}, "error": None}


def op_ping(backend):
    ok, detail = backend.health()
    return {"ok": ok, "result": {"alive": ok, "detail": detail},
            "error": None if ok else {"code": ERR_BACKEND_DOWN,
                                      "message": detail}}


POS_RE = re.compile(
    r"map\s+(\d+)\s+at\s+(-?[\d.]+),\s*(-?[\d.]+),\s*(-?[\d.]+)"
    r"(?:\s+in\s+(.+?))?\s*$", re.I)


def op_pinfo(backend, name):
    if not name or not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z0-9_\-']{1,12}", name):
        return {"ok": False, "result": None,
                "error": {"code": ERR_BAD_PARAMS,
                          "message": "param 'name' must be a toon name"}}
    guid = backend.guid(name)
    if guid is None:
        return {"ok": True, "result": {
            "name": name, "online": False,
            "caveat": "no GUID for %r in the characters DB - offline truth "
                      "is db-search" % name}, "error": None}
    ok, text = backend.ask("position", guid)
    if not ok:
        return {"ok": False, "result": None,
                "error": {"code": ERR_BACKEND_DOWN, "message": text}}
    m = POS_RE.search(text)
    if not m:
        # command server answered but the bot is not live (offline / not a
        # bot / empty reply) - offline truth is db-search
        return {"ok": True, "result": {
            "name": name, "online": False, "reply": text,
            "caveat": "not answering on the command server (offline or not "
                      "an online bot) - offline truth is db-search"},
            "error": None}
    zone = m.group(5).strip() if m.group(5) else None
    return {"ok": True, "result": {
        "name": name, "online": True, "guid": guid,
        "map": int(m.group(1)),
        "position": [float(m.group(i)) for i in (2, 3, 4)],
        "zone": zone, "reply": text}, "error": None}


OPS = {"catalog": op_catalog, "ping": op_ping, "pinfo": op_pinfo}


def dispatch(req, backend):
    """One request dict -> one response envelope. The ONLY place requests
    are interpreted; the envelope is identical for every backend."""
    def envelope(ok, result, error):
        return {"interface": INTERFACE, "ok": ok, "op": req.get("op"),
                "backend": backend.name,
                "queried_at": datetime.datetime.now(datetime.timezone.utc)
                              .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "latency_ms": 0, "result": result, "error": error}
    op = req.get("op") if isinstance(req, dict) else None
    if not op:
        return envelope(False, None, {"code": ERR_BAD_PARAMS,
                                      "message": "request must be {\"op\": ...}"})
    if op in DB_ANSWERABLE:
        return envelope(False, None, {
            "code": ERR_WRONG_SERVICE,
            "message": "'%s' is DB-answerable - the routing rule sends it to "
                       "db-search, never here" % op,
            "route_hint": "db-search"})
    if op in CHAT_TUNNEL:
        return envelope(False, None, {
            "code": ERR_WRONG_SERVICE,
            "message": "'%s' is refused - '.playerbots bot remove' covers "
                       "registered bots (chat-tunnel owns command sends); "
                       "force-kicking a non-bot plain session is an "
                       "accepted gap" % op,
            "route_hint": "chat-tunnel"})
    if op not in OPS:
        return envelope(False, None, {
            "code": ERR_UNKNOWN_OP,
            "message": "unknown op '%s' (catalog lists what routes here)" % op,
            "route_hint": "db-search (if a DB query can answer it)"})
    if op == "pinfo":
        r = OPS[op](backend, req.get("name"))
    else:
        r = OPS[op](backend)
    return envelope(r["ok"], r["result"], r["error"])


# ---------------------------------------------------------------------------
# transport: unix socket, one JSON line in, one JSON line out
# ---------------------------------------------------------------------------

def serve(sock_path):
    if sock_path == "-" or not sock_path:
        sock_path = SOCK
    backend = PbCmdBackend()
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    os.chmod(sock_path, 0o600)
    srv.listen(8)
    sys.stderr.write("rtt-engine: serving %s (interface %s, backend %s -> "
                     "command server %s:%d)\n"
                     % (sock_path, INTERFACE, backend.name, PBHOST, PBPORT))
    sys.stderr.flush()
    try:
        while True:
            conn, _ = srv.accept()
            try:
                f = conn.makefile("rwb")
                line = f.readline()
                if not line:
                    continue
                t0 = time.monotonic()
                try:
                    req = json.loads(line.decode("utf-8"))
                except Exception as e:
                    resp = {"interface": INTERFACE, "ok": False, "op": None,
                            "backend": backend.name, "queried_at": "", "latency_ms": 0,
                            "result": None,
                            "error": {"code": ERR_BAD_PARAMS,
                                      "message": "bad JSON: %s" % e}}
                else:
                    resp = dispatch(req, backend)
                resp["latency_ms"] = int((time.monotonic() - t0) * 1000)
                f.write((json.dumps(resp) + "\n").encode("utf-8"))
                f.flush()
            finally:
                conn.close()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        if os.path.exists(sock_path):
            os.unlink(sock_path)


def query(sock_path, req):
    if sock_path == "-" or not sock_path:
        sock_path = SOCK
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT + 5)
    s.connect(sock_path)
    try:
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode("utf-8"))
    finally:
        s.close()


def main():
    p = argparse.ArgumentParser(description="rtt-engine: real-time/non-polled service")
    sub = p.add_subparsers(dest="mode", required=True)
    ps = sub.add_parser("serve", help="run the service on a unix socket")
    ps.add_argument("--socket", default="-")
    pq = sub.add_parser("query", help="one-shot client: prints the JSON envelope")
    pq.add_argument("request", help="request JSON, e.g. '{\"op\":\"ping\"}'")
    pq.add_argument("--socket", default="-")
    a = p.parse_args()
    if a.mode == "serve":
        serve(a.socket)
    else:
        try:
            req = json.loads(a.request)
        except Exception as e:
            sys.stderr.write("bad request JSON: %s\n" % e)
            return 2
        print(json.dumps(query(a.socket, req), indent=2, sort_keys=True))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
