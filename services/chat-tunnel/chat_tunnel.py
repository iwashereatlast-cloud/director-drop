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
             get_reply(correlation_id)
  GET  /health

Run:  python3 services/chat-tunnel/chat_tunnel.py
Env:  CHAT_TUNNEL_LOG /tmp/leo.log     log to tail
      CHAT_TUNNEL_FIFO /tmp/leo.in     FIFO commands are written to
      CHAT_TUNNEL_PORT 8765            local-only HTTP port
      CHAT_TUNNEL_WINDOW 30           reply window, seconds
      CHAT_TUNNEL_RETENTION 86400     events older than this are purged
      CHAT_TUNNEL_PURGE_EVERY 60      purge/timing sweep cadence, seconds
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

LOG = os.environ.get("CHAT_TUNNEL_LOG", "/tmp/leo.log")
FIFO = os.environ.get("CHAT_TUNNEL_FIFO", "/tmp/leo.in")
DB = os.environ.get("CHAT_TUNNEL_DB", os.path.join(HERE, "tunnel.db"))
PORT = int(os.environ.get("CHAT_TUNNEL_PORT", "8765"))
WINDOW = float(os.environ.get("CHAT_TUNNEL_WINDOW", "30"))
RETENTION = float(os.environ.get("CHAT_TUNNEL_RETENTION", str(24 * 3600)))
PURGE_EVERY = float(os.environ.get("CHAT_TUNNEL_PURGE_EVERY", "60"))
MAX_ROWS = int(os.environ.get("CHAT_TUNNEL_MAX_ROWS", "100000"))
MASTER = os.environ.get("CHAT_TUNNEL_MASTER", "Leo")

# A14 (docs/assumptions.md): the headless client logs chat as
# "[Name] [whisper]: text" - same shape director.py's LINE_RE parses.
LINE_RE = re.compile(r"\[([A-Za-z]+)\] \[(whisper|guild)\]: (.*)")

# master-issued server commands go out as '.playerbots ...' lines instead of
# a /w (same split director.py's order_line() makes, minimal subset here)
MASTER_CMDS = ("bot add", "bot remove", "bot" )


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
    with db() as conn:
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
    try:
        with open(FIFO, "w") as f:
            f.write(line + "\n")
    except OSError as e:
        return False, "FIFO write to %s failed (no reader? leo shell down?): %s" % (FIFO, e)
    with db() as conn:
        conn.execute(
            "INSERT INTO events (ts, direction, sender, target, command,"
            " correlation_id, raw_line, channel, status)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (now(), "sent", MASTER, bot, cmd, cid, line, None, "pending"))
    return True, {"correlation_id": cid, "line": line, "bot": bot, "cmd": cmd}


def sweep_timeouts(conn, t):
    conn.execute(
        "UPDATE events SET status='timeout' WHERE direction='sent'"
        " AND status='pending' AND ts < ?", (t - WINDOW,))


def match_reply(sender, channel, text, raw_line, t):
    """Bind a reply to the exact command that triggered it: same bot (sender)
    + inside the reply window, OLDEST pending command first. A garbled or
    out-of-window line never consumes a pending command."""
    with db() as conn:
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
    return matched


def handle_reply_line(raw_line, t):
    m = LINE_RE.search(raw_line)
    if not m:
        return None                      # garbled/foreign line: ignored, never matched
    sender, channel, text = m.group(1), m.group(2), m.group(3).strip()
    if not text:
        return None
    return match_reply(sender, channel, text, raw_line.rstrip("\n"), t)


def get_reply(correlation_id):
    cid = str(correlation_id or "").strip()
    with db() as conn:
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
    with db() as conn:
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


def ev_json(r):
    return {
        "id": r["id"], "ts": r["ts"], "direction": r["direction"],
        "sender": r["sender"], "target": r["target"], "command": r["command"],
        "correlation_id": r["correlation_id"], "raw_line": r["raw_line"],
        "channel": r["channel"], "status": r["status"],
        "matched_correlation_id": r["matched_correlation_id"],
    }


def purge_loop():
    while True:
        time.sleep(PURGE_EVERY)
        t = now()
        try:
            with db() as conn:
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
        if self.path == "/health":
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
    threading.Thread(target=purge_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("chat-tunnel: 127.0.0.1:%d tailing %s -> %s (window=%ss"
          " retention=%ss)" % (PORT, LOG, DB, WINDOW, RETENTION))
    srv.serve_forever()


if __name__ == "__main__":
    main()
