#!/usr/bin/env python3
"""director - LLM <-> altbot command interface for Chazbot (I_WAS_CMD / Leo shell).

Transport: writes chat lines to the Leo shell FIFO (/tmp/leo.in) as /w <bot> <cmd>.
Eyes: read-only SQL census (auth_socket RO account via ~/.my.cnf-director) + human
tails /tmp/leo.log. LLM: local Ollama (off the world thread). Stdlib only.

Modes:
  python3 director.py chat            interactive terminal LLM REPL; dry-run first, 'go' sends
  python3 director.py daemon         in-game interface: whisper Leo (or say 'Leo' in guild) and
                                     the LLM answers as the general and directs bots (confirm via 'go')
  python3 director.py run orders.json [--go]   execute a JSON marching-orders file
  python3 director.py census         print live online-bot roster (read-only)

orders.json schema: {"orders": [{"bot": "<name>", "cmd": "<whisper command>"}], "pace_sec": 2}

Daemon auth: only toons on the master account (default i_was_here1st, all its toons)
get LLM treatment; override with DIRECTOR_MASTERS=Cecil,Golbez. Guild chat triggers
require the word 'leo' in the line. Validated orders execute immediately (set
DIRECTOR_CONFIRM=1 to restore the whisper-'go' confirm step). Run daemon with
python3 -u for live logs.
"""
import argparse, fcntl, json, os, re, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("DIRECTOR_MODEL", "gemma4:12b")
FIFO = os.environ.get("LEO_FIFO", "/tmp/leo.in")
LOG = os.environ.get("DIRECTOR_LOG", "/tmp/leo.log")
AUDIT = os.path.join(HERE, "audit.log")
ACCOUNT_LIKE = os.environ.get("DIRECTOR_ACCOUNT_LIKE", "i_was_here%")
MASTER_ACCOUNT = os.environ.get("DIRECTOR_MASTER_ACCOUNT", "i_was_here1st")
MYCNF = os.path.expanduser(os.environ.get("DIRECTOR_MYCNF", "~/.my.cnf-director"))
# ^ read-only MySQL identity (auth_socket user, SELECT-only grants). The
# write-capable ~/.my.cnf must NEVER be the default: this process is LLM-driven.
# ^ read via --defaults-file (NOT --defaults-extra-file): MySQL reads ~/.my.cnf
# AFTER the extra file, so the full-power acore identity in ~/.my.cnf would
# override the RO user. --defaults-file reads ONLY the RO file.
CONFIRM = os.environ.get("DIRECTOR_CONFIRM", "") not in ("", "0", "no", "off")
# ^ daemon default: validated orders execute IMMEDIATELY. DIRECTOR_CONFIRM=1
# restores the whisper-'go'/'skip' confirmation step for every plan.

# profession skill ids -> short codes for the roster (character_skills.skill)
SKILLS = {129: "firstaid", 164: "bs", 165: "lw", 171: "alchemy", 182: "herb",
          185: "cook", 186: "mine", 197: "tailor", 333: "enchant", 356: "fish",
          393: "skin", 755: "jewel", 762: "eng", 773: "scribe"}

# whisper-bot command prefixes the LLM is allowed to emit (no leading /w in cmd)
WHITELIST = ("nc", "co", "cs", "gb", "sendmail", "summon", "talents", "equip",
             "repair", "buy", "sell", "mail", "lfg", "join", "roll", "stats",
             "rpg", "follow", "stay", "attack", "tell", "wait", "release",
             "repop", "position", "go", "invite", "give leader", "buff",
             "reset botAI", "quests", "drop", "share", "leave")

# master-issued server commands (Leo runs them himself via the shell, not /w to a bot)
MASTER_CMDS = ("bot add", "bot remove")

ORDER_RULES = """Reply with ONE JSON object and nothing else:
{"reply": "<short human-readable answer>", "orders": [{"bot": "<bot name>", "cmd": "<command>"}]}
Rules:
- Only name bots from the live roster (the CLI validates against a live SQL census and skips others).
- cmd is a whisper-bot command WITHOUT any leading slash, e.g. "nc +new rpg,+grind,-follow".
- Allowed command prefixes: nc, co, cs, gb, sendmail, summon, talents, equip, repair, buy, sell, mail, lfg, join, roll, stats, rpg, follow, stay, attack, tell, wait, release, repop, position, go, invite, give leader, buff, reset botAI, quests, drop, share, leave.
- Presence control: use cmd "bot add" to log an OFFLINE unit in, "bot remove" to log an ONLINE unit out. These run as server commands from Leo himself. If the roster shows a unit OFFLINE, do not refuse - propose a "bot add" order for it.
- Task recipes (issue each cmd as its own order for the same bot; -follow is required or the bot clings to its master and never moves out):
  * Questing: cmd "nc +new rpg,-follow" then cmd "rpg status do quest". The bot picks a quest from its log; if it has none, it will say so.
  * Grinding mobs: cmd "nc +new rpg,+grind,-follow" (optionally then cmd "rpg status go grind").
  * Gathering mats: cmd "nc +new rpg,+gather,-follow" then cmd "rpg status do gather". ONLY for bots whose roster jobs include herb or mine - the server refuses others (no skinning in gather mode).
  * Stand down / return to idle: cmd "nc -new rpg,-grind,-gather" then cmd "rpg status idle".
- Never invent bot names, item links, or gold amounts. If unsure, explain in reply and leave orders empty.
- IMPORTANT: when the master asks you to DO something (bring a unit online, stand one down, assign a task), do not merely describe what you could do - you MUST put the order in the orders array of this same reply. A reply like "I can bring him online with a bot add order" WITHOUT the attached order is a failure.
- The system validates and executes the orders after your reply. Keep replies short."""

SYSTEM = ("You are the director of altbots on a private WoW server (master: Leo).\n"
          "The human gives marching orders as natural language, JSON, or pasted lists.\n" + ORDER_RULES + "\n"
          "Bots grind solo with rpg+grind strategies already set.")

GENERAL = ("You are Leo, the general of a private WoW guild's alt-bot army. "
           "The guild master (C B) talks to you in-game through whispers or guild chat. "
           "Answer tersely and in character, like a general: short, confident, no fluff, at most 3 sentences. "
           "A live roster of every toon on the bot accounts is provided in the last system message, each marked ONLINE or OFFLINE. "
           "Answer status questions from that roster. If a toon is listed, you know them: report their level or ONLINE/OFFLINE status plainly. "
           "If a toon is NOT listed at all, say you do not know that name. "
           "Only toons marked ONLINE can take direct orders, but OFFLINE units can be brought online with a 'bot add' order. "
           "The roster lists each unit's professions as jobs - match gathering orders to units with herb or mine. "
           "When reporting counts, count the roster entries carefully; if unsure, list names instead of numbers. "
           "When he gives marching orders for the bots, " + ORDER_RULES + "\n"
           "Bots grind solo with rpg+grind strategies already set.")


def census(online_only=True):
    q = ("SELECT c.name, a.username, c.level, c.map, c.online, gm.guildid, "
         "(SELECT GROUP_CONCAT(cs.skill) FROM acore_characters.character_skills cs "
         "WHERE cs.guid=c.guid) "
         "FROM acore_characters.characters c JOIN acore_auth.account a ON a.id=c.account "
         "LEFT JOIN acore_characters.guild_member gm ON gm.guid=c.guid "
         "WHERE a.username LIKE '" + ACCOUNT_LIKE + "'"
         + (" AND c.online=1" if online_only else "") +
         " ORDER BY a.username, c.name;")
    env = dict(os.environ)
    env.pop("MYSQL_PWD", None)  # let the defaults file supply credentials
    out = subprocess.run(["mysql", "--defaults-file=" + MYCNF, "-N", "-B", "-e", q],
                         capture_output=True, text=True, env=env)
    if out.returncode != 0:
        raise RuntimeError("census failed: " + out.stderr.strip())
    bots = {}
    for line in out.stdout.strip().splitlines():
        name, acct, level, mapid, online, guildid, skills = line.split("\t")
        jobs = ""
        if skills and skills != "NULL":
            jobs = "+".join(SKILLS.get(int(s), "s" + s)
                            for s in skills.split(",") if s.strip())
        bots[name] = {"account": acct, "level": int(level), "map": int(mapid),
                      "online": int(online) == 1, "jobs": jobs,
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
    out = subprocess.run(["mysql", "--defaults-file=" + MYCNF, "-N", "-B", "-e", q],
                         capture_output=True, text=True, env=env)
    if out.returncode != 0:
        raise RuntimeError("masters failed: " + out.stderr.strip())
    return set(l.strip().lower() for l in out.stdout.strip().splitlines() if l.strip())


def roster_text(bots):
    if not bots:
        return "NONE on the bot accounts"
    parts = []
    for n, b in bots.items():
        p = "%s(acc %s, lvl %s, map %s" % (n, b["account"].lower(), b["level"], b["map"])
        if b.get("jobs"):
            p += ", jobs " + b["jobs"]
        p += ") " + ("ONLINE" if b["online"] else "OFFLINE")
        parts.append(p)
    return "; ".join(parts)


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


def is_master_cmd(cmd):
    return any(cmd == mc or cmd.startswith(mc + " ") for mc in MASTER_CMDS)


def order_line(bot, cmd):
    """FIFO line for an order: master cmds go to the server, others are /w to the bot."""
    if is_master_cmd(cmd):
        return ".playerbots %s %s" % (cmd, bot)
    return "/w %s %s" % (bot, cmd)


def validate(order, bots):
    bot = str(order.get("bot", ""))
    cmd = str(order.get("cmd", ""))
    if is_master_cmd(cmd):
        all_bots = census(online_only=False)
        if bot not in all_bots:
            return "SKIP %s: not in census" % bot
        if cmd.startswith("bot add") and all_bots[bot]["online"]:
            return "SKIP %s: already online" % bot
        if cmd.startswith("bot remove") and not all_bots[bot]["online"]:
            return "SKIP %s: already offline" % bot
        return None
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
        print("  -> " + order_line(bot, cmd))
    if dry:
        print("(dry-run: nothing sent; type 'go' to execute, 'skip' to discard)")
        return plan
    for bot, cmd in plan:
        send_line(order_line(bot, cmd), pace)
        print("  sent: " + order_line(bot, cmd))
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

# confirm/discard words, used only in DIRECTOR_CONFIRM=1 mode (only matter while a plan is pending)
GO_WORDS = ("go", "do it", "confirm", "confirmed", "execute", "approved", "approve",
            "yes", "sure", "affirmative", "send it", "make it so")
SKIP_WORDS = ("skip", "no", "cancel", "abort", "discard", "negative", "belay that")


def daemon():
    # single-instance guard: a second daemon would double-answer every whisper
    lock_fd = os.open(os.path.join(HERE, "daemon.lock"), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("another director.py daemon is already running (daemon.lock); not starting")
    authed = masters()
    print("daemon: Leo is listening. masters: %s" % ",".join(sorted(authed)))
    print("daemon: orders execute immediately (DIRECTOR_CONFIRM=1 restores go/skip).")
    print("whisper Leo, or say his name in guild chat. Ctrl-C to stop.")
    pending = {}       # sender -> [order dicts]  (raw {"bot","cmd"} dicts so the
                       # confirm path can re-validate against a FRESH census)
    convs = {}         # sender -> [messages]
    proc = subprocess.Popen(["tail", "-n", "0", "-F", LOG],
                            stdout=subprocess.PIPE, bufsize=1, text=True)
    try:
        while True:
            raw = proc.stdout.readline()
            if not raw:
                break
            sender = None
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
                print("daemon: [%s] (%s): %s" % (sender, channel, text))
                low = text.lower().strip()
                if CONFIRM and low in GO_WORDS and sender in pending:
                    plan = pending.pop(sender)
                    sent = execute(plan, census(), 2.0, dry=False)
                    if sent:
                        say(sender, "executed: " + "; ".join("%s <- %s" % (b, c) for b, c in sent))
                    else:
                        say(sender, "plan no longer executable - discarded.")
                    continue
                if CONFIRM and low in SKIP_WORDS and sender in pending:
                    pending.pop(sender)
                    say(sender, "plan discarded.")
                    continue
                conv = convs.setdefault(sender, [])
                # rebuild each turn: persona first, last 10 history turns, then a
                # FRESH full roster (online+offline) as its own system message
                # right before the user text so the model cannot miss it
                history = [m2 for m2 in conv if m2["role"] != "system"][-10:]
                conv[:] = [{"role": "system", "content": GENERAL}]
                conv.extend(history)
                conv.append({"role": "user", "content": text})
                try:
                    roster_msg = ("Live roster (name, account, level, map, status): "
                                  + roster_text(census(online_only=False)))
                    conv.insert(len(conv) - 1, {"role": "system", "content": roster_msg})
                    reply = llm(conv)
                except Exception as e:
                    conv.pop()
                    say(sender, "llm error: %s" % e, guild=(channel == "guild"))
                    continue
                conv.append({"role": "assistant", "content": reply})
                obj = parse_orders(reply)
                msg = (obj.get("reply") or reply).strip()
                if not msg:
                    # gemma sometimes returns empty output; never suffer silence
                    print("daemon: empty LLM reply from %s for %s" % (MODEL, sender))
                    msg = "(the model returned an empty reply - raw: %r)" % reply
                orders = obj.get("orders") or []
                sent, skips = [], []
                if orders:
                    bots = census()
                    for o in orders:
                        err = validate(o, bots)
                        (skips.append(err) if err else sent.append(o))
                    if skips:
                        msg += " | " + "; ".join(skips)
                    if sent:
                        if CONFIRM:
                            pending[sender] = sent
                            msg += (" | plan: " + "; ".join(order_line(str(o["bot"]), str(o["cmd"])) for o in sent)
                                    + " - whisper me 'go' to execute, 'skip' to drop.")
                        else:
                            # auto-execute: validation is the safety net, no ritual
                            for o in sent:
                                send_line(order_line(str(o["bot"]), str(o["cmd"])), 2.0)
                            msg += " | executed: " + "; ".join(order_line(str(o["bot"]), str(o["cmd"])) for o in sent)
                print("daemon -> %s: %s" % (sender, msg))
                if channel == "guild" and not sent:
                    say(None, msg, guild=True)
                else:
                    say(sender, msg)
            except Exception as e:
                print("daemon line error: %s" % e)
                if sender:
                    # never drop a turn silently: the master must know we choked
                    try:
                        say(sender, "error: %s" % e)
                    except Exception:
                        pass
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
