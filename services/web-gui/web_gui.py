#!/usr/bin/env python3
"""web-gui (service 4 of 4) - read-mostly Cockpit-style dashboard over services 1-3.

Panels (roadmap row 4, resynced to the post-rtt-engine stack 2026-11-10):

- **Leo's chat (live)** - what Leo sees in chat, via chat-tunnel's cursor
  seam (`get_events_since` + `wait_for_events` long-poll): whispers, guild
  chat and inspect system replies with their raw display lines, newest at
  the bottom, no hot-polling.
- **Bots** - db-search `get_online_census` table with the staleness caveat,
  Leo's own row on top (db-search `get_character`), group/raid badges from
  db-search `get_group_membership` (Leo's group auto; any toon on demand),
  and a **live** button per row that asks chat-tunnel `inspect(bot,
  "position")` (A23 - the retired rtt-engine's job, now owned by the
  tunnel).
- **Query catalog browser** - the db-search catalog, read-only execution
  with the full envelope (`queried_at`, staleness `caveat`), plus the
  chat-tunnel grammar (`describe`) as a second, display-only tab.

Write actions (deliberate, minimal): the **Leo session control** -
`POST /api/leo {"action": "start"|"stop"}` runs `systemctl --user
start|stop leo.service` (WEBGUI_LEO_UNIT), and `inspect` sends a live
read command through the tunnel. Everything else is read-only:
`send_command` is never proxied; the catalog executes only db-search's
static SELECTs. On a LAN bind all of it sits behind the login screen.

Local-only HTTP like chat-tunnel: binds 127.0.0.1 by default
(WEB_GUI_BIND - a specific address or `lan`; the wildcard 0.0.0.0 is
ALWAYS refused). Every backend call degrades to a per-panel "backend
down" state - one sibling being offline never breaks the page.

Env knobs (all optional):
  WEBGUI_PORT           8766
  WEB_GUI_BIND          127.0.0.1 (default, no login needed) - a SPECIFIC
                        address (e.g. 192.168.88.2), or `lan` to auto-resolve
                        the host's current LAN address at startup (survives
                        moving the server). The wildcard 0.0.0.0 is ALWAYS
                        refused. Any non-loopback bind requires a login
                        (WEB_GUI_USER / WEB_GUI_PASS) and prints a prominent
                        startup warning: no TLS - credentials cross the LAN
                        in cleartext. Firewall it; the SSH tunnel is the
                        recommended default (see README).
  WEB_GUI_USER          director   (login user, non-loopback binds only)
  WEB_GUI_PASS          (required for a non-loopback bind; empty = refuse)
  WEB_GUI_AUTH          auto (on for non-loopback binds) | on | off (off is
                        refused on non-loopback binds)
  WEBGUI_TUNNEL         http://127.0.0.1:8765     (chat-tunnel base URL)
  WEBGUI_DBSEARCH_SOCK  /tmp/db-search.sock       (db-search unix socket)
  WEBGUI_LEO            Leo        (the shell toon: own table row + group)
  WEBGUI_LEO_UNIT       leo.service (systemctl --user unit the Leo button
                        starts/stops)
  WEBGUI_CENSUS         i_was_here  (account prefix for the bot status table)
  WEBGUI_TIMEOUT        15          (seconds, per backend call)

Run: python3 web_gui.py serve
"""
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("WEBGUI_PORT", "8766"))
BIND = (os.environ.get("WEB_GUI_BIND") or os.environ.get("WEBGUI_ADDR")
        or "127.0.0.1")
TUNNEL = os.environ.get("WEBGUI_TUNNEL", "http://127.0.0.1:8765").rstrip("/")
DB_SOCK = os.environ.get("WEBGUI_DBSEARCH_SOCK", "/tmp/db-search.sock")
LEO = os.environ.get("WEBGUI_LEO", "Leo")
AUDIT = os.environ.get("WEBGUI_AUDIT",
                       os.path.expanduser("~/.webgui-audit.jsonl"))
POS_POLL = float(os.environ.get("WEBGUI_POS_POLL", "300"))
# how long phase 2 waits for probe answers (the real relay can take
# longer than a probe-a-time 5s window; live 2026-11-23: Edgar's answer
# was matched by the tunnel AFTER the poller had already given up)
POS_WAIT = float(os.environ.get("WEBGUI_POS_WAIT", "25"))
# the operator's review cursor: everything since the last 'mark reviewed'
REVIEW_FILE = os.environ.get(
    "WEBGUI_REVIEW_FILE",
    os.path.expanduser("~/.webgui-last-review.json"))
# the hand-play registry, FILE-BACKED so toggles stick across restarts
# (the live set in director.HANDPLAY is process memory; without this file
# a systemd restart silently reset every toggle)
HANDPLAY_FILE = os.environ.get(
    "WEBGUI_HANDPLAY_FILE", os.path.expanduser("~/.webgui-handplay.json"))
# the last state set per toon (states are fire-and-forget whispers with no
# lease - without this registry the mission column cannot show them)
STATES_FILE = os.environ.get(
    "WEBGUI_STATES_FILE", os.path.expanduser("~/.webgui-states.json"))
LEO_UNIT = os.environ.get("WEBGUI_LEO_UNIT", "leo.service")
CENSUS = os.environ.get("WEBGUI_CENSUS", "i_was_here")
TIMEOUT = float(os.environ.get("WEBGUI_TIMEOUT", "15"))

AUTH_USER = os.environ.get("WEB_GUI_USER", "director")
AUTH_PASS = os.environ.get("WEB_GUI_PASS", "")
SESSION_TTL = float(os.environ.get("WEB_GUI_SESSION_TTL", str(12 * 3600)))
AUTH_MODE = os.environ.get("WEB_GUI_AUTH", "auto").strip().lower()

# chat-tunnel's own INSPECT_QUERIES (A23) - mirrored so a stale GUI fails
# closed (the tunnel re-validates anyway)
LIVE_QUERIES = ("state", "position", "tpos", "movement", "target", "hp",
                "strategy", "action", "values", "travel", "budget")

INTERFACE = "web-gui/2"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

# The GUI's mission forms: ONLY these order shapes are ever built (the
# client never supplies a free-form cmd); director's validate() is the
# second wall either way.
MISSION_OPS = ("travel", "farm", "walk", "vendor", "vendor_all", "state",
               "standdown", "online", "offline", "leave", "disband",
               "status")
MISSION_DEST_OPS = ("travel", "farm", "walk", "vendor")
# The bot states - nc-only, per the owner doctrine of 2026-11-12 (director
# commit 8f3cd7c): 'rpg status' is READ-ONLY on this build; every command
# goes through nc. The gather preset needs Herbalism/Mining; the idle
# preset is the stand-down and releases a farm lease via validate().
# what 'nc ?' should mention when a preset has truly taken (the confirm
# half of the operator's verify loop). idle's [""] = any answered readback
# confirms (nothing-enabled is a negative state; the answer IS the proof)
PRESET_MARKERS = {
    "grind": ["grind"],
    "quest": ["quest"],
    "gather": ["gather"],
    "leveling": ["quest", "grind"],
    "follow": ["follow"],
    "idle": [""],
}


STATE_PRESETS = {
    "grind": ("nc +new rpg,+grind,-quest,-gather,-follow",),
    "quest": ("nc +new rpg,+quest,-grind,-gather,-follow",),
    # operator PROVEN sequence (live 2026-11-24, executed by hand from
    # Golbez in Westfall: 'nc +new rpg,-follow' then 'rpg do gather' =
    # she gathered the zone on her mount): engine ON, follow off, then
    # the ACTION LAUNCHER; no strategy toggles needed
    "gather": ("nc +new rpg,-follow", "rpg do gather"),
    # operator 2026-11-24: leveling is quest+grind ONLY - gather is not
    # part of it (it filled the fleet's bags with mats and split focus)
    "leveling": ("nc +new rpg,+quest,+grind,-follow",),
    "follow": ("nc +follow",),
    "idle": ("nc -new rpg,-grind,-quest,-gather,-follow",),
}
_DEST_RE = __import__("re").compile(r"^[A-Za-z0-9_' -]{1,40}$")

_DIRECTOR = {"mod": None}


def _director():
    """Lazy import of the mission engine (same process, shared lease file).
    The hand-play registry is reloaded from its file on first import so
    operator toggles survive restarts."""
    if _DIRECTOR["mod"] is None:
        sys.path.insert(0, REPO_ROOT)
        import director
        try:
            with open(HANDPLAY_FILE) as f:
                saved = json.load(f).get("handplay", [])
            director.HANDPLAY.clear()
            director.HANDPLAY.update(str(x).lower() for x in saved)
        except (OSError, ValueError):
            pass          # no file yet: the env defaults stand
        _DIRECTOR["mod"] = director
    return _DIRECTOR["mod"]


def _handplay_save(d):
    try:
        with open(HANDPLAY_FILE, "w") as f:
            json.dump({"handplay": sorted(d.HANDPLAY)}, f)
    except OSError:
        pass

STATES = {}


def _states_load():
    try:
        with open(STATES_FILE) as f:
            for k, v in json.load(f).items():
                STATES[str(k).lower()] = v
    except (OSError, ValueError):
        pass


def _states_save():
    try:
        with open(STATES_FILE, "w") as f:
            json.dump(STATES, f)
    except OSError:
        pass


def _state_set(bot, state):
    STATES[str(bot).lower()] = {"state": state, "ts": time.time()}
    _states_save()


def _state_clear(bot):
    STATES.pop(str(bot).lower(), None)
    _states_save()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _audit(action, detail):
    """Every GUI write action, appended - the operator's own trail now that
    the GUI is the steering point. Never blocks the action on failure."""
    try:
        with open(AUDIT, "a") as f:
            f.write(json.dumps({"ts": _now(), "action": action,
                                "detail": detail}) + "\n")
    except OSError:
        pass


def _unreachable(source, e):
    return {"ok": False, "source": source, "queried_at": _now(),
            "error": {"code": "unreachable",
                      "message": "%s is not answering: %s" % (source, e)}}


# ------------------------------------------------------------- backends ----
def tunnel_call(tool, args, timeout=None):
    source = "chat-tunnel"
    body = json.dumps({"tool": tool, "args": args}).encode()
    req = urllib.request.Request(TUNNEL + "/", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError) as e:
        return _unreachable(source, e)


def tunnel_health():
    try:
        with urllib.request.urlopen(TUNNEL + "/health", timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError) as e:
        return _unreachable("chat-tunnel", e)


def unix_call(sock_path, source, req):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(TIMEOUT)
        s.connect(sock_path)
    except OSError as e:
        return _unreachable(source, e)
    try:
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode())
    except (OSError, ValueError) as e:
        return _unreachable(source, e)
    finally:
        s.close()


def db_call(tool, params):
    return unix_call(DB_SOCK, "db-search",
                     {"tool": tool, "params": params or {}})


# ------------------------------------------------------------- leo (unit) --
def leo_active():
    """systemctl --user is-active, graceful: any error reads as 'down'."""
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", LEO_UNIT],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and r.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def leo_action(action):
    if action not in ("start", "stop"):
        return {"ok": False, "queried_at": _now(),
                "error": {"code": "bad_request",
                          "message": "action must be start|stop, not %r"
                                     % (action,)}}
    try:
        r = subprocess.run(["systemctl", "--user", action, LEO_UNIT],
                           capture_output=True, text=True, timeout=30)
        ok = r.returncode == 0
        return {"ok": ok, "queried_at": _now(), "unit": LEO_UNIT,
                "action": action, "active": leo_active(),
                "error": None if ok else {
                    "code": "leo_error",
                    "message": (r.stderr or r.stdout or
                                "systemctl %s %s failed" % (action, LEO_UNIT)
                                ).strip()[:300]}}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "queried_at": _now(), "unit": LEO_UNIT,
                "action": action, "active": False,
                "error": {"code": "leo_error", "message": str(e)}}


# ------------------------------------------------------------- handlers ----
# --- live position poller: the zone column's true current location ------
# The DB's map/zone are last-flushed NUMBERS (A5/A3 staleness); the live
# truth is chat-tunnel's inspect position reply, which carries the zone
# NAME in pipes (the same shape director's _live_position parses). A
# background thread polls each online bot every WEBGUI_POS_POLL seconds
# (default 300; 0 = off) and caches the latest answer per toon.
POS_RE = __import__("re").compile(
    r"([-\d.]+) ([-\d.]+) ([-\d.]+) (\d+) [-\d.]+ \|([^|]*)\|")
POSITIONS = {}
POSITIONS_LOCK = threading.Lock()


def _position_loop():
    """Two phases per cycle (live fix 2026-11-23):
    1. FIRE - one paced pass, an inspect per census toon (every toon:
       the A5 flag must never hide a live bot);
    2. HARVEST - collect answers for POS_WAIT seconds, in ANY order they
       arrive. Stragglers stay pending and get their chance again next
       cycle - the old design waited ~5s per toon, serially, and the real
       relay's latency exceeded the window, abandoning matched replies."""
    pending = {}          # correlation_id -> toon (survives cycles)
    while True:
        try:
            cen = db_call("get_online_census", {"account_like": CENSUS})
            if cen.get("ok"):
                for row in cen.get("rows", []):
                    name = row[0]
                    r = tunnel_call("inspect", {"bot": name,
                                                "query": "position"})
                    cid = ((r.get("result") or {})
                           .get("correlation_id")) if r.get("ok") else None
                    if cid:
                        pending[cid] = name
                    time.sleep(1)          # pace the FIFO writes
            deadline = time.time() + max(10.0, POS_WAIT)
            while pending and time.time() < deadline:
                time.sleep(1)
                for cid in list(pending):
                    rr = tunnel_call("get_reply",
                                     {"correlation_id": cid})
                    if not rr.get("ok"):
                        del pending[cid]          # server lost it
                        continue
                    rep = (rr.get("result") or {}).get("reply")
                    if rep:
                        m = POS_RE.match((rep.get("command") or "").strip())
                        if m:
                            with POSITIONS_LOCK:
                                POSITIONS[pending[cid]] = {
                                    "x": float(m.group(1)),
                                    "y": float(m.group(2)),
                                    "map": int(m.group(4)),
                                    "zone": m.group(5).strip(),
                                    "ts": time.time()}
                        del pending[cid]
                        continue
                    sent = (rr.get("result") or {}).get("sent") or {}
                    if sent.get("status") in ("timeout", "expired"):
                        del pending[cid]
        except Exception:
            pass
        time.sleep(max(30.0, POS_POLL))


# last send/receipt per toon, fed by api_mission: a reply-matched command
# is proof of life; the DB flag alone is only a hint (A5)
RECEIPTS = {}


def _receipt_note(bot, line, status):
    RECEIPTS[str(bot)] = {"line": str(line)[:120], "status": status,
                          "ts": time.time()}


def api_truth(census):
    """Tier 0: ONE reconciled presence state per toon. Signals, weakest to
    strongest: the DB online flag (A5 hint - boot-reset, async on login),
    a live position-probe answer (proof of life). Every gate should
    consult THIS, never the raw flag.
      live      - answered a probe within the last two poll cycles
      db_online - flag says online, no live proof yet (verify soon)
      offline   - no live proof and the flag says offline"""
    out = {}
    now = time.time()
    horizon = max(120.0, 2 * max(30.0, POS_POLL))
    with POSITIONS_LOCK:
        pos = dict(POSITIONS)
    rows = census.get("rows", []) if census.get("ok") else []
    for r in rows:
        name, flag = r[0], str(r[6]) == "1"
        p = pos.get(name)
        if p and now - p.get("ts", 0) < horizon:
            out[name] = {"state": "live", "why": "answered a position probe",
                         "zone": p.get("zone")}
        elif flag:
            out[name] = {"state": "db_online",
                         "why": "DB flag online (A5 hint), no live proof yet"}
        else:
            out[name] = {"state": "offline",
                         "why": "no live proof and the DB flag says offline"}
    return {"ok": True, "queried_at": _now(), "truth": out}


def api_positions():
    with POSITIONS_LOCK:
        return {"ok": True, "queried_at": _now(),
                "positions": {k: dict(v) for k, v in POSITIONS.items()}}


WORLD_SEEN = {"starttime": None}


def api_world_uptime():
    """The worldserver liveness signal (operator ask: 'does the web-gui
    have any way to tell if ac-worldserver is down or up'): the server
    rewrites its uptime heartbeat row every ~10-15 minutes; the verdict
    is the age of that last write. A NEW starttime = the server
    RESTARTED (detected across polls; audited once)."""
    r = db_call("get_worldserver_uptime", {})
    if not r.get("ok") or not r.get("rows"):
        return {"ok": False, "state": "unknown",
                "reason": "uptime unavailable: %s"
                          % ((r.get("error") or {}).get("message")
                             or "no rows")}
    try:
        st, up = int(r["rows"][0][1]), int(r["rows"][0][2])
        maxp = int(r["rows"][0][3])
    except (ValueError, IndexError):
        return {"ok": False, "state": "unknown",
                "reason": "bad heartbeat row"}
    age = time.time() - (st + up)
    out = {"ok": True, "state": "up" if age <= 900 else "silent",
           "age_s": int(age), "starttime": st, "uptime_s": up,
           "maxplayers": maxp}
    if WORLD_SEEN["starttime"] is not None and st != WORLD_SEEN["starttime"]:
        out["restarted"] = True
        _audit("world", "the worldserver RESTARTED (new starttime %d)" % st)
    WORLD_SEEN["starttime"] = st
    return out


def api_overview():
    out = {"ok": True, "interface": INTERFACE, "queried_at": _now(),
           "census_prefix": CENSUS, "leo": LEO, "leo_unit": LEO_UNIT,
           "leo_active": leo_active(),
           "tunnel": tunnel_health(),
           "leo_char": db_call("get_character", {"toon": LEO}),
           "leo_group": db_call("get_group_membership", {"toon": LEO}),
           "missions": api_missions(),
           "handplay": api_handplay(),
           "states": dict(STATES),
           "positions": api_positions(),
           "progress": db_call("get_leveling_progress",
                               {"account": CENSUS}),
           "wallets": db_call("get_wallets", {"account": CENSUS}),
           "census": db_call("get_online_census",
                             {"account_like": CENSUS})}
    tr = api_truth(out["census"])
    out["truth"] = tr["truth"]
    out["world"] = api_world_uptime()
    horizon = max(120.0, 2 * max(30.0, POS_POLL))
    freshest = max((v.get("ts", 0) for v in POSITIONS.values()), default=0)
    with POSITIONS_LOCK:
        nprobe = len(POSITIONS)
    out["live_silent"] = bool(
        POS_POLL > 0 and out.get("leo_active")
        and (nprobe == 0 or time.time() - freshest > horizon))
    return out


def _tunnel_result(resp):
    """chat-tunnel envelope -> {ok, queried_at, result|error}."""
    if resp.get("ok"):
        return {"ok": True, "queried_at": _now(), "result": resp["result"]}
    if "source" in resp:
        return resp
    return {"ok": False, "queried_at": _now(),
            "error": {"code": "tunnel_error",
                      "message": resp.get("error") or "chat-tunnel error"}}


def api_events(after_id, limit):
    return _tunnel_result(tunnel_call("get_events_since",
                                      {"after_id": after_id, "limit": limit}))


def api_events_wait(after_id, timeout, limit):
    """wait_for_events long-polls server-side for up to 25s - the proxy's
    own client timeout must OUTLIVE the poll (the frontend asks for 20s),
    or a quiet chat reads as 'timed out' (the bug this fixed)."""
    try:
        t = max(0.0, min(float(timeout or 20), 25.0))
    except (TypeError, ValueError):
        t = 20.0
    return _tunnel_result(tunnel_call(
        "wait_for_events", {"after_id": after_id, "timeout": t,
                            "limit": limit}, timeout=t + TIMEOUT))


def api_reply(cid):
    return _tunnel_result(tunnel_call("get_reply", {"correlation_id": cid}))


def api_live(bot, query):
    if not isinstance(bot, str) or not bot:
        return {"ok": False, "queried_at": _now(),
                "error": {"code": "bad_request",
                          "message": "live needs a {\"bot\": ...}"}}
    if query not in LIVE_QUERIES:
        return {"ok": False, "queried_at": _now(),
                "error": {"code": "gui_read_only",
                          "message": "web-gui live reads allowlist: %s "
                                     "(anything else routes to the owning "
                                     "service)" % ", ".join(LIVE_QUERIES)}}
    return _tunnel_result(tunnel_call("inspect", {"bot": bot, "query": query}))


def api_grammar():
    return _tunnel_result(tunnel_call("describe", {}))


def api_catalogs():
    return {"ok": True, "queried_at": _now(),
            "db_search": db_call("catalog", {})}


def api_query(tool, params):
    if not isinstance(tool, str) or not tool:
        return {"ok": False, "queried_at": _now(),
                "error": {"code": "bad_request",
                          "message": "request must be {\"tool\": ..., \"params\": {...}}"}}
    if not isinstance(params, dict):
        return {"ok": False, "queried_at": _now(),
                "error": {"code": "bad_request",
                          "message": "params must be an object"}}
    return db_call(tool, params)


def api_missions():
    """The shared lease registry: what every bot is doing right now."""
    try:
        reg = _director().leases()
    except Exception as e:
        return {"ok": False, "queried_at": _now(),
                "error": {"code": "director_unavailable",
                          "message": "the mission engine is not loadable: %s" % e}}
    return {"ok": True, "queried_at": _now(), "missions": reg}


def api_warnings():
    """Per-toon warning flags from db-search (provable subset first):
    full bags (0 free slots) / nearly full (<=2) - the vendor-run trigger.
    Bag rows flush with the character save, so a just-sold bot can stay
    flagged for minutes (documented staleness, not an error)."""
    census = db_call("get_online_census", {"account_like": CENSUS})
    out = []
    scan_errors = []
    BAGS = {}
    if census.get("ok"):
        for r in census.get("rows", []):
            name = r[0]
            bag = db_call("get_bag_space", {"toon": name})
            if not bag.get("ok"):
                # NEVER silent: a toon whose bags cannot be read (scope
                # gate, grant, flush error) is reported, not skipped - a
                # missing alarm must be visible, not a mystery
                scan_errors.append({"toon": name,
                                    "code": (bag.get("error") or {}).get("code")
                                    or (bag.get("code") or "error"),
                                    "message": ((bag.get("error") or {})
                                                .get("message")
                                                or "bag scan failed")})
                continue
            if bag.get("ok") and bag.get("rows"):
                try:
                    cap, used, free = (int(float(x)) for x in bag["rows"][0])
                except (ValueError, IndexError):
                    continue
                BAGS[name] = {"cap": cap, "used": used, "free": free}
                if free <= 0:
                    out.append({"toon": name, "kind": "bags_full",
                                "detail": "0/%d bag slots free - needs a "
                                          "vendor run" % cap})
                elif free <= 2:
                    out.append({"toon": name, "kind": "bags_tight",
                                "detail": "%d/%d bag slots free" % (free, cap)})
    return {"ok": True, "queried_at": _now(), "warnings": out,
            "scan_errors": scan_errors,
            "bags": BAGS, "census_ok": census.get("ok") is True}


def api_audit(limit):
    """The GUI's own write-action trail (see _audit)."""
    try:
        n = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        n = 50
    try:
        with open(AUDIT) as f:
            lines = f.readlines()[-n:]
    except OSError:
        lines = []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return {"ok": True, "queried_at": _now(), "entries": out}


def api_handplay():
    """The hand-play lock (director's HANDPLAY set): toons that are never
    commanded or brought online as bots. Read + toggle - the GUI's director
    instance is the mission engine, so this is the live registry."""
    try:
        d = _director()
    except Exception as e:
        return {"ok": False, "queried_at": _now(),
                "error": {"code": "director_unavailable",
                          "message": "the mission engine is not loadable: %s" % e}}
    return {"ok": True, "queried_at": _now(),
            "handplay": sorted(d.HANDPLAY)}


def api_handplay_set(toon, on):
    if not isinstance(toon, str) or not __import__("re").fullmatch(r"[A-Za-z]+", toon):
        return {"ok": False, "queried_at": _now(),
                "error": {"code": "bad_request",
                          "message": "bad toon name %r" % (toon,)}}
    try:
        d = _director()
    except Exception as e:
        return {"ok": False, "queried_at": _now(),
                "error": {"code": "director_unavailable",
                          "message": "the mission engine is not loadable: %s" % e}}
    name = toon.strip().lower()
    if on:
        d.HANDPLAY.add(name)
    else:
        d.HANDPLAY.discard(name)
    _handplay_save(d)
    _audit("handplay", "%s %s" % ("lock" if on else "unlock", toon))
    return {"ok": True, "queried_at": _now(), "toon": toon,
            "handplay": on, "handplay_list": sorted(d.HANDPLAY)}


def _trusted_live():
    """Toons the reconciler has live-verified (Tier 0 proof of life)."""
    cen = db_call("get_online_census", {"account_like": CENSUS})
    return [n for n, t in api_truth(cen).get("truth", {}).items()
            if t.get("state") == "live"]


def _review_load():
    try:
        with open(REVIEW_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _review_mark():
    """Store the 'now' cursor: the newest event id + the wall clock."""
    newest = 0
    r = tunnel_call("get_recent_events", {"limit": 1})
    if r.get("ok"):
        evs = (r.get("result") or {}).get("events") or []
        if evs:
            newest = int(evs[0].get("id") or 0)
    rv = {"ts": time.time(), "ts_str": _now(), "event_id": newest}
    try:
        with open(REVIEW_FILE, "w") as f:
            json.dump(rv, f)
    except OSError:
        pass
    return rv


def api_review():
    """The 'since your last review' digest: every GUI action (audit) and
    every tunnel event since the operator last said 'mark reviewed' - so
    nothing slips through the cracks between visits. First review (no
    file) starts from the beginning of the audit."""
    rv = _review_load()
    since = float(rv.get("ts") or 0)
    last_id = int(rv.get("event_id") or 0)
    new_actions = []
    for e in api_audit(500).get("entries", []):
        try:
            t = datetime.strptime(e.get("ts", ""),
                                  "%Y-%m-%dT%H:%M:%SZ"
                                  ).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if t > since:
            new_actions.append(e)
    ev = api_events(str(last_id), "500")
    new_events = (ev.get("result") or {}).get("events") or []
    return {"ok": True, "queried_at": _now(),
            "since": since, "since_str": rv.get("ts_str") or "",
            "counts": {"actions": len(new_actions),
                       "events": len(new_events)},
            "new_actions": new_actions, "new_events": new_events}


def api_doctor():
    """One sweep of every subsystem the dashboard depends on, each timed,
    each PASS/FAIL with its real error string - the operator pastes ONE
    block instead of spelunking a browser console (live 2026-11-23:
    'bags not populating' took four rounds of remote-controlled
    evidence gathering; never again - the GUI diagnoses itself)."""
    rows = []

    def probe(name, fn):
        t0 = time.time()
        try:
            detail = fn()
            rows.append({"name": name, "ok": True,
                         "ms": int((time.time() - t0) * 1000),
                         "detail": str(detail)[:200]})
        except Exception as e:
            rows.append({"name": name, "ok": False,
                         "ms": int((time.time() - t0) * 1000),
                         "detail": str(e)[:200]})

    def _census():
        r = db_call("get_online_census", {"account_like": CENSUS})
        if not r.get("ok"):
            raise RuntimeError(json.dumps(r)[:160])
        return "%d toon rows" % len(r.get("rows", []))

    probe("db-search census", _census)

    def _bags():
        r = db_call("get_online_census", {"account_like": CENSUS})
        if not r.get("ok"):
            raise RuntimeError("census unavailable")
        bad, seen = [], 0
        names = {}
        for row in r.get("rows", []):
            names[row[0]] = True
        wf = api_warnings()
        for name in list(names)[:100]:
            seen += 1
            b = (wf.get("bags") or {}).get(name)
            if b is None:
                bad.append(name)
        if bad:
            raise RuntimeError("no bag data for: %s" % ", ".join(bad[:8])
                               + (" (+%d more)" % (len(bad) - 8)
                                  if len(bad) > 8 else ""))
        return "%d toons, all scanned" % seen

    probe("bag scan (per toon)", _bags)

    def _prog():
        r = db_call("get_leveling_progress", {"account": CENSUS})
        if not r.get("ok"):
            raise RuntimeError(json.dumps(r.get("error") or r)[:160])
        return "%d toon rows" % len(r.get("rows", []))

    probe("leveling progress (xp/hr)", _prog)

    def _wallets():
        r = db_call("get_wallets", {"account": CENSUS})
        if not r.get("ok"):
            raise RuntimeError(json.dumps(r.get("error") or r)[:160])
        return "%d wallets" % len(r.get("rows", []))

    probe("wallets (gold)", _wallets)

    def _tunnel():
        r = tunnel_health()
        if not r.get("ok"):
            raise RuntimeError(json.dumps(r)[:160])
        return "healthy"

    def _world():
        r = api_world_uptime()
        if not r.get("ok") or r.get("state") != "up":
            raise RuntimeError(json.dumps(r)[:160])
        return "up %dm, heartbeat fresh" % (r.get("uptime_s", 0) // 60)

    probe("worldserver heartbeat", _world)

    probe("chat-tunnel", _tunnel)

    def _engine():
        r = api_missions()
        if not r.get("ok"):
            raise RuntimeError(json.dumps(r.get("error") or r)[:160])
        return "%d active leases" % len(r.get("missions", {}))

    probe("mission engine (leases)", _engine)

    text = "director-drop doctor %s\n" % _now()
    for r in rows:
        text += "%s  %-28s %5dms  %s\n" % (
            "PASS" if r["ok"] else "FAIL", r["name"], r["ms"], r["detail"])
    return {"ok": True, "queried_at": _now(), "rows": rows, "text": text}


def api_mission(bot, op, dest):
    """Submit ONE mission order through director's rails (submit_orders).
    The GUI only builds the five templated shapes; validate() re-checks
    everything (destination resolution, census, lease, whitelist)."""
    if op not in MISSION_OPS:
        return {"ok": False, "queried_at": _now(),
                "error": {"code": "bad_request",
                          "message": "op must be one of %s" %
                                     ", ".join(MISSION_OPS)}}
    if not isinstance(bot, str) or not __import__("re").fullmatch(r"[A-Za-z]+", bot):
        return {"ok": False, "queried_at": _now(),
                "error": {"code": "bad_request",
                          "message": "bad toon name %r" % (bot,)}}
    try:
        d = _director()
    except Exception as e:
        return {"ok": False, "queried_at": _now(),
                "error": {"code": "director_unavailable",
                          "message": "the mission engine is not loadable: %s" % e}}
    if op in MISSION_DEST_OPS and bot != "ALL":
        # Tier 1 (strict for missions): a mission costs a lease, a
        # teleport and minutes - never spend it on a toon with neither
        # live proof nor an online flag. Whispers stay permissive.
        tr = api_truth(db_call("get_online_census",
                               {"account_like": CENSUS}))
        state = (tr.get("truth") or {}).get(bot, {}).get("state", "offline")
        if state == "offline":
            _audit("mission", "%s %s %s -> refused (Tier 1: no live proof "
                   "and the DB flag says offline)" % (bot, op, dest or ""))
            return {"ok": False, "queried_at": _now(),
                "error": {"code": "refused",
                          "message": "%s never answered a live probe and "
                          "the DB flag says offline - missions are "
                          "refused until the bot is verified (Tier 1)"
                          % bot}}
    if op == "status":
        # the readback op (operator ask: 'a function that does rpg status
        # so i can see what the bots are doing'): one toon, or the whole
        # flag-online census. A READ via the read-only command - never an
        # order; ask_bot bypasses validate() by design. Defensive: an ask
        # failure must answer the request, never kill the handler.
        try:
            if bot == "ALL":
                cen = db_call("get_online_census", {"account_like": CENSUS})
                names = [r[0] for r in (cen.get("rows") or [])
                         if str(r[6]) == "1"]
                got = d.ask_all_bots(names, "rpg status")
                answers = [{"bot": n,
                            "status": "answered" if got.get(n) else "no answer",
                            "detail": got.get(n) or ""} for n in names]
                ok = any(x["status"] == "answered" for x in answers)
                _audit("mission", "ALL status sweep -> %d answered of %d"
                       % (sum(1 for x in answers
                              if x["status"] == "answered"), len(answers)))
                return {"ok": ok, "queried_at": _now(), "results": answers,
                        "result": answers[0] if answers else
                        {"status": "failed",
                         "detail": "no online census toons"}}
            a = d.ask_bot(bot, "rpg status")
        except Exception as e:
            _audit("mission", "%s status -> ASK CRASHED: %s" % (bot, e))
            return {"ok": False, "queried_at": _now(),
                    "result": {"bot": bot, "status": "error",
                               "detail": "the ask crashed: %s" % e},
                    "results": [{"bot": bot, "status": "error",
                                 "detail": str(e)}]}
        _audit("mission", "%s status -> %s" % (bot, a or "no answer"))
        return {"ok": a is not None, "queried_at": _now(),
                "result": {"bot": bot,
                           "status": "answered" if a else "no answer",
                           "detail": a or ""},
                "results": [{"bot": bot,
                             "status": "answered" if a else "no answer",
                             "detail": a or ""}]}
    if op == "vendor_all":
        # one town, every bags-full toon: the bulk answer to a wall of
        # bag alarms (each order still walks validate(); leased/offline
        # bots come back refused per-bot, not fatal)
        if not isinstance(dest, str) or not _DEST_RE.fullmatch(dest.strip()):
            return {"ok": False, "queried_at": _now(),
                "error": {"code": "bad_request",
                          "message": "vendor_all needs a destination name "
                                     "(a town with vendors)"}}
        flagged = [w["toon"] for w in api_warnings().get("warnings", [])
                   if w.get("kind") == "bags_full"]
        if not flagged:
            return {"ok": True, "queried_at": _now(), "results": [],
                    "note": "no bags-full toons right now"}
        orders = [{"bot": t, "cmd": "vendor %s" % dest.strip()}
                  for t in flagged]
        results = d.submit_orders(orders, master=LEO,
                                  trusted=_trusted_live())
        ok = any(r.get("status") in ("started", "sent") for r in results)
        _audit("mission", "ALL vendor_all %s -> %s"
               % (dest, "; ".join("%s:%s" % (r.get("status"),
                                             (r.get("detail") or "")[:80])
                                  for r in results)))
        return {"ok": ok, "queried_at": _now(),
                "result": results[0] if results else
                {"status": "failed", "detail": "no result"},
                "results": results}
    if op in MISSION_DEST_OPS:
        if not isinstance(dest, str) or not _DEST_RE.fullmatch(dest.strip()):
            return {"ok": False, "queried_at": _now(),
                "error": {"code": "bad_request",
                          "message": "%s needs a destination name (letters, "
                                     "digits, spaces, max 40)" % op}}
        orders = [{"bot": bot, "cmd": "%s %s" % (op, dest.strip())}]
    elif op == "state":
        preset = dest if isinstance(dest, str) else ""
        if preset not in STATE_PRESETS:
            return {"ok": False, "queried_at": _now(),
                "error": {"code": "bad_request",
                          "message": "state preset must be one of %s"
                                     % ", ".join(STATE_PRESETS)}}
        # THE OPERATOR'S VERIFY LOOP (2026-11-24, verbatim): 'I send NC
        # ?, read what it sends back, decide if its missing something, if
        # it is I send the command THEN I DO A NC ? AGAIN TO CONFIRM.'
        # Readback -> apply -> readback; both readbacks are shown in the
        # response. A preset whose after-readback does not confirm is
        # UNCONFIRMED, never sent-and-working.
        before = d.ask_bot(bot, "nc ?", wait=3)
        # multi-whisper presets are paced (back-to-back commands to the
        # same bot get dropped by the client)
        paced = []
        for c in STATE_PRESETS[preset]:
            try:
                paced.extend(d.submit_orders(
                    [{"bot": bot, "cmd": c}], master=LEO,
                    trusted=_trusted_live()))
            except Exception as e:
                paced.append({"bot": bot, "cmd": c, "status": "failed",
                              "detail": str(e)})
            if len(STATE_PRESETS[preset]) > 1:
                time.sleep(5)
        time.sleep(2)
        after = d.ask_bot(bot, "nc ?", wait=5)
        sent = bool(paced) and all(r.get("status") in ("started", "sent")
                                   for r in paced)
        confirmed = after and any(w.lower() in after.lower()
                                  for w in PRESET_MARKERS.get(preset, []))
        ok = sent and bool(confirmed)
        for r in paced:
            _receipt_note(r.get("bot", bot), r.get("detail", ""),
                          r.get("status", "unknown"))
        if sent:
            _state_set(bot, preset)
        _audit("mission", "%s state %s -> sent=%s confirmed=%s (before: %s; "
               "after: %s)"
               % (bot, preset, sent, bool(confirmed), (before or "?")[:80],
                  (after or "?")[:80]))
        detail = ("sent and nc ? CONFIRMS the strategy" if confirmed else
                  ("sent but nc ? did not confirm (after: %s)"
                   % ((after or "no answer")[:120]) if sent else
                   "not sent - see the per-order results"))
        return {"ok": ok, "queried_at": _now(),
                "result": {"bot": bot, "status":
                           "confirmed" if ok else "unconfirmed",
                           "detail": detail},
                "results": paced,
                "verify": {"before": before or "no answer",
                           "after": after or "no answer",
                           "confirmed": bool(confirmed)}}
    elif op == "disband":
        # Leo's whole group, via the proven rails: a 'group remove' order
        # per current member (validate() rewrites bots to the plain
        # 'leave' whisper, humans to the GM remove, and checks live
        # membership itself)
        grp = db_call("get_group_membership", {"toon": LEO})
        members = []
        if grp.get("ok"):
            members = [r[4] for r in grp.get("rows", [])
                       if r[4] and r[4] != LEO]
        if not members:
            return {"ok": True, "queried_at": _now(), "results": [],
                    "note": "%s is not in a group" % LEO}
        orders = [{"bot": m, "cmd": "group remove"} for m in members]
        results = d.submit_orders(orders, master=LEO,
                                  trusted=_trusted_live())
        ok = any(r.get("status") in ("started", "sent") for r in results)
        _audit("mission", "ALL disband -> %s"
               % "; ".join("%s:%s" % (r.get("status"),
                                      (r.get("detail") or "")[:80])
                           for r in results))
        return {"ok": ok, "queried_at": _now(),
                "result": results[0] if results else
                {"status": "failed", "detail": "no result"},
                "results": results}
    elif op == "leave":
        # the plain 'leave' whisper (LeaveGroupAction - the whitelisted,
        # no-GM-needed way a bot drops its party/raid; validate() checks
        # census/whitelist as always)
        orders = [{"bot": bot, "cmd": "leave"}]
    elif op in ("online", "offline"):
        # master commands (.playerbots bot add/remove) - validate() checks
        # the census state itself (refuses add-if-online / remove-if-offline)
        cmd = "bot add" if op == "online" else "bot remove"
        orders = [{"bot": bot, "cmd": cmd}]
    else:                                          # standdown (nc doctrine)
        orders = [{"bot": bot, "cmd": "nc -new rpg,-grind,-quest,-gather,-follow"}]
    try:
        results = d.submit_orders(orders, master=LEO,
                                  trusted=_trusted_live())
    except Exception as e:
        return {"ok": False, "queried_at": _now(),
                "error": {"code": "director_error",
                          "message": "submitting the order failed: %s" % e}}
    all_ok = bool(results) and all(r.get("status") in ("started", "sent")
                                   for r in results)
    any_ok = any(r.get("status") in ("started", "sent") for r in results)
    # a fleet order (bot=ALL) is ok if ANY landed (per-bot skips like
    # already-online are expected in a broadcast)
    ok = all_ok if bot != "ALL" else any_ok
    for r in results:
        _receipt_note(r.get("bot", bot), r.get("detail", ""),
                      r.get("status", "unknown"))
    if ok:
        if op == "state" and isinstance(dest, str) and dest in STATE_PRESETS:
            _state_set(bot, dest)
        elif op == "standdown":
            for r in results:
                if r.get("status") == "sent":
                    _state_set(r.get("bot", bot), "idle")
        elif op in MISSION_DEST_OPS:
            _state_clear(bot)
    _audit("mission", "%s %s %s -> %s" % (bot, op, dest or "",
           "; ".join("%s:%s" % (r.get("status"), (r.get("detail") or "")[:80])
                     for r in results)))
    return {"ok": ok, "queried_at": _now(),
            "result": results[0] if results else
            {"status": "failed", "detail": "no result"},
            "results": results}


# ------------------------------------------------------------------ page ----
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>director-drop web-gui - login</title>
<style>
body{margin:0;background:#15171d;color:#d8dce4;font:14px/1.5 system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh}
form{background:#1d2027;border:1px solid #2a2e38;border-radius:8px;padding:24px;width:300px}
h1{font-size:16px;margin:0 0 14px}label{display:block;margin:8px 0 3px;color:#8b93a3;font-size:12px}
input{width:100%;box-sizing:border-box;background:#15171d;color:#d8dce4;
border:1px solid #2a2e38;border-radius:5px;padding:6px 8px;font-size:13px}
button{margin-top:14px;width:100%;background:#3b82c4;color:#fff;border:none;
border-radius:5px;padding:8px;font-size:13px;cursor:pointer}
.err{color:#d05b5b;font-size:12px;min-height:16px;margin-top:8px}
</style></head><body>
<form id="f">
  <h1>director-drop web-gui</h1>
  <label>user</label><input id="u" autocomplete="username">
  <label>password</label><input id="p" type="password" autocomplete="current-password">
  <button type="submit">log in</button>
  <div class="err" id="e"></div>
</form>
<script>
document.getElementById('f').onsubmit=async(e)=>{
  e.preventDefault();
  const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({user:u.value,pass:p.value})});
  if(r.ok){location.href='/';return}
  document.getElementById('e').textContent='login failed';
};</script>
</body></html>"""

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>director-drop web-gui</title>
<style>
:root{--bg:#15171d;--panel:#1d2027;--line:#2a2e38;--fg:#d8dce4;--dim:#8b93a3;
--acc:#3b82c4;--ok:#4caf78;--warn:#d9a441;--err:#d05b5b;--mono:13px/1.45 ui-monospace,Menlo,Consolas,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line)}
header h1{font-size:17px;margin:0}
header .sub{color:var(--dim);font-size:12px}
.chips{display:flex;gap:8px;margin-left:auto;flex-wrap:wrap;align-items:center}
.chip{padding:3px 10px;border-radius:12px;font-size:12px;border:1px solid var(--line);color:var(--dim)}
.chip.up{color:var(--ok);border-color:var(--ok)}
.chip.down{color:var(--err);border-color:var(--err)}
.chip button{margin-left:8px;padding:1px 8px;font-size:11px}
main{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:14px 20px;max-width:1500px;margin:0 auto}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px;min-width:0}
.panel h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin:0 0 8px}
.panel .note{color:var(--dim);font-size:12px;margin:0 0 8px}
#panel-bots{grid-column:1/-1}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--dim);text-align:left;font-weight:500;border-bottom:1px solid var(--line);padding:4px 8px;white-space:nowrap}
td{border-bottom:1px solid var(--line);padding:4px 8px;white-space:nowrap}
tr:last-child td{border-bottom:none}
.on{color:var(--ok)}.off{color:var(--dim)}
.badge{display:inline-block;padding:0 6px;border-radius:9px;font-size:11px;margin-right:4px}
.badge.pending{background:#3a3320;color:var(--warn)}
.badge.replied{background:#1d3325;color:var(--ok)}
.badge.timeout{background:#38201f;color:var(--err)}
.badge.raid{background:#2a2f45;color:#9db4ee}
.badge.party{background:#1d3325;color:var(--ok)}
.badge.sys{background:#33302a;color:var(--warn)}
.dir-sent{color:var(--acc)}.dir-reply{color:var(--ok)}
#feed{font:var(--mono);max-height:420px;overflow-y:auto}
#feed .ev{padding:2px 0;border-bottom:1px dotted var(--line);word-break:break-all}
#feed .ev .raw{color:var(--dim)}
button{background:var(--acc);color:#fff;border:none;border-radius:5px;padding:3px 9px;font-size:12px;cursor:pointer}
button:hover{filter:brightness(1.15)}
button:disabled{background:var(--line);color:var(--dim);cursor:default}
button.warn{background:var(--warn)}
select,input{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:3px 7px;font-size:13px}
pre{font:var(--mono);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px;overflow:auto;max-height:260px;margin:8px 0 0}
.cat-cols{display:grid;grid-template-columns:220px 1fr;gap:10px}
.tools{max-height:300px;overflow-y:auto}
.tools button{display:block;width:100%;text-align:left;background:transparent;border:1px solid transparent;color:var(--fg);padding:4px 8px;margin:1px 0}
.tools button.sel{background:var(--bg);border-color:var(--line);color:var(--acc)}
.caveat{color:var(--warn);font-size:12px;margin:6px 0}
.err{color:var(--err)}
.tabs{display:flex;gap:6px;margin-bottom:8px}
.tabs button{background:transparent;border:1px solid var(--line);color:var(--dim)}
.tabs button.sel{color:var(--acc);border-color:var(--acc)}
.live{color:var(--acc);font-size:12px}
@media(max-width:1000px){main{grid-template-columns:1fr}}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.65);display:flex;
align-items:center;justify-content:center;z-index:20}
.modal .box{background:var(--panel);border:1px solid var(--line);
border-radius:8px;padding:16px;width:680px;max-width:95vw;max-height:88vh;overflow:auto}
.modal h3{margin:0 0 4px;font-size:16px;color:var(--acc)}
.modal .meta{color:var(--dim);font-size:12px;margin-bottom:10px}
.modal .sect{margin-top:12px;padding-top:8px;border-top:1px solid var(--line)}
.modal .sect b{display:block;font-size:11px;text-transform:uppercase;
letter-spacing:.06em;color:var(--dim);margin-bottom:6px}
</style></head><body>
<header>
  <h1>director-drop</h1><span class="sub">web-gui/2 &middot; read-mostly</span>
  <div class="chips">
    <span class="chip" id="chip-tunnel">chat-tunnel ?</span>
    <span class="chip" id="chip-db">db-search ?</span>
    <span class="chip" id="chip-leo">Leo session ?</span>
    <span class="chip" id="chip-gold">gold ?</span>
    <span class="chip" id="chip-world">world ?</span>
    <span class="chip" id="chip-refresh">updated ?</span>
    <button id="fleet-online" class="warn">all online</button>
    <button id="fleet-offline" class="warn">all offline</button>
    <button id="disband" class="warn">disband party</button>
    <button id="status-sweep" class="warn">status sweep</button>
    <button id="panic" style="background:var(--err)">STAND DOWN ALL</button>
  </div>
</header>
<main>
  <section class="panel" id="panel-bots">
    <h2>Bots &mdash; census + groups + missions</h2>
    <p class="note">DB rows are last-flushed values (A3/A5 staleness). &ldquo;live&rdquo; asks chat-tunnel <code>inspect</code> for the position right now (offline truth is db-search). The group badge is Leo's group; &ldquo;group&rdquo; checks any toon's own party/raid. <b>Click a toon's name to give them a mission.</b></p>
    <table id="bots"><thead><tr>
      <th>toon</th><th>account</th><th>lvl</th><th>class</th><th>map</th><th>zone</th><th>online (DB)</th><th>guild</th><th>group</th><th>mission</th><th>xp/hr</th><th>bags</th><th></th>
    </tr></thead><tbody></tbody></table>
    <div id="warnings" style="margin:6px 0"></div>
    <div id="toon-modal" class="modal" style="display:none"><div class="box" id="toon-modal-box"></div></div>
    <p class="note" id="bots-note"></p>
  </section>
  <section class="panel" id="panel-feed">
    <h2>Leo's chat &mdash; live (chat-tunnel cursor)</h2>
    <p class="note">whispers, guild chat and inspect system replies exactly as Leo sees them; long-polled, newest at the bottom</p>
    <div id="feed"></div>
  </section>
  <section class="panel" id="panel-catalog">
    <h2>Query catalog browser</h2>
    <div class="tabs">
      <button id="tab-db" class="sel">db-search</button>
      <button id="tab-grammar">tunnel grammar</button>
      <button id="tab-audit">action log</button>
      <button id="tab-doctor">doctor</button>
    </div>
    <div class="cat-cols" id="cat-wrap">
      <div class="tools" id="tools"></div>
      <div id="tool-detail"><p class="note">pick a tool</p></div>
    </div>
    <div id="grammar-wrap" style="display:none"><pre id="grammar-out">...</pre></div>
    <div id="audit-wrap" style="display:none"><pre id="audit-out">...</pre></div>
    <div id="doctor-wrap" style="display:none"><pre id="doctor-out">...</pre></div>
  </section>
</main>
<script>
const $=(id)=>document.getElementById(id);
const esc=(s)=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function chip(id, up, txt, btn){const e=$('chip-'+id);e.className='chip '+(up?'up':'down');e.innerHTML=esc(txt)+(btn?' <button class="'+(up?'warn':'')+'" id="btn-'+id+'">'+esc(btn)+'</button>':'')}
async function jget(u){const r=await fetch(u);return r.json()}
async function jpost(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});return r.json()}

const CLASSES={1:'Warrior',2:'Paladin',3:'Hunter',4:'Rogue',5:'Priest',6:'Death Knight',7:'Shaman',8:'Mage',9:'Warlock',10:'Monk',11:'Druid'};

let overview=null, selToon=null, selDest=null;
const MVERBS={farm:'farming in',travel:'travelling to',walk:'marching to',vendor:'selling in'};
function xphr(name){const p=((overview.progress||{}).rows)||[];
  for(const r of p){if(r[0]===name)return r[7]}return null}
function gold(name){const w=((overview.wallets||{}).rows)||[];
  for(const r of w){if(r[0]===name)return parseInt(r[3])||0}return 0}
async function loadOverview(){
  let o; try{ o=await jget('/api/overview') }catch(e){ return }
  overview=o;
  const t=o.tunnel||{}, d=o.census||{};
  chip('tunnel', t.ok===true, 'chat-tunnel '+(t.ok?'up':'down'));
  chip('db', d.ok===true, 'db-search '+(d.ok?'up':'down'));
  chip('leo', o.leo_active===true, 'Leo session '+(o.leo_active?'up':'down'),
       o.leo_active?'stop':'start');
  chip('refresh', true, 'updated '+o.queried_at);
  const tot=((o.wallets||{}).rows||[]).reduce((a,r)=>a+(parseInt(r[3])||0),0);
  const w=o.world||{};
  const wch=$('chip-world');
  if(w.state==='up'){
    wch.className='chip up';
    const upm=Math.floor((w.uptime_s||0)/60);
    wch.textContent='world up '+(upm>=60?Math.floor(upm/60)+'h'+(upm%60)+'m':upm+'m');
  }else if(w.state==='silent'){
    wch.className='chip down';
    const agm=Math.floor((w.age_s||0)/60);
    wch.textContent='world SILENT '+agm+'m';
    wch.title='the worldserver last wrote its heartbeat '+agm+' minutes ago - it is down or frozen';
  }else{
    wch.className='chip down';
    wch.textContent='world ?';
    wch.title=(w.reason||'uptime unavailable');
  }
  $('chip-gold').className='chip up';
  $('chip-gold').textContent='gold '+(tot/10000).toFixed(1)+'g';
  const bl=$('btn-leo'); if(bl) bl.onclick=async()=>{
    bl.disabled=true;
    await jpost('/api/leo',{action: overview.leo_active?'stop':'start'});
    setTimeout(loadOverview, 1500);
  };

  // Leo's group -> badge map (member name -> raid/party + leader)
  const g=o.leo_group||{}; const gmap={};
  if(g.ok){
    for(const r of (g.rows||[])){
      const type=parseInt(r[0])||0;
      gmap[r[4]]={kind:(type&2)?'raid':'party', leader:r[3], subgroup:r[8]};
    }
  }
  const badge=(name)=>{
    const m=gmap[name];
    return m?'<span class="badge '+m.kind+'" title="leader '+esc(m.leader)+', subgroup '+esc(m.subgroup)+'">'+m.kind+'</span>':'';
  };

  const tb=$('bots').querySelector('tbody'); tb.innerHTML='';
  const mbadge=(name)=>{
  const m=((overview.missions||{}).missions||{})[name];
  if(m) return '<span class="badge '+(m.kind==='farm'?'raid':'party')+'" title="pid '+esc(m.pid)+'">'+esc(MVERBS[m.kind]||m.kind)+' '+esc(m.name)+'</span>';
  const st=((overview.states||{})[name.toLowerCase()]||{});
  if(st.state) return '<span class="badge sys" title="state set via the GUI">'+esc(st.state)+'</span>';
  return '<span class="off">&mdash;</span>';
};
let bagsData={};
function bagsCell(name){
  const b=bagsData[name];
  if(!b) return '<td class="off">?</td>';
  const cls=(b.free<=0)?'timeout':(b.free<=2)?'pending':'on';
  return '<td class="'+cls+'" title="capacity '+b.cap+' - used '+b.used+'">'+b.free+'/'+b.cap+'</td>';
}
function onlineCell(name, dbOnline){
  const t=(((overview.truth|| {}))&&overview.truth[name])||{};
  if(t.state==='live')
    return '<td class="on" title="answered a live position probe - proof of life, whatever the DB flag says">live</td>';
  if(t.state==='db_online')
    return '<td class="pending" title="'+esc(t.why||'')+' - the reconciler is verifying">online (DB)</td>';
  return '<td class="off" title="'+esc(t.why||'offline')+'">offline</td>';
}
function zoneCell(name, zoneId){
  const p=(((overview.positions||{}).positions)||{})[name];
  if(p && p.zone && (Date.now()/1000 - p.ts) < 1800){
    return '<td class="on" title="live position: '+esc(p.x.toFixed(1)+', '+p.y.toFixed(1))+' (map '+esc(p.map)+')">'+esc(p.zone)+'</td>';
  }
  return '<td>'+esc(zoneId)+'</td>';
}
const row=(r)=>'<tr><td><a href="#" class="toonlink" data-name="'+esc(r[0])+'">'+esc(r[0])+'</a></td><td>'+esc(r[1])+'</td><td>'+esc(r[2])+'</td><td>'+esc(CLASSES[r[3]]||r[3])+'</td><td>'+esc(r[4])+'</td>'+zoneCell(r[0], r[5])+'</td>'
    +onlineCell(r[0], String(r[6]))+'<td>'+esc(r[7]||'')+'</td>'
    +'<td>'+badge(r[0])+'</td><td>'+mbadge(r[0])+'</td>'
    +'<td>'+esc(xphr(r[0])!==null?Math.round(xphr(r[0])):'')+'</td>'
    +bagsCell(r[0])
    +'<td><button data-name="'+esc(r[0])+'" class="livebtn">live</button> <button data-name="'+esc(r[0])+'" class="grpbtn">group</button></td></tr>';
  if((o.leo_char||{}).ok && (o.leo_char.rows||[]).length){
    const c=o.leo_char.rows[0];   // get_character: name,account,level,race,class,map,zone,money,totaltime,online
    tb.innerHTML=row([c[0],c[1],c[2],c[4],c[5],c[6],c[9],'']);
  }
  if(d.ok){
    for(const r of (d.rows||[])) tb.innerHTML+=row(r);
    $('bots-note').textContent=(d.row_count||0)+' bot rows - '+d.caveat;
  if(o.live_silent){
    const ws=(o.world||{}).state==='silent'
      ? ' THE WORLDSERVER ITSELF has stopped writing its heartbeat (last '+Math.floor((o.world.age_s||0)/60)+'m ago) - the server is down or frozen, not just the fleet.'
      : ' The Leo session or the fleet is not answering; the worldserver heartbeat is fresh.';
    $('warnings').innerHTML='<p class="err" style="margin:2px 0">&#9888; LIVE PROBES ARE SILENT.'+ws+' Nothing is verified right now.</p>'+$('warnings').innerHTML;
  }
  const pnote=[];
  if(!((o.progress||{}).ok)) pnote.push('leveling progress unavailable: '+esc(((o.progress||{}).error||{}).message||'db-search'));
  if(!((o.missions||{}).ok)) pnote.push('mission registry unavailable: '+esc(((o.missions||{}).error||{}).message||'engine'));
  if(pnote.length) $('bots-note').innerHTML+='<br><span class="err">'+pnote.join(' | ')+'</span>';
  } else if(!(o.leo_char||{}).ok) {
    $('bots-note').innerHTML='<span class="err">'+esc((d.error||{}).message||'db-search unreachable')+'</span>';
  }
  tb.querySelectorAll('button.livebtn').forEach(b=>b.onclick=()=>livePos(b));
  tb.querySelectorAll('button.grpbtn').forEach(b=>b.onclick=()=>grpCheck(b));
  tb.querySelectorAll('a.toonlink').forEach(a=>a.onclick=(e)=>{e.preventDefault();openModal(a.dataset.name)});
}

// ---- toon modal: identity + grouped actions (director's rails) --------
async function openModal(name){
  selToon=name; selDest=null;
  const o=overview||{};
  const m=((o.missions||{}).missions||{})[name];
  const w=((o.wallets||{}).rows||[]).find(r=>r[0]===name);
  const x=xphr(name);
  const box=$('toon-modal-box');
  box.innerHTML='<h3>'+esc(name)+'</h3>'
    +'<div class="meta">account '+esc((w&&w[1])||'?')+' &middot; lvl '+esc((w&&w[2])||'?')
    +' &middot; '+(gold(name)/10000).toFixed(1)+'g'
    +(x!==null?' &middot; '+Math.round(x)+' xp/hr':'')
    +(m?' &middot; <b>'+esc(MVERBS[m.kind]||m.kind)+' '+esc(m.name)+'</b>':'')
    +'</div>'
    +'<div id="handplay-row" style="margin:6px 0"></div>'
    +'<div class="sect"><b>existence</b>'
    +'<button id="op-online">bring online</button> '
    +'<button id="op-offline">take offline</button> '
    +'<button id="op-leave">leave party</button></div>'
    +'<div class="sect"><b>missions (destination required)</b>'
    +'<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:6px">'
    +'<input id="dest-in" placeholder="destination (game_tele), e.g. BootyBay" style="width:240px">'
    +'<button id="dest-search">find</button>'
    +'<input id="mat-in" placeholder="or farm what? (mat, e.g. Peacebloom)" style="width:240px">'
    +'<button id="mat-search">mats</button></div>'
    +'<div id="dest-list"></div><div id="dest-picked"></div>'
    +'<div style="display:flex;gap:6px;flex-wrap:wrap">'
    +'<button id="op-travel" class="warn" disabled>travel there</button>'
    +'<button id="op-farm" class="warn" disabled>farm there</button>'
    +'<button id="op-vendor" class="warn" disabled>vendor run</button></div></div>'
    +'<div class="sect"><b>states (nc-only presets)</b>'
    +'<div style="display:flex;gap:6px;flex-wrap:wrap">'
    +'<button id="op-grind">grind</button>'
    +'<button id="op-quest">quest</button>'
    +'<button id="op-gather">gather</button>'
    +'<button id="op-leveling">leveling</button>'
    +'<button id="op-follow">follow</button>'
    +'<button id="op-idle">idle</button>'
    +'<button id="op-standdown" style="background:var(--err)">stand down</button> '
    +'<button id="op-status">what are you doing?</button></div></div>'
    +'<p class="note" id="mission-result"></p>'
    +'<div style="margin-top:10px"><button id="op-close">close</button></div>';
  const hp=(((o.handplay||{}).handplay)||[]);
  $('handplay-row').innerHTML = hp.includes(name.toLowerCase())
    ? '<span class="badge timeout">hand-play - commands refused</span> '
      +'<button id="op-hpunlock">allow commands (remove lock)</button>'
    : '<button id="op-hplock">mark hand-play (refuse commands)</button>';
  const hpbtn=$('op-hpunlock')||$('op-hplock');
  hpbtn.dataset.label=hpbtn.textContent;
  hpbtn.onclick=()=>{
    if(hpbtn.dataset.armed!=='1'){hpbtn.dataset.armed='1';hpbtn.textContent='CONFIRM?';setTimeout(()=>{hpbtn.dataset.armed='0';hpbtn.textContent=hpbtn.dataset.label},2500);return}
    hpbtn.dataset.armed='0';hpbtn.textContent=hpbtn.dataset.label;hpbtn.disabled=true;
    jpost('/api/handplay',{toon:name,on:hpbtn.id==='op-hplock'}).then(async()=>{
      await loadOverview(); openModal(name);});
  };
  $('toon-modal').style.display='flex';
  $('toon-modal').onclick=(e)=>{if(e.target.id==='toon-modal')closeModal()};
  $('op-close').onclick=closeModal;
  function pickDest(n){selDest=n;$('dest-picked').innerHTML='destination: <b style="color:var(--warn)">'+esc(n)+'</b>';
    ['op-travel','op-farm','op-vendor'].forEach(id=>$(id).disabled=false);}
  $('dest-search').onclick=async()=>{
    const q=await jpost('/api/query',{tool:'get_game_tele',params:{name:$('dest-in').value}});
    $('dest-list').innerHTML=(q.ok&&q.rows.length)?q.rows.map(r=>'<button class="destbtn" data-n="'+esc(r[0])+'">'+esc(r[0])+' (map '+esc(r[1])+')</button>').join(' '):'<span class="err">'+esc((q.error||{}).message||'no match')+'</span>';
    $('dest-list').querySelectorAll('.destbtn').forEach(b=>b.onclick=()=>pickDest(b.dataset.n));
  };
  $('mat-search').onclick=async()=>{
    const q=await jpost('/api/query',{tool:'get_mat_spawns',params:{mat:$('mat-in').value}});
    $('dest-list').innerHTML=(q.ok&&q.rows.length)?q.rows.map(r=>'<button class="destbtn" data-n="'+esc(r[0])+'">'+esc(r[0])+' ('+esc(r[2])+' spawns)</button>').join(' '):'<span class="err">'+esc((q.error||{}).message||'no match')+'</span>';
    $('dest-list').querySelectorAll('.destbtn').forEach(b=>b.onclick=()=>pickDest(b.dataset.n));
  };
  const confirmable=(btn,op,preset)=>{
    btn.onclick=()=>{
      if(btn.dataset.armed!=='1'){btn.dataset.armed='1';btn.textContent='CONFIRM?';setTimeout(()=>{btn.dataset.armed='0';btn.textContent=btn.dataset.label},2500);return}
      btn.dataset.armed='0';btn.textContent=btn.dataset.label;
      runMission(op,preset);
    };
  };
  ['op-travel','op-farm','op-vendor','op-standdown','op-online','op-offline','op-leave']
    .forEach(id=>{const b=$(id);b.dataset.label=b.textContent;confirmable(b,id.slice(3));});
  ['op-grind','op-quest','op-gather','op-leveling','op-follow','op-idle']
    .forEach(id=>{const b=$(id);b.dataset.label=b.textContent;confirmable(b,'state',id.slice(3));});
  $('op-status').onclick=async()=>{
    const out=$('mission-result'); out.textContent='asking...';
    const r=await jpost('/api/mission',{bot:selToon,op:'status'});
    const det=r.ok?((r.result&&r.result.detail)||''):(r.error&&r.error.message)||'no answer';
    out.innerHTML=r.ok?'<span class="on">'+esc(det)+'</span>':'<span class="err">'+esc(det)+'</span>';
  };
  async function runMission(op,preset){
    const out=$('mission-result'); out.textContent='submitting...';
    const r=await jpost('/api/mission',{bot:selToon,op:op,dest:preset||((op==='standdown'||op==='online'||op==='offline'||op==='leave'||op==='status')?undefined:selDest)});
    const det=r.ok?((r.results||[]).map(x=>x.status+': '+x.detail).join(' | ')):((r.error||{}).message||(r.result||{}).detail);
    out.innerHTML=r.ok?'<span class="on">'+esc((r.results||[]).length+' order(s): '+det)+'</span>':'<span class="err">'+esc(det)+'</span>';
    setTimeout(loadOverview,1500);
  }
}
function closeModal(){$('toon-modal').style.display='none';selToon=null}

// ---- fleet-wide actions (ALL broadcast through the same rails) ----------
async function fleetAction(op,label){
  const b=$('panic');
  if(b.dataset.armed!=='1'){b.dataset.armed='1';b.textContent='CONFIRM '+label+'?';
    setTimeout(()=>{b.dataset.armed='0';b.textContent=b.dataset.label},3000);return}
  b.dataset.armed='0';b.textContent=b.dataset.label;
  b.disabled=true;
  const r=await jpost('/api/mission',{bot:'ALL',op:op});
  b.disabled=false;
  if(r.ok){setTimeout(loadOverview,1500)}
}
$('panic').dataset.label='STAND DOWN ALL';
$('panic').onclick=()=>fleetAction('standdown','stand-down-all');
$('fleet-online').onclick=async()=>{
  const b=$('fleet-online');
  if(b.dataset.armed!=='1'){b.dataset.armed='1';b.textContent='CONFIRM all online?';setTimeout(()=>{b.dataset.armed='0';b.textContent='all online'},3000);return}
  b.dataset.armed='0';b.textContent='all online';b.disabled=true;
  await jpost('/api/mission',{bot:'ALL',op:'online'});b.disabled=false;setTimeout(loadOverview,1500);
};
$('status-sweep').onclick=async()=>{
  const b=$('status-sweep');
  if(b.dataset.armed!=='1'){b.dataset.armed='1';b.textContent='CONFIRM sweep?';setTimeout(()=>{b.dataset.armed='0';b.textContent='status sweep'},3000);return}
  b.dataset.armed='0';b.textContent='status sweep';b.disabled=true;
  const r=await jpost('/api/mission',{bot:'ALL',op:'status'});
  b.disabled=false;
  if(r.ok){setTimeout(loadOverview,1500)}
};
$('disband').onclick=async()=>{
  const b=$('disband');
  if(b.dataset.armed!=='1'){b.dataset.armed='1';b.textContent='CONFIRM disband?';setTimeout(()=>{b.dataset.armed='0';b.textContent='disband party'},3000);return}
  b.dataset.armed='0';b.textContent='disband party';b.disabled=true;
  await jpost('/api/mission',{bot:'Leo',op:'disband'});b.disabled=false;setTimeout(loadOverview,1500);
};
$('fleet-offline').onclick=async()=>{
  const b=$('fleet-offline');
  if(b.dataset.armed!=='1'){b.dataset.armed='1';b.textContent='CONFIRM all offline?';setTimeout(()=>{b.dataset.armed='0';b.textContent='all offline'},3000);return}
  b.dataset.armed='0';b.textContent='all offline';b.disabled=true;
  await jpost('/api/mission',{bot:'ALL',op:'offline'});b.disabled=false;setTimeout(loadOverview,1500);
};


async function livePos(btn){
  const name=btn.dataset.name, cell=btn.parentElement;
  btn.disabled=true; cell.querySelector('.live')?.remove();
  const r=await jpost('/api/live',{bot:name,query:'position'});
  const s=document.createElement('span'); s.className='live';
  if(!r.ok){ s.className='err'; s.textContent=' '+((r.error||{}).message||'tunnel unreachable'); cell.appendChild(s); btn.disabled=false; return }
  const cid=(r.result||{}).correlation_id;
  s.textContent=' asking...'; cell.appendChild(s);
  for(let i=0;i<12;i++){
    await new Promise(res=>setTimeout(res,1000));
    const rr=await jget('/api/reply?cid='+encodeURIComponent(cid));
    if(rr.ok){
      const rep=(rr.result||{}).reply;
      if(rep){ s.textContent=' '+rep.command; btn.disabled=false; return }
      if((rr.result||{}).sent && rr.result.sent.status!=='pending'){
        s.textContent=' offline - truth is db-search'; btn.disabled=false; return }
    }
  }
  s.textContent=' no reply in window'; btn.disabled=false;
}

async function grpCheck(btn){
  const name=btn.dataset.name, cell=btn.parentElement;
  btn.disabled=true; cell.querySelector('.grp')?.remove();
  const r=await jpost('/api/query',{tool:'get_group_membership',params:{toon:name}});
  const s=document.createElement('span'); s.className='live grp';
  if(r.ok && r.row_count>0){
    const type=parseInt(r.rows[0][0])||0, kind=(type&2)?'raid':'party';
    s.innerHTML='<span class="badge '+kind+'">'+kind+'('+r.row_count+')</span> leader '+esc(r.rows[0][3]);
  } else if(r.ok){ s.textContent=' ungrouped'; }
  else { s.className='err'; s.textContent=' '+((r.error||{}).message||'db-search unreachable'); }
  cell.appendChild(s); btn.disabled=false;
}

// ---- live feed: chat-tunnel cursor seam (get_events_since + wait_for_events)
let cursor=0, feedOn=true;
function addEvent(ev){
  const f=$('feed');
  const div=document.createElement('div'); div.className='ev';
  const ch=ev.channel?' <span class="badge sys">'+esc(ev.channel)+'</span>':'';
  const stat=ev.direction==='sent'?' <span class="badge '+esc(ev.status)+'">'+esc(ev.status)+'</span>'
    :(ev.matched_correlation_id?' <span class="badge replied">matched</span>':' <span class="badge timeout">unmatched</span>');
  div.innerHTML='<span class="dir-'+esc(ev.direction)+'">'+esc(ev.direction)+'</span> '+esc(ev.ts)
    +' '+esc(ev.sender)+' &rarr; '+esc(ev.target)+stat+ch+' '+esc(ev.command)
    +(ev.correlation_id?' <span title="correlation_id">['+esc(ev.correlation_id)+']</span>':'')
    +'<div class="raw">'+esc(ev.raw_line)+'</div>';
  f.appendChild(div);
  while(f.children.length>200) f.removeChild(f.firstChild);
  f.scrollTop=f.scrollHeight;
}
async function feedLoop(){
  let first; try{ first=await jget('/api/events?after_id=0&limit=200') }catch(e){ setTimeout(feedLoop,3000); return }
  if(first.ok){ for(const ev of (first.result.events||[])) addEvent(ev); cursor=first.result.last_id }
  else $('feed').innerHTML='<p class="err">'+esc((first.error||{}).message||'chat-tunnel unreachable')+'</p>';
  while(feedOn){
    let w; try{ w=await jget('/api/events/wait?after_id='+cursor+'&timeout=20') }catch(e){ await new Promise(r=>setTimeout(r,3000)); continue }
    if(w.ok){ for(const ev of (w.result.events||[])) addEvent(ev); cursor=w.result.last_id }
    else if((w.error||{}).code==='unreachable'){ $('feed').innerHTML='<p class="err">'+esc((w.error||{}).message)+'</p>'; await new Promise(r=>setTimeout(r,3000)) }
  }
}
feedLoop();

// ---- catalog browser
let catalogs=null;
async function loadCatalogs(){
  try{ catalogs=await jget('/api/catalogs') }catch(e){ return }
  renderTools();
}
function renderTools(){
  const list=$('tools'); list.innerHTML='';
  const cat=(catalogs.db_search||{});
  if(!cat.ok){ list.innerHTML='<p class="err">'+esc((cat.error||{}).message||'unreachable')+'</p>'; return }
  for(const it of (cat.rows||[])){
    const b=document.createElement('button'); b.textContent=it.name;
    if(curTool===it.name) b.className='sel';
    b.onclick=()=>{curTool=it.name; renderTools(); renderDetail(it)};
    list.appendChild(b);
  }
}
let curTool=null;
function renderDetail(it){
  const d=$('tool-detail');
  let form='';
  for(const k of Object.keys(it.params||{})){
    form+='<label style="display:block;margin:6px 0">'+esc(k)+': <input id="p_'+esc(k)+'" style="width:60%"></label>';
  }
  d.innerHTML='<b style="color:var(--acc)">'+esc(it.name)+'</b>'
    +'<p class="note">'+esc(it.can||'')+'</p>'
    +(it.cannot?'<p class="note">does NOT answer: '+esc(it.cannot)+'</p>':'')
    +(it.caveat?'<p class="caveat">caveat: '+esc(it.caveat)+'</p>':'')
    +form+'<button id="run">run</button><pre id="out"></pre>';
  $('run').onclick=async()=>{
    const params={}; for(const k of Object.keys(it.params||{})){ const v=$('p_'+k).value; if(v) params[k]=v }
    const out=$('out'); out.textContent='...';
    const r=await jpost('/api/query',{tool:it.name,params});
    out.textContent=JSON.stringify(r,null,2);
  };
}
async function loadGrammar(){
  const g=await jget('/api/grammar');
  $('grammar-out').textContent=g.ok?JSON.stringify(g.result,null,2):JSON.stringify(g,null,2);
}
$('tab-db').onclick=()=>{$('tab-db').className='sel';$('tab-grammar').className='';$('cat-wrap').style.display='grid';$('grammar-wrap').style.display='none'};
$('tab-grammar').onclick=()=>{$('tab-grammar').className='sel';$('tab-db').className='';$('tab-audit').className='';$('tab-doctor').className='';$('cat-wrap').style.display='none';$('audit-wrap').style.display='none';$('doctor-wrap').style.display='none';$('grammar-wrap').style.display='block';loadGrammar()};
$('tab-audit').onclick=async()=>{$('tab-audit').className='sel';$('tab-db').className='';$('tab-grammar').className='';$('tab-doctor').className='';$('cat-wrap').style.display='none';$('grammar-wrap').style.display='none';$('audit-wrap').style.display='block';$('doctor-wrap').style.display='none';
  const NL=String.fromCharCode(10);
  const rv=await jget('/api/review');
  const digest=(rv.ok&&rv.counts)?'<p style="margin:4px 0"><b>since '+(rv.since_str?esc(rv.since_str):'the beginning')+':</b> '
    +rv.counts.actions+' raw actions, '+rv.counts.events+' chat events - <button id="mark-reviewed" class="warn">mark reviewed</button></p>'
    +(rv.new_actions||[]).map(e=>esc(e.ts+' '+e.action+' '+e.detail)).join(NL):'';
  const a=await jget('/api/audit?limit=100');
  $('audit-out').textContent=(digest?digest+NL+NL:'')
    +(a.ok?(a.entries.map(e=>e.ts+' '+e.action+' '+e.detail).join(NL)||'(no actions yet)'):JSON.stringify(a));
  const mrb=$('mark-reviewed');
  if(mrb){mrb.onclick=async()=>{
    if(mrb.dataset.armed!=='1'){mrb.dataset.armed='1';mrb.textContent='CONFIRM?';setTimeout(()=>{mrb.dataset.armed='0';mrb.textContent='mark reviewed'},2500);return}
    mrb.dataset.armed='0';mrb.textContent='mark reviewed';
    await jpost('/api/review',{action:'mark'});
    setTimeout(()=>$('tab-audit').onclick(),500);
  };}};
$('tab-doctor').onclick=async()=>{
  $('tab-doctor').className='sel';$('tab-db').className='';$('tab-grammar').className='';$('tab-audit').className='';
  $('cat-wrap').style.display='none';$('grammar-wrap').style.display='none';$('audit-wrap').style.display='none';
  $('doctor-wrap').style.display='block';
  $('doctor-out').textContent='running the sweep... (the bag scan can take a moment)';
  const d=await jget('/api/doctor');
  $('doctor-out').textContent=(d.ok?d.text:JSON.stringify(d));
  $('doctor-out').style.whiteSpace='pre';
};
$('tab-db').onclick=()=>{$('tab-db').className='sel';$('tab-grammar').className='';$('tab-audit').className='';$('tab-doctor').className='';$('cat-wrap').style='grid';$('grammar-wrap').style.display='none';$('audit-wrap').style.display='none';$('doctor-wrap').style.display='none'};

async function loadWarnings(){
  let w; try{ w=await jget('/api/warnings') }catch(e){ return }
  bagsData=w.bags||{};
  const el=$('warnings');
  if(!w.ok || !w.warnings.length){ el.innerHTML=''; return }
  const full=w.warnings.filter(x=>x.kind==='bags_full');
  const errs=(w.scan_errors||[]).map(x=>
    '<a href="#" class="warnchip" data-n="'+esc(x.toon)+'"><span class="badge pending" title="'+esc(x.message)+'">'+esc(x.toon)+': bag scan FAILED ('+esc(x.code)+')</span></a>').join(' ');
  const errrow=errs?'<p class="note" style="margin:2px 0" title="these toons can never raise a bag alarm until the scan works">&#9888; '+errs+'</p>':'';
  el.innerHTML=errrow+'<p class="note" style="margin:2px 0">&#9888; '+w.warnings.map(x=>
    '<a href="#" class="warnchip" data-n="'+esc(x.toon)+'"><span class="badge '+(x.kind==='bags_full'?'timeout':'pending')+'" title="'+esc(x.detail)+'">'+esc(x.toon)+': '+esc(x.kind==='bags_full'?'bags FULL - vendor run':'bags nearly full')+'</span></a>').join(' ')
    +(full.length?' <input id="vendorall-town" placeholder="town, e.g. Stormwind" style="width:160px"><button id="vendorall-btn" class="warn">vendor run all '+full.length+'</button>':'')+'</p>';
  el.querySelectorAll('a.warnchip').forEach(a=>a.onclick=(e)=>{e.preventDefault();openModal(a.dataset.n)});
  const vb=$('vendorall-btn');
  if(vb){vb.dataset.label=vb.textContent;vb.onclick=async()=>{
    if(vb.dataset.armed!=='1'){vb.dataset.armed='1';vb.textContent='CONFIRM?';setTimeout(()=>{vb.dataset.armed='0';vb.textContent=vb.dataset.label},3000);return}
    vb.dataset.armed='0';vb.textContent=vb.dataset.label;vb.disabled=true;
    await jpost('/api/mission',{bot:'ALL',op:'vendor_all',dest:$('vendorall-town').value});
    vb.disabled=false;
    setTimeout(loadOverview,1500); setTimeout(loadWarnings,2000);
  };}
}
async function tick(){ await loadOverview() }
tick(); loadCatalogs(); loadWarnings();
setInterval(tick,5000); setInterval(loadWarnings,60000);
</script></body></html>"""


# -------------------------------------------------------------- sessions ---
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
COOKIE_NAME = "webgui_session"


def is_loopback(addr):
    return (addr in ("localhost", "::1")
            or (addr.startswith("127.") and addr != "127.255.255.255"))


if AUTH_MODE == "on":
    AUTH_ENABLED = True
elif AUTH_MODE == "off":
    AUTH_ENABLED = False
else:
    AUTH_ENABLED = not is_loopback(BIND)


def new_session():
    tok = secrets.token_urlsafe(32)
    with SESSIONS_LOCK:
        now = time.time()
        for t in [t for t, exp in SESSIONS.items() if exp <= now]:
            del SESSIONS[t]
        SESSIONS[tok] = now + SESSION_TTL
    return tok


def session_valid(tok):
    with SESSIONS_LOCK:
        exp = SESSIONS.get(tok)
    return exp is not None and exp > time.time()


def drop_session(tok):
    with SESSIONS_LOCK:
        SESSIONS.pop(tok, None)


def check_login(user, password):
    ok_u = hmac.compare_digest(str(user or "").encode(), AUTH_USER.encode())
    ok_p = hmac.compare_digest(str(password or "").encode(), AUTH_PASS.encode())
    return ok_u and ok_p and bool(AUTH_PASS)


# -------------------------------------------------------------- server ----
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def reply(self, code, obj, ctype="application/json", headers=()):
        body = (obj if isinstance(obj, bytes) else
                json.dumps(obj).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def session_token(self):
        c = SimpleCookie()
        c.load(self.headers.get("Cookie") or "")
        m = c.get(COOKIE_NAME)
        return m.value if m else None

    def authed(self):
        if not AUTH_ENABLED:
            return True
        tok = self.session_token()
        return tok is not None and session_valid(tok)

    def do_GET(self):
        if AUTH_ENABLED and not self.authed():
            if self.path.startswith("/api/") or self.path == "/health":
                self.reply(401, {"ok": False,
                                 "error": "not logged in (POST /login first)"})
            elif self.path == "/login":
                self.reply(200, LOGIN_PAGE.encode(), "text/html; charset=utf-8")
            else:
                self.reply(302, b"", "text/plain",
                           headers=(("Location", "/login"),))
            return
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        if parsed.path == "/" or parsed.path.startswith("/index.html"):
            self.reply(200, PAGE.encode(), "text/html; charset=utf-8")
        elif parsed.path == "/health":
            self.reply(200, {"ok": True, "service": "web-gui",
                             "interface": INTERFACE, "port": PORT,
                             "tunnel": TUNNEL, "db_search_sock": DB_SOCK,
                             "leo": LEO, "leo_unit": LEO_UNIT,
                             "census_prefix": CENSUS})
        elif parsed.path == "/api/overview":
            self.reply(200, api_overview())
        elif parsed.path == "/api/events":
            self.reply(200, api_events((q.get("after_id") or ["0"])[0],
                                      (q.get("limit") or ["200"])[0]))
        elif parsed.path == "/api/events/wait":
            self.reply(200, api_events_wait((q.get("after_id") or ["0"])[0],
                                            (q.get("timeout") or ["20"])[0],
                                            (q.get("limit") or ["200"])[0]))
        elif parsed.path == "/api/reply":
            self.reply(200, api_reply((q.get("cid") or [""])[0]))
        elif parsed.path == "/api/grammar":
            self.reply(200, api_grammar())
        elif parsed.path == "/api/catalogs":
            self.reply(200, api_catalogs())
        elif parsed.path == "/api/missions":
            self.reply(200, api_missions())
        elif parsed.path == "/api/handplay":
            self.reply(200, api_handplay())
        elif parsed.path == "/api/warnings":
            self.reply(200, api_warnings())
        elif parsed.path == "/api/doctor":
            self.reply(200, api_doctor())
        elif parsed.path == "/api/review":
            self.reply(200, api_review())
        elif parsed.path == "/api/audit":
            self.reply(200, api_audit((q.get("limit") or ["50"])[0]))
        elif parsed.path == "/api/leo":
            self.reply(200, {"ok": True, "queried_at": _now(),
                             "unit": LEO_UNIT, "active": leo_active()})
        else:
            self.reply(404, {"ok": False, "error": "no such path"})

    def do_POST(self):
        if self.path == "/login":
            self.do_login()
            return
        if self.path == "/logout" and AUTH_ENABLED:
            tok = self.session_token()
            if tok:
                drop_session(tok)
            self.reply(200, {"ok": True},
                       headers=((("Set-Cookie",
                                  "%s=deleted; Path=/; Max-Age=0"
                                  % COOKIE_NAME)),))
            return
        if AUTH_ENABLED and not self.authed():
            self.reply(401, {"ok": False,
                             "error": "not logged in (POST /login first)"})
            return
        if self.path not in ("/api/query", "/api/live", "/api/leo",
                            "/api/mission", "/api/handplay",
                            "/api/review"):
            self.reply(404, {"ok": False, "error": "no such path"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode() or "{}")
        except (ValueError, UnicodeDecodeError) as e:
            self.reply(400, {"ok": False,
                             "error": "bad JSON: %s" % e})
            return
        if self.path == "/api/query":
            resp = api_query(req.get("tool"), req.get("params"))
        elif self.path == "/api/live":
            resp = api_live(req.get("bot"), req.get("query"))
        elif self.path == "/api/mission":
            resp = api_mission(req.get("bot"), req.get("op"), req.get("dest"))
        elif self.path == "/api/handplay":
            resp = api_handplay_set(req.get("toon"), req.get("on") is True)
        elif self.path == "/api/review":
            if req.get("action") == "mark":
                rv = _review_mark()
                _audit("review", "marked reviewed at %s (event_id %d)"
                       % (rv.get("ts_str"), rv.get("event_id", 0)))
                resp = {"ok": True, "queried_at": _now(), "review": rv}
            else:
                resp = api_review()
        else:
            resp = leo_action(req.get("action"))
        self.reply(200, resp)

    def do_login(self):
        if not AUTH_ENABLED:
            self.reply(200, {"ok": True, "note": "loopback bind - no login required"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode() or "{}")
        except (ValueError, UnicodeDecodeError) as e:
            self.reply(400, {"ok": False, "error": "bad JSON: %s" % e})
            return
        if not check_login(req.get("user"), req.get("pass")):
            time.sleep(0.5)
            self.reply(401, {"ok": False, "error": "login failed"})
            return
        tok = new_session()
        self.reply(200, {"ok": True},
                   headers=((("Set-Cookie",
                              "%s=%s; Path=/; HttpOnly; SameSite=Strict" %
                              (COOKIE_NAME, tok))),))


def resolve_lan_addr():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def main():
    global BIND
    if BIND in ("", "0.0.0.0", "::"):
        sys.exit("web-gui: refusing to bind %r - WEB_GUI_BIND needs a SPECIFIC "
                 "address (e.g. 192.168.88.2 or lan), never the wildcard" % BIND)
    if BIND.lower() in ("lan", "auto"):
        addr = resolve_lan_addr()
        if not addr or is_loopback(addr):
            sys.exit("web-gui: WEB_GUI_BIND=lan could not resolve a non-loopback "
                     "address on this host (no route out?); pass an explicit "
                     "address or keep the 127.0.0.1 default")
        BIND = addr
    if AUTH_MODE == "off" and not is_loopback(BIND):
        sys.exit("web-gui: WEB_GUI_AUTH=off with a non-loopback bind (%s) is "
                 "refused - the LAN bind requires the login screen" % BIND)
    if AUTH_ENABLED and not AUTH_PASS:
        sys.exit("web-gui: WEB_GUI_BIND=%s is not loopback and WEB_GUI_PASS "
                 "is not set - the login screen gates the whole GUI (the "
                 "query browser executes real DB reads, and the Leo button "
                 "can stop the session); set WEB_GUI_USER / WEB_GUI_PASS, or "
                 "keep the 127.0.0.1 default and use an SSH tunnel" % BIND)
    if AUTH_ENABLED:
        warn = ("\n" + "!" * 70 + "\n"
                "WARNING: web-gui is bound to %s - NOT loopback.\n"
                "The GUI has no TLS: the login and session cookie cross the \n"
                "network in CLEARTEXT, and behind the login the query browser \n"
                "executes REAL DB reads and the Leo button can stop/start \n"
                "the world session. Anyone who can reach this address and \n"
                "guesses the password can read your roster data.\n"
                "You are responsible for firewalling %s:%d (allow only the \n"
                "trusted LAN segment). Recommended default: keep 127.0.0.1 and \n"
                "ssh -N -L 8766:127.0.0.1:8766 <host> from the laptop.\n"
                + "!" * 70 + "\n") % (BIND, BIND, PORT)
        print(warn, file=sys.stderr)
        print(warn)
    _states_load()
    if POS_POLL > 0:
        threading.Thread(target=_position_loop, daemon=True).start()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print("web-gui: http://%s:%d/ (tunnel %s, db-search %s, leo %s [%s], "
          "census prefix %r)" % (BIND, PORT, TUNNEL, DB_SOCK, LEO, LEO_UNIT,
                                 CENSUS))
    srv.serve_forever()


if __name__ == "__main__":
    main()
