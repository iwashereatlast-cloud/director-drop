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
MISSION_OPS = ("travel", "farm", "walk", "level", "standdown")
MISSION_DEST_OPS = ("travel", "farm", "walk")
_DEST_RE = __import__("re").compile(r"^[A-Za-z0-9_' -]{1,40}$")

_DIRECTOR = {"mod": None}


def _director():
    """Lazy import of the mission engine (same process, shared lease file)."""
    if _DIRECTOR["mod"] is None:
        sys.path.insert(0, REPO_ROOT)
        import director
        _DIRECTOR["mod"] = director
    return _DIRECTOR["mod"]


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unreachable(source, e):
    return {"ok": False, "source": source, "queried_at": _now(),
            "error": {"code": "unreachable",
                      "message": "%s is not answering: %s" % (source, e)}}


# ------------------------------------------------------------- backends ----
def tunnel_call(tool, args):
    source = "chat-tunnel"
    body = json.dumps({"tool": tool, "args": args}).encode()
    req = urllib.request.Request(TUNNEL + "/", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
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
def api_overview():
    return {"ok": True, "interface": INTERFACE, "queried_at": _now(),
            "census_prefix": CENSUS, "leo": LEO, "leo_unit": LEO_UNIT,
            "leo_active": leo_active(),
            "tunnel": tunnel_health(),
            "leo_char": db_call("get_character", {"toon": LEO}),
            "leo_group": db_call("get_group_membership", {"toon": LEO}),
            "missions": api_missions(),
            "census": db_call("get_online_census",
                              {"account_like": CENSUS})}


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
    return _tunnel_result(tunnel_call(
        "wait_for_events", {"after_id": after_id, "timeout": timeout,
                            "limit": limit}))


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
    if op in MISSION_DEST_OPS:
        if not isinstance(dest, str) or not _DEST_RE.fullmatch(dest.strip()):
            return {"ok": False, "queried_at": _now(),
                "error": {"code": "bad_request",
                          "message": "%s needs a destination name (letters, "
                                     "digits, spaces, max 40)" % op}}
        cmd = "%s %s" % (op, dest.strip())
    elif op == "level":
        cmd = "nc +new rpg,+grind,-follow"        # the grind cycle (fewshot)
    else:                                          # standdown
        cmd = "rpg status idle"                   # releases a farm lease
    try:
        d = _director()
    except Exception as e:
        return {"ok": False, "queried_at": _now(),
                "error": {"code": "director_unavailable",
                          "message": "the mission engine is not loadable: %s" % e}}
    try:
        results = d.submit_orders([{"bot": bot, "cmd": cmd}], master=LEO)
    except Exception as e:
        return {"ok": False, "queried_at": _now(),
                "error": {"code": "director_error",
                          "message": "submitting the order failed: %s" % e}}
    r = results[0] if results else {"status": "failed", "detail": "no result"}
    return {"ok": r.get("status") in ("started", "sent"),
            "queried_at": _now(), "result": r}


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
</style></head><body>
<header>
  <h1>director-drop</h1><span class="sub">web-gui/2 &middot; read-mostly</span>
  <div class="chips">
    <span class="chip" id="chip-tunnel">chat-tunnel ?</span>
    <span class="chip" id="chip-db">db-search ?</span>
    <span class="chip" id="chip-leo">Leo session ?</span>
    <span class="chip" id="chip-refresh">updated ?</span>
  </div>
</header>
<main>
  <section class="panel" id="panel-bots">
    <h2>Bots &mdash; census + groups (db-search)</h2>
    <p class="note">DB rows are last-flushed values (A3/A5 staleness). &ldquo;live&rdquo; asks chat-tunnel <code>inspect</code> for the position right now (offline truth is db-search). The group badge is Leo's group; &ldquo;group&rdquo; checks any toon's own party/raid.</p>
    <table id="bots"><thead><tr>
      <th>toon</th><th>account</th><th>lvl</th><th>class</th><th>map</th><th>zone</th><th>online (DB)</th><th>guild</th><th>group</th><th></th>
    </tr></thead><tbody></tbody></table>
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
    </div>
    <div class="cat-cols" id="cat-wrap">
      <div class="tools" id="tools"></div>
      <div id="tool-detail"><p class="note">pick a tool</p></div>
    </div>
    <div id="grammar-wrap" style="display:none"><pre id="grammar-out">...</pre></div>
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
const MVERBS={farm:'farming in',travel:'travelling to',walk:'marching to'};
async function loadOverview(){
  let o; try{ o=await jget('/api/overview') }catch(e){ return }
  overview=o;
  const t=o.tunnel||{}, d=o.census||{};
  chip('tunnel', t.ok===true, 'chat-tunnel '+(t.ok?'up':'down'));
  chip('db', d.ok===true, 'db-search '+(d.ok?'up':'down'));
  chip('leo', o.leo_active===true, 'Leo session '+(o.leo_active?'up':'down'),
       o.leo_active?'stop':'start');
  chip('refresh', true, 'updated '+o.queried_at);
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
  if(!m) return '<span class="off">&mdash;</span>';
  return '<span class="badge '+(m.kind==='farm'?'raid':'party')+'" title="pid '+esc(m.pid)+'">'+esc(MVERBS[m.kind]||m.kind)+' '+esc(m.name)+'</span>';
};
const row=(r)=>'<tr><td><a href="#" class="toonlink" data-name="'+esc(r[0])+'">'+esc(r[0])+'</a></td><td>'+esc(r[1])+'</td><td>'+esc(r[2])+'</td><td>'+esc(CLASSES[r[3]]||r[3])+'</td><td>'+esc(r[4])+'</td><td>'+esc(r[5])+'</td>'
    +'<td class="'+(String(r[6])==='1'?'on':'off')+'">'+(String(r[6])==='1'?'online':'offline')+'</td><td>'+esc(r[7]||'')+'</td>'
    +'<td>'+badge(r[0])+'</td><td>'+mbadge(r[0])+'</td>'
    +'<td><button data-name="'+esc(r[0])+'" class="livebtn">live</button> <button data-name="'+esc(r[0])+'" class="grpbtn">group</button></td></tr>';
  if((o.leo_char||{}).ok && (o.leo_char.rows||[]).length){
    const c=o.leo_char.rows[0];   // get_character: name,account,level,race,class,map,zone,money,totaltime,online
    tb.innerHTML=row([c[0],c[1],c[2],c[4],c[5],c[6],c[9],'']);
  }
  if(d.ok){
    for(const r of (d.rows||[])) tb.innerHTML+=row(r);
    $('bots-note').textContent=(d.row_count||0)+' bot rows - '+d.caveat;
  } else if(!(o.leo_char||{}).ok) {
    $('bots-note').innerHTML='<span class="err">'+esc((d.error||{}).message||'db-search unreachable')+'</span>';
  }
  tb.querySelectorAll('button.livebtn').forEach(b=>b.onclick=()=>livePos(b));
  tb.querySelectorAll('button.grpbtn').forEach(b=>b.onclick=()=>grpCheck(b));
  tb.querySelectorAll('a.toonlink').forEach(a=>a.onclick=(e)=>{e.preventDefault();openDrawer(a.dataset.name)});
}

// ---- mission drawer: give the selected toon an order (director's rails)
async function openDrawer(name){
  selToon=name; selDest=null;
  const d=$('mission-drawer'); d.style.display='block';
  const m=((overview.missions||{}).missions||{})[name];
  d.innerHTML='<b style="color:var(--acc)">Mission: '+esc(name)+'</b>'
    +(m?' &mdash; currently '+esc(MVERBS[m.kind]||m.kind)+' <b>'+esc(m.name)+'</b>':'')
    +'<p class="note">orders go through the mission engine\u2019s own validate/lease rails; the destination must be a known teleport location</p>'
    +'<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">'
    +'<input id="dest-in" placeholder="destination (game_tele), e.g. BootyBay" style="width:240px">'
    +'<button id="dest-search">find</button>'
    +'<input id="mat-in" placeholder="or farm what? (mat, e.g. Peacebloom)" style="width:240px">'
    +'<button id="mat-search">mats</button></div>'
    +'<div id="dest-list" style="margin:6px 0"></div>'
    +'<div id="dest-picked" style="margin:6px 0"></div>'
    +'<div style="display:flex;gap:6px;flex-wrap:wrap">'
    +'<button id="op-travel" class="warn" disabled>travel there</button>'
    +'<button id="op-farm" class="warn" disabled>farm there</button>'
    +'<button id="op-level">level (grind cycle)</button>'
    +'<button id="op-standdown">stand down</button>'
    +'<button id="op-close">close</button></div>'
    +'<p class="note" id="mission-result"></p>';
  function pickDest(n){selDest=n;$('dest-picked').innerHTML='destination: <b style="color:var(--warn)">'+esc(n)+'</b>';$('op-travel').disabled=false;$('op-farm').disabled=false;}
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
  const confirmable=(btn,op)=>{
    btn.onclick=()=>{
      if(btn.dataset.armed!=='1'){btn.dataset.armed='1';btn.textContent='CONFIRM?';setTimeout(()=>{btn.dataset.armed='0';btn.textContent=btn.dataset.label},2500);return}
      btn.dataset.armed='0';btn.textContent=btn.dataset.label;
      runMission(op);
    };
  };
  ['op-travel','op-farm','op-level','op-standdown'].forEach(id=>{const b=$(id);b.dataset.label=b.textContent;confirmable(b,id.slice(3));});
  $('op-close').onclick=()=>{d.style.display='none';selToon=null};
  async function runMission(op){
    const out=$('mission-result'); out.textContent='submitting...';
    const r=await jpost('/api/mission',{bot:selToon,op:op,dest:(op==='level'||op==='standdown')?undefined:selDest});
    out.innerHTML=r.ok?'<span class="on">'+esc(r.result.status)+': '+esc(r.result.detail)+'</span>':'<span class="err">'+esc((r.error||{}).message||(r.result||{}).detail)+'</span>';
    setTimeout(loadOverview,1500);
  }
}

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
$('tab-grammar').onclick=()=>{$('tab-grammar').className='sel';$('tab-db').className='';$('cat-wrap').style.display='none';$('grammar-wrap').style.display='block';loadGrammar()};

async function tick(){ await loadOverview() }
tick(); loadCatalogs(); setInterval(tick,5000);
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
                            "/api/mission"):
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
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print("web-gui: http://%s:%d/ (tunnel %s, db-search %s, leo %s [%s], "
          "census prefix %r)" % (BIND, PORT, TUNNEL, DB_SOCK, LEO, LEO_UNIT,
                                 CENSUS))
    srv.serve_forever()


if __name__ == "__main__":
    main()
