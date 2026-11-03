#!/usr/bin/env python3
"""director - LLM <-> altbot command interface for Chazbot (I_WAS_CMD / Leo shell).

Transport: writes chat lines to the Leo shell FIFO (/tmp/leo.in) as /w <bot> <cmd>.
Eyes: read-only SQL census + human tails /tmp/leo.log. LLM: local Ollama (off the
world thread). Stdlib only.

Modes:
  python3 director.py chat            interactive terminal LLM REPL; dry-run first, 'go' sends
  python3 director.py daemon         in-game interface: whisper Leo (or say 'Leo' in guild) and
                                     the LLM answers as the general and directs bots (confirm via 'go')
  python3 director.py run orders.json [--go]   execute a JSON marching-orders file
  python3 director.py census         print live online-bot roster (read-only)

orders.json schema: {"orders": [{"bot": "<name>", "cmd": "<whisper command>"}], "pace_sec": 2}

Daemon auth: only toons on the master account (default i_was_here1st, all its toons)
get LLM treatment; override with DIRECTOR_MASTERS=Cecil,Golbez. Guild chat triggers
require the word 'leo' in the line. Orders always confirm via whisper: Leo replies
with the plan, whisper him 'go' or 'skip'.
"""
import argparse, json, os, re, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("DIRECTOR_MODEL", "gemma4:12b")
FIFO = os.environ.get("LEO_FIFO", "/tmp/leo.in")
LOG = os.environ.get("DIRECTOR_LOG", "/tmp/leo.log")
AUDIT = os.path.join(HERE, "audit.log")
ACCOUNT_LIKE = os.environ.get("DIRECTOR_ACCOUNT_LIKE", "i_was_here%")
MASTER_ACCOUNT = os.environ.get("DIRECTOR_MASTER_ACCOUNT", "i_was_here1st")
MYCNF = os.path.expanduser(os.environ.get("DIRECTOR_MYCNF", "~/.my.cnf"))

# whisper-bot command prefixes the LLM is allowed to emit (no leading /w in cmd)
WHITELIST = ("nc", "co", "cs", "gb", "sendmail", "summon", "talents", "equip",
             "repair", "buy", "sell", "mail", "lfg", "join", "roll", "stats",
             "rpg", "follow", "stay", "attack", "tell", "wait", "release",
             "repop", "position", "go", "invite", "give leader", "buff",
             "reset botAI", "quests", "drop", "share", "leave")

ORDER_RULES = """Reply with ONE JSON object and nothing else:
{"reply": "<short human-readable answer>", "orders": [{"bot": "<bot name>", "cmd": "<command>"}]}
Rules:
- Only name bots from the live roster (the CLI validates against a live SQL census and skips others).
- cmd is a whisper-bot command WITHOUT any leading slash, e.g. "nc +new rpg,+grind,-follow".
- Allowed command prefixes: nc, co, cs, gb, sendmail, summon, talents, equip, repair, buy, sell, mail, lfg, join, roll, stats, rpg, follow, stay, attack, tell, wait, release, repop, position, go, invite, give leader, buff, reset botAI, quests, drop, share, leave.
- Never invent bot names, item links, or gold amounts. If unsure, explain in reply and leave orders empty.
- Orders are NOT executed immediately: the human confirms. Keep replies short."""

SYSTEM = ("You are the director of altbots on a private WoW server (master: Leo).\n"
          "The human gives marching orders as natural language, JSON, or pasted lists.\n" + ORDER_RULES + "\n"
          "Bots grind solo with rpg+grind strategies already set.")

GENERAL = ("You are Leo, the general of a private WoW guild's alt-bot army. "
           "The guild master (C B) talks to you in-game through whispers or guild chat. "
           "Answer tersely and in character, like a general: short, confident, no fluff, at most 3 sentences. "
           "For status questions use the live roster below. "
           "When he gives marching orders for the bots, " + ORDER_RULES + "\n"
           "Bots grind solo with rpg+grind strategies already set. Live roster (name, account, level, map, guild):")


def census():
    q = ("SELECT c.name, a.username, c.level, c.map, gm.guildid "
         "FROM acore_characters.characters c JOIN acore_auth.account a ON a.id=c.account "
         "LEFT JOIN acore_characters.guild_member gm ON gm.guid=c.guid "
         "WHERE a.username LIKE '" + ACCOUNT_LIKE + "' AND c.online=1 "
         "ORDER BY a.username, c.name;")
    env = dict(os.environ)
    env.pop("MYSQL_PWD", None)  # let ~/.my.cnf supply credentials
    out = subprocess.run(["mysql", "--defaults-extra-file=" + MYCNF, "-N", "-B", "-e", q],
                         capture_output=True, text=True, env=env)
    if out.returncode != 0:
        raise RuntimeError("census failed: " + out.stderr.strip())
    bots = {}
    for line in out.stdout.strip().splitlines():
        name, acct, level, mapid, guildid = line.split("\t")
        bots[name] = {"account": acct, "level": int(level), "map": int(mapid),
                      "guildid": int(guildid) if guildid != "NULL" else 0}
    return bots


def masters():
    names = os.environ.get("DIRECTOR_MASTERS", "")
    if names.strip():
        return set(n.strip().lower() for n in names.split(",") if n.strip())
    q = ("SELECT c.name FROM acore_characters.characters c "
         "JOIN acore_auth.account a ON a.id=c.account "
         "WHERE a.username LIKE '" + MASTER_ACCOUNT + "%';")
    env = dict(os.environ)
    env.pop("MYSQL_PWD", None)
    out = subprocess.run(["mysql", "--defaults-extra-file=" + MYCNF, "-N", "-B", "-e", q],
                         capture_output=True, text=True, env=env)
    if out.returncode != 0:
        raise RuntimeError("masters failed: " + out.stderr.strip())
    return set(l.strip().lower() for l in out.stdout.strip().splitlines() if l.strip())


def roster_text(bots):
    return "; ".join("%s(acc %s, lvl %s, map %s)" % (n, b["account"].lower(), b["level"], b["map"])
                     for n, b in bots.items()) or "NONE online"


def llm(messages):
    body = json.dumps({"model": MODEL, "messages": messages, "stream": False,
                       "options": {"temperature": 0.2}}).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())["message"]["content"]


def parse_orders(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {"reply": text, "orders": []}
    try:
        obj = json.loads(m.group(0))
        obj.setdefault("reply", "")
        obj.setdefault("orders", [])
        return obj
    except json.JSONDecodeError:
        return {"reply": text, "orders": []}


def validate(order, bots):
    bot = str(order.get("bot", ""))
    cmd = str(order.get("cmd", ""))
    if bot not in bots:
        return "SKIP %s: not online (census)" % bot
    if not any(cmd == w or cmd.startswith(w + " ") for w in WHITELIST):
        return "SKIP %s: cmd not whitelisted: %r" % (bot, cmd)
    return None


def send_line(line, pace):
    subprocess.run(["timeout", "3", "bash", "-c",
                    'printf "%s\\n" "$1" > ' + FIFO, "_", line])
    with open(AUDIT, "a") as f:
        f.write(time.strftime("%F %T ") + "SENT " + line + "\n")
    if pace:
        time.sleep(pace)


def say(recipient, msg, guild=False):
    msg = (msg or "").strip()
    if not msg:
        return
    for i in range(0, len(msg), 200):
        chunk = msg[i:i + 200]
        send_line(("/g " if guild else "/w %s " % recipient) + chunk, 0.5)


def execute(orders, bots, pace, dry):
    plan = []
    for o in orders:
        err = validate(o, bots)
        if err:
            print("  " + err)
            continue
        plan.append((str(o["bot"]), str(o["cmd"])))
    if not plan:
        print("nothing executable")
        return None
    for bot, cmd in plan:
        print("  -> /w %s %s" % (bot, cmd))
    if dry:
        print("(dry-run: nothing sent; type 'go' to execute, 'skip' to discard)")
        return plan
    for bot, cmd in plan:
        send_line("/w %s %s" % (bot, cmd), pace)
        print("  sent: /w %s %s" % (bot, cmd))
    return plan


def repl():
    bots = census()
    print("online bots: " + (", ".join("%s(lvl%s)" % (n, b["level"]) for n, b in bots.items()) or "NONE"))
    print("commands: <message to LLM> | census | go | skip | quit")
    messages = [{"role": "system", "content": SYSTEM}]
    pending = None
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line == "quit":
            break
        if line == "census":
            bots = census()
            print("online bots: " + (", ".join(bots) or "NONE"))
            continue
        if pending is not None and line == "go":
            execute(pending, census(), 2.0, dry=False)
            pending = None
            continue
        if pending is not None and line == "skip":
            print("discarded")
            pending = None
            continue
        messages.append({"role": "user", "content": line})
        try:
            raw = llm(messages)
        except Exception as e:
            print("llm error: %s" % e)
            messages.pop()
            continue
        messages.append({"role": "assistant", "content": raw})
        obj = parse_orders(raw)
        print(obj.get("reply") or raw)
        orders = obj.get("orders") or []
        if orders:
            pending = execute(orders, census(), 2.0, dry=True)


LINE_RE = re.compile(r"\[([A-Za-z]+)\] \[(whisper|guild)\]: (.*)")


def daemon():
    authed = masters()
    print("daemon: Leo is listening. masters: %s" % ",".join(sorted(authed)))
    print("whisper Leo, or say his name in guild chat. Ctrl-C to stop.")
    pending = {}       # sender -> [(bot, cmd), ...]
    convs = {}         # sender -> [messages]
    proc = subprocess.Popen(["tail", "-n", "0", "-F", LOG],
                            stdout=subprocess.PIPE, text=True)
    try:
        for raw in proc.stdout:
            try:
                m = LINE_RE.search(raw)
                if not m:
                    continue
                sender, channel, text = m.group(1), m.group(2), m.group(3).strip()
                if sender in ("Leo", "System") or not text:
                    continue
                if sender.lower() not in authed:
                    continue
                if channel == "guild" and "leo" not in text.lower():
                    continue
                if len(text) > 300:
                    continue
                low = text.lower().strip()
                if low == "go" and sender in pending:
                    plan = pending.pop(sender)
                    execute(plan, census(), 2.0, dry=False)
                    say(sender, "executed: " + "; ".join("%s <- %s" % (b, c) for b, c in plan))
                    continue
                if low == "skip" and sender in pending:
                    pending.pop(sender)
                    say(sender, "plan discarded.")
                    continue
                conv = convs.setdefault(sender, [])
                sysmsg = {"role": "system",
                          "content": GENERAL + " " + roster_text(census())}
                conv[:] = ([sysmsg] + conv[-10:]) if conv and conv[0]["role"] == "system" else [sysmsg]
                conv.append({"role": "user", "content": text})
                try:
                    reply = llm(conv)
                except Exception as e:
                    conv.pop()
                    say(sender, "llm error: %s" % e, guild=(channel == "guild"))
                    continue
                conv.append({"role": "assistant", "content": reply})
                obj = parse_orders(reply)
                msg = (obj.get("reply") or reply).strip()
                orders = obj.get("orders") or []
                plan, skips = [], []
                if orders:
                    bots = census()
                    for o in orders:
                        err = validate(o, bots)
                        (skips.append(err) if err else plan.append((str(o["bot"]), str(o["cmd"]))))
                    if skips:
                        msg += " | " + "; ".join(skips)
                    if plan:
                        pending[sender] = plan
                        msg += (" | plan: " + "; ".join("/w %s %s" % (b, c) for b, c in plan)
                                + " - whisper me 'go' to execute, 'skip' to drop.")
                if channel == "guild" and not plan:
                    say(None, msg, guild=True)
                else:
                    say(sender, msg)
            except Exception as e:
                print("daemon line error: %s" % e)
    finally:
        proc.terminate()


def run_file(path, go, pace):
    with open(path) as f:
        data = json.load(f)
    orders = data["orders"] if isinstance(data, dict) else data
    execute(orders, census(), pace, dry=not go)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["chat", "run", "census", "daemon"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--go", action="store_true", help="actually send (default dry-run)")
    ap.add_argument("--pace", type=float, default=2.0)
    args = ap.parse_args()
    if args.mode == "census":
        for n, b in census().items():
            print(n, b)
    elif args.mode == "run":
        if not args.file:
            sys.exit("run needs a JSON file")
        run_file(args.file, args.go, args.pace)
    elif args.mode == "daemon":
        daemon()
    else:
        repl()
