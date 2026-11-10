#!/usr/bin/env python3
"""chat-tunnel liveprobe - live acceptance battery, self-contained.

Talks ONLY to a running chat-tunnel's local-only JSON interface (no botcmd,
no daemon, no old-stack tooling). Sends a fixed battery of probe commands to
one or more bots, measures real reply latencies, and tabulates:

  bot      - the bot probed
  cid      - correlation_id of the sent command
  status   - replied | timeout | pending (at deadline)
  reply    - first 40 chars of the bound reply (or - )
  latency  - seconds between send and the bound reply landing
  verdict  - OK (reply sender == the bot probed) | WRONG-SENDER | SILENT

Exit 0 iff every probe is OK. Also prints the stats counters (parse truth) and
a window suggestion from the observed latencies, so CHAT_TUNNEL_WINDOW gets
set on evidence instead of a guess.

Usage (serverpc, leo online, chat-tunnel running):
  python3 services/chat-tunnel/liveprobe.py [--port 8765]
      [--bots Ayla,Locke] [--cmd "nc ?"] [--rounds 2] [--wait 35]
"""
import argparse
import json
import socket
import sys
import time
import urllib.request


def rtt_pinfo(sock_path, name):
    """One rtt-engine/1 request over its unix socket: is this bot online and
    reachable SERVER-side (command server), through a path that does not
    involve the FIFO whisper transport? None = rtt-engine not answerable."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(sock_path)
        s.sendall((json.dumps({"op": "pinfo", "name": name}) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        env = json.loads(data.decode())
        if env.get("ok"):
            return env["result"].get("online")
        return None
    except OSError:
        return None


def call(port, tool, args=None, timeout=30):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/" % port,
        data=json.dumps({"tool": tool, "args": args or {}}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bots", default="Ayla")
    ap.add_argument("--cmd", default="nc ?")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--pace", type=float, default=1.0,
                    help="seconds between sends")
    ap.add_argument("--wait", type=float, default=None,
                    help="per-probe wait for the reply (default: 2x service window)")
    ap.add_argument("--rtt-sock", default=None,
                    help="rtt-engine unix socket (e.g. /tmp/rtt-engine.sock): "
                         "pre-flight each bot with pinfo - a server-side "
                         "online check on a path that bypasses the FIFO")
    a = ap.parse_args()
    bots = [b.strip() for b in a.bots.split(",") if b.strip()]

    health = call(a.port, "stats")
    if not health["ok"]:
        print("service not reachable: %r" % health)
        return 2
    cfg = health["result"]
    wait = a.wait if a.wait is not None else max(30.0, 2 * 30.0)
    print("chat-tunnel liveprobe: %d round(s) of %r to %s (per-probe wait %.0fs)"
          % (a.rounds, a.cmd, ",".join(bots), wait))
    preflight = {}
    if a.rtt_sock:
        for bot in bots:
            online = rtt_pinfo(a.rtt_sock, bot)
            preflight[bot] = online
            print("  preflight %-10s server-side: %s"
                  % (bot, "online" if online else
                     ("OFFLINE" if online is False else "unanswerable")))
    print("uptime %.0fs  lines seen/parsed/noise/garbled: %d/%d/%d/%d  "
          "replies matched/unmatched: %d/%d"
          % (cfg["uptime_sec"], cfg["lines_seen"], cfg["lines_parsed"],
             cfg["lines_noise"], cfg["lines_garbled"],
             cfg["replies_matched"], cfg["replies_unmatched"]))
    print()

    probes = []                       # (bot, cid, sent_at)
    for rnd in range(a.rounds):
        for bot in bots:
            r = call(a.port, "send_command", {"bot": bot, "cmd": a.cmd})
            if not r["ok"]:
                print("SEND FAILED for %s: %s" % (bot, r["error"]))
                return 2
            probes.append((bot, r["result"]["correlation_id"], time.time()))
            time.sleep(a.pace)

    rows, bad = [], 0
    for bot, cid, sent_at in probes:
        deadline = sent_at + wait
        res = None
        while time.time() < deadline:
            r = call(a.port, "get_reply", {"correlation_id": cid})
            res = r["result"]
            if res["sent"]["status"] != "pending" or res["reply"]:
                break
            time.sleep(0.5)
        if not res:
            res = call(a.port, "get_reply", {"correlation_id": cid})["result"]
        status = res["sent"]["status"]
        reply = res["reply"]
        lat = (reply["ts"] - res["sent"]["ts"]) if reply else None
        sender = reply["sender"] if reply else None
        if status == "replied" and reply and sender.lower() == bot.lower():
            verdict = "OK"
        elif status == "replied":
            verdict = "WRONG-SENDER"   # bound, but not the bot we probed
        elif status == "timeout":
            # no in-window reply at all; if the rtt pre-flight saw the bot
            # online server-side, the bot is reachable but not answering -
            # a different layer than a bot that is simply offline
            verdict = "SILENT (server: online - not answering)" \
                if preflight.get(bot) else \
                "SILENT (server: offline or unanswerable)"
        else:
            verdict = "PENDING"         # still open at deadline (wait too short)
        if verdict != "OK":
            bad += 1
        # the server's own record: what we sent / what we parsed must match
        # what the world actually saw (both directions, no garbles)
        try:
            v = call(a.port, "verify", {"correlation_id": cid})["result"]
            dbv = "S:%s R:%s" % (v["sent"]["verdict"], v["reply"]["verdict"])
            if v["sent"]["verdict"] == "garbled" or v["reply"]["verdict"] == "garbled":
                bad += 1
        except Exception as e:
            dbv = "db:unavailable (%s)" % str(e)[:30]
        rows.append((bot, cid, status, reply, lat, verdict, dbv))

    print("%-10s %-13s %-8s %-42s %8s  %-24s %s" %
          ("bot", "cid", "status", "reply", "latency", "db-verify", "verdict"))
    for bot, cid, status, reply, lat, verdict, dbv in rows:
        txt = (reply["command"][:40] if reply else "-")
        print("%-10s %-13s %-8s %-42s %8s  %-24s %s" %
              (bot, cid, status, txt,
               "%.1fs" % lat if lat else "-", dbv, verdict))

    lat_ok = [r[4] for r in rows if r[4]]
    print()
    s = call(a.port, "stats")["result"]
    print("after probe: lines seen/parsed/noise/garbled: %d/%d/%d/%d  "
          "replies matched/unmatched: %d/%d  db rows: %d "
          "(pending %d / replied %d / timeout %d)"
          % (s["lines_seen"], s["lines_parsed"], s["lines_noise"],
             s["lines_garbled"],
             s["replies_matched"], s["replies_unmatched"], s["db_rows"],
             s["db_sent_pending"], s["db_sent_replied"], s["db_sent_timeout"]))
    if lat_ok:
        want = max(30.0, max(lat_ok) * 1.5)
        print("observed reply latency: max %.1fs  -  evidence-based window: "
              "%.0fs (set CHAT_TUNNEL_WINDOW accordingly)"
              % (max(lat_ok), want))
    print("parse truth: %s"
          % ("OK - no display line was garbled (noise lines are expected)"
             if s["lines_garbled"] == 0
             else "CHECK - %d garbled display line(s); compare against the raw log (A14)"
                  % s["lines_garbled"]))
    print("%d/%d probes OK" % (len(rows) - bad, len(rows)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
