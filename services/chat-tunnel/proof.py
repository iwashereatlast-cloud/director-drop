#!/usr/bin/env python3
"""chat-tunnel proof harness - replay/fixture proof, no live worldserver.

Starts the real service against a fixture log + FIFO in a temp dir and drives
it over its local-only JSON interface. Scenarios:

  1 basic        - one command, one reply: get_reply binds the right cid
  2 interleaved  - two commands to two bots, replies out of order: each reply
                   binds to ITS bot's command, not "the next line seen"
  3 garbled      - every line of fixtures/replay-corrupt.log.txt is ignored;
                   the following good reply still binds to the pending cid
  4 late         - reply lands after the window: command times out, the late
                   reply is stored unmatched (no false binding)
  5 ordered      - two commands to the SAME bot: each reply binds to its own
                   command, oldest-pending first
  6 retention    - old events are purged on the retention sweep (DB bounded)
  7 cursor       - get_events_since: ascending, gapless, empty at the head
  8 longpoll     - wait_for_events: wakes on a new line, sits out its timeout

Run:  python3 services/chat-tunnel/proof.py   (exit 0 = all scenarios PASS)
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.path.join(HERE, "chat_tunnel.py")
FIXTURE = os.path.join(HERE, "fixtures", "replay-corrupt.log.txt")

WINDOW = 3.0          # seconds; small so the late-reply scenario runs fast
RETENTION = 30.0      # main run; the retention scenario spins up its own
                      # short-retention instance (a tiny retention here would
                      # purge events mid-scenario and fake a "late" failure)
PURGE_EVERY = 1.0


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Tunnel:
    def __init__(self, tmp, retention=RETENTION, pace=0.05):
        self.log = os.path.join(tmp, "leo.log")
        self.fifo = os.path.join(tmp, "leo.in")
        self.db = os.path.join(tmp, "tunnel.db")
        self.logdb = os.path.join(tmp, "logdb.json")
        open(self.logdb, "w").write("[]")
        os.mkfifo(self.fifo)
        open(self.log, "w").close()
        self.port = free_port()
        env = dict(os.environ,
                   CHAT_TUNNEL_LOG=self.log,
                   CHAT_TUNNEL_FIFO=self.fifo,
                   CHAT_TUNNEL_DB=self.db,
                   CHAT_TUNNEL_PORT=str(self.port),
                   CHAT_TUNNEL_WINDOW=str(WINDOW),
                   CHAT_TUNNEL_RETENTION=str(retention),
                   CHAT_TUNNEL_PURGE_EVERY=str(PURGE_EVERY),
                   CHAT_TUNNEL_PACE=str(pace),
                   CHAT_TUNNEL_LOGDB_FIXTURE=self.logdb,
                   CHAT_TUNNEL_ALLOW="Ayla,Locke,Gau,Mog")
        self.proc = subprocess.Popen([sys.executable, SERVICE], env=env,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
        self._drain = threading.Thread(target=self._fifo_reader, daemon=True)
        self._drain.start()
        self._wait_health()

    def _fifo_reader(self):
        fd = os.open(self.fifo, os.O_RDONLY)
        while True:
            if not os.read(fd, 4096):
                time.sleep(0.1)

    def _wait_health(self, timeout=10):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                self.call("get_recent_events", {"limit": 1})
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("service did not come up: %s"
                           % self.proc.stdout.read())

    def call(self, tool, args=None):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/" % self.port,
            data=json.dumps({"tool": tool, "args": args or {}}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())

    def set_logdb(self, rows):
        with open(self.logdb, "w") as f:
            json.dump(rows, f)

    def send(self, bot, cmd):
        r = self.call("send_command", {"bot": bot, "cmd": cmd})
        assert r["ok"], r
        return r["result"]["correlation_id"]

    def append_log(self, line):
        with open(self.log, "a") as f:
            f.write(line + "\n")

    def reply(self, bot, text):
        # A14-confirmed live shape: loguru envelope around the display line
        self.append_log(
            "2026-11-09 19:26:21.153 | MESSAGES |"
            " headless.world.chat.chat:display:124 - [%s] [whisper]: %s"
            % (bot, text))

    def sync(self, text, timeout=5):
        """Wait until the tailer has ingested a reply line with this text.
        Scenarios MUST sync before ending, or a not-yet-ingested line leaks
        into the next scenario and can legitimately steal its match (the
        service matches sender+window; it cannot know the line is 'old')."""
        self.wait_for(
            lambda: any(e["command"] == text for e in
                       self.call("get_recent_events", {"limit": 50})
                       ["result"]["events"]),
            timeout, "ingestion of %r" % text)

    def get_reply(self, cid):
        r = self.call("get_reply", {"correlation_id": cid})
        assert r["ok"], r
        return r["result"]

    def wait_for(self, fn, timeout, what):
        t0 = time.time()
        while time.time() - t0 < timeout:
            v = fn()
            if v:
                return v
            time.sleep(0.1)
        raise AssertionError("timeout waiting for %s" % what)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


RESULTS = []


def scenario(name):
    def deco(fn):
        def run(t):
            fn(t)
            RESULTS.append((name, True, ""))
        return run
    return deco


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def s_basic(t):
    cid = t.send("Ayla", "nc ?")
    t.reply("Ayla", "Strategies: -new rpg,+grind")
    r = t.wait_for(lambda: t.get_reply(cid)["reply"], 5, "reply to bind")
    check(r["matched_correlation_id"] == cid, "reply bound to wrong command")
    check(r["sender"] == "Ayla" and r["command"].startswith("Strategies:"),
          "reply content wrong: %r" % r)
    check(t.get_reply(cid)["sent"]["status"] == "replied", "sent not replied")
    # a later unrelated line must NOT rebind anything
    t.reply("Ayla", "late chatter")
    t.sync("late chatter")
    check(t.get_reply(cid)["reply"]["id"] == r["id"],
          "later line stole the binding")


def s_interleaved(t):
    cid_ayla = t.send("Ayla", "nc ?")
    cid_locke = t.send("Locke", "co ?")
    t.reply("Locke", "Combat: -flee" )        # out of order
    t.reply("Ayla", "Strategies: -new rpg")
    r1 = t.wait_for(lambda: t.get_reply(cid_locke)["reply"], 5, "locke reply")
    r2 = t.wait_for(lambda: t.get_reply(cid_ayla)["reply"], 5, "ayla reply")
    check(r1["matched_correlation_id"] == cid_locke,
          "Locke reply bound to wrong command")
    check(r2["matched_correlation_id"] == cid_ayla,
          "Ayla reply bound to wrong command")
    check(r1["id"] < r2["id"], "reply order not preserved")


def s_garbled(t):
    cid = t.send("Gau", "nc ?")
    with open(FIXTURE) as f:
        for line in f.read().splitlines():
            if line.strip() and not line.startswith("#"):
                t.append_log(line)
    time.sleep(0.4)
    check(t.get_reply(cid)["reply"] is None,
          "a garbled line consumed the pending match")
    t.reply("Gau", "Strategies: -follow")
    r = t.wait_for(lambda: t.get_reply(cid)["reply"], 5, "reply after garble")
    check(r["matched_correlation_id"] == cid,
          "reply after garble bound to wrong command")


def s_late(t):
    cid = t.send("Ayla", "nc ?")
    t.reply("Locke", "someone else replies first")   # different bot: no match
    t.sync("someone else replies first")
    time.sleep(WINDOW + 1.0)                          # window lapses
    t.wait_for(lambda: t.get_reply(cid)["sent"]["status"] == "timeout",
               WINDOW + 5, "timeout state")
    t.reply("Ayla", "very late reply")                # now too late to bind
    t.sync("very late reply")
    got = t.get_reply(cid)
    check(got["reply"] is None, "late reply falsely bound to a timed-out command")
    ev = t.call("get_recent_events", {"limit": 50, "direction": "reply"})
    late = [e for e in ev["result"]["events"] if e["command"] == "very late reply"]
    check(late and late[0]["matched_correlation_id"] is None,
          "late reply not stored as unmatched")
    check(late[0]["correlation_id"] != cid, "late reply stole the command's cid")


def s_ordered(t):
    cid1 = t.send("Gau", "who")
    cid2 = t.send("Gau", "wts")
    t.reply("Gau", "answer to who")
    t.append_log("2026-11-09 19:26:21.153 | MESSAGES |"
                 " headless.world.chat.chat:display:124 - [Mog] [party]: loot roll")
    t.append_log("2026-11-09 19:26:21.153 | MESSAGES |"
                 " headless.world.chat.chat:display:124 - [Mog] [1. General - Stormwind City]: anyone selling?")
    t.reply("Gau", "answer to wts")
    t.sync("answer to wts")
    r1 = t.wait_for(lambda: t.get_reply(cid1)["reply"], 5, "first reply")
    r2 = t.wait_for(lambda: t.get_reply(cid2)["reply"], 5, "second reply")
    check(r1["matched_correlation_id"] == cid1, "reply 1 bound to wrong command")
    check(r2["matched_correlation_id"] == cid2, "reply 2 bound to wrong command")
    check(r1["command"] == "answer to who" and r2["command"] == "answer to wts",
          "replies bound out of order (oldest-pending-first violated)")
    ev = t.call("get_recent_events", {"limit": 10})["result"]["events"]
    party = [e for e in ev if e.get("channel") == "party"]
    check(party != [], "party chat is recorded with its channel")
    gen = [e for e in ev if e.get("channel") == "1. General - Stormwind City"]
    check(gen != [], "numbered custom channel recorded (label kept verbatim)")
    check(all(e["matched_correlation_id"] is None for e in ev
              if e.get("channel") in ("party", "1. General - Stormwind City")),
          "non-whisper channels never participate in matching")


def s_retention(t):
    # a dedicated short-retention instance: the purge is folded into the
    # write path (no timer thread), so WRITES trigger it - old events must
    # vanish once they age out and the next insert lands
    import tempfile
    tmp = tempfile.mkdtemp(prefix="chat-tunnel-proof-ret-")
    r = Tunnel(tmp, retention=2.0)
    try:
        cid = r.send("Ayla", "nc ?")
        r.reply("Ayla", "to be purged")
        r.wait_for(lambda: r.get_reply(cid)["reply"], 5, "reply stored")
        time.sleep(2.0 + 0.5)          # the events age past retention
        filler = r.send("Ayla", "who")  # a write triggers the inline purge
        ev = r.call("get_recent_events", {"limit": 100})["result"]["events"]
        olds = [e for e in ev if e["command"] in ("nc ?", "to be purged")]
        check(olds == [], "aged events survived the inline retention purge")
        news = [e for e in ev if e["command"] == "who"]
        check(news != [], "the triggering write was itself purged")
    finally:
        r.stop()


def s_cursor(t):
    ok0 = t.call("get_events_since", {"after_id": 0})["result"]["last_id"]
    cid = t.send("Ayla", "nc ?")
    t.reply("Ayla", "Strategies: -follow")
    t.wait_for(lambda: t.get_reply(cid)["reply"], 5, "reply stored")
    r = t.call("get_events_since", {"after_id": ok0})["result"]
    ids = [e["id"] for e in r["events"]]
    check(len(ids) == 2, "cursor returned %d events, want 2" % len(ids))
    check(ids == sorted(ids), "cursor events not ascending")
    check(r["events"][0]["direction"] == "sent"
          and r["events"][1]["direction"] == "reply",
          "cursor order wrong: %r" % r["events"])
    check(r["last_id"] == ids[-1], "last_id cursor wrong")
    # an empty cursor read must return zero events, not a replay
    empty = t.call("get_events_since", {"after_id": r["last_id"]})["result"]
    check(empty["events"] == [], "after last_id the cursor must be empty")


def s_longpoll(t):
    import threading
    last = t.call("get_events_since", {"after_id": 0})["result"]["last_id"]
    # a reply lands 0.6s into the poll: the call must return promptly with it
    timer = threading.Timer(0.6, lambda: t.reply("Ayla", "live feed line"))
    timer.start()
    t0 = time.time()
    r = t.call("wait_for_events", {"after_id": last, "timeout": 5})["result"]
    took = time.time() - t0
    timer.join()
    check(len(r["events"]) == 1
          and r["events"][0]["command"] == "live feed line",
          "long-poll returned wrong batch: %r" % r["events"])
    check(took < 3.0, "long-poll woke late (%.1fs)" % took)
    # nothing lands: the poll must sit out its timeout and return empty
    last = r["last_id"]
    t0 = time.time()
    r = t.call("wait_for_events", {"after_id": last, "timeout": 1.0})["result"]
    took = time.time() - t0
    check(r["events"] == [], "idle long-poll must return empty")
    check(0.8 <= took <= 3.0, "idle long-poll returned in %.1fs, want ~1s" % took)


def s_describe(t):
    g = t.call("describe", {})["result"]
    check(g["send"]["whisper"]["line"] == "/w <bot> <command>",
          "grammar: whisper line shape")
    check(".playerbots bot add Gau" in g["send"]["master_command"]["example"],
          "grammar: master command example")
    check("verify" in g["tools"] and "describe" in g["tools"],
          "grammar: tool list includes the new tools")
    check("whisper" in g["listen_channels"] and "party" in g["listen_channels"],
          "grammar: listen channels include whisper + party")


def s_verify(t):
    # delivered: the server logged exactly what we sent and what we parsed
    cid = t.send("Ayla", "nc ?")
    t.reply("Ayla", "Strategies: -new rpg")
    ev = t.wait_for(lambda: t.get_reply(cid)["reply"], 5, "reply stored")
    t.set_logdb([[int(ev["ts"]), "Player Ayla whisper Leo: Strategies: -new rpg"]])
    sent_ev = t.get_reply(cid)["sent"]
    t.set_logdb([[int(sent_ev["ts"]), "Player Leo whisper Ayla: nc ?"],
                 [int(ev["ts"]), "Player Ayla whisper Leo: Strategies: -new rpg"]])
    v = t.call("verify", {"correlation_id": cid})["result"]
    check(v["sent"]["verdict"] == "delivered", "verify: sent delivered")
    check(v["reply"]["verdict"] == "delivered", "verify: reply delivered")
    # garbled: the server saw DIFFERENT text from the same speakers
    t.set_logdb([[int(sent_ev["ts"]), "Player Leo whisper Ayla: nc ?? GARBLED"],
                 [int(ev["ts"]), "Player Ayla whisper Leo: Strategies: -new rpg"]])
    v = t.call("verify", {"correlation_id": cid})["result"]
    check(v["sent"]["verdict"] == "garbled",
          "verify: garbled send is CAUGHT, not assumed fine")
    check(v["sent"].get("expected") == "Player Leo whisper Ayla: nc ?",
          "verify: garble report carries the expected line")
    # regression (live false positive): another whisper between the SAME
    # pair sits in the window - the exact match must win, not the neighbor
    t.set_logdb([[int(sent_ev["ts"]), "Player Leo whisper Ayla: nc ?? GARBLED"],
                 [int(sent_ev["ts"]), "Player Leo whisper Ayla: nc ?"],
                 [int(ev["ts"]), "Player Ayla whisper Leo: Strategies: -new rpg"]])
    v = t.call("verify", {"correlation_id": cid})["result"]
    check(v["sent"]["verdict"] == "delivered",
          "verify: exact match beats a same-speaker neighbor (no false garble)")
    # not_found: nothing logged in the window
    t.set_logdb([])
    v = t.call("verify", {"correlation_id": cid})["result"]
    check(v["sent"]["verdict"] == "not_found", "verify: silent log = not_found")
    check(v["reply"]["verdict"] == "no_event" or v["reply"]["verdict"] == "not_found",
          "verify: reply verdict on empty log")


def s_allowlist(t):
    # send to a non-roster toon is REFUSED
    r = t.call("send_command", {"bot": "Zuljin", "cmd": "nc ?"})
    check(r["ok"] is False and "allowlist" in r["error"],
          "send to a non-roster toon is refused with the allowlist reason")
    # a reply from a non-roster sender is dropped, never matched
    s0 = t.call("stats", {})["result"]["lines_filtered"]
    cid = t.send("Ayla", "nc ?")
    t.append_log("2026-11-09 19:26:21.153 | MESSAGES |"
                 " headless.world.chat.chat:display:124 - [Rando] [whisper]: noise line")
    t.reply("Ayla", "Strategies: -follow")
    t.wait_for(lambda: t.get_reply(cid)["reply"], 5, "roster reply stored")
    s1 = t.call("stats", {})["result"]["lines_filtered"]
    check(s1 > s0, "non-roster sender counted as filtered")
    ev = t.call("get_recent_events", {"limit": 50})["result"]["events"]
    check(all(e["sender"] != "Rando" for e in ev),
          "non-roster sender never lands in events")
    # roster bots still work end to end
    r = t.get_reply(cid)
    check(r["sent"]["status"] == "replied" and r["reply"]["sender"] == "Ayla",
          "roster bot unaffected by the allowlist")


def s_inspect(t):
    # non-roster toon refused; bad query refused
    r = t.call("inspect", {"bot": "Zuljin", "query": "position"})
    check(r["ok"] is False and "allowlist" in r["error"],
          "inspect: non-roster toon refused")
    r = t.call("inspect", {"bot": "Ayla", "query": "teleport"})
    check(r["ok"] is False and "bad query" in r["error"],
          "inspect: bad query refused with the vocabulary")
    # happy path: inspect command + [System] reply correlates
    cid = t.call("inspect", {"bot": "Ayla", "query": "position"})["result"]["correlation_id"]
    t.append_log("2026-11-09 19:26:21.153 | MESSAGES |"
                 " headless.world.chat.chat:display:124 - [System]: Ayla: -8721.05 -2327.83 161.476 0 -0.455113 |Redridge Mountains|")
    r = t.wait_for(lambda: t.get_reply(cid)["reply"], 5, "inspect reply")
    check(r["matched_correlation_id"] == cid and r["channel"] == "system",
          "inspect: [System] reply bound to its command")
    check(r["command"].startswith("-8721.05"), "inspect: reply text carried")
    check(t.get_reply(cid)["sent"]["status"] == "replied", "inspect: sent replied")
    # cross-channel isolation: a [System] line must NOT steal a whisper match
    cid_w = t.send("Ayla", "nc ?")
    t.append_log("2026-11-09 19:26:21.153 | MESSAGES |"
                 " headless.world.chat.chat:display:124 - [System]: Ayla: ambient system line")
    t.sync("ambient system line")   # ingested BEFORE the next inspect exists
    check(t.get_reply(cid_w)["reply"] is None,
          "inspect: [System] line did not steal the whisper match")
    # ...and a whisper reply must not steal an inspect match
    cid_i = t.call("inspect", {"bot": "Ayla", "query": "hp"})["result"]["correlation_id"]
    t.reply("Ayla", "Strategies: -follow")   # answers the pending WHISPER
    r = t.wait_for(lambda: t.get_reply(cid_w)["reply"], 5, "whisper reply")
    check(r["matched_correlation_id"] == cid_w
          and t.get_reply(cid_i)["reply"] is None,
          "inspect: whisper reply did not steal the inspect match")
    t.append_log("2026-11-09 19:26:21.153 | MESSAGES |"
                 " headless.world.chat.chat:display:124 - [System]: Ayla: 4567/4567")
    r = t.wait_for(lambda: t.get_reply(cid_i)["reply"], 5, "inspect hp reply")
    check(r["matched_correlation_id"] == cid_i, "inspect: hp bound to its command")
    # offline fallback: a timed-out inspect carries the db-search note
    cid_t = t.call("inspect", {"bot": "Locke", "query": "state"})["result"]["correlation_id"]
    t.wait_for(lambda: t.get_reply(cid_t)["sent"]["status"] == "timeout",
               WINDOW + 5, "inspect timeout")
    check("db-search" in t.get_reply(cid_t).get("note", "").lower(),
          "inspect: timed-out read points to db-search as the offline truth")


SCENARIOS = [
    ("basic - reply binds to its own correlation_id", s_basic),
    ("interleaved - per-bot binding, out-of-order replies", s_interleaved),
    ("garbled - junk lines never consume a match", s_garbled),
    ("late - past-window reply never falsely binds", s_late),
    ("ordered - same-bot replies bind oldest-pending-first", s_ordered),
    ("retention - purge keeps the DB bounded", s_retention),
    ("cursor - get_events_since reads ascending from a cursor", s_cursor),
    ("longpoll - wait_for_events returns new lines promptly", s_longpoll),
    ("describe - the grammar exposes send shapes and tools", s_describe),
    ("verify - delivery + garble check against the chat log", s_verify),
    ("allowlist - non-roster send refused, non-roster chatter dropped", s_allowlist),
    ("inspect - [System] replies correlate, channels never cross-steal", s_inspect),
]


def main():
    tmp = tempfile.mkdtemp(prefix="chat-tunnel-proof-")
    t = Tunnel(tmp)
    failed = 0
    try:
        for name, fn in SCENARIOS:
            try:
                fn(t)
                print("PASS  %s" % name)
            except Exception as e:
                failed += 1
                print("FAIL  %s: %s" % (name, e))
    finally:
        t.stop()
    print("%d/%d scenarios passed" % (len(SCENARIOS) - failed, len(SCENARIOS)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
