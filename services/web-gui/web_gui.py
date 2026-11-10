#!/usr/bin/env python3
"""web-gui (service 4 of 4) - read-only Cockpit-style dashboard over services 1-3.

Panels (roadmap row 4): a bot status table (db-search census + rtt-engine
live position, on demand), a live tunnel event/command feed (chat-tunnel
get_recent_events), and a query catalog browser (db-search + rtt-engine
catalogs, with read-only execution of catalog queries).

READ-ONLY by construction: the GUI proxies only the read tools of the three
sibling services. chat-tunnel's `send_command` is never exposed (command
sends stay with chat-tunnel callers), and rtt ops are allowlisted to
{catalog, ping, pinfo} before the request leaves this process.

Local-only HTTP like chat-tunnel: binds 127.0.0.1 (WEBGUI_ADDR must be
loopback; anything else is refused). Every backend call degrades to a
per-panel "backend down" state - one sibling being offline never breaks
the page.

Env knobs (all optional):
  WEBGUI_PORT           8766
  WEB_GUI_BIND          127.0.0.1 (default, no login needed) - a SPECIFIC
                        address (e.g. 192.168.88.2), or `lan` to auto-resolve
                        the host's current LAN address at startup (survives
                        moving the server). The wildcard 0.0.0.0 is ALWAYS
                        refused. Any non-loopback bind requires a login
                        (WEB_GUI_USER / WEB_GUI_PASS) and prints a prominent
                        startup warning: no TLS - credentials cross the LAN
                        in cleartext, and the query browser executes real DB
                        reads. Firewall it; the SSH tunnel is the recommended
                        default (see README).
  WEB_GUI_USER          director   (login user, non-loopback binds only)
  WEB_GUI_PASS          (required for a non-loopback bind; empty = refuse)
  WEB_GUI_AUTH          auto (on for non-loopback binds) | on | off (off is
                        refused on non-loopback binds)
  WEBGUI_TUNNEL         http://127.0.0.1:8765     (chat-tunnel base URL)
  WEBGUI_DBSEARCH_SOCK  /tmp/db-search.sock       (db-search unix socket)
  WEBGUI_RTT_SOCK       /tmp/rtt-engine.sock     (rtt-engine unix socket)
  WEBGUI_CENSUS         i_was_here  (account prefix for the bot status table)
  WEBGUI_TIMEOUT        15          (seconds, per backend call)

Run: python3 web_gui.py serve
"""
import hmac
import json
import os
import secrets
import socket
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
RTT_SOCK = os.environ.get("WEBGUI_RTT_SOCK", "/tmp/rtt-engine.sock")
CENSUS = os.environ.get("WEBGUI_CENSUS", "i_was_here")
TIMEOUT = float(os.environ.get("WEBGUI_TIMEOUT", "15"))

AUTH_USER = os.environ.get("WEB_GUI_USER", "director")
AUTH_PASS = os.environ.get("WEB_GUI_PASS", "")
SESSION_TTL = float(os.environ.get("WEB_GUI_SESSION_TTL", str(12 * 3600)))
AUTH_MODE = os.environ.get("WEB_GUI_AUTH", "auto").strip().lower()


def is_loopback(addr):
    return (addr in ("localhost", "::1")
            or (addr.startswith("127.") and addr != "127.255.255.255"))


if AUTH_MODE == "on":
    AUTH_ENABLED = True
elif AUTH_MODE == "off":
    AUTH_ENABLED = False
else:
    AUTH_ENABLED = not is_loopback(BIND)

SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
COOKIE_NAME = "webgui_session"


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

RTT_READ_OPS = ("catalog", "ping", "pinfo")

INTERFACE = "web-gui/1"


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


def _err_code(resp):
    err = resp.get("error")
    if isinstance(err, dict):
        return err.get("code")
    return resp.get("code")


def db_call(tool, params):
    return unix_call(DB_SOCK, "db-search",
                     {"tool": tool, "params": params or {}})


def rtt_call(op, name=None):
    req = {"op": op}
    if name is not None:
        req["name"] = name
    return unix_call(RTT_SOCK, "rtt-engine", req)


# ------------------------------------------------------------- handlers ----
def api_overview():
    return {"ok": True, "interface": INTERFACE, "queried_at": _now(),
            "census_prefix": CENSUS,
            "tunnel": tunnel_health(),
            "rtt_ping": rtt_call("ping"),
            "census": db_call("get_online_census", {"account_like": CENSUS})}


def api_events(limit, direction):
    args = {"limit": limit}
    if direction:
        args["direction"] = direction
    resp = tunnel_call("get_recent_events", args)
    if resp.get("ok"):
        return {"ok": True, "queried_at": _now(),
                "events": resp["result"]["events"]}
    if "source" in resp:
        return resp
    return {"ok": False, "queried_at": _now(),
            "error": {"code": "tunnel_error",
                      "message": resp.get("error") or "chat-tunnel error"}}


def api_catalogs():
    return {"ok": True, "queried_at": _now(),
            "db_search": db_call("catalog", {}),
            "rtt_engine": rtt_call("catalog")}


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


def api_rtt(op, name):
    if op not in RTT_READ_OPS:
        return {"ok": False, "queried_at": _now(),
                "error": {"code": "gui_read_only",
                          "message": "web-gui is read-only: op %r is not one of "
                                     "%s (writes route to the owning service)"
                                     % (op, "/".join(RTT_READ_OPS))}}
    if op == "pinfo" and not (isinstance(name, str) and name):
        return {"ok": False, "queried_at": _now(),
                "error": {"code": "bad_request",
                          "message": "pinfo needs a {\"name\": ...}"}}
    return rtt_call(op, name)


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
.chips{display:flex;gap:8px;margin-left:auto;flex-wrap:wrap}
.chip{padding:3px 10px;border-radius:12px;font-size:12px;border:1px solid var(--line);color:var(--dim)}
.chip.up{color:var(--ok);border-color:var(--ok)}
.chip.down{color:var(--err);border-color:var(--err)}
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
.badge{display:inline-block;padding:0 6px;border-radius:9px;font-size:11px}
.badge.pending{background:#3a3320;color:var(--warn)}
.badge.replied{background:#1d3325;color:var(--ok)}
.badge.timeout{background:#38201f;color:var(--err)}
.dir-sent{color:var(--acc)}.dir-reply{color:var(--ok)}
#feed{font:var(--mono);max-height:420px;overflow-y:auto}
#feed .ev{padding:2px 0;border-bottom:1px dotted var(--line);word-break:break-all}
#feed .ev .raw{color:var(--dim)}
button{background:var(--acc);color:#fff;border:none;border-radius:5px;padding:3px 9px;font-size:12px;cursor:pointer}
button:hover{filter:brightness(1.15)}
button:disabled{background:var(--line);color:var(--dim);cursor:default}
select,input{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:3px 7px;font-size:13px}
pre{font:var(--mono);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px;overflow:auto;max-height:260px;margin:8px 0 0}
.cat-cols{display:grid;grid-template-columns:200px 1fr;gap:10px}
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
  <h1>director-drop</h1><span class="sub">read-only web-gui &middot; web-gui/1</span>
  <div class="chips" id="chips">
    <span class="chip" id="chip-tunnel">chat-tunnel ?</span>
    <span class="chip" id="chip-db">db-search ?</span>
    <span class="chip" id="chip-rtt">rtt-engine ?</span>
    <span class="chip" id="chip-refresh">updated ?</span>
  </div>
</header>
<main>
  <section class="panel" id="panel-bots">
    <h2>Bots &mdash; census (db-search)</h2>
    <p class="note">DB rows are last-flushed values (A3/A5 staleness); &ldquo;live&rdquo; asks rtt-engine for the current position of an online bot.</p>
    <table id="bots"><thead><tr>
      <th>toon</th><th>account</th><th>lvl</th><th>class</th><th>map</th><th>zone</th><th>online (DB)</th><th>guild</th><th></th>
    </tr></thead><tbody></tbody></table>
    <p class="note" id="bots-note"></p>
  </section>
  <section class="panel" id="panel-feed">
    <h2>Tunnel feed (chat-tunnel)</h2>
    <p class="note">sent commands and matched replies, newest first</p>
    <div id="feed"></div>
  </section>
  <section class="panel" id="panel-catalog">
    <h2>Query catalog browser</h2>
    <div class="tabs">
      <button id="tab-db" class="sel">db-search</button>
      <button id="tab-rtt">rtt-engine</button>
    </div>
    <div class="cat-cols">
      <div class="tools" id="tools"></div>
      <div id="tool-detail"><p class="note">pick a tool</p></div>
    </div>
  </section>
</main>
<script>
const $=(id)=>document.getElementById(id);
let curTab='db', curTool=null, latestCid=null;

function chip(id, up, txt){const e=$('chip-'+id);e.className='chip '+(up?'up':'down');e.textContent=txt}
async function jget(u){const r=await fetch(u);return r.json()}
async function jpost(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});return r.json()}

const CLASSES={1:'Warrior',2:'Paladin',3:'Hunter',4:'Rogue',5:'Priest',6:'Death Knight',7:'Shaman',8:'Mage',9:'Warlock',10:'Monk',11:'Druid'};
const esc=(s)=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function loadOverview(){
  let o; try{ o=await jget('/api/overview') }catch(e){ return }
  const t=o.tunnel||{}, d=o.census||{}, p=o.rtt_ping||{};
  chip('tunnel', t.ok===true, 'chat-tunnel '+(t.ok?'up':'down'));
  chip('db', d.ok===true, 'db-search '+(d.ok?'up':'down'));
  chip('rtt', p.ok===true, 'rtt-engine '+(p.ok?'up':'down'));
  chip('refresh', true, 'updated '+o.queried_at);
  const tb=$('bots').querySelector('tbody'); tb.innerHTML='';
  if(d.ok){
    for(const r of (d.rows||[])){
      const tr=document.createElement('tr');
      tr.innerHTML='<td>'+esc(r[0])+'</td><td>'+esc(r[1])+'</td><td>'+esc(r[2])+'</td><td>'+esc(CLASSES[r[3]]||r[3])+'</td><td>'+esc(r[4])+'</td><td>'+esc(r[5])+'</td>'
        +'<td class="'+(String(r[6])==='1'?'on':'off')+'">'+(String(r[6])==='1'?'online':'offline')+'</td><td>'+esc(r[7]||'')+'</td>'
        +'<td><button data-name="'+esc(r[0])+'">live</button></td>';
      tb.appendChild(tr);
    }
    $('bots-note').textContent=(d.row_count||0)+' rows - '+d.caveat;
  } else {
    $('bots-note').innerHTML='<span class="err">'+esc((d.error||{}).message||'db-search unreachable')+'</span>';
  }
  tb.querySelectorAll('button[data-name]').forEach(b=>b.onclick=()=>livePos(b));
}

async function livePos(btn){
  const name=btn.dataset.name, cell=btn.parentElement;
  btn.disabled=true; cell.querySelector('.live')?.remove();
  const r=await jpost('/api/rtt',{op:'pinfo',name});
  const s=document.createElement('span'); s.className='live';
  if(r.ok&&r.result&&r.result.online){
    const p=r.result.position||[];
    s.textContent=' '+r.result.zone+' ('+p.map(x=>Math.round(+x||0)).join(', ')+')';
  } else if(r.ok){
    s.textContent=' offline - truth is db-search';
  } else {
    s.className='err'; s.textContent=' '+((r.error||{}).message||'rtt-engine unreachable');
  }
  cell.appendChild(s); btn.disabled=false;
}

async function loadFeed(){
  let e; try{ e=await jget('/api/events?limit=100') }catch(err){ return }
  const f=$('feed'); f.innerHTML='';
  if(!e.ok){ f.innerHTML='<p class="err">'+esc((e.error||{}).message||'chat-tunnel unreachable')+'</p>'; return }
  for(const ev of (e.events||[])){
    const div=document.createElement('div'); div.className='ev';
    const stat=ev.direction==='sent'?' <span class="badge '+esc(ev.status)+'">'+esc(ev.status)+'</span>'
      :(ev.matched_correlation_id?' <span class="badge replied">matched</span>':' <span class="badge timeout">unmatched</span>');
    div.innerHTML='<span class="dir-'+esc(ev.direction)+'">'+esc(ev.direction)+'</span> '+esc(ev.ts)
      +' '+esc(ev.sender)+' &rarr; '+esc(ev.target)+stat+' '+esc(ev.command)
      +(ev.correlation_id?' <span title="correlation_id">['+esc(ev.correlation_id)+']</span>':'')
      +'<div class="raw">'+esc(ev.raw_line)+'</div>';
    f.appendChild(div);
    if(ev.direction==='sent') latestCid=ev.correlation_id;
  }
  if(!(e.events||[]).length) f.innerHTML='<p class="note">no tunnel events yet</p>';
}

let catalogs=null;
async function loadCatalogs(){
  try{ catalogs=await jget('/api/catalogs') }catch(e){ return }
  renderTools();
}
function renderTools(){
  const list=$('tools'); list.innerHTML='';
  const cat=curTab==='db'?(catalogs.db_search||{}):(catalogs.rtt_engine||{});
  let items;
  if(curTab==='db'){ items=(cat.rows||[]).map(r=>({name:r.name,desc:r.can,params:r.params,caveat:r.caveat})) }
  else { items=((cat.result&&cat.result.ops)||[]).map(v=>({name:v.op,desc:v.answers||'',params:v.op==='pinfo'?{name:'toon name'}:{},caveat:''})) }
  if(!cat.ok){ list.innerHTML='<p class="err">'+esc((cat.error||{}).message||'unreachable')+'</p>'; return }
  for(const it of items){
    const b=document.createElement('button'); b.textContent=it.name;
    if(curTool===it.name) b.className='sel';
    b.onclick=()=>{curTool=it.name; renderTools(); renderDetail(it)};
    list.appendChild(b);
  }
}
function renderDetail(it){
  const d=$('tool-detail');
  let form='';
  const keys=Object.keys(it.params||{});
  for(const k of keys){
    form+='<label style="display:block;margin:6px 0">'+esc(k)+': <input id="p_'+esc(k)+'" style="width:60%"></label>';
  }
  d.innerHTML='<b style="color:var(--acc)">'+esc(it.name)+'</b>'
    +'<p class="note">'+esc(it.desc||'')+'</p>'
    +(it.caveat?'<p class="caveat">caveat: '+esc(it.caveat)+'</p>':'')
    +form+'<button id="run">run</button><pre id="out"></pre>';
  $('run').onclick=async()=>{
    const params={}; for(const k of keys){ const v=$('p_'+k).value; if(v) params[k]=v }
    const out=$('out'); out.textContent='...';
    const r=curTab==='db'?await jpost('/api/query',{tool:it.name,params})
      :await jpost('/api/rtt',Object.assign({op:it.name},params));
    out.textContent=JSON.stringify(r,null,2);
  };
}
$('tab-db').onclick=()=>{curTab='db';curTool=null;$('tab-db').className='sel';$('tab-rtt').className='';renderTools();$('tool-detail').innerHTML='<p class="note">pick a tool</p>'};
$('tab-rtt').onclick=()=>{curTab='rtt';curTool=null;$('tab-rtt').className='sel';$('tab-db').className='';renderTools();$('tool-detail').innerHTML='<p class="note">pick a tool</p>'};

async function tick(){ await Promise.all([loadOverview(),loadFeed()]) }
tick(); loadCatalogs(); setInterval(tick,5000);
</script></body></html>"""


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
        if self.path == "/" or self.path.startswith("/index.html"):
            self.reply(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/health":
            self.reply(200, {"ok": True, "service": "web-gui",
                             "interface": INTERFACE, "port": PORT,
                             "tunnel": TUNNEL, "db_search_sock": DB_SOCK,
                             "rtt_sock": RTT_SOCK, "census_prefix": CENSUS})
        elif self.path == "/api/overview":
            self.reply(200, api_overview())
        elif self.path.startswith("/api/events"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            limit = (q.get("limit") or ["50"])[0]
            direction = (q.get("direction") or [None])[0]
            self.reply(200, api_events(limit, direction))
        elif self.path == "/api/catalogs":
            self.reply(200, api_catalogs())
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
                       headers=(("Set-Cookie",
                                 "%s=deleted; Path=/; Max-Age=0" % COOKIE_NAME),))
            return
        if AUTH_ENABLED and not self.authed():
            self.reply(401, {"ok": False,
                             "error": "not logged in (POST /login first)"})
            return
        if self.path not in ("/api/query", "/api/rtt"):
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
        else:
            resp = api_rtt(req.get("op"), req.get("name"))
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
                   headers=(("Set-Cookie",
                             "%s=%s; Path=/; HttpOnly; SameSite=Strict" %
                             (COOKIE_NAME, tok)),))


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
                 "query browser executes real DB reads); set WEB_GUI_USER / "
                 "WEB_GUI_PASS, or keep the 127.0.0.1 default and use an SSH "
                 "tunnel" % BIND)
    if AUTH_ENABLED:
        warn = ("\n" + "!" * 70 + "\n"
                "WARNING: web-gui is bound to %s - NOT loopback.\n"
                "The GUI has no TLS: the login and session cookie cross the \n"
                "network in CLEARTEXT, and behind the login the query browser \n"
                "executes REAL DB reads. Anyone who can reach this address and \n"
                "guesses the password can read your roster data.\n"
                "You are responsible for firewalling %s:%d (allow only the \n"
                "trusted LAN segment). Recommended default: keep 127.0.0.1 and \n"
                "ssh -N -L 8766:127.0.0.1:8766 <host> from the laptop.\n"
                + "!" * 70 + "\n") % (BIND, BIND, PORT)
        print(warn, file=sys.stderr)
        print(warn)
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print("web-gui: http://%s:%d/ (tunnel %s, db-search %s, rtt-engine %s, "
          "census prefix %r)" % (BIND, PORT, TUNNEL, DB_SOCK, RTT_SOCK, CENSUS))
    srv.serve_forever()


if __name__ == "__main__":
    main()
