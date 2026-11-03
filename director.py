#!/usr/bin/env python3
"""director - LLM <-> altbot command interface for Chazbot (I_WAS_CMD / Leo shell).

Transport: writes chat lines to the Leo shell FIFO (/tmp/leo.in) as /w <bot> <cmd>.
Eyes: read-only SQL census + human tails /tmp/leo.log. LLM: local Ollama (off the
world thread). Stdlib only.

Modes:
  python3 director.py chat            interactive LLM REPL; orders dry-run first, type 'go'
  python3 director.py run orders.json [--go]   execute a JSON marching-orders file
  python3 director.py census          print live online-bot roster (read-only)

orders.json schema: {"orders": [{"bot": "<name>", "cmd": "<whisper command>"}], "pace_sec": 2}
"""
import argparse, json, os, re, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("DIRECTOR_MODEL", "gemma4:12b")
FIFO = os.environ.get("LEO_FIFO", "/tmp/leo.in")
AUDIT = os.path.join(HERE, "audit.log")
ACCOUNT_LIKE = os.environ.get("DIRECTOR_ACCOUNT_LIKE", "i_was_here%")
MYCNF = os.path.expanduser(os.environ.get("DIRECTOR_MYCNF", "~/.my.cnf"))

# whisper-bot command prefixes the LLM is allowed to emit (no leading /w in cmd)
WHITELIST = ("nc", "co", "cs", "gb", "sendmail", "summon", "talents", "equip",
             "repair", "buy", "sell", "mail", "lfg", "join", "roll", "stats",
             "rpg", "follow", "stay", "attack", "tell", "wait", "release",
             "repop", "position", "go", "invite", "give leader", "buff",
             "reset botAI", "quests", "drop", "share")

SYSTEM = """You are the director of altbots on a private WoW server (master: Leo).
The human gives marching orders as natural language, JSON, or pasted lists.
Reply with ONE JSON object and nothing else:
{"reply": "<short human-readable summary>", "orders": [{"bot": "<bot name>", "cmd": "<command>"}]}
Rules:
- Only name bots the CLI confirms online (it validates against a live SQL census and skips others).
- cmd is a whisper-bot command WITHOUT any leading slash, e.g. "nc +new rpg,+grind,-follow".
- Allowed command prefixes: nc, co, cs, gb, sendmail, summon, talents, equip, repair, buy, sell, mail, lfg, join, roll, stats, rpg, follow, stay, attack, tell, wait, release, repop, position, go, invite, give leader, buff, reset botAI, quests, drop, share.
- Never invent bot names, item links, or gold amounts. sendmail reaches the master only; prefer gb (guild bank) for items. If unsure, explain in reply and leave orders empty.
- Bots grind solo with rpg+grind strategies already set."""


def census():
    q = ("SELECT c.name, a.username, c.level, c.map "
         "FROM acore_characters.characters c JOIN acore_auth.account a ON a.id=c.account "
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
        name, acct, level, mapid = line.split("\t")
        bots[name] = {"account": acct, "level": int(level), "map": int(mapid)}
    return bots


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


def run_file(path, go, pace):
    with open(path) as f:
        data = json.load(f)
    orders = data["orders"] if isinstance(data, dict) else data
    execute(orders, census(), pace, dry=not go)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["chat", "run", "census"])
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
    else:
        repl()
