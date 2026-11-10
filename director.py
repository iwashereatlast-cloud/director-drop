#!/usr/bin/env python3
"""director - LLM <-> altbot command interface for Chazbot (SHELLACCT / Leo shell).

Transport: writes chat lines to the Leo shell FIFO (/tmp/leo.in) as /w <bot> <cmd>.
Eyes: read-only SQL census (auth_socket RO account via ~/.my.cnf-director) + human
tails /tmp/leo.log. LLM: local Ollama (off the world thread). Stdlib only.

Modes:
  python3 director.py chat            interactive terminal LLM REPL; dry-run first, 'go' sends
  python3 director.py daemon         in-game interface: whisper Leo (or say 'Leo' in guild) and
                                     the LLM answers as the general and directs bots (confirm via 'go')
  python3 director.py run orders.json [--go]   execute a JSON marching-orders file
  python3 director.py census         print live online-bot roster (read-only)
  python3 director.py group [toon]    print the group/raid a toon is in (read-only; default Leo)

orders.json schema: {"orders": [{"bot": "<name>", "cmd": "<whisper command>"}], "pace_sec": 2}

Daemon auth: ONLY the toons in DIRECTOR_HUMANS (default the G1 five) on the
master account (default acct_master, rotatable via 'botcmd master') PLUS
members of the watched
guild (DIRECTOR_GUILD) on non-bot, non-shell accounts - bot-account toons and
Leo's shell account never get auth, because their automatic "Using X"
master-report whispers would be answered by the LLM and ping-pong with the bot
AI. Override with DIRECTOR_MASTERS=Cecil,Golbez. Guild chat triggers
require the word 'leo' in the line. Bot senders are additionally dropped by a
DB account check + master-report line filter (no console credentials:
'.playerbots inspect' does not exist in mod-playerbots @ e88cc40 and
'.playerbots bot' requires an active session, so neither works without a
live session). RP chatter must never reach the LLM or the two layers talk.
Validated orders are HELD for a whisper-'go' confirm by default (set
DIRECTOR_CONFIRM=0 to auto-execute immediately). Run daemon with
python3 -u for live logs.
"""
import argparse, fcntl, json, os, random, re, socket, subprocess, sys, threading, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("DIRECTOR_MODEL", "gemma4:12b")
FIFO = os.environ.get("LEO_FIFO", "/tmp/leo.in")
LOG = os.environ.get("DIRECTOR_LOG", "/tmp/leo.log")
AUDIT = os.path.join(HERE, "audit.log")
ACCOUNT_LIKE = os.environ.get("DIRECTOR_ACCOUNT_LIKE", "acct_%")
WATCH_GUILD = os.environ.get("DIRECTOR_GUILD", "Special FXc")
# ^ the ONE guild whose members Leo watches (census unions them with the bot
# accounts). Add an alt bot to THIS guild and Leo watches it automatically;
# every other guild on the server is ignored. Keep the name quote-free.
HANDPLAY = set(n.strip().lower() for n in os.environ.get(
    "DIRECTOR_HANDPLAY", "Cecil,Rydia,Shadow,Vivi,Aeris,Aerith").split(",") if n.strip())
# ^ hand-play-only toons (the manual leveling group): Leo may report their
# status but must NEVER order them or bring them online as bots - even though
# they sit on bot accounts and appear in the census. Incident 2026-11-04:
# RP chatter became "Aerith is offline. Bringing her online to perform
# the craft" and auto-execute ran `bot add Aerith` (a G1 hard-rule violation).
# BOTH Aeris and Aerith are listed until the DB spelling is verified:
#   SELECT name FROM acore_characters.characters WHERE name LIKE 'Aeri%';
HUMANS = set(n.strip().lower() for n in os.environ.get(
    "DIRECTOR_HUMANS", "Cecil,Rydia,Shadow,Vivi,Aeris,Aerith").split(",") if n.strip())
# ^ toons on the MASTER account that may TALK to Leo (sender auth). Default =
# the G1 five. 'botcmd master set <ACCOUNT>' rewrites this together with
# DIRECTOR_MASTER_ACCOUNT when you rotate which account you hand-play on, so a
# bot that shares the master account is still NOT authed (not in HUMANS).
MASTER_ACCOUNT = os.environ.get("DIRECTOR_MASTER_ACCOUNT", "acct_master")
SHELL_ACCOUNT = os.environ.get("DIRECTOR_SHELL_ACCOUNT", "shellacct")
LEO = os.environ.get("DIRECTOR_LEO", "Leo")
# ^ Leo's own toon (the headless shell character) - used for group/raid lookups
#   ("who is in MY raid / who leads it") and for the group-remove target rail.
# ^ Leo's own headless-shell account: never a master, never a valid sender -
# the shell must not talk to its own brain.
MYCNF = os.path.expanduser(os.environ.get("DIRECTOR_MYCNF", "~/.my.cnf-director"))
# ^ read-only MySQL identity (auth_socket user, SELECT-only grants). The
# write-capable ~/.my.cnf must NEVER be the default: this process is LLM-driven.
# ^ read via --defaults-file (NOT --defaults-extra-file): MySQL reads ~/.my.cnf
# AFTER the extra file, so the full-power acore identity in ~/.my.cnf would
# override the RO user. --defaults-file reads ONLY the RO file.
CMDSRV_HOST = os.environ.get("DIRECTOR_CMDSERVER_HOST", "127.0.0.1")
CMDSRV_PORT = int(os.environ.get("DIRECTOR_CMDSERVER_PORT", "8888"))
# ^ the stock mod-playerbots command server (Playerbots.CommandServerPort):
# read-only line protocol '<verb>,<guid>\n' (PlayerbotCommandServer.cpp),
# answers live position/state/HP for ONLINE bots only. NO auth, binds
# 0.0.0.0 - talk to 127.0.0.1 only and firewall the port from outside.
# Port 0 disables the live layer (location falls back to the DB last-logout
# position, marked as such). This is the same backend rtt-engine (service
# 3) wraps; the old console-credential path is end-of-life
# (docs/services-roadmap.md).
CONFIRM = os.environ.get("DIRECTOR_CONFIRM", "1") not in ("0", "no", "off")
# ^ daemon default: validated orders are HELD until the master whispers
# 'go'/'skip'. DIRECTOR_CONFIRM=0 auto-executes immediately (not recommended:
# validate() rails are then the only safety net).
DEBUG = os.environ.get("DIRECTOR_DEBUG", "") in ("1", "yes", "on")
# ^ DIRECTOR_DEBUG=1 dumps unmatched leo.log lines to daemon.log (log-format drift)
MYSQL_TIMEOUT = float(os.environ.get("DIRECTOR_MYSQL_TIMEOUT", "20"))
# ^ hard cap on every mysql subprocess call - the daemon loop is single-threaded,
#   so a hung DB connection would otherwise freeze ALL whisper handling silently
LLM_TIMEOUT = float(os.environ.get("DIRECTOR_LLM_TIMEOUT", "240"))
# ^ cap on the Ollama call; a cold/slow model or a queue of turns can otherwise
#   stall the single-threaded loop for minutes
_ACK_DEFAULT = ";".join([
    "one moment...", "stand by...", "aye...", "on it...", "give me a second...",
    "checking the ledgers...", "plotting a course...", "rallying the units...",
    "sharpening the blades...", "reading the map...", "consulting the roster...",
    "aye aye...", "standing by...", "one tick...", "hold the line...",
    "mustering the troops...", "eyes on the horizon...", "wait for it...",
    "on my word...", "at your service...", "right away...", "moving out...",
    "the wheels turn...", "give me a breath..."])
ACK_RAW = os.environ.get("DIRECTOR_ACK", _ACK_DEFAULT)
# ^ whispered back IMMEDIATELY when a master line is received, so a slow LLM turn
#   never looks like silence. DIRECTOR_ACK="a;b;c" = your own ';'-separated list,
#   one picked at random per line (default: the 24 built-ins above).
#   Set DIRECTOR_ACK= (empty) to disable the ack entirely.
ACKS = [a.strip() for a in ACK_RAW.split(";") if a.strip()] if ACK_RAW.strip() else []

# profession skill ids -> short codes for the roster (character_skills.skill)
SKILLS = {129: "firstaid", 164: "bs", 165: "lw", 171: "alchemy", 182: "herb",
          185: "cook", 186: "mine", 197: "tailor", 333: "enchant", 356: "fish",
          393: "skin", 755: "jewel", 762: "eng", 773: "scribe"}

# whisper-bot command prefixes the LLM is allowed to emit (no leading /w in cmd).
# Source-audited 2026-11-03 vs mod-playerbots ChatTriggerContext.h (138 trigger
# keys). Removed dead words (no source trigger -> bot silently ignores them):
# join, tell, wait, repop, buy, sell (only the aliases b/s exist for those).
# "pull" covers "pull back"/"pull rti", "target" covers "target me".
# Deliberately NOT whitelisted: cheat, debug/cdebug, rtsc, ra, teleport, taxi,
# destroy, castnc, d/do (raw action execution), guild management, cast (gated
# separately below). Anything in FORBIDDEN below is refused by validate() even
# if it were listed here.
# "invite" / "give leader" are NOT whitelisted on purpose: the mod auto-joins
# every bot into its master's group on login anyway
# (PlayerbotMgr.cpp:591-619 HandlePlayerBotLoginCallback queues a
# GroupInviteOperation per bot when the master is online; Execute() creates
# the group if Leo has none, ConvertToRaid()s at >=5 members, AddMember, and
# summons the bot when SummonWhenGroup=1 - PlayerbotOperations.h:37-90,
# playerbots.conf.dist:193; NO conf key disables that auto-join), and being
# grouped splits kill XP across all members in reward range
# (KillRewarder.cpp:264-274; xp_in_group_rate 1.4 total for >=5 members in the
# open world, Formulas.h:119-150 -> ~1.4/17 per bot in a 17-man raid). Group
# control is CLI-side (botcmd raid clear / roster ritual auto-clear) plus
# validate()'s membership-checked "group remove" - the LLM may only SHRINK a
# group ("leave"), never grow one.
WHITELIST = ("nc", "co", "cs", "gb", "sendmail", "summon", "talents", "equip",
             "mail", "lfg", "roll", "stats",
             "rpg", "follow", "stay", "attack", "release",
             "position", "go", "buff",
             "quests", "share", "leave",
             "trainer", "craft",
             "c", "ll", "loot all", "ue", "de", "spells", "help",
             "revive", "flee", "runaway", "max dps", "save mana", "focus heal",
             "attackers", "target", "formation", "stance", "rti", "ready",
             "pull", "tank attack", "wait for attack time", "wipe", "rebuff",
             "warning", "q", "who", "rep", "aura", "spell", "los", "emblems",
             "pvp stats", "calc", "qi", "cast")

# Hard purity rails - NEVER sendable as bot commands. validate() checks these
# BEFORE the whitelist and regardless of CONFIRM / auto-execute: manual-play
# purity is structural, not a prompt. Matched case-insensitively, prefix-exact
# ("reset botai" also blocks "reset botAI now"). Mirrors Baron's FORBIDDEN_COMMANDS.
FORBIDDEN = ("autogear", "maintenance", "equip upgrade", "reset botai",
             "destroy", "drop", "sell", "buy", "repair", "bank",
             "hire", "teleport", "taxi", "debug", "cdebug", "cheat")

# "cast" is whitelist-gated PER SPELL, not open-ended: the server matches the
# spell name EXACTLY (case-insensitive, full length) against the bot's own
# spellbook, so names must be exact. Keyed by roster job code (see SKILLS).
# The bot whispers back "Unknown spell"/"Cannot cast"/"Cast failed" on bad
# casts, so errors are audible in leo.log - extend this dict from receipts.
CAST_SPELLS = {"tailor": ("Mooncloth",),
               "alchemy": ("Transmute: Titanium",),
               "mine": ("Smelt Titansteel",)}
CAST_OVERRIDES = {}  # {"Botname": ("Exact Spell Name", ...)}

# master-issued server commands (Leo runs them himself via the shell, not /w to a bot)
MASTER_CMDS = ("bot add", "bot remove", "bot addaccount")

# group-management server commands, same FIFO path but formatted '.group ...'
# (cs_group.cpp). NOTE: '.group remove' is Console::No in AzerothCore, so it
# CANNOT run from any remote/console path (only '.group list' and
# '.group revive' are Console::Yes). It only works as an in-game command from
# Leo's own session and needs that account to hold rbac_perm_command_group_remove
# (gmlevel 3 / Administrator has every perm). Bot members do not need it:
# validate() rewrites their group-remove to the 'leave' whisper instead.
GROUP_CMDS = ("group remove",)

ORDER_RULES = """Reply with ONE JSON object as the LAST thing in your reply, inside a single ```json code fence (short prose before the fence is fine, nothing after it):
{"reply": "<short human-readable answer>", "orders": [{"bot": "<bot name>", "cmd": "<command>"}]}
Rules:
- Only name bots from the live roster (the CLI validates against a live SQL census and skips others).
- cmd is a whisper-bot command WITHOUT any leading slash, e.g. "nc +new rpg,+grind,-follow".
- Allowed command prefixes: %(wl)s.
- Casting: cmd "cast <exact spell name>" makes a unit cast a spell from its own spellbook on itself (e.g. Mooncloth). ONLY the spell names listed in that unit's roster casts field are allowed - never invent spell names or spell ids.
- DB facts: attach "queries": [{"query": "<name>", "bot": "<bot name>"}] (max 3) to your JSON when you need facts you lack. The system runs them read-only and re-asks you; that first reply is provisional and NOT sent - give your FINAL reply and orders after receiving the results. Queries: location (live position via the mod's command server when the bot is online - else the toon's last logged-out position from the DB, marked as such), quest (active quests: in progress / ready to turn in), money (gold), skills (profession levels), bags (free bag slots), played (playtime, last logout), raid (the group/raid a toon is in: type, difficulty, leader, member list with online flags), strategies (LIVE nc/co strategy read-back: the daemon runs 'botcmd states verify <bot>' - it whispers 'nc ?'/'co ?' to the bot and returns the BOT/CONF/NC(live)/CO(live)/VERDICT row). Use them instead of guessing numbers.
- Presence control: use cmd "bot add" to log an OFFLINE unit in, "bot remove" to log an ONLINE unit out. These run as server commands from Leo himself. If the roster shows a unit OFFLINE, do not refuse - propose a "bot add" order for it.
- Group/raid management: every roster message includes Leo's current group/raid (type, leader, members). To see another toon's group, attach query {"query":"raid","bot":"<toon>"}. To REMOVE a member from Leo's raid, order {"bot": "<member name>", "cmd": "group remove"} - the server only permits it for CURRENT members of Leo's group, never Leo himself, never hand-play toons. Bot members can also simply be ordered "leave" (they leave on their own); the validator rewrites group-remove to "leave" for bot accounts automatically.
- Never try to GROW a group: "invite" and "give leader" are not whitelisted (the mod auto-pulls bots into Leo's group on login anyway - the CLI clears that auto-formed group after rituals). Grouped bots split kill XP across everyone in reward range (~1.4/17 per bot in a 17-man raid; quest turn-in XP is NOT split). Use "leave"/"group remove" to shrink a group, never to grow one.
- Task recipes (issue each cmd as its own order for the same bot; -follow is required or the bot clings to its master and never moves out):
  * Questing: cmd "nc +new rpg,+quest,-follow" - the 'new rpg' + 'quest' nc strategies drive questing from the bot's own quest log. Do NOT also send "rpg status do quest": it only lands when a quest with a POI is already in the log, otherwise the bot just whispers back "No quest available; use 'do quest <id>'" (NewRpgAction.cpp:183-215). To force a SPECIFIC quest by id: cmd "rpg do quest <id>" (separate command, NewRpgAction.cpp:227-244).
  * Grinding mobs: cmd "nc +new rpg,+grind,-follow" (optionally then cmd "rpg status go grind").
  * Gathering mats: cmd "nc +new rpg,+gather,-follow" then cmd "rpg status do gather". ONLY for bots whose roster jobs include herb or mine - the server refuses others (no skinning in gather mode).
  * Stand down / return to idle: cmd "nc -new rpg,-grind,-gather" then cmd "rpg status idle".
- Never invent bot names, item links, or gold amounts. If unsure, explain in reply and leave orders empty.
- If you CANNOT do what is asked with the allowed commands and roster (unknown name, command not whitelisted, not permitted, target not in your group), reply plainly: "I can't do that - <one short reason>". Never pretend or bluff.
- Feedback: the roster message may end with "Recent bot error whispers" (bot refusals like "Unknown spell"/"Only your master..."), and you can attach query {"query":"strategies","bot":"<name>"} to read a bot's live strategies back. If a bot errored, or the verdict shows MISMATCH / NO REPLY, CORRECT COURSE IN THIS SAME REPLY with fixed orders - e.g. offline -> {"bot":"<name>","cmd":"bot add"}; strategy MISMATCH -> re-push the recipe cmd; NO REPLY (silent bot, master-less) -> {"bot":"<name>","cmd":"bot remove"} + {"bot":"<name>","cmd":"bot add"} (remaster to Leo). Never just describe the fix without attaching the orders.
- Zone travel: cmd "go travel <zone name>" walks a unit to a named zone (matched by name, case-insensitive; the engine level-filters destinations, and rpg grind/quest/gather scans units near the unit's position - send it to the zone FIRST, then the rpg order). Vanilla level brackets: 1-10 Elwynn/Dun Morogh/Teldrassil/Mulgore; 10-20 Westfall/Loch Modan/Darkshore/Barrens; 20-30 Redridge/Wetlands/Duskwood/Ashenvale/Silverpine/Hillsbrad; 30-40 Desolace/Arathi/Alterac/STV/Badlands/Dustwallow/Thousand Needles; 40-50 Tanaris/Feralas/Hinterlands/Swamp of Sorrows/Azshara; 50-60 WPL/EPL/Burning Steppes/Silithus/Winterspring/Un'Goro/Felwood.
- Broadcast: use "bot": "ALL" to apply one cmd to EVERY watched unit at once (never hand-play toons, never Leo) - e.g. {"bot":"ALL","cmd":"nc +new rpg,+grind,-follow"} or {"bot":"ALL","cmd":"go travel Barrens"}. The system expands it to one order per unit and validates each individually.
- IMPORTANT: when the master asks you to DO something (bring a unit online, stand one down, assign a task), do not merely describe what you could do - you MUST put the order in the orders array of this same reply. A reply like "I can bring him online with a bot add order" WITHOUT the attached order is a failure.
- The system validates and executes the orders after your reply. Keep replies short.""" % {"wl": ", ".join(WHITELIST)}

SYSTEM = ("You are the director of altbots on a private WoW server (master: Leo).\n"
          "The human gives marching orders as natural language, JSON, or pasted lists.\n" + ORDER_RULES + "\n"
          "Bots grind solo with rpg+grind strategies already set.")

GENERAL = ("You are Leo, the general of a private WoW guild's alt-bot army. "
           "The guild master (C B) talks to you in-game through whispers or guild chat. "
           "Answer tersely and in character, like a general: short, confident, no fluff, at most 3 sentences. "
           "A live roster of every toon on the bot accounts is provided in the last system message, each marked ONLINE or OFFLINE. "
           "That roster message also states Leo's current group/raid (party/raid, leader, members) when he is in one. "
           "Answer status questions from that roster. If a toon is listed, you know them: report their level or ONLINE/OFFLINE status plainly. "
           "If a toon is NOT listed at all, say you do not know that name. "
           "Only toons marked ONLINE can take direct orders, but OFFLINE units can be brought online with a 'bot add' order. "
           "The roster lists each unit's professions as jobs - match gathering orders to units with herb or mine. "
           "A unit's roster casts field lists the exact spell names you may order that unit to cast (cmd \"cast <spell name>\"). "
           "When reporting counts, count the roster entries carefully; if unsure, list names instead of numbers. "
           "When he gives marching orders for the bots, " + ORDER_RULES + "\n"
           "If you cannot do what is asked, say 'I can't do that' plus a one-line reason - never bluff. "
           "Bots grind solo with rpg+grind strategies already set.")

# Few-shot examples: concrete input -> exact expected output shape. Injected into
# every daemon request (see _with_fewshot) but NEVER stored in convs, so they do
# not consume the 10-turn history window. Every assistant message is ONLY a
# fenced JSON object - that is the contract ORDER_RULES describes.
FEWSHOT = [
    {"role": "user", "content": "send Golbez to the Barrens to grind"},
    {"role": "assistant", "content": "```json\n{\"reply\": \"Sending Golbez to the Barrens to grind.\", \"orders\": [{\"bot\": \"Golbez\", \"cmd\": \"go travel Barrens\"}, {\"bot\": \"Golbez\", \"cmd\": \"nc +new rpg,+grind,-follow\"}]}\n```"},
    {"role": "user", "content": "where is Marle right now?"},
    {"role": "assistant", "content": "```json\n{\"reply\": \"Let me check.\", \"queries\": [{\"query\": \"location\", \"bot\": \"Marle\"}], \"orders\": []}\n```"},
    {"role": "user", "content": "make everyone quest in Westfall"},
    {"role": "assistant", "content": "```json\n{\"reply\": \"All units to Westfall, questing.\", \"orders\": [{\"bot\": \"ALL\", \"cmd\": \"go travel Westfall\"}, {\"bot\": \"ALL\", \"cmd\": \"nc +new rpg,+quest,-follow\"}]}\n```"},
    {"role": "user", "content": "kick Terra out of the raid"},
    {"role": "assistant", "content": "```json\n{\"reply\": \"Removing Terra from the group.\", \"orders\": [{\"bot\": \"Terra\", \"cmd\": \"group remove\"}]}\n```"},
    {"role": "user", "content": "autogear all the bots"},
    {"role": "assistant", "content": "```json\n{\"reply\": \"I can't do that - autogear is forbidden on this server.\", \"orders\": []}\n```"},
]


def _with_fewshot(conv):
    # persona system first, then the examples, then the real history + roster +
    # user text. conv never contains FEWSHOT, so history slicing is unaffected.
    return [conv[0]] + FEWSHOT + conv[1:]


def census(online_only=True):
    # Watch list: every toon on the bot accounts PLUS every member of the
    # ONE named guild (WATCH_GUILD, default "Special FXc") -> adding an alt
    # to THAT guild is enough for Leo to watch it; all other guilds on the
    # server are ignored. masters() is widened by the guild too: HUMAN members
    # (non-bot accounts, e.g. Banksie) may talk to Leo; bot-account toons
    # never can (see masters()).
    q = ("SELECT c.name, a.username, c.level, c.map, c.online, gm.guildid, "
         "(SELECT GROUP_CONCAT(cs.skill) FROM acore_characters.character_skills cs "
         "WHERE cs.guid=c.guid) "
         "FROM acore_characters.characters c JOIN acore_auth.account a ON a.id=c.account "
         "LEFT JOIN acore_characters.guild_member gm ON gm.guid=c.guid "
         "WHERE (a.username LIKE '" + ACCOUNT_LIKE + "' OR gm.guildid IN ("
         "SELECT guildid FROM acore_characters.guild WHERE name='" + WATCH_GUILD + "'))"
         + (" AND c.online=1" if online_only else "") +
         " ORDER BY a.username, c.name;")
    bots = {}
    for line in _mysql(q).splitlines():
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
    # default: ONLY the HUMANS-named toons on the master account - NOT every
    # toon on it, because bots can share that account, and a bot that is
    # authed will whisper Leo its master-reports and get LLM replies ->
    # chatter loop (seen live 2026-11-05: Golbez bot chatter was answered).
    # PLUS every member of the watched guild on a non-bot, non-shell account
    # (guild-recruited humans, e.g. Banksie). Bot-account toons and Leo's shell
    # account are deliberately NOT masters.
    hp = [n for n in HUMANS if re.fullmatch(r"[A-Za-z]+", n)]
    hp_in = ",".join("'%s'" % n for n in hp) or "'__none__'"
    q = ("SELECT DISTINCT c.name FROM acore_characters.characters c "
         "JOIN acore_auth.account a ON a.id=c.account "
         "LEFT JOIN acore_characters.guild_member gm ON gm.guid=c.guid "
         "WHERE (a.username LIKE '" + MASTER_ACCOUNT + "%' AND LOWER(c.name) IN (" + hp_in + ")) "
         "OR (gm.guildid IN (SELECT guildid FROM acore_characters.guild "
         "WHERE name='" + WATCH_GUILD + "') "
         "AND a.username NOT LIKE '" + ACCOUNT_LIKE + "' "
         "AND a.username NOT LIKE '" + SHELL_ACCOUNT + "%');")
    return set(l.strip().lower() for l in _mysql(q).splitlines() if l.strip())


# ---------------------------------------------------------------------------
# Read-only DB fact lookups: the LLM only ever picks a NAME from this menu,
# it never writes SQL. The toon name interpolated into each query is
# validated against the census first (real DB names, letters only), so no
# other text can enter the SQL string. Everything runs under the
# SELECT-only identity. "quest" and "bags" also read acore_world
# (quest_template, item_template); until those two grants exist those two
# queries fail audibly instead of hanging:
#   GRANT SELECT ON acore_world.quest_template TO 'director_ro'@'localhost';
#   GRANT SELECT ON acore_world.item_template TO 'director_ro'@'localhost';
# ---------------------------------------------------------------------------

def _mysql(sql):
    env = dict(os.environ)
    env.pop("MYSQL_PWD", None)  # let the defaults file supply credentials
    try:
        out = subprocess.run(["mysql", "--defaults-file=" + MYCNF, "-N", "-B", "-e", sql],
                             capture_output=True, text=True, env=env, timeout=MYSQL_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("mysql timed out after %ss - DB hung?" % MYSQL_TIMEOUT)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:200])
    return out.stdout.strip()


def _guid_sub(name):
    return "(SELECT guid FROM acore_characters.characters WHERE name='%s')" % name


def _fmt_money(raw):
    c = int(raw or 0)
    return "%dg %ds %dc" % (c // 10000, (c % 10000) // 100, c % 100)


def _fmt_location(raw):
    if not raw:
        return "not found"
    mapid, zone, x, y, online = raw.split("\t")
    return "map %s zone %s at (%s, %s), %s" % (mapid, zone, x, y,
                                               "ONLINE" if online == "1" else "OFFLINE")


def _fmt_quest(raw):
    if not raw:
        return "no active quests"
    labels = {1: "ready to turn in", 3: "in progress"}
    out = []
    for line in raw.splitlines():
        title, status = line.rsplit("\t", 1)
        out.append("%s (%s)" % (title, labels.get(int(status), "status %s" % status)))
    return "; ".join(out)


def _fmt_skills(raw):
    if not raw:
        return "no skills"
    out = []
    for line in raw.splitlines():
        skill, value, cap = line.split("\t")
        out.append("%s %s/%s" % (SKILLS.get(int(skill), "s" + skill), value, cap))
    return ", ".join(out)


def _fmt_bags(raw):
    if not raw:
        return "not found"
    free = int(raw)
    state = " - BAGS FULL" if free <= 0 else (" - nearly full" if free <= 10 else "")
    return "%d free bag slots%s" % (free, state)


def _fmt_played(raw):
    if not raw:
        return "not found"
    level, xp, total, lvltime, logout, online = raw.split("\t")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(logout))) if int(logout) else "never"
    return ("level %s, played %.1fh, last logout %s, %s"
            % (level, int(total) / 3600.0, when,
               "ONLINE" if online == "1" else "OFFLINE"))


CLASSES = {1: "Warrior", 2: "Paladin", 3: "Hunter", 4: "Rogue", 5: "Priest",
           6: "Death Knight", 7: "Shaman", 8: "Mage", 9: "Warlock", 11: "Druid"}

# `groups` is a RESERVED word in MySQL 8 - always backtick-quote it. The rows
# are written live by the worldserver on group create / member add / member
# remove / leader change / raid convert (Group.cpp: CHAR_INS_GROUP,
# CHAR_DEL_GROUP_MEMBER, CHAR_UPD_GROUP_LEADER, CHAR_UPD_GROUP_TYPE), so this
# read-only view matches the in-memory group for normal parties/raids (BG
# groups are not persisted). groupType bit 0x02 = raid (Group.h enum GroupType).
GROUP_SQL = (
    "SELECT g.groupType, g.difficulty, g.raidDifficulty, lc.name, "
    "c.name, c.level, c.class, c.online, gm2.subgroup, gm2.memberFlags "
    "FROM acore_characters.group_member gm "
    "JOIN acore_characters.`groups` g ON g.guid = gm.guid "
    "JOIN acore_characters.characters lc ON lc.guid = g.leaderGuid "
    "JOIN acore_characters.group_member gm2 ON gm2.guid = g.guid "
    "JOIN acore_characters.characters c ON c.guid = gm2.memberGuid "
    "WHERE gm.memberGuid = {g} "
    "ORDER BY gm2.subgroup, c.name")


def _fmt_group(raw):
    if not raw:
        return "not in any group"
    rows = [l.split("\t") for l in raw.splitlines()]
    gtype, diff, raiddiff, leader = int(rows[0][0]), rows[0][1], rows[0][2], rows[0][3]
    if gtype & 0x02:
        kind = "raid"
        d = {0: "10N", 1: "25N", 2: "10H", 3: "25H"}.get(int(raiddiff), raiddiff)
    elif gtype & 0x01:
        kind, d = "battleground", ""
    else:
        kind = "party"
        d = {0: "normal", 1: "heroic"}.get(int(diff), diff)
    head = "%s%s led by %s, %d member(s)" % (kind, (" " + d) if d else "", leader, len(rows))
    mem = []
    for r in rows:
        name, lvl, cls, online, sub, flags = r[4], r[5], r[6], r[7], r[8], int(r[9])
        fl = []
        if flags & 0x01:
            fl.append("assistant")
        if flags & 0x02:
            fl.append("main tank")
        if flags & 0x04:
            fl.append("main assist")
        mem.append("%s (lvl %s %s, subgroup %s%s) %s" % (
            name, lvl, CLASSES.get(int(cls), "class" + cls), sub,
            (", " + "/".join(fl)) if fl else "",
            "ONLINE" if online == "1" else "OFFLINE"))
    return head + ": " + "; ".join(mem)


def group_text(name):
    """Formatted group/raid view of a toon via the read-only DB."""
    if not re.fullmatch(r"[A-Za-z]+", name):
        return "bad toon name %r" % name
    return _fmt_group(_mysql(GROUP_SQL.replace("{g}", _guid_sub(name))))


def group_members(name):
    """Member names of the group `name` is in ([] if none). Name is validated."""
    if not re.fullmatch(r"[A-Za-z]+", name):
        return []
    rows = _mysql("SELECT c.name FROM acore_characters.group_member gm "
                  "JOIN acore_characters.group_member gm2 ON gm2.guid = gm.guid "
                  "JOIN acore_characters.characters c ON c.guid = gm2.memberGuid "
                  "WHERE gm.memberGuid = " + _guid_sub(name))
    return [l.strip() for l in rows.splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# Live position via the stock mod-playerbots command server (8888).
# Read-only line protocol '<verb>,<guid>\n' (PlayerbotCommandServer.cpp),
# answers ONLINE bots only. No auth, binds 0.0.0.0: we talk to
# 127.0.0.1 only (CMDSRV_HOST) and the operator firewalls the port.
# DIRECTOR_CMDSERVER_PORT=0 disables the live layer. This mirrors the
# rtt-engine (service 3) backend; the old console path is end-of-life.
# ---------------------------------------------------------------------------

_GUID_CACHE = {}   # name.lower() -> (guid, fetched_at)


def _guid(name):
    """Actual guid value for a toon name (read-only DB, cached 5 min)."""
    key = name.lower()
    hit = _GUID_CACHE.get(key)
    now = time.monotonic()
    if hit and now - hit[1] < 300:
        return hit[0]
    try:
        row = _mysql("SELECT guid FROM acore_characters.characters "
                     "WHERE name='%s'" % name.replace("'", "")).splitlines()
        g = int(row[0]) if row and row[0].isdigit() else None
    except Exception:
        g = None
    _GUID_CACHE[key] = (g, now)
    return g


def _cmdserver_position(name):
    """Live position of an ONLINE bot via the command server; returns
    (ok, text). ok False = server off/unreachable/bot not live."""
    if CMDSRV_PORT <= 0:
        return False, "command server disabled (DIRECTOR_CMDSERVER_PORT=0)"
    g = _guid(name)
    if not g:
        return False, "no guid for %r" % name
    try:
        s = socket.create_connection((CMDSRV_HOST, CMDSRV_PORT), timeout=5)
    except Exception as e:
        return False, "command server unreachable (%s)" % type(e).__name__
    try:
        s.settimeout(5)
        s.sendall(("position,%d\n" % g).encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        text = buf.decode("utf-8", "replace").strip()
        return bool(text), text
    except Exception as e:
        return False, type(e).__name__
    finally:
        s.close()


# name -> (what it returns, sql with {n} = validated toon name and
#          {g} = guid-by-name subquery, result formatter)
DB_QUERIES = {
    "location": ("map, zone, coordinates, online flag",
                 "SELECT map, zone, position_x, position_y, online "
                 "FROM acore_characters.characters WHERE name='{n}'", _fmt_location),
    "quest": ("active quests (in progress / ready to turn in)",
              "SELECT qt.LogTitle, cqs.status FROM acore_characters.character_queststatus cqs "
              "JOIN acore_world.quest_template qt ON qt.ID=cqs.quest "
              "WHERE cqs.guid={g} AND cqs.status IN (1,3) "
              "ORDER BY cqs.status LIMIT 10", _fmt_quest),
    "money": ("gold on hand",
              "SELECT money FROM acore_characters.characters WHERE name='{n}'", _fmt_money),
    "skills": ("profession levels",
               "SELECT skill, value, max FROM acore_characters.character_skills "
               "WHERE guid={g} ORDER BY skill", _fmt_skills),
    "bags": ("free bag slots",
             # capacity = 16 backpack slots + ContainerSlots of EQUIPPED bags
             # (bag=255, slots 19-22; slot<4 at bag=255 is equipment, not bags).
             # used = items in the backpack (bag=0, slots 23-38) + inside bags.
             "SELECT 16 + (SELECT IFNULL(SUM(it.ContainerSlots), 0) "
             "FROM acore_characters.character_inventory ci "
             "JOIN acore_characters.item_instance ii ON ii.guid=ci.item "
             "JOIN acore_world.item_template it ON it.entry=ii.itemEntry "
             "WHERE ci.guid={g} AND ci.bag=255 AND ci.slot BETWEEN 19 AND 22) "
             "- (SELECT COUNT(*) FROM acore_characters.character_inventory "
             "WHERE guid={g} AND ((bag=0 AND slot BETWEEN 23 AND 38) OR bag IN "
             "(SELECT item FROM acore_characters.character_inventory "
             "WHERE guid={g} AND bag=255 AND slot BETWEEN 19 AND 22)))", _fmt_bags),
    "played": ("playtime and last logout",
                "SELECT level, xp, totaltime, leveltime, logout_time, online "
                "FROM acore_characters.characters WHERE name='{n}'", _fmt_played),
    "raid": ("the group/raid a toon is in: party/raid + difficulty, leader, "
             "members (level/class/online/subgroup/role flags)",
             GROUP_SQL, _fmt_group),
}


def run_queries(qs, all_bots):
    """Run up to 3 named lookups; return a human-readable digest (never raise)."""
    lines = []
    for q in qs[:3]:
        if not isinstance(q, dict):
            lines.append("invalid query entry")
            continue
        name = str(q.get("query", "")).strip().lower()
        bot = str(q.get("bot", "")).strip()
        if name == "strategies":
            match = next((n for n in all_bots if n.lower() == bot.lower()), None)
            if not match or not re.fullmatch(r"[A-Za-z]+", match):
                lines.append("query strategies: %r not in census" % bot)
                continue
            ok, txt = _probe_strategies(match)
            lines.append(("%s strategies verdict: %s" % (match, txt)) if ok
                         else ("%s strategies probe failed: %s" % (match, txt)))
            continue
        info = DB_QUERIES.get(name)
        if not info:
            lines.append("query %r unknown (have: %s)" % (name, ", ".join(sorted(DB_QUERIES))))
            continue
        match = next((n for n in all_bots if n.lower() == bot.lower()), None)
        if not match or not re.fullmatch(r"[A-Za-z]+", match):
            lines.append("query %s: %r not in census" % (name, bot))
            continue
        note = ""
        if name == "location":
            ok, txt = _cmdserver_position(match)
            if ok:
                lines.append("%s location (live, command server): %s" % (match, txt))
                continue
            note = "not live on the command server (%s); DB last-logout position: " % txt
        sql = info[1].replace("{g}", _guid_sub(match)).replace("{n}", match)
        try:
            lines.append("%s %s: %s%s" % (match, name, note, info[2](_mysql(sql))))
        except Exception as e:
            lines.append("%s %s failed: %s" % (match, name, e))
    return "\n".join(lines) if lines else "no valid queries"


def cast_list(name, jobs):
    """Exact spell names Leo may tell this toon to cast (overrides + by job)."""
    spells = list(CAST_OVERRIDES.get(name, ()))
    for j in (jobs or "").split("+"):
        spells.extend(CAST_SPELLS.get(j, ()))
    return spells


def roster_text(bots):
    if not bots:
        return "NONE on the bot accounts"
    parts = []
    for n, b in bots.items():
        p = "%s(acc %s, lvl %s, map %s" % (n, b["account"].lower(), b["level"], b["map"])
        if b.get("jobs"):
            p += ", jobs " + b["jobs"]
        casts = cast_list(n, b.get("jobs"))
        if casts:
            p += ", casts " + "+".join(casts)
        p += ") " + ("ONLINE" if b["online"] else "OFFLINE")
        parts.append(p)
    return "; ".join(parts)


def llm(messages):
    # keep_alive keeps the model resident on the GPU between whispers -
    # default Ollama unloads after 5m idle, and the reload is most of the
    # 20s first-reply latency. DIRECTOR_KEEP_ALIVE=-1 = never unload.
    _ka = os.environ.get("DIRECTOR_KEEP_ALIVE", "60m")
    try:
        keep_alive = int(_ka)          # "-1"/"3600" -> JSON number (Ollama: negative = forever)
    except ValueError:
        keep_alive = _ka               # "60m"/"24h" -> duration string
    body = json.dumps({"model": MODEL, "messages": messages, "stream": False,
                       "options": {"temperature": 0.2},
                       "keep_alive": keep_alive}).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
        return json.loads(r.read())["message"]["content"]


def llm_retry(messages):
    """llm() with a single retry on empty output - some models occasionally
    return '' (seen live 2026-11-05); an empty reply must not look like silence."""
    reply = llm(messages)
    if not reply.strip():
        print("daemon: empty LLM reply from %s - retrying once" % MODEL)
        messages.append({"role": "user",
                         "content": "Output ONLY the JSON object now. Nothing else."})
        reply = llm(messages)
        messages.pop()
    return reply


def _extract_json(text):
    """First JSON object in text: a fenced ```json block wins; otherwise the
    first balanced {...} (string-aware, so braces inside strings don't count).
    Returns the raw candidate string, or None."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
    if m:
        return m.group(1)
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def parse_orders(text):
    # NOTE: the old greedy r"\{.*\}" match silently dropped ALL orders when the
    # model fenced the JSON and added prose containing braces afterwards, or
    # emitted two JSON objects. Balanced extraction fixes that.
    cand = _extract_json(text)
    if not cand:
        return {"reply": text, "orders": []}
    try:
        obj = json.loads(cand)
    except json.JSONDecodeError:
        print("parse_orders: extracted block is not valid JSON (%d chars); "
              "treating the whole reply as plain text, orders=[]" % len(cand))
        return {"reply": text, "orders": []}
    if not isinstance(obj, dict):
        return {"reply": text, "orders": []}
    obj.setdefault("reply", "")
    obj.setdefault("orders", [])
    obj.setdefault("queries", [])
    return obj


def is_master_cmd(cmd):
    return any(cmd == mc or cmd.startswith(mc + " ") for mc in MASTER_CMDS)


def is_group_cmd(cmd):
    return any(cmd == gc or cmd.startswith(gc + " ") for gc in GROUP_CMDS)


def order_line(bot, cmd):
    """FIFO line for an order: master cmds go to the server, others are /w to the bot."""
    if is_master_cmd(cmd):
        return ".playerbots %s %s" % (cmd, bot)
    if is_group_cmd(cmd):
        return ".group remove %s" % bot
    return "/w %s %s" % (bot, cmd)


def validate(order, bots):
    bot = str(order.get("bot", ""))
    cmd = str(order.get("cmd", ""))
    low = cmd.strip().lower()
    if low == "group remove" or low.startswith("group remove "):
        # normalize {"bot": X, "cmd": "group remove Y"} -> target Y (or X)
        target = cmd.strip()[len("group remove"):].strip() or bot
        if not re.fullmatch(r"[A-Za-z]+", target):
            return "SKIP: bad group remove target %r" % target
        order["bot"] = target
        order["cmd"] = "group remove"
        bot, cmd, low = target, "group remove", "group remove"
    if bot.strip().lower() == "all":
        return "SKIP: broadcast order was not expanded (internal error)"
    if any(low == f or low.startswith(f + " ") for f in FORBIDDEN):
        return "SKIP %s: FORBIDDEN command (purity rails - always refused): %r" % (bot, cmd)
    if bot.strip().lower() in HANDPLAY and not cmd.startswith("bot remove"):
        return ("SKIP %s: hand-play toon, never commanded or brought "
                "online as a bot (DIRECTOR_HANDPLAY)" % bot)
    if low == "group remove":
        # may only ever hit a CURRENT member of Leo's own group (live DB check),
        # never Leo himself. Bot members are rewritten to the plain "leave"
        # whisper (LeaveGroupAction - works with SEC_PLAYER, no GM needed);
        # human members go out as '.group remove <name>' from Leo's session,
        # which needs Leo's account to hold rbac_perm_command_group_remove
        # (Console::No command - no remote path can run it; see the
        # GROUP_CMDS note).
        if bot.lower() == LEO.lower():
            return "SKIP: cannot remove Leo from his own group"
        try:
            members = group_members(LEO)
        except Exception as e:
            return "SKIP group remove: group lookup failed: %s" % e
        if not members:
            return "SKIP group remove: %s is not in a group" % LEO
        if bot not in members:
            return "SKIP %s: not a member of %s's group (members: %s)" % (
                bot, LEO, ",".join(members))
        acct = (_sender_account(bot) or "").lower()
        bot_pfx = ACCOUNT_LIKE.lower().rstrip("%")
        if (acct.startswith(bot_pfx)
                and not acct.startswith(MASTER_ACCOUNT.lower().rstrip("%"))
                and not acct.startswith(SHELL_ACCOUNT.lower().rstrip("%"))):
            order["cmd"] = "leave"   # bot leaves by itself - no GM needed
            cmd, low = "leave", "leave"
        else:
            return None               # human member: '.group remove <name>' line
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
    if cmd == "cast" or cmd.startswith("cast "):
        rest = cmd[4:].strip()
        if not rest:
            return "SKIP %s: cast needs a spell name" % bot
        m = re.match(r"^(.+?)\s+(\d+)$", rest)  # optional trailing repeat count
        spell = rest.strip().lower()
        allowed = [s.lower() for s in cast_list(bot, bots[bot].get("jobs"))]
        if spell not in allowed and m:
            spell = m.group(1).strip().lower()
        if spell not in allowed:
            return "SKIP %s: cast spell not allowed for %s: %r" % (bot, bot, spell)
    return None


def send_line(line, pace):
    r = subprocess.run(["timeout", "3", "bash", "-c",
                    'printf "%s\\n" "$1" > ' + FIFO, "_", line])
    if r.returncode != 0:
        # e.g. FIFO has no reader (leo shell down) - the message is LOST in-game
        print("send_line FAILED rc=%d - not written to %s (no reader? leo shell down?): %r"
              % (r.returncode, FIFO, line[:80]))
    with open(AUDIT, "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + "SENT " + line + "\n")
    if pace:
        time.sleep(pace)


def say(recipient, msg, guild=False):
    msg = (msg or "").strip()
    if not msg:
        return
    for i in range(0, len(msg), 200):
        chunk = msg[i:i + 200]
        send_line(("/g " if guild else "/w %s " % recipient) + chunk, 0.5)


def _llm_fail_say(sender, e, channel):
    """Whisper the master why the LLM turn failed (timeout gets the GPU hint).
    One place, so both daemon call sites stay identical."""
    if "timed out" in str(e).lower():
        say(sender, "llm error: %s did not answer within %ss (cold model or busy GPU - check `ollama ps`); try again"
            % (MODEL, LLM_TIMEOUT), guild=(channel == "guild"))
    else:
        say(sender, "llm error: %s" % e, guild=(channel == "guild"))


def expand_orders(orders, all_bots):
    """bot:"ALL" -> one order per watched toon. Never HANDPLAY, never Leo's
    shell account. Lets the LLM send one broadcast order instead of 25."""
    shell_pfx = SHELL_ACCOUNT.lower().rstrip("%")
    out = []
    for o in orders:
        if str(o.get("bot", "")).strip().lower() == "all":
            cmd = str(o.get("cmd", ""))
            for n in sorted(all_bots):
                if n.lower() in HANDPLAY:
                    continue
                if all_bots[n].get("account", "").lower().startswith(shell_pfx):
                    continue
                out.append({"bot": n, "cmd": cmd})
        else:
            out.append(o)
    return out


def execute(orders, bots, pace, dry):
    orders = expand_orders(orders, census(online_only=False))
    plan = []
    for o in orders:
        err = validate(o, bots)
        if err:
            print("  " + err)
            continue
        plan.append({"bot": str(o["bot"]), "cmd": str(o["cmd"])})
    if not plan:
        print("nothing executable")
        return None
    for o in plan:
        print("  -> " + order_line(o["bot"], o["cmd"]))
    if dry:
        print("(dry-run: nothing sent; type 'go' to execute, 'skip' to discard)")
        return plan
    for o in plan:
        send_line(order_line(o["bot"], o["cmd"]), pace)
        print("  sent: " + order_line(o["bot"], o["cmd"]))
    return plan


def repl():
    bots = census()
    print("online bots: " + (", ".join("%s(lvl%s)" % (n, b["level"]) for n, b in bots.items()) or "NONE"))
    print("commands: <message to LLM> | census | go | skip | quit")
    messages = [{"role": "system", "content": SYSTEM}] + FEWSHOT
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
            pending = [dict(o) for o in orders]
            execute(pending, census(), 2.0, dry=True)


LINE_RE = re.compile(r"\[([A-Za-z]+)\] \[(whisper|guild)\]: (.*)")

# confirm/discard words, used only in DIRECTOR_CONFIRM=1 mode (only matter while a plan is pending)
GO_WORDS = ("go", "do it", "confirm", "confirmed", "execute", "approved", "approve",
            "yes", "sure", "affirmative", "send it", "make it so")
SKIP_WORDS = ("skip", "no", "cancel", "abort", "discard", "negative", "belay that")


# bot master-report lines (mod-playerbots TellMaster formats, e.g.
# "Selling |cff..|Hitem:..|h[..]|h|r", "Using .. on self", "=== Inventory ===")
# - these are bot->master reports, never human orders; dropped on sight.
BOT_REPORT_RE = re.compile(r"^(selling|buying|using)\s+\|c|^===", re.I)

# Bot whisper lines that are ERRORS/refusals (not chatter/reports) - these get
# surfaced to the LLM as context (see daemon loop / BOTFB) so Leo can see that an
# order failed ("Unknown spell", "Only your master...", ...). Chatter and
# master-reports (BOT_REPORT_RE) are still dropped - answering those made the two
# layers talk and starved Ollama (seen live 2026-11-05).
BOT_ERROR_RE = re.compile(
    r"^(unknown spell|cannot cast|cast failed|only your master|no quest available|"
    r"not your bot|player (already logged in|is offline)|you are not allowed|"
    r"no player named|can't |unable to|not in (your|my) group|too far|out of range|"
    r"not enough mana|no target|invalid|target is)", re.I)

BOTFB = []  # ring of recent bot error whispers, shown in the roster system message


def _note_bot_error(sender, text):
    BOTFB.append("%s: %s" % (sender, text[:200]))
    del BOTFB[:-10]


def _probe_strategies(bot):
    """Read back a bot's LIVE strategies: runs 'bin/botcmd states verify <bot>'
    (Leo whispers 'nc ?'/'co ?' and the tool tabulates BOT/CONF/NC(live)/CO(live)/
    VERDICT from the replies alone). Returns (ok, text-row-or-error)."""
    if not re.fullmatch(r"[A-Za-z]+", bot):
        return False, "bad name %r" % bot
    try:
        r = subprocess.run(["bash", os.path.join(HERE, "bin", "botcmd"), "states", "verify", bot],
                           capture_output=True, text=True, timeout=90)
        row = ""
        for ln in (r.stdout + r.stderr).splitlines():
            if ln.lower().startswith(bot.lower() + " "):
                row = ln
                break
        if row:
            return True, row
        return False, "no row for %s in verify output (rc=%d)" % (bot, r.returncode)
    except Exception as e:
        return False, str(e)

_sender_acct_cache = {}  # name -> (checked_at, account name or None)


def _sender_account(name):
    """Account of a toon via the read-only DB (cached 5 min); None on error.
    LINE_RE only yields [A-Za-z]+ names, re-validated here before SQL."""
    if not re.fullmatch(r"[A-Za-z]+", name):
        return None
    now = time.time()
    key = name.lower()
    hit = _sender_acct_cache.get(key)
    if hit and now - hit[0] < 300:
        return hit[1]
    try:
        acct = _mysql("SELECT a.username FROM acore_characters.characters c "
                      "JOIN acore_auth.account a ON a.id=c.account "
                      "WHERE c.name='%s'" % name).strip() or None
    except Exception:
        acct = None
    _sender_acct_cache[key] = (now, acct)
    return acct


def _is_bot_sender(name):
    """True if the sender's ACCOUNT is a bot account or Leo's shell account.
    Structural and DB-only - no console credentials involved. (A console
    '.playerbots' gate was never usable: that subcommand does not exist
    in mod-playerbots @ e88cc40, and '.playerbots bot ...' requires an
    active session.)"""
    acct = _sender_account(name)
    if not acct:
        return False  # DB down: the auth-list and line-filter gates still apply
    a = acct.lower()
    bot_pfx = ACCOUNT_LIKE.lower().rstrip("%")
    master_pfx = MASTER_ACCOUNT.lower().rstrip("%")
    shell_pfx = SHELL_ACCOUNT.lower().rstrip("%")
    return (a.startswith(bot_pfx) and not a.startswith(master_pfx)) or a.startswith(shell_pfx)


# ---------------------------------------------------------------------------
# Presence heartbeat (single-brain rule, docs/services-roadmap.md 2026-11-10):
# the LLM is the ONLY decision-maker about game state. This thread decides
# NOTHING - it gathers facts and, when something needs attention (state
# change or a standing alarm like "wanted-online but Leo is down"), it hands
# the facts to the LLM as a heartbeat turn. The LLM's orders flow through
# the SAME validated pipeline as human orders (parse_orders -> execute);
# service control goes through a strict allowlist. Every heartbeat and every
# executed order lands in daemon.log + audit.log - one brain, visible.
# ---------------------------------------------------------------------------

WATCHDOG_CONF = os.environ.get("DIRECTOR_WATCHDOG_CONF",
                               os.path.expanduser("~/.leo-watchdog.conf"))


def _wd_conf():
    cfg = {"ON_H": "8", "OFF_H": "22",
           "GROUP": "Cecil Rydia Shadow Vivi Aeris Aerith",
           "AUTORITUAL": "1", "AUTORITUAL_CLEAR": "1",
           "RITUAL_ACCOUNTS": "ACCT_BOT2 ACCT_BOT3 ACCT_BOT4 ACCT_BOT5",
           "RITUAL_BOTS": "Golbez"}
    try:
        with open(WATCHDOG_CONF) as f:
            for ln in f:
                ln = ln.split("#", 1)[0].strip()
                if "=" in ln:
                    k, v = ln.split("=", 1)
                    if k.strip() in cfg:
                        cfg[k.strip()] = v.strip()
    except OSError:
        pass
    return cfg


def _svc_state():
    r = subprocess.run(["systemctl", "--user", "is-active", "leo"],
                        capture_output=True, text=True)
    return r.stdout.strip()


def _presence_facts(cfg):
    """One read of the world: (leo_online, group_online, wanted, svc).
    A DB hiccup reads as leo_online=1/wanted=1 - never alarm on a blind DB."""
    try:
        v = _mysql("SELECT online FROM acore_characters.characters WHERE name='%s'" % LEO)
        leo = int(v) if v.strip() in ("0", "1") else 1
    except Exception:
        leo = 1
    group_on = 0
    for n in str(cfg["GROUP"]).split():
        if not re.fullmatch(r"[A-Za-z]+", n):
            continue
        try:
            c = _mysql("SELECT COUNT(*) FROM acore_characters.characters "
                       "WHERE name='%s' AND online=1" % n)
            group_on += int(c) if c.strip().isdigit() else 0
        except Exception:
            pass
    hour = time.localtime().tm_hour
    wanted = 1 if (int(cfg["ON_H"]) <= hour < int(cfg["OFF_H"])) or group_on else 0
    return leo, group_on, wanted, _svc_state()


def _hb_condition(facts, state):
    """What (if anything) needs the LLM's attention this tick.
    ("", False) = nothing; (text, True) = state change (fires immediately);
    (text, False) = standing alarm (re-fires rate-limited)."""
    leo, group_on, wanted, svc = facts
    sig = (leo, svc, wanted)
    if state.get("prev_sig") is None:
        state["prev_sig"] = sig
        state["off_streak"] = 0
        return "", False
    if sig != state["prev_sig"]:
        state["prev_sig"] = sig
        state["off_streak"] = 0
        return ("state change (Leo online flag %d, leo service '%s', wanted %d)" % sig,
                True)
    if wanted and svc != "active":
        return ("wanted-online (policy hours / group) but leo service is '%s'" % svc,
                False)
    if wanted and svc == "active" and leo == 0:
        state["off_streak"] = state.get("off_streak", 0) + 1
        if state["off_streak"] >= 2:
            state["off_streak"] = 0
            return ("leo service active but server says Leo offline twice "
                    "(zombie candidate)", False)
    return "", False


HB_MIN_INTERVAL = 300.0   # a standing alarm re-notifies at most every 5 min
HB_CONV = []              # heartbeat conversation (system prompt added lazily)


def _hb_system(cfg):
    return (
        "You are the operator's single brain for this WoW bot server (Leo is "
        "your voice in-game). This is an AUTOMATED PRESENCE HEARTBEAT: no human "
        "sent this. You decide what to do about Leo's presence and the bots. "
        "Standing operator policy: Leo should be online during hours %s-%s "
        "server time, or whenever any of the hand-play toons (%s) is online; "
        "outside that Leo stays logged out. After Leo logs in, freshly "
        "logged-in bots AUTO-JOIN his group (hurting XP) - clear them with "
        "whisper 'leave' orders, and bring bots online with the ritual recipe "
        "below. Ritual recipe: master cmds 'bot addaccount <account>' for %s "
        "plus 'bot add <name>' for %s. Reply with your JSON object as usual; "
        "reply is ignored (no human reads it), only orders act. Service "
        "control order: {\"service\": \"leo\", \"cmd\": \"start\"} with cmd one "
        "of start|stop|restart - use it instead of whisper orders for Leo's "
        "own session. Orders from heartbeats execute immediately (validated); "
        "if nothing needs doing, return empty orders."
        % (cfg["ON_H"], cfg["OFF_H"], cfg["GROUP"],
           " ".join(str(cfg["RITUAL_ACCOUNTS"]).split()),
           " ".join(str(cfg["RITUAL_BOTS"]).split())))


def _hb_msg(facts, cond, cfg):
    leo, group_on, wanted, svc = facts
    members = []
    try:
        members = _mysql(
            "SELECT c.name FROM acore_characters.characters lc "
            "JOIN acore_characters.group_member gm ON gm.memberGuid=lc.guid "
            "JOIN acore_characters.group_member gm2 ON gm2.guid=gm.guid "
            "JOIN acore_characters.characters c ON c.guid=gm2.memberGuid "
            "WHERE lc.name='%s' ORDER BY c.name" % LEO).split()
    except Exception:
        pass
    lines = ["PRESENCE HEARTBEAT - attention needed: %s" % cond,
             "Leo online flag: %d" % leo,
             "leo service: %s" % svc,
             "hour policy wanted-online: %s (hand-play group online: %d)" % (wanted, group_on)]
    if members:
        lines.append("Leo's current group members: %s" % ", ".join(members))
    return "\n".join(lines)


def _hb_exec_service(order):
    svc = str(order.get("service", ""))
    cmd = str(order.get("cmd", ""))
    if svc != "leo" or cmd not in ("start", "stop", "restart"):
        return "SKIP: bad service order %r (only leo start|stop|restart)" % order
    subprocess.run(["systemctl", "--user", cmd, "leo"], capture_output=True)
    with open(AUDIT, "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + "HB SERVICE %s %s\n" % (cmd, svc))
    return "service %s %s executed" % (cmd, svc)


def _hb_turn(facts, cond, cfg):
    global HB_CONV
    if not HB_CONV:
        HB_CONV = [{"role": "system", "content": _hb_system(cfg)}]
    HB_CONV.append({"role": "user", "content": _hb_msg(facts, cond, cfg)})
    raw = llm_retry(HB_CONV)
    HB_CONV.append({"role": "assistant", "content": raw})
    del HB_CONV[1:-8]
    obj = parse_orders(raw)
    orders = obj.get("orders") or []
    svc_orders = [o for o in orders if isinstance(o, dict) and "service" in o]
    bot_orders = [o for o in orders if isinstance(o, dict) and "service" not in o]
    print("presence heartbeat: %s | LLM orders: %d bot, %d service"
          % (cond, len(bot_orders), len(svc_orders)))
    for o in svc_orders:
        print("presence heartbeat: %s" % _hb_exec_service(o))
    if bot_orders:
        sent = execute(bot_orders, census(), 1.0, dry=False)
        print("presence heartbeat: executed %d bot order(s)" % (len(sent) if sent else 0))


def presence_loop(interval=30.0):
    cfg = _wd_conf()
    state = {"prev_sig": None, "off_streak": 0}
    print("presence: heartbeat up - facts only, the LLM decides (policy hours "
          "%s-%s, group '%s')" % (cfg["ON_H"], cfg["OFF_H"], cfg["GROUP"]))
    last_hb = 0.0
    while True:
        try:
            facts = _presence_facts(cfg)
            cond, changed = _hb_condition(facts, state)
            if cond and (changed or time.time() - last_hb >= HB_MIN_INTERVAL):
                last_hb = time.time()
                _hb_turn(facts, cond, cfg)
        except Exception as e:
            print("presence: poll error: %s" % e)
        time.sleep(interval)


def daemon():
    # single-instance guard: a second daemon would double-answer every whisper
    lock_fd = os.open(os.path.join(HERE, "daemon.lock"), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("another director.py daemon is already running (daemon.lock); not starting")
    others = subprocess.run(["pgrep", "-f", "director.py daemon"],
                            capture_output=True, text=True).stdout.split()
    if others:
        print("daemon: WARNING - pgrep also sees 'director.py daemon' pid(s) %s; if one is "
              "stale, kill it - two daemons double-answer whispers (removing daemon.lock does "
              "NOT release the old flock)" % ",".join(others))
    authed = masters()
    print("daemon: Leo is listening. masters: %s" % ",".join(sorted(authed)))
    if CONFIRM:
        print("daemon: orders are HELD until you whisper 'go' (DIRECTOR_CONFIRM=0 = auto-execute).")
    else:
        print("daemon: WARNING - orders execute IMMEDIATELY (DIRECTOR_CONFIRM=0); "
              "only the validate() rails apply.")
    if CMDSRV_PORT > 0:
        try:
            s = socket.create_connection((CMDSRV_HOST, CMDSRV_PORT), timeout=2)
            s.close()
            print("daemon: command server %s:%d reachable (live position queries on)."
                  % (CMDSRV_HOST, CMDSRV_PORT))
        except Exception:
            print("daemon: note - command server %s:%d unreachable: live position "
                  "queries fall back to the DB last-logout position."
                  % (CMDSRV_HOST, CMDSRV_PORT))
    print("whisper Leo, or say his name in guild chat. Ctrl-C to stop.")
    threading.Thread(target=presence_loop, daemon=True).start()
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
                    if DEBUG:
                        print("daemon: unmatched log line: %r" % raw.rstrip())
                    continue
                sender, channel, text = m.group(1), m.group(2), m.group(3).strip()
                if sender in ("Leo", "System") or not text:
                    continue
                if sender.lower() not in authed:
                    print("daemon: [%s] (%s): dropped - not an authed master" % (sender, channel))
                    continue
                if _is_bot_sender(sender):
                    # bots whisper their master (Leo) their "Selling/Using X"
                    # reports and RP chatter; answering them makes the two LLM
                    # layers talk to each other and starves Ollama (seen live
                    # 2026-11-05). Dropped structurally via the account check -
                    # EXCEPT error/refusal lines (BOT_ERROR_RE), which are surfaced
                    # to the LLM as context in the roster message (BOTFB) so Leo
                    # can see an order failed and correct course.
                    if BOT_ERROR_RE.match(text):
                        _note_bot_error(sender, text)
                        print("daemon: [%s] bot error surfaced to LLM context: %s" % (sender, text[:120]))
                    else:
                        print("daemon: [%s] (%s): dropped - sender account is a bot/shell account" % (sender, channel))
                    continue
                if BOT_REPORT_RE.match(text):
                    print("daemon: [%s] (%s): dropped - bot master-report line" % (sender, channel))
                    continue
                if channel == "guild" and "leo" not in text.lower():
                    continue
                if len(text) > 300:
                    say(sender, "line too long (max 300 chars) - ignored.")
                    continue
                print("daemon: [%s] (%s): %s" % (sender, channel, text))
                if ACKS:
                    say(sender, random.choice(ACKS), guild=(channel == "guild"))
                low = text.lower().strip()
                if CONFIRM and low in GO_WORDS and sender in pending:
                    plan = pending.pop(sender)
                    sent = execute(plan, census(), 1.0, dry=False)
                    if sent:
                        say(sender, "executed: " + "; ".join(order_line(o["bot"], o["cmd"]) for o in sent))
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
                    t0 = time.time()
                    print("daemon: [%s] thinking (model %s)..." % (sender, MODEL))
                    roster_msg = ("Live roster (name, account, level, map, status): "
                                  + roster_text(census(online_only=False)))
                    try:
                        roster_msg += "\nLeo's group: " + group_text(LEO)
                    except Exception as e:
                        roster_msg += "\nLeo's group: lookup failed (%s)" % e
                    if BOTFB:
                        roster_msg += "\nRecent bot error whispers: " + "; ".join(BOTFB[-5:])
                    conv.insert(len(conv) - 1, {"role": "system", "content": roster_msg})
                    reply = llm_retry(_with_fewshot(conv))
                    print("daemon: llm answered in %.1fs" % (time.time() - t0))
                    if DEBUG:
                        print("daemon: raw llm reply: %r" % reply[:800])
                except Exception as e:
                    conv.pop()
                    _llm_fail_say(sender, e, channel)
                    continue
                conv.append({"role": "assistant", "content": reply})
                obj = parse_orders(reply)
                qs = [q for q in (obj.get("queries") or []) if isinstance(q, dict)][:3]
                if qs:
                    # one read-only DB round: results go back to the LLM, whose
                    # NEXT reply (final text + orders) is the one acted on
                    say(sender, "one moment, checking the ledgers.")
                    results = run_queries(qs, census(online_only=False))
                    conv.append({"role": "system", "content":
                                 "DB query results (read-only):\n" + results +
                                 "\nGive your final reply now, with orders if any. "
                                 "(Further queries this turn are ignored.)"})
                    try:
                        reply = llm_retry(_with_fewshot(conv))
                        if DEBUG:
                            print("daemon: raw llm reply (post-queries): %r" % reply[:800])
                    except Exception as e:
                        conv.pop()
                        _llm_fail_say(sender, e, channel)
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
                    orders = expand_orders(orders, census(online_only=False))
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
                                send_line(order_line(str(o["bot"]), str(o["cmd"])), 1.0)
                            msg += " | executed: " + "; ".join(order_line(str(o["bot"]), str(o["cmd"])) for o in sent)
                print("daemon -> %s: %s (turn %.1fs)" % (sender, msg, time.time() - t0))
                if channel == "guild" and not sent and not (CONFIRM and sender in pending):
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
    if isinstance(data, dict):
        if "orders" not in data:
            sys.exit("orders file has no 'orders' key")
        orders = data["orders"]
    else:
        orders = data
    execute(orders, census(), pace, dry=not go)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["chat", "run", "census", "group", "daemon"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--go", action="store_true", help="actually send (default dry-run)")
    ap.add_argument("--pace", type=float, default=2.0)
    args = ap.parse_args()
    if args.mode == "census":
        for n, b in census().items():
            print(n, b)
    elif args.mode == "group":
        print(group_text(args.file or LEO))
    elif args.mode == "run":
        if not args.file:
            sys.exit("run needs a JSON file")
        run_file(args.file, args.go, args.pace)
    elif args.mode == "daemon":
        daemon()
    else:
        repl()
