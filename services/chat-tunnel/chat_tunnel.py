#!/usr/bin/env python3
"""chat-tunnel (service 1 of 4) - single owner of the Leo client chat log.

Tails the headless client chat log (default /tmp/leo.log), parses whisper
traffic into structured events stored in SQLite (WAL, retention-purged so the
DB never grows unbounded), sends commands through the Leo FIFO with a unique
correlation_id per sent command, and binds each bot reply to the exact command
that triggered it: same bot (sender) + inside the reply window, OLDEST pending
first - never "the next line seen".

Local-only JSON interface (127.0.0.1):
  POST /  {"tool": "send_command", "args": {"bot": "Ayla", "cmd": "nc ?"}}
          -> {"ok": true, "result": {"correlation_id": "...", ...}}
      tools: send_command(bot, cmd)
             get_recent_events(limit, direction)
             get_events_since(after_id, limit)      - cursor read (GUI feed)
             wait_for_events(after_id, timeout, limit) - long-poll (GUI feed)
             get_reply(correlation_id)
             stats()
  GET  /health

Run:  python3 services/chat-tunnel/chat_tunnel.py
Env:  CHAT_TUNNEL_LOG /tmp/leo.log     log to tail
      CHAT_TUNNEL_FIFO /tmp/leo.in     FIFO commands are written to
      CHAT_TUNNEL_PORT 8765            local-only HTTP port
      CHAT_TUNNEL_WINDOW 30           reply window, seconds
      CHAT_TUNNEL_RETENTION 86400     events older than this are purged
      CHAT_TUNNEL_PURGE_EVERY 60      purge/timing sweep cadence, seconds
      CHAT_TUNNEL_PACE 1.0           min seconds between FIFO writes (whisper
                                     throttle: bursts get dropped in-game)
      CHAT_TUNNEL_MAX_ROWS 100000     hard row cap (oldest purged first)
      CHAT_TUNNEL_DB <repo>/services/chat-tunnel/tunnel.db
"""
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

LOG = os.environ.get("CHAT_TUNNEL_LOG", "/tmp/leo.log")
FIFO = os.environ.get("CHAT_TUNNEL_FIFO", "/tmp/leo.in")
DB = os.environ.get("CHAT_TUNNEL_DB", os.path.join(HERE, "tunnel.db"))
PORT = int(os.environ.get("CHAT_TUNNEL_PORT", "8765"))
WINDOW = float(os.environ.get("CHAT_TUNNEL_WINDOW", "30"))
RETENTION = float(os.environ.get("CHAT_TUNNEL_RETENTION", str(24 * 3600)))
PURGE_EVERY = float(os.environ.get("CHAT_TUNNEL_PURGE_EVERY", "60"))
# minimum seconds between FIFO writes: the client/server throttle whispers,
# so bursts get dropped - sends are serialized and spaced by this
PACE = float(os.environ.get("CHAT_TUNNEL_PACE", "1.0"))
_send_lock = threading.Lock()
_last_send = [0.0]
MAX_ROWS = int(os.environ.get("CHAT_TUNNEL_MAX_ROWS", "100000"))
MASTER = os.environ.get("CHAT_TUNNEL_MASTER", "Leo")

# A14 (docs/assumptions.md) CONFIRMED live 2026-11-09: the headless client
# wraps every chat line in a loguru envelope, e.g.
#   2026-11-09 19:26:21.153 | MESSAGES | headless.world.chat.chat:display:124 - [Marle] [whisper]: Selling ...
# interleaved with DEBUG add_message lines that carry no chat. Anchoring to
# the display wrapper (not a bare search) means noise can never false-match
# a chat line embedded inside another message's text.
LINE_RE = re.compile(
    r"chat:display:\d+ - \[([A-Za-z]+)\] \[(whisper|guild)\]: (.*)")

# master-issued server commands go out as '.playerbots ...' lines instead of
# a /w (same split director.py's order_line() makes, minimal subset here)
MASTER_CMDS = ("bot add", "bot remove", "bot" )

# in-process counters for the stats tool (parse truth + health at a glance).
# lines_noise = log lines with no chat display wrapper at all (DEBUG etc.) -
# expected and harmless. lines_garbled = lines that LOOK like display lines
# but failed to parse - these are the A14 red flag.
STATS = {"lines_seen": 0, "lines_parsed": 0, "lines_noise": 0,
         "lines_garbled": 0,
         "replies_matched": 0, "replies_unmatched": 0,
         "sends_ok": 0, "sends_failed": 0, "started_at": time.time()}

# signaled after every event insert - the seam wait_for_events long-polls on
NEW_EVENT = threading.Condition()


def order_line(bot, cmd):
    if cmd.startswith("."):
        return cmd
    if any(cmd == mc or cmd.startswith(mc + " ") for mc in MASTER_CMDS):
        return ".playerbots %s %s" % (cmd, bot)
    return "/w %s %s" % (bot, cmd)


def db():
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                direction TEXT NOT NULL,      -- sent | reply
                sender TEXT NOT NULL,          -- sent: master; reply: bot name
                target TEXT NOT NULL,          -- sent: bot name; reply: master
                command TEXT,                  -- sent: the command; reply: text
                correlation_id TEXT,          -- sent: unique per command; reply: its own id
                raw_line TEXT,
                channel TEXT,                  -- whisper | guild (reply side)
                status TEXT,                   -- sent: pending|replied|timeout
                matched_correlation_id TEXT    -- reply: sent event it answered
            )""")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_cid ON events(correlation_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_target_pending "
            "ON events(direction, target, status, ts)")


def now():
    return time.time()


def new_cid():
    return uuid.uuid4().hex[:12]


def send_command(bot, cmd):
    bot = str(bot or "").strip()
    cmd = str(cmd or "").strip()
    if not re.fullmatch(r"[A-Za-z]+", bot):
        return False, "bad bot name %r (want [A-Za-z]+)" % bot
    if not cmd or "\n" in cmd or "\r" in cmd or len(cmd) > 300:
        return False, "bad command %r" % cmd[:40]
    line = order_line(bot, cmd)
    cid = new_cid()
    # nonblocking FIFO write: ENXIO when no reader (leo shell down) instead
    # of blocking an HTTP worker forever on a hung shell; lines < PIPE_BUF
    # are atomic on a FIFO, so a concurrent writer cannot interleave.
    # Serialized + spaced by PACE: the client/server drops whisper bursts.
    with _send_lock:
        delay = _last_send[0] + PACE - now()
        if delay > 0:
            time.sleep(delay)
        try:
            fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(fd, (line + "\n").encode())
            finally:
                os.close(fd)
        except OSError as e:
            STATS["sends_failed"] += 1
            return False, "FIFO write to %s failed (no reader? leo shell down?): %s" % (FIFO, e)
        _last_send[0] = now()
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO events (ts, direction, sender, target, command,"
            " correlation_id, raw_line, channel, status)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (now(), "sent", MASTER, bot, cmd, cid, line, None, "pending"))
    _maybe_purge()
    STATS["sends_ok"] += 1
    with NEW_EVENT:
        NEW_EVENT.notify_all()
    return True, {"correlation_id": cid, "line": line, "bot": bot, "cmd": cmd}


def sweep_timeouts(conn, t):
    conn.execute(
        "UPDATE events SET status='timeout' WHERE direction='sent'"
        " AND status='pending' AND ts < ?", (t - WINDOW,))


def match_reply(sender, channel, text, raw_line, t):
    """Bind a reply to the exact command that triggered it: same bot (sender)
    + inside the reply window, OLDEST pending command first. A garbled or
    out-of-window line never consumes a pending command."""
    with closing(db()) as conn, conn:
        sweep_timeouts(conn, t)
        if channel == "whisper":
            row = conn.execute(
                "SELECT id, correlation_id FROM events"
                " WHERE direction='sent' AND status='pending'"
                " AND target = ? COLLATE NOCASE AND ts >= ?"
                " ORDER BY ts ASC, id ASC LIMIT 1",
                (sender, t - WINDOW)).fetchone()
        else:
            row = None
        if row:
            matched = row["correlation_id"]
            conn.execute("UPDATE events SET status='replied' WHERE id=?",
                         (row["id"],))
        else:
            matched = None
        cid = new_cid()
        conn.execute(
            "INSERT INTO events (ts, direction, sender, target, command,"
            " correlation_id, raw_line, channel, status, matched_correlation_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (t, "reply", sender, MASTER, text, cid, raw_line, channel,
             None, matched))
    _maybe_purge()
    if matched:
        STATS["replies_matched"] += 1
    else:
        STATS["replies_unmatched"] += 1
    with NEW_EVENT:
        NEW_EVENT.notify_all()
    return matched


def handle_reply_line(raw_line, t):
    STATS["lines_seen"] += 1
    if "chat:display:" not in raw_line:
        STATS["lines_noise"] += 1     # DEBUG/loguru noise: no chat in it at all
        return None
    m = LINE_RE.search(raw_line)
    if not m:
        STATS["lines_garbled"] += 1
        return None                  # garbled/foreign line: ignored, never matched
    sender, channel, text = m.group(1), m.group(2), m.group(3).strip()
    if not text:
        STATS["lines_garbled"] += 1
        return None
    STATS["lines_parsed"] += 1
    return match_reply(sender, channel, text, raw_line.rstrip("\n"), t)


def get_reply(correlation_id):
    cid = str(correlation_id or "").strip()
    with closing(db()) as conn, conn:
        sweep_timeouts(conn, now())
        sent = conn.execute(
            "SELECT * FROM events WHERE correlation_id=? AND direction='sent'",
            (cid,)).fetchone()
        if not sent:
            return False, "unknown correlation_id %r" % cid
        reply = conn.execute(
            "SELECT * FROM events WHERE matched_correlation_id=?"
            " ORDER BY id ASC LIMIT 1", (cid,)).fetchone()
        return True, {"sent": ev_json(sent), "reply": ev_json(reply) if reply else None}


def get_recent_events(limit=50, direction=None):
    limit = max(1, min(int(limit or 50), 1000))
    with closing(db()) as conn, conn:
        sweep_timeouts(conn, now())
        if direction in ("sent", "reply", None):
            q = "SELECT * FROM events"
            args = []
            if direction:
                q += " WHERE direction=?"
                args.append(direction)
            q += " ORDER BY id DESC LIMIT ?"
            args.append(limit)
            rows = conn.execute(q, args).fetchall()
        else:
            return False, "bad direction %r (want sent|reply)" % direction
        return True, {"events": [ev_json(r) for r in rows]}


def get_events_since(after_id, limit=200):
    """Cursor read: every event with id > after_id, ascending - the poll
    pattern for a live GUI feed. `after_id` is the last id the reader saw;
    the result carries the new cursor (`last_id`). Retention-purged rows are
    simply gone (a GUI re-syncs from id 0 after a gap longer than retention)."""
    after = int(after_id or 0)
    limit = max(1, min(int(limit or 200), 1000))
    with closing(db()) as conn, conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT ?",
            (after, limit)).fetchall()
        last = conn.execute("SELECT MAX(id) AS m FROM events").fetchone()["m"] or 0
    return True, {"events": [ev_json(r) for r in rows], "last_id": last}


def wait_for_events(after_id, timeout=10, limit=200):
    """Server-side long-poll: blocks until an event with id > after_id lands
    (or `timeout` passes), then returns ascending - one request per batch of
    new lines, no hot-polling. This is the real-time seam for the web GUI."""
    after = int(after_id or 0)
    timeout = max(0.0, min(float(timeout or 10), 25.0))
    limit = max(1, min(int(limit or 200), 1000))
    deadline = now() + timeout
    while True:
        ok, res = get_events_since(after, limit)
        if not ok:
            return False, res
        if res["events"] or now() >= deadline:
            return True, res
        with NEW_EVENT:
            NEW_EVENT.wait(max(0.05, deadline - now()))


def describe():
    """The command grammar itself - the single source of truth for HOW to
    talk through this tunnel. The LLM (or any client) fetches this once and
    knows the interface; the daemon bakes it into its system prompt at
    startup. Changes here propagate to every client on restart - no
    hand-maintained prompt to drift from the enforcing code."""
    return True, {
        "interface": "chat-tunnel/1",
        "purpose": "communication only: talk to bots, listen to bots, "
                   "verify the conversation is not garbled in either "
                   "direction, serve the event feed",
        "send": {
            "whisper": {
                "line": "/w <bot> <command>",
                "example": "/w Ayla nc ?",
                "constraints": "bot name matches [A-Za-z]+; text max 300 "
                               "chars, no newlines; sends are paced >= "
                               "%ss apart (whisper throttle)" % PACE,
            },
            "master_command": {
                "line": ".playerbots <subcommand> <bot>",
                "example": ".playerbots bot add Gau",
                "notes": "server-side bot management from Leo's session "
                         "(bot add / bot remove / bot addaccount)",
            },
            "raw_dot": {
                "line": "<anything starting with .>",
                "notes": "passed through verbatim (server command)",
            },
        },
        "listen_channels": ["whisper", "guild"],
        "listen_note": "chat rides a loguru envelope in the log; only "
                       "display lines are parsed. Send-side /s and /g are "
                       "NOT advertised: unverified on this client.",
        "reply_matching": {
            "rule": "a reply binds to the OLDEST still-pending command to "
                    "the same bot within the window - sender + time window, "
                    "never 'next line seen'",
            "window_sec": WINDOW,
        },
        "tools": ["send_command", "get_recent_events", "get_events_since",
                  "wait_for_events", "get_reply", "verify", "describe",
                  "stats"],
    }


# --- delivery verification: tunnel events vs the server's own chat log ---
# The server records every whisper into acore_auth.logs (CONFIRMED live
# 2026-11-10, roadmap). This is the communication-only DB function that
# lives here: compare what WE sent/parsed against what the SERVER saw, and
# report garbles. Read-only, same MySQL identity the rest of the stack uses.
MYCNF = os.environ.get("CHAT_TUNNEL_MYCNF",
                       os.path.expanduser("~/.my.cnf-director"))
LOGDB_FIXTURE = os.environ.get("CHAT_TUNNEL_LOGDB_FIXTURE", "")


def _logdb_rows(t_lo, t_hi):
    """[(unixtime, string)] whisper rows from acore_auth.logs in the window.
    A fixture file (JSON list) replaces the DB - the proof harness runs the
    same code path without a worldserver."""
    if LOGDB_FIXTURE:
        try:
            with open(LOGDB_FIXTURE) as f:
                rows = json.load(f)
            return [(int(r[0]), r[1]) for r in rows
                    if t_lo <= int(r[0]) <= t_hi]
        except (OSError, ValueError) as e:
            return []
    env = dict(os.environ)
    env.pop("MYSQL_PWD", None)
    out = subprocess.run(
        ["mysql", "--defaults-file=" + MYCNF, "-N", "-B", "-e",
         "SELECT time, string FROM acore_auth.logs "
         "WHERE type='chat.whisper' AND time BETWEEN %d AND %d" % (t_lo, t_hi)],
        capture_output=True, text=True, env=env, timeout=10)
    if out.returncode != 0:
        raise RuntimeError("acore_auth.logs query failed: %s"
                           % out.stderr.strip()[:200])
    rows = []
    for ln in out.stdout.splitlines():
        parts = ln.split("\t", 1)
        if len(parts) == 2 and parts[0].strip().isdigit():
            rows.append((int(parts[0]), parts[1]))
    return rows


def _verify_side(ev, direction):
    """Compare one tunnel event against the server's chat log.
    Verdict: delivered (logged, text identical) | garbled (logged, text
    differs) | not_found (nothing logged in the window) | no_event."""
    if not ev:
        return {"verdict": "no_event"}
    t = int(ev["ts"])
    rows = _logdb_rows(t - 3, t + 30)
    if direction == "sent":
        needle = "Player %s whisper %s: %s" % (MASTER, ev["target"], ev["command"])
    else:
        needle = "Player %s whisper %s: %s" % (ev["sender"], MASTER, ev["command"])
    for _, s in rows:
        if s == needle:
            return {"verdict": "delivered", "db_line": s}
        # same speakers, different text = garbled in transit
        head = needle.split(":")[0]
        if s.startswith(head + ":"):
            return {"verdict": "garbled", "db_line": s, "expected": needle}
    return {"verdict": "not_found"}


def verify(correlation_id):
    """Both-direction garble check for one command: what the tunnel sent
    vs what the server logged, and what the tunnel parsed vs what the
    server logged. Purely on-demand - this tool never runs itself."""
    cid = str(correlation_id or "").strip()
    got = get_reply(cid)
    if not got[0]:
        return got
    sent_ev, reply_ev = got[1]["sent"], got[1]["reply"]
    return True, {"correlation_id": cid,
                  "sent": _verify_side(sent_ev, "sent"),
                  "reply": _verify_side(reply_ev, "reply")}


def stats():
    """Parse truth + health in one call: in-process line counters plus the
    DB's own status tally. `lines_noise` (DEBUG etc.) growing is normal;
    `lines_garbled` growing while real whisper lines exist in the log means
    LINE_RE is missing a live format (A14 FAIL)."""
    out = dict(STATS)
    out["uptime_sec"] = round(now() - STATS["started_at"], 1)
    with closing(db()) as conn, conn:
        conn.row_factory = sqlite3.Row
        tally = {r["status"] or "reply": r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM events GROUP BY status")}
        out["db_rows"] = sum(tally.values())
        out["db_sent_pending"] = tally.get("pending", 0)
        out["db_sent_replied"] = tally.get("replied", 0)
        out["db_sent_timeout"] = tally.get("timeout", 0)
        out["db_reply_rows"] = tally.get("reply", 0)
    return True, out


def ev_json(r):
    return {
        "id": r["id"], "ts": r["ts"], "direction": r["direction"],
        "sender": r["sender"], "target": r["target"], "command": r["command"],
        "correlation_id": r["correlation_id"], "raw_line": r["raw_line"],
        "channel": r["channel"], "status": r["status"],
        "matched_correlation_id": r["matched_correlation_id"],
    }


_PURGE = {"n": 0, "last": 0.0}


def _maybe_purge():
    """Retention, folded into the write path: the service has NO timer
    thread - housekeeping runs inline on writes (every 50 inserts or
    PURGE_EVERY seconds since the last sweep, whichever comes first)."""
    _PURGE["n"] += 1
    t = now()
    if _PURGE["n"] < 50 and t - _PURGE["last"] < PURGE_EVERY:
        return
    _PURGE["n"] = 0
    _PURGE["last"] = t
    try:
        with closing(db()) as conn, conn:
            sweep_timeouts(conn, t)
            conn.execute("DELETE FROM events WHERE ts < ?", (t - RETENTION,))
            conn.execute(
                "DELETE FROM events WHERE id NOT IN"
                " (SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                (MAX_ROWS,))
    except sqlite3.Error as e:
        print("purge error: %s" % e, file=sys.stderr)


def tail_loop():
    while True:
        try:
            proc = subprocess.Popen(["tail", "-n", "0", "-F", LOG],
                                    stdout=subprocess.PIPE, bufsize=1,
                                    text=True)
        except OSError as e:
            print("tail spawn failed: %s" % e, file=sys.stderr)
            time.sleep(2)
            continue
        while True:
            raw = proc.stdout.readline()
            if not raw:
                break
            try:
                handle_reply_line(raw, now())
            except Exception as e:
                print("line handling error: %s" % e, file=sys.stderr)
        proc.wait()
        print("tail exited (rc=%s), restarting" % proc.returncode, file=sys.stderr)
        time.sleep(2)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/grammar":
            ok, res = describe()
            self.reply(200, {"ok": True, "result": res})
        elif self.path == "/health":
            self.reply(200, {"ok": True, "service": "chat-tunnel",
                             "log": LOG, "fifo": FIFO, "db": DB,
                             "window": WINDOW, "retention": RETENTION})
        else:
            self.reply(404, {"ok": False, "error": "no such path"})

    def do_POST(self):
        if self.path != "/":
            self.reply(404, {"ok": False, "error": "no such path"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode() or "{}")
        except (ValueError, UnicodeDecodeError) as e:
            self.reply(400, {"ok": False, "error": "bad JSON: %s" % e})
            return
        tool = req.get("tool")
        args = req.get("args") or {}
        try:
            if tool == "send_command":
                ok, res = send_command(args.get("bot"), args.get("cmd"))
            elif tool == "get_recent_events":
                ok, res = get_recent_events(args.get("limit"),
                                             args.get("direction"))
            elif tool == "get_reply":
                ok, res = get_reply(args.get("correlation_id"))
            elif tool == "get_events_since":
                ok, res = get_events_since(args.get("after_id"),
                                           args.get("limit"))
            elif tool == "wait_for_events":
                ok, res = wait_for_events(args.get("after_id"),
                                          args.get("timeout"), args.get("limit"))
            elif tool == "describe":
                ok, res = describe()
            elif tool == "verify":
                ok, res = verify(args.get("correlation_id"))
            elif tool == "stats":
                ok, res = stats()
            else:
                self.reply(404, {"ok": False, "error": "no such tool %r" % tool})
                return
        except Exception as e:
            self.reply(500, {"ok": False, "error": "internal: %s" % e})
            return
        self.reply(200, {"ok": ok, "result": res} if ok
                   else {"ok": False, "error": res})


def main():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    lock_path = DB + ".lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("another chat-tunnel owns %s (lock %s); not starting"
                 % (DB, lock_path))
    init_db()
    threading.Thread(target=tail_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("chat-tunnel: 127.0.0.1:%d tailing %s -> %s (window=%ss"
          " retention=%ss)" % (PORT, LOG, DB, WINDOW, RETENTION))
    srv.serve_forever()


if __name__ == "__main__":
    main()
