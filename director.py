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
  python3 director.py group [toon]    print the group/raid a toon is in (read-only; default Leo)

orders.json schema: {"orders": [{"bot": "<name>", "cmd": "<whisper command>"}], "pace_sec": 2}

Daemon auth: ONLY the toons in DIRECTOR_HUMANS (default the G1 five) on the
master account (default i_was_here1st, rotatable via 'botcmd master') PLUS
members of the watched
guild (DIRECTOR_GUILD) on non-bot, non-shell accounts - bot-account toons and
Leo's shell account never get auth, because their automatic "Using X"
master-report whispers would be answered by the LLM and ping-pong with the bot
AI. Override with DIRECTOR_MASTERS=Cecil,Golbez. Guild chat triggers
require the word 'leo' in the line. Bot senders are additionally dropped by a
DB account check + master-report line filter (no SOAP involved:
'.playerbots inspect' does not exist in mod-playerbots @ e88cc40 and
'.playerbots bot' requires an active session, so neither works from the SOAP
console). RP chatter must never reach the LLM or the two layers talk.
Validated orders are HELD for a whisper-'go' confirm by default (set
DIRECTOR_CONFIRM=0 to auto-execute immediately). Run daemon with
python3 -u for live logs.
"""
import argparse, base64, fcntl, html, json, os, re, subprocess, sys, time, urllib.error, urllib.request
from xml.sax.saxutils import escape as _xml_escape

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("DIRECTOR_MODEL", "gemma4:12b")
FIFO = os.environ.get("LEO_FIFO", "/tmp/leo.in")
LOG = os.environ.get("DIRECTOR_LOG", "/tmp/leo.log")
AUDIT = os.path.join(HERE, "audit.log")
ACCOUNT_LIKE = os.environ.get("DIRECTOR_ACCOUNT_LIKE", "i_was_here%")
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
MASTER_ACCOUNT = os.environ.get("DIRECTOR_MASTER_ACCOUNT", "i_was_here1st")
SHELL_ACCOUNT = os.environ.get("DIRECTOR_SHELL_ACCOUNT", "i_was_cmd")
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
SOAPCNF = os.path.expanduser(os.environ.get("DIRECTOR_SOAPCNF", "~/.soap-director"))
# ^ 3-line side file (chmod 600) for the worldserver SOAP endpoint: url, user,
# pass. The credentials are a real game account with gmlevel >= Administrator
# (AzerothCore checks the account table over HTTP Basic - verified in
# ACSOap.cpp; there is no SOAP user/password key in worldserver.conf). Missing
# or short file = SOAP off: the location query falls back to the DB logout
# position (stale, but the LLM is told so).
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
ACK = os.environ.get("DIRECTOR_ACK", "one moment...")
# ^ whispered back IMMEDIATELY when a master line is received, so a slow LLM turn
#   never looks like silence. Set DIRECTOR_ACK= (empty) to disable.

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
WHITELIST = ("nc", "co", "cs", "gb", "sendmail", "summon", "talents", "equip",
             "mail", "lfg", "roll", "stats",
             "rpg", "follow", "stay", "attack", "release",
             "position", "go", "invite", "give leader", "buff",
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
MASTER_CMDS = ("bot add", "bot remove")

# group-management server commands, same FIFO path but formatted '.group ...'
# (cs_group.cpp). NOTE: '.group remove' is Console::No in AzerothCore, so it
# CANNOT run via SOAP (SOAP queues console commands; only '.group list' and
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
- DB facts: attach "queries": [{"query": "<name>", "bot": "<bot name>"}] (max 3) to your JSON when you need facts you lack. The system runs them read-only and re-asks you; that first reply is provisional and NOT sent - give your FINAL reply and orders after receiving the results. Queries: location (live map/zone via SOAP .pinfo when configured - else the toon's last logged-out position, marked as such), quest (active quests: in progress / ready to turn in), money (gold), skills (profession levels), bags (free bag slots), played (playtime, last logout), raid (the group/raid a toon is in: type, difficulty, leader, member list with online flags). Use them instead of guessing numbers.
- Presence control: use cmd "bot add" to log an OFFLINE unit in, "bot remove" to log an ONLINE unit out. These run as server commands from Leo himself. If the roster shows a unit OFFLINE, do not refuse - propose a "bot add" order for it.
- Group/raid management: every roster message includes Leo's current group/raid (type, leader, members). To see another toon's group, attach query {"query":"raid","bot":"<toon>"}. To REMOVE a member from Leo's raid, order {"bot": "<member name>", "cmd": "group remove"} - the server only permits it for CURRENT members of Leo's group, never Leo himself, never hand-play toons. Bot members can also simply be ordered "leave" (they leave on their own); the validator rewrites group-remove to "leave" for bot accounts automatically.
- Task recipes (issue each cmd as its own order for the same bot; -follow is required or the bot clings to its master and never moves out):
  * Questing: cmd "nc +new rpg,-follow" then cmd "rpg status do quest". The bot picks a quest from its log; if it has none, it will say so.
  * Grinding mobs: cmd "nc +new rpg,+grind,-follow" (optionally then cmd "rpg status go grind").
  * Gathering mats: cmd "nc +new rpg,+gather,-follow" then cmd "rpg status do gather". ONLY for bots whose roster jobs include herb or mine - the server refuses others (no skinning in gather mode).
  * Stand down / return to idle: cmd "nc -new rpg,-grind,-gather" then cmd "rpg status idle".
- Never invent bot names, item links, or gold amounts. If unsure, explain in reply and leave orders empty.
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
           "Bots grind solo with rpg+grind strategies already set.")


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
    env = dict(os.environ)
    env.pop("MYSQL_PWD", None)  # let the defaults file supply credentials
    try:
        out = subprocess.run(["mysql", "--defaults-file=" + MYCNF, "-N", "-B", "-e", q],
                             capture_output=True, text=True, env=env, timeout=MYSQL_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("mysql timed out after %ss (census) - DB hung?" % MYSQL_TIMEOUT)
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
    env = dict(os.environ)
    env.pop("MYSQL_PWD", None)
    try:
        out = subprocess.run(["mysql", "--defaults-file=" + MYCNF, "-N", "-B", "-e", q],
                             capture_output=True, text=True, env=env, timeout=MYSQL_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("mysql timed out after %ss (masters) - DB hung?" % MYSQL_TIMEOUT)
    if out.returncode != 0:
        raise RuntimeError("masters failed: " + out.stderr.strip())
    return set(l.strip().lower() for l in out.stdout.strip().splitlines() if l.strip())


# ---------------------------------------------------------------------------
# Read-only DB fact lookups: the LLM only ever picks a NAME from this menu,
# it never writes SQL. The toon name interpolated into each query is
# validated against the census first (real DB names, letters only), so no
# other text can enter the SQL string. Everything runs under the
# SELECT-only identity. "quest" and "bags" also read acore_world
# (quest_template, item_template); until those two grants exist those two
# queries fail audibly instead of hanging:
#   GRANT SELECT ON acore_world.quest_template TO 'iwashere'@'localhost';
#   GRANT SELECT ON acore_world.item_template TO 'iwashere'@'localhost';
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
# SOAP (optional): live worldserver-console access for the location query.
# One operation only: ns1:executeCommand over HTTP Basic auth with a game
# account whose gmlevel is >= Administrator (AzerothCore checks the account
# table itself - verified in ACSOap.cpp). The server needs SOAP.Enabled=1
# (default bind 127.0.0.1:7878). Config file ~/.soap-director, 3 lines:
#   http://127.0.0.1:7878/
#   <game account name>
#   <game account password>
# ---------------------------------------------------------------------------

def _soap_cfg():
    try:
        with open(SOAPCNF) as f:
            vals = [l.strip() for l in f.read().splitlines()][:3]
    except OSError:
        return None
    if len(vals) < 3 or not all(vals):
        return None
    return vals[0], vals[1], vals[2]


def soap_command(cmd, timeout=10):
    """Run one console command via SOAP; returns (ok, text). Never raises."""
    cfg = _soap_cfg()
    if not cfg:
        return False, "SOAP not configured (" + SOAPCNF + " missing/incomplete)"
    url, user, pw = cfg
    env = ('<?xml version="1.0" encoding="utf-8"?>'
           '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
           'xmlns:ns1="urn:AC"><SOAP-ENV:Body>'
           '<ns1:executeCommand><command>' + _xml_escape(cmd) + '</command></ns1:executeCommand>'
           '</SOAP-ENV:Body></SOAP-ENV:Envelope>')
    auth = base64.b64encode((user + ":" + pw).encode()).decode()
    req = urllib.request.Request(url, data=env.encode("utf-8"), headers={
        "Content-Type": "text/xml; charset=utf-8", "SOAPAction": "urn:AC",
        "Authorization": "Basic " + auth})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return False, "HTTP %d (bad account/password or gmlevel below Administrator)" % e.code
    except Exception as e:
        return False, type(e).__name__ + ": " + str(e)
    if "<SOAP-ENV:Fault" in body:
        m = re.search(r"<faultstring[^>]*>(.*?)</faultstring>", body, re.S)
        return False, "server fault: " + html.unescape(m.group(1) if m else "?")
    m = re.search(r"<[^>]*executeCommandResponse[^>]*>(.*)</[^>]*executeCommandResponse>", body, re.S)
    if not m:
        return False, "unexpected SOAP response: " + body[:120]
    text = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
    return bool(text), text


def _fmt_pinfo(raw):
    """Keep the useful lines of '.pinfo' console output, drop the rest."""
    keep = []
    for line in raw.splitlines():
        line = re.sub(r"\|c[0-9A-Fa-f]{8}|\|r", "", line).strip().strip("|").strip()
        if not line:
            continue
        low = line.lower()
        if any(k in low for k in ("map:", "zone:", "area:", "online", "login",
                                   "guild:", "level:", "alive", "money:")):
            keep.append(line)
    return "; ".join(keep) if keep else raw[:200]


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
            ok, txt = soap_command(".pinfo " + match)
            if ok:
                lines.append("%s location (live .pinfo): %s" % (match, _fmt_pinfo(txt)))
                continue
            note = "SOAP unavailable (%s); DB last-logout position: " % (txt or "empty .pinfo response")
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
        # (Console::No command - SOAP cannot run it; see GROUP_CMDS note).
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


def execute(orders, bots, pace, dry):
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
    Structural and DB-only - works with SOAP off. (The old SOAP
    '.playerbots inspect' gate was removed: that subcommand does not exist
    in mod-playerbots @ e88cc40, and '.playerbots bot ...' requires an
    active session, so neither is usable from the SOAP console.)"""
    acct = _sender_account(name)
    if not acct:
        return False  # DB down: the auth-list and line-filter gates still apply
    a = acct.lower()
    bot_pfx = ACCOUNT_LIKE.lower().rstrip("%")
    master_pfx = MASTER_ACCOUNT.lower().rstrip("%")
    shell_pfx = SHELL_ACCOUNT.lower().rstrip("%")
    return (a.startswith(bot_pfx) and not a.startswith(master_pfx)) or a.startswith(shell_pfx)


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
    if not _soap_cfg():
        print("daemon: note - SOAP not configured (%s): live .pinfo location queries fall "
              "back to last-logout position. Bot-sender gating is DB-based and unaffected." % SOAPCNF)
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
                    # 2026-11-05). Dropped structurally via the account check.
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
                if ACK:
                    say(sender, ACK, guild=(channel == "guild"))
                low = text.lower().strip()
                if CONFIRM and low in GO_WORDS and sender in pending:
                    plan = pending.pop(sender)
                    sent = execute(plan, census(), 2.0, dry=False)
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
                    conv.insert(len(conv) - 1, {"role": "system", "content": roster_msg})
                    reply = llm_retry(conv)
                    print("daemon: llm answered in %.1fs" % (time.time() - t0))
                    if DEBUG:
                        print("daemon: raw llm reply: %r" % reply[:800])
                except Exception as e:
                    conv.pop()
                    if "timed out" in str(e).lower():
                        say(sender, "llm error: %s did not answer within %ss (cold model or busy GPU - check `ollama ps`); try again"
                            % (MODEL, LLM_TIMEOUT), guild=(channel == "guild"))
                    else:
                        say(sender, "llm error: %s" % e, guild=(channel == "guild"))
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
                        reply = llm_retry(conv)
                        if DEBUG:
                            print("daemon: raw llm reply (post-queries): %r" % reply[:800])
                    except Exception as e:
                        conv.pop()
                        if "timed out" in str(e).lower():
                            say(sender, "llm error: %s did not answer within %ss (cold model or busy GPU - check `ollama ps`); try again"
                                % (MODEL, LLM_TIMEOUT), guild=(channel == "guild"))
                        else:
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
