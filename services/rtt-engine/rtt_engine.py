#!/usr/bin/env python3
"""rtt-engine ("8.8.8.8") - real-time / non-polled query-and-command service.

Service 3 of 4 (docs/services-roadmap.md). Owns the worldserver's
tick-time-sensitive surface: live console reads/writes the DB cannot answer.
Everything DB-answerable is REFUSED here with a route_hint to db-search
(the routing rule is structural: it lives in the op catalog, not in callers).

Interface (STABLE, backend-independent - "rtt-engine/1"):
  transport : unix socket (default /tmp/rtt-engine.sock), one JSON request
              per line, one JSON response per line, connection per request.
  request   : {"op": "<name>", ...params}
  response  : {"interface": "rtt-engine/1", "ok": bool, "op": str,
               "backend": "soap"|"cmdbus"|"mock", "queried_at": iso8601,
               "latency_ms": int, "result": {...}|null,
               "error": {"code": str, "message": str,
                         "route_hint": str|omitted}|null}

Backend seam: every op goes through a Backend with exactly two methods -
  execute(command, timeout) -> (ok, text)
  health()                  -> (ok, detail)
SoapBackend wraps the EXISTING SOAP path (same envelope / ~/.soap-director
3-line conf as director.py soap_command and lib/botcmd/common.sh _soap_post).
CmdBusBackend is the future in-core command bus stub - same signature, so the
backend swaps WITHOUT touching the interface or any caller. MockBackend serves
scripted fixture replies so the proof harness runs without a worldserver.

Stdlib only. Modes:
  rtt_engine.py serve [--socket P] [--backend soap|cmdbus|mock]
  rtt_engine.py query '<json>' [--socket P]     one-shot client
"""
import argparse
import base64
import datetime
import html
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from xml.sax.saxutils import escape as _xml_escape

INTERFACE = "rtt-engine/1"
SOCK = os.environ.get("RTT_SOCK", "/tmp/rtt-engine.sock")
BACKEND = os.environ.get("RTT_BACKEND", "soap")
SOAPCNF = os.environ.get("RTT_SOAPCNF",
                         os.path.expanduser(os.environ.get("DIRECTOR_SOAPCNF",
                                                           "~/.soap-director")))
TIMEOUT = float(os.environ.get("RTT_TIMEOUT", "10"))
# console passthrough gate: prefix-allowlisted commands only (RTT_CONSOLE_ALLOW
# is a space-separated list; a bare token matches itself + anything after it).
CONSOLE_ALLOW = [c for c in os.environ.get(
    "RTT_CONSOLE_ALLOW", ".pinfo .server info .group list .group revive"
).split() if c]
MOCK_FIXTURE = os.environ.get(
    "RTT_MOCK_FIXTURE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "fixtures", "mock-responses.json"))

# The routing rule, made structural: these asks are DB-answerable, so this
# service REFUSES them and points the caller at db-search. Names mirror the
# director.py DB_QUERIES catalog (the future db-search tools).
DB_ANSWERABLE = ("location-static", "quest", "money", "skills", "bags",
                 "played", "raid", "census")

ERR_UNKNOWN_OP = "unknown_op"
ERR_WRONG_SERVICE = "wrong_service"
ERR_BAD_PARAMS = "bad_params"
ERR_BACKEND_DOWN = "backend_unavailable"
ERR_BACKEND_ERROR = "backend_error"
ERR_REFUSED = "refused_console"


# ---------------------------------------------------------------------------
# backend seam
# ---------------------------------------------------------------------------

class SoapBackend:
    """The existing SOAP path, wrapped verbatim (envelope + conf file +
    Basic auth mirror director.py soap_command / common.sh _soap_post)."""
    name = "soap"

    def _cfg(self):
        try:
            with open(SOAPCNF) as f:
                vals = [l.strip() for l in f.read().splitlines()][:3]
        except OSError:
            return None
        if len(vals) < 3 or not all(vals):
            return None
        return vals[0], vals[1], vals[2]

    def execute(self, command, timeout=TIMEOUT):
        cfg = self._cfg()
        if not cfg:
            return False, "SOAP not configured (%s missing/incomplete)" % SOAPCNF
        url, user, pw = cfg
        env = ('<?xml version="1.0" encoding="utf-8"?>'
               '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
               'xmlns:ns1="urn:AC"><SOAP-ENV:Body>'
               '<ns1:executeCommand><command>' + _xml_escape(command) +
               '</command></ns1:executeCommand>'
               '</SOAP-ENV:Body></SOAP-ENV:Envelope>')
        auth = base64.b64encode((user + ":" + pw).encode()).decode()
        req = urllib.request.Request(url, data=env.encode("utf-8"), headers={
            "Content-Type": "text/xml; charset=utf-8", "SOAPAction": "urn:AC",
            "Authorization": "Basic " + auth})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return False, "HTTP %d (bad account/password or gmlevel below Administrator)" % e.code
        except Exception as e:
            return False, type(e).__name__ + ": " + str(e)
        if "<SOAP-ENV:Fault" in body:
            m = re.search(r"<faultstring[^>]*>(.*?)</faultstring>", body, re.S)
            return False, "server fault: " + html.unescape(m.group(1) if m else "?")
        m = re.search(r"<[^>]*executeCommandResponse[^>]*>(.*)</[^>]*executeCommandResponse>",
                      body, re.S)
        if not m:
            return False, "unexpected SOAP response: " + body[:120]
        text = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        return bool(text), text

    def health(self):
        ok, text = self.execute(".server info")
        return ok, text


class CmdBusBackend:
    """Future in-core command bus stub. Same seam signature as SoapBackend:
    once the core exposes the bus, only this class changes - the interface,
    the op catalog and every caller stay untouched."""
    name = "cmdbus"

    def execute(self, command, timeout=TIMEOUT):
        return False, ("in-core command bus not wired yet - seam ready, "
                       "swap RTT_BACKEND=cmdbus once the core endpoint exists")

    def health(self):
        return False, "in-core command bus not wired yet"


class MockBackend:
    """Scripted backend for the proof harness: serves fixture replies from
    MOCK_FIXTURE ({"<command>": {"ok": bool, "text": str}, ...}); unknown
    commands answer like an offline worldserver would (backend_down)."""
    name = "mock"

    def __init__(self):
        with open(MOCK_FIXTURE) as f:
            self.script = json.load(f)

    def execute(self, command, timeout=TIMEOUT):
        r = self.script.get(command)
        if r is None:
            return False, "mock: no scripted reply for %r" % command
        return bool(r.get("ok")), r.get("text", "")

    def health(self):
        return self.execute(".server info")


BACKENDS = {"soap": SoapBackend, "cmdbus": CmdBusBackend, "mock": MockBackend}


# ---------------------------------------------------------------------------
# op catalog (tick-time-sensitive / non-polled ONLY - see DB_ANSWERABLE)
# ---------------------------------------------------------------------------

def _pinfo(raw):
    """Keep the useful lines of '.pinfo' console output, drop the rest
    (same filter idea as director.py _fmt_pinfo)."""
    keep = []
    for line in raw.splitlines():
        line = re.sub(r"\|c[0-9A-Fa-f]{8}|\|r", "", line).strip().strip("|").strip()
        if not line:
            continue
        low = line.lower()
        if any(k in low for k in ("map:", "zone:", "area:", "online",
                                   "position", "login",
                                   "guild:", "level:", "alive", "money:")):
            keep.append(line)
    return keep if keep else raw[:200]


def op_catalog(backend):
    return {"ok": True, "result": {"ops": [
        {"op": "catalog", "answers": "this list (what routes here and why)"},
        {"op": "ping", "answers": "backend liveness + round-trip latency"},
        {"op": "server_info", "answers": "live server state (online count, "
         "uptime) - ticks change it every second"},
        {"op": "pinfo", "answers": "live toon state (online now, map/zone/"
         "position) - the DB only knows the LAST LOGOUT position (A5)"},
        {"op": "console", "answers": "allowlisted console passthrough "
         "(RTT_CONSOLE_ALLOW); the escape hatch for tick-sensitive console "
         "verbs"},
    ], "refused": {"ops": list(DB_ANSWERABLE),
                   "route_hint": "db-search (DB-answerable - never this service)"}},
           "error": None}


def op_ping(backend):
    ok, detail = backend.health()
    return {"ok": ok, "result": {"alive": ok, "detail": detail},
            "error": None if ok else {"code": ERR_BACKEND_DOWN,
                                      "message": detail}}


def op_server_info(backend):
    ok, text = backend.execute(".server info")
    if not ok:
        return {"ok": False, "result": None,
                "error": {"code": ERR_BACKEND_DOWN, "message": text}}
    players = uptime = revision = None
    for line in text.splitlines():
        m = re.search(r"connected players?\s*:\s*(\d+)", line, re.I)
        if m:
            players = int(m.group(1))
        m = re.search(r"uptime\s*:\s*(.+)", line, re.I)
        if m:
            uptime = m.group(1).strip()
        m = re.search(r"revision\s*:\s*(\S+)", line, re.I)
        if m:
            revision = m.group(1)
    return {"ok": True, "result": {"online_players": players,
                                   "uptime": uptime, "revision": revision,
                                   "raw": text}, "error": None}


def op_pinfo(backend, name):
    if not name or not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z0-9_\-']{1,12}", name):
        return {"ok": False, "result": None,
                "error": {"code": ERR_BAD_PARAMS,
                          "message": "param 'name' must be a toon name"}}
    ok, text = backend.execute(".pinfo " + name)
    if not ok:
        return {"ok": False, "result": None,
                "error": {"code": ERR_BACKEND_DOWN, "message": text}}
    lines = _pinfo(text)
    online = any("online" in l.lower() and "offline" not in l.lower()
                 for l in lines)
    pos = zone = map_ = None
    for l in lines:
        m = re.search(r"Position\s*:\s*\[?(-?[\d.]+),?\s*(-?[\d.]+),?\s*(-?[\d.]+)",
                      l, re.I)
        if m:
            pos = [float(m.group(i)) for i in (1, 2, 3)]
        m = re.search(r"Zone\s*:\s*([^,;|]+?)(?:\s*,|$)", l, re.I)
        if m:
            zone = m.group(1).strip()
        m = re.search(r"Map\s*:\s*(\d+)", l, re.I)
        if m:
            map_ = int(m.group(1))
    return {"ok": True, "result": {"name": name, "online": online,
                                   "map": map_, "zone": zone,
                                   "position": pos, "detail": "; ".join(lines)},
            "error": None}


def op_console(backend, command):
    if not command or not isinstance(command, str):
        return {"ok": False, "result": None,
                "error": {"code": ERR_BAD_PARAMS,
                          "message": "param 'command' must be a console command string"}}
    if not any(command == a or command.startswith(a + " ")
               for a in CONSOLE_ALLOW):
        return {"ok": False, "result": None,
                "error": {"code": ERR_REFUSED,
                          "message": "command %r is not in the console allowlist "
                                     "(RTT_CONSOLE_ALLOW: %s)" % (command,
                                                                 " ".join(CONSOLE_ALLOW))}}
    ok, text = backend.execute(command)
    if not ok:
        return {"ok": False, "result": None,
                "error": {"code": ERR_BACKEND_DOWN, "message": text}}
    return {"ok": True, "result": {"command": command, "reply": text},
            "error": None}


OPS = {"catalog": op_catalog, "ping": op_ping, "server_info": op_server_info,
       "pinfo": op_pinfo, "console": op_console}


def dispatch(req, backend):
    """One request dict -> one response envelope. The ONLY place requests
    are interpreted; the envelope is identical for every backend."""
    def envelope(ok, result, error):
        return {"interface": INTERFACE, "ok": ok, "op": req.get("op"),
                "backend": backend.name,
                "queried_at": datetime.datetime.now(datetime.timezone.utc)
                              .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "latency_ms": 0, "result": result, "error": error}
    op = req.get("op")
    if not isinstance(req, dict) or not op:
        return envelope(False, None, {"code": ERR_BAD_PARAMS,
                                      "message": "request must be {\"op\": ...}"})
    if op in DB_ANSWERABLE:
        return envelope(False, None, {
            "code": ERR_WRONG_SERVICE,
            "message": "'%s' is DB-answerable - the routing rule sends it to "
                       "db-search, never here" % op,
            "route_hint": "db-search"})
    if op not in OPS:
        return envelope(False, None, {
            "code": ERR_UNKNOWN_OP,
            "message": "unknown op '%s' (catalog lists what routes here)" % op,
            "route_hint": "db-search (if a DB query can answer it)"})
    if op == "pinfo":
        r = OPS[op](backend, req.get("name"))
    elif op == "console":
        r = OPS[op](backend, req.get("command"))
    else:
        r = OPS[op](backend)
    return envelope(r["ok"], r["result"], r["error"])


# ---------------------------------------------------------------------------
# transport: unix socket, one JSON line in, one JSON line out
# ---------------------------------------------------------------------------

def serve(sock_path, backend_name):
    if sock_path == "-" or not sock_path:
        sock_path = SOCK
    backend = BACKENDS[backend_name]()
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    os.chmod(sock_path, 0o600)
    srv.listen(8)
    sys.stderr.write("rtt-engine: serving %s (interface %s, backend %s)\n"
                    % (sock_path, INTERFACE, backend.name))
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
    ps.add_argument("--backend", choices=sorted(BACKENDS), default="-")
    pq = sub.add_parser("query", help="one-shot client: prints the JSON envelope")
    pq.add_argument("request", help="request JSON, e.g. '{\"op\":\"ping\"}'")
    pq.add_argument("--socket", default="-")
    a = p.parse_args()
    if a.mode == "serve":
        serve(a.socket, a.backend if a.backend != "-" else BACKEND)
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
