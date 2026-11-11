#!/usr/bin/env python3
"""director - LLM <-> altbot command interface for Chazbot (I_WAS_CMD / Leo shell).

Hands (this process is the brain's body, never a fact source): ALL DB facts
come from the db-search service (unix socket, typed read-only tools, every
result carries queried_at + a staleness caveat); ALL sends, replies and
live/session facts come from the chat-tunnel service (local-only HTTP on
127.0.0.1:8765). The LLM never sees free-form SQL and this file never touches
MySQL, the Leo FIFO, /tmp/leo.log, or any game port directly. LLM: local
Ollama (off the world thread). Stdlib only.

Modes:
  python3 director.py chat            interactive terminal LLM REPL; dry-run first, 'go' sends
  python3 director.py daemon         in-game interface: whisper Leo (or say 'Leo' in guild) and
                                     the LLM answers as the general and directs bots (confirm via 'go')
  python3 director.py run orders.json [--go]   execute a JSON marching-orders file
  python3 director.py census         print live online-bot roster (via db-search)
  python3 director.py group [toon]    print the group/raid a toon is in (via db-search; default Leo)

orders.json schema: {"orders": [{"bot": "<name>", "cmd": "<whisper command>"}], "pace_sec": 2}

Daemon auth: ONLY the toons in DIRECTOR_HUMANS (default the G1 five) on the
master account (default i_was_here1st, rotatable via 'botcmd master') PLUS
members of the watched guild (DIRECTOR_GUILD) on non-bot, non-shell accounts
- bot-account toons and Leo's shell account never get auth, because their
automatic "Using X" master-report whispers would be answered by the LLM and
ping-pong with the bot AI. Override with DIRECTOR_MASTERS=Cecil,Golbez. Guild
chat triggers require the word 'leo' in the line. Bot senders are additionally
dropped by an account check (db-search) + master-report line filter. RP chatter
must never reach the LLM or the two layers talk. NOTE: chat-tunnel only
records lines from roster toons (its allowlist), so a master who is neither a
guild member nor on a watched account is invisible to the daemon - roster
membership is the listen gate.
Validated orders are HELD for a whisper-'go' confirm by default (set
DIRECTOR_CONFIRM=0 to auto-execute immediately). Run daemon with
python3 -u for live logs.
"""
import argparse, fcntl, json, os, random, re, socket, subprocess, sys, threading, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT = os.path.join(HERE, "audit.log")
# ^ append-only local audit trail: every executed send and service order lands
#   here with a timestamp - one brain, visible
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("DIRECTOR_MODEL", "gemma4:12b")
DBSEARCH_SOCK = os.environ.get("DIRECTOR_DBSEARCH_SOCK", "/tmp/db-search.sock")
# ^ db-search service (roadmap row 2): typed read-only DB tools, one JSON line
#   per request on a local unix socket. The ONLY source of DB facts here.
TUNNEL_URL = os.environ.get("DIRECTOR_TUNNEL_URL", "http://127.0.0.1:8765/")
# ^ chat-tunnel service (roadmap row 1): owns /tmp/leo.log and /tmp/leo.in.
#   The ONLY path for sends, replies and live reads here.
SVC_TIMEOUT = float(os.environ.get("DIRECTOR_SVC_TIMEOUT", "30"))
# ^ cap on one service call (db-search caps its own queries at 10s; the
#   tunnel's wait_for_events long-poll blocks up to 25s and gets extra slack)
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
# ^ toons on the MASTER account that may TALK to Leo (sender auth). Default = the
# G1 five. 'botcmd master set <ACCOUNT>' rewrites this together with
# DIRECTOR_MASTER_ACCOUNT when you rotate which account you hand-play on, so a
# bot that shares the master account is still NOT authed (not in HUMANS).
MASTER_ACCOUNT = os.environ.get("DIRECTOR_MASTER_ACCOUNT", "i_was_here1st")
SHELL_ACCOUNT = os.environ.get("DIRECTOR_SHELL_ACCOUNT", "i_was_cmd")
LEO = os.environ.get("DIRECTOR_LEO", "Leo")
# ^ Leo's own toon (the headless shell character) - used for group/raid lookups
#   ("who is in MY raid / who leads it") and for the group-remove target rail.
# ^ Leo's own headless-shell account: never a master, never a valid sender -
#   the shell must not talk to its own brain.
CONFIRM = os.environ.get("DIRECTOR_CONFIRM", "1") not in ("0", "no", "off")
# ^ daemon default: validated orders are HELD until the master whispers
# 'go'/'skip'. DIRECTOR_CONFIRM=0 auto-executes immediately (not recommended:
# validate() rails are then the only safety net).
DEBUG = os.environ.get("DIRECTOR_DEBUG", "") in ("1", "yes", "on")
# ^ DIRECTOR_DEBUG=1 dumps raw LLM replies to daemon.log
LLM_TIMEOUT = float(os.environ.get("DIRECTOR_LLM_TIMEOUT", "120"))
# ^ cap on ONE Ollama call; a slow model or a queue of turns can otherwise stall
#   the single-threaded loop for minutes (live 2026-11-10: a warm model still
#   took ~5 min and returned empty - the master now gets a hard fail at 2 min
#   instead of silence). The model is kept resident (DIRECTOR_KEEP_ALIVE=-1,
#   A17) so 120s is generous for a warm turn.
STILL_WORKING = float(os.environ.get("DIRECTOR_STILL_WORKING", "45"))
# ^ if a whisper-triggered LLM turn is still running after this many seconds,
#   whisper the master a heads-up so slow is never mistaken for a hang
STILL_WORKING_MSG = os.environ.get("DIRECTOR_STILL_WORKING_MSG",
                                   "still with you - the model is thinking...")
MAX_TOKENS = int(os.environ.get("DIRECTOR_MAX_TOKENS", "4096"))
# ^ runaway BRAKE, not a budget (num_predict). Live evidence 2026-11-10: a
#   healthy Leo turn is ~4.2k prompt + ~740 thinking + ~200 answer tokens and
#   lands in well under 30s (trace: stv -> Booty Bay translation, correct
#   JSON) - but ONE turn spiralled to 16,938 generated tokens at 144 t/s and
#   ate the whole 120s LLM timeout (journalctl task 31796). 4096 (~28s of
#   pure generation) lets real turns breathe and stops a spiral cold. 0 = off.
THINKING = os.environ.get("DIRECTOR_THINKING", "1") not in ("0", "no", "off")
# ^ gemma4:12b is a thinking model and the reasoning IS the product (owner:
#   "the whole point of the LLM is the LLM") - thinking stays ON by default.
#   DIRECTOR_THINKING=0 sends "think": false for deployments that want raw
#   speed; if the Ollama build rejects the field, llm() retries once without.
TURN_MAX = float(os.environ.get("DIRECTOR_TURN_MAX", "240"))
# ^ hard cap on ONE whole turn (roster + LLM + queries + sends), not just the
#   LLM call: live 2026-11-10 a turn sat silent for ~2 min because the nudge
#   only covered the LLM calls - a stall in census/queries/sends got NO
#   indication. Past this the turn is abandoned: the master is told WHICH
#   stage stuck and for how long, and the loop moves on (all service calls
#   carry finite timeouts, so the abandoned worker dies on its own).
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

# profession skill ids -> short codes (character_skills.skill, via db-search
# get_skills) - used to gate "cast <spell>" orders per profession
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
# control is the LLM's "leave"/"group remove" orders plus validate()'s
# membership-checked group-remove rail - the LLM may only SHRINK a group
# ("leave"), never grow one.
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
             "pvp stats", "calc", "qi", "cast", "boat", "carriage")

# Hard purity rails - NEVER sendable as bot commands. validate() checks these
# BEFORE the whitelist and regardless of CONFIRM / auto-execute: manual-play
# purity is structural, not a prompt. Matched case-insensitively, prefix-exact
# ("reset botai" also blocks "reset botAI now"). Mirrors Baron's FORBIDDEN_COMMANDS.
FORBIDDEN = ("autogear", "maintenance", "equip upgrade", "reset botai",
             "destroy", "drop", "sell", "buy", "repair", "bank",
             "hire", "teleport", "taxi", "debug", "cdebug", "cheat")

# "cast" is whitelist-gated PER SPELL, not open-ended: the server matches the
# spell name EXACTLY (case-insensitive, full length) against the bot's own
# spellbook, so names must be exact. Keyed by profession code (see SKILLS);
# validate() checks the target's DB skills (db-search get_skills) before
# allowing the cast. The bot whispers back "Unknown spell"/"Cannot cast"/
# "Cast failed" on bad casts, so errors are audible via chat-tunnel - extend
# this dict from receipts.
CAST_SPELLS = {"tailor": ("Mooncloth",),
              "alchemy": ("Transmute: Titanium",),
              "mine": ("Smelt Titansteel",)}
CAST_OVERRIDES = {}  # {"Botname": ("Exact Spell Name", ...)}

# master-issued server commands (Leo runs them himself, not /w to a bot)
# Zone -> engine destination translation table (the master speaks ZONE names,
# the travel engine matches SUBZONE/town names - A24). Curated from the 3.3.5
# AreaTable; every destination must be live-confirmed via the A25 batch probe
# (live-probe recipe for a destination: order it, then read the bot's reply
# or live position back - a FAIL row gets its mapping fixed, not shipped).
ZONE_DESTINATIONS = {
    # zone (canonical + common abbreviations)      engine destination   bracket
    "Elwynn Forest":        ("Goldshire",           "1-10"),
    "Dun Morogh":           ("Kharanos",            "1-10"),
    "Teldrassil":           ("Dolanaar",            "1-10"),
    "Mulgore":              ("Bloodhoof Village",   "1-10"),
    "Westfall":             ("Sentinel Hill",      "10-20"),
    "Loch Modan":           ("Thelsamar",           "10-20"),
    "Darkshore":            ("Auberdine",           "10-20"),
    "The Barrens":          ("Crossroads",          "10-20"),
    "Redridge Mountains":   ("Lakeshire",          "20-30"),
    "Wetlands":             ("Menethil Harbor",    "20-30"),
    "Duskwood":             ("Darkshire",          "20-30"),
    "Ashenvale":            ("Astranaar",          "20-30"),
    "Silverpine Forest":    ("The Sepulcher",      "20-30"),
    "Hillsbrad Foothills":  ("Tarren Mill",        "20-30"),
    "Stranglethorn Vale":   ("Booty Bay",          "30-40"),
    "Desolace":             ("Nijel's Point",      "30-40"),
    "Arathi Highlands":     ("Refuge Point",       "30-40"),
    "Badlands":             ("Kargath",            "35-45"),
    "Dustwallow Marsh":     ("Theramore",          "35-45"),
    "Thousand Needles":     ("Freewind Post",      "40-50"),
    "Tanaris":               ("Gadgetzan",          "40-50"),
    "Feralas":              ("Feathermoon",        "40-50"),
    "The Hinterlands":      ("Aerie Peak",         "40-50"),
    "Swamp of Sorrows":     ("Stonard",            "45-55"),
    "Azshara":              ("Azshara",            "45-55"),
    "Un'Goro Crater":       ("Marshal's Refuge",   "48-55"),
    "Felwood":              ("Felwood",            "48-55"),
    "Western Plaguelands":  ("Andorhal",           "50-58"),
    "Eastern Plaguelands":  ("Light's Hope Chapel", "53-60"),
    "Burning Steppes":      ("Flame Crest",        "52-60"),
    "Winterspring":         ("Everlook",           "53-60"),
    "Silithus":             ("Cenarion Hold",      "55-60"),
}
# Zone id -> name and map id -> name, for translating the DB's raw ids into the
# names players use (the roster and location query report them). Sourced from
# the engine's own zone documentation (playerbots.conf.dist ZoneBracket
# comments, mod-playerbots @ 405f8119) - A27; live-verify against the in-game
# map when a toon's reported zone looks wrong.
MAP_NAMES = {0: "Eastern Kingdoms", 1: "Kalimdor", 530: "Outland", 571: "Northrend"}
ZONE_NAMES = {
    1: "Dun Morogh", 3: "Badlands", 4: "Blasted Lands", 8: "Swamp of Sorrows",
    10: "Duskwood", 11: "Wetlands", 12: "Elwynn Forest", 14: "Durotar",
    15: "Dustwallow Marsh", 16: "Azshara", 17: "The Barrens", 28: "Western Plaguelands",
    33: "Stranglethorn Vale", 38: "Loch Modan", 40: "Westfall", 44: "Redridge Mountains",
    45: "Arathi Highlands", 46: "Burning Steppes", 47: "The Hinterlands",
    51: "Searing Gorge", 85: "Tirisfal Glades", 130: "Silverpine Forest",
    139: "Eastern Plaguelands", 141: "Teldrassil", 148: "Darkshore",
    215: "Mulgore", 267: "Hillsbrad Foothills", 331: "Ashenvale", 357: "Feralas",
    361: "Felwood", 400: "Thousand Needles", 405: "Desolace", 406: "Stonetalon Mountains",
    440: "Tanaris", 490: "Un'Goro Crater", 618: "Winterspring",
    1377: "Silithus", 1497: "Undercity", 1519: "Stormwind City", 1537: "Ironforge",
    1637: "Orgrimmar", 1638: "Thunder Bluff",
}

ZONE_ABBREVIATIONS = {"stv": "Stranglethorn Vale", "wpl": "Western Plaguelands",
                      "epl": "Eastern Plaguelands", "barrens": "The Barrens",
                      "tn": "Thousand Needles", "hillsbrad": "Hillsbrad Foothills",
                      "elwynn": "Elwynn Forest", "plaguelands": "Eastern Plaguelands"}

# Harbormaster policy (operator-approved 2026-11-10): Leo is NOT a teleporter.
# The GM '.tele name' power is only ever exercised as a BOAT/ZEPPELIN leg
# between real shipping-route harbors, narrated as travel. Cross-continent
# movement is impossible outside these pairs - validate() enforces it.
TRAVEL_ROUTES = [
    ("Booty Bay", "Ratchet", "boat", "neutral"),
    ("Menethil Harbor", "Theramore", "boat", "neutral"),
    ("Menethil Harbor", "Auberdine", "boat", "neutral"),
    ("Gromgol Base Camp", "Undercity", "zeppelin", "horde"),
    ("Gromgol Base Camp", "Orgrimmar", "zeppelin", "horde"),
    ("Undercity", "Orgrimmar", "zeppelin", "horde"),
]
RACE_FACTION = {1: "alliance", 3: "alliance", 4: "alliance", 7: "alliance",
                11: "alliance", 2: "horde", 5: "horde", 6: "horde", 8: "horde",
                10: "horde"}
CARRIAGE = os.environ.get("DIRECTOR_CARRIAGE", "0") in ("1", "yes", "on")
# ^ when on, Leo may "send a carriage": teleport a bot to a ROUTE harbor
#   (departure points only, still route-bounded). Default OFF: legs to the
#   dock are walks.
BOAT_DOCK_RADIUS = 500.0   # yards from the departure harbor's tele point

MASTER_CMDS = ("bot add", "bot remove", "bot addaccount")

# group-management server commands; chat-tunnel formats them as '.group ...'
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
- Only name bots from the live roster (the system validates it against the live roster from the DB service and skips others).
- cmd is a whisper-bot command WITHOUT any leading slash, e.g. "nc +new rpg,+grind,-follow".
- Allowed command prefixes: %(wl)s.
- Casting: cmd "cast <exact spell name>" makes a unit cast a spell from its own spellbook on itself. ONLY these exact spell names are ever allowed: Mooncloth (tailors), Transmute: Titanium (alchemists), Smelt Titansteel (miners) - the system checks the unit's professions and refuses everything else. Never invent spell names or spell ids.
- NPC/mob travel is covered by the same dest query: "send Ayla to the trainer in Goldshire", "have Gau grind yetis" - attach {"query":"dest","bot":"<any>","name":"<npc/mob place name>"} and order the returned "go x;y" (the bot pathfinds to the exact spot). Prefer the specific name you mean; say which destination you sent them to.
- Quest by name: to send a bot after a SPECIFIC quest the master names, attach query {"query":"quest","bot":"<name>"} FIRST - the results carry quest ids - then order cmd "rpg do quest <id>". NEVER invent quest ids; if the quest is not in the bot's log, say so.
- Silent-bot procedure (A20): if a bot ignores orders or a probe times out, do not report it as fine. 1) attach query {"query":"strategies","bot":"<name>"} - a timeout means silent (offline truth is db-search). 2) If the roster shows it ONLINE and it stayed silent, it is a corpse or master-less: order {"bot":"<name>","cmd":"revive"}, wait, then re-probe. 3) Still silent: remaster - {"bot":"<name>","cmd":"bot remove"} then {"bot":"<name>","cmd":"bot add"}. Attach the orders in the same reply; never just describe them.
- DB facts: attach "queries": [{"query": "<name>", "bot": "<bot name>"}] (max 3) to your JSON when you need facts you lack. The system runs them read-only through the DB service and re-asks you; that first reply is provisional and NOT sent - give your FINAL reply and orders after receiving the results. Each result carries a queried_at time and a staleness caveat - respect both, never present stale numbers as current. Queries: location (the toon's last-flushed map/zone from the DB, marked as such - live position is the tunnel's job), quest (active quests: in progress / ready to turn in), money (gold), skills (profession levels), bags (free bag slots), played (playtime, last logout), raid (the group/raid a toon is in: type, difficulty, leader, member list with online flags), strategies (LIVE strategy read-back through the tunnel's inspect command - returns the bot's live strategy list, or a timeout note if it stays silent). Use them instead of guessing numbers.
- Presence control: use cmd "bot add" to log an OFFLINE unit in, "bot remove" to log an ONLINE unit out. These run as server commands from Leo himself. If the roster shows a unit OFFLINE, do not refuse - propose a "bot add" order for it.
- Group/raid management: every roster message includes Leo's current group/raid (type, leader, members). To see another toon's group, attach query {"query":"raid","bot":"<toon>"}. To REMOVE a member from Leo's raid, order {"bot": "<member name>", "cmd": "group remove"} - the system only permits it for CURRENT members of Leo's group, never Leo himself, never hand-play toons. Bot members can also simply be ordered "leave" (they leave on their own); the validator rewrites group-remove to "leave" for bot accounts automatically.
- Never try to GROW a group: "invite" and "give leader" are not whitelisted (the mod auto-pulls bots into Leo's group on login anyway - the brain clears that auto-formed group with leave orders). Grouped bots split kill XP across everyone in reward range (~1.4/17 per bot in a 17-man raid; quest turn-in XP is NOT split). Use "leave"/"group remove" to shrink a group, never to grow one.
- Task recipes (issue each cmd as its own order for the same bot; -follow is required or the bot clings to its master and never moves out):
  * Questing: cmd "nc +new rpg,+quest,-follow" - the 'new rpg' + 'quest' nc strategies drive questing from the bot's own quest log. Do NOT also send "rpg status do quest": it only lands when a quest with a POI is already in the log, otherwise the bot just whispers back "No quest available; use 'do quest <id>'" (NewRpgAction.cpp:183-215). To force a SPECIFIC quest by id: cmd "rpg do quest <id>" (separate command, NewRpgAction.cpp:227-244).
  * Grinding mobs: cmd "nc +new rpg,+grind,-follow" (optionally then cmd "rpg status go grind").
  * Gathering mats: cmd "nc +new rpg,+gather,-follow" then cmd "rpg status do gather". ONLY for units whose skills query shows herb or mine - the server refuses others (no skinning in gather mode).
  * Stand down / return to idle: cmd "nc -new rpg,-grind,-gather" then cmd "rpg status idle".
- Never invent bot names, item links, or gold amounts. If unsure, explain in reply and leave orders empty.
- If you CANNOT do what is asked with the allowed commands and roster (unknown name, command not whitelisted, not permitted, target not in your group), reply plainly: "I can't do that - <one short reason>". Never pretend or bluff.
- Feedback: the roster message may end with "Recent bot error whispers" (bot refusals like "Unknown spell"/"Only your master..."), and you can attach query {"query":"strategies","bot":"<name>"} to read a bot's live strategies back. If a bot errored, or a strategies query timed out (silent, master-less bot), CORRECT COURSE IN THIS SAME REPLY with fixed orders - e.g. offline -> {"bot":"<name>","cmd":"bot add"}; wrong strategies -> re-push the recipe cmd; silent bot -> {"bot":"<name>","cmd":"bot remove"} + {"bot":"<name>","cmd":"bot add"} (remaster to Leo). Never just describe the fix without attaching the orders.
- Zone travel: "go travel <name>" is DEAD on this build (the engine's travel catalogs are never loaded - a live 31/31 probe got "Clearing travel target" for every destination, including its own example). To send a unit to a named destination, order {"bot":"<name>","cmd":"travel <destination>"}: the SYSTEM runs the whole journey - it stands the unit down, hops it to the nearest flight master, reads its own "taxi ?" list, flies the long leg if the network reaches it, hops the last mile, and whispers you the arrival. Narrate it as "<bot> is travelling to <destination>" and say HOW (on foot / via flight). The system whispers progress; do not poll the unit yourself unless asked. "walk <destination>" is the ground-only variant. Cross-continent trips need the harbormaster (boat/zeppelin routes below) - a travel order to another continent will be refused with a map note.
- Destination translation: the master speaks in ZONE names and abbreviations ("send Golbez to Stranglethorn Vale", "go to stv", "level in Thousand Needles"); the engine wants the town/subzone name. Translate with this table (zone -> destination, level bracket):
%(zones)s
  Common abbreviations: %(abbr)s. If the master names a zone not in the table, pick the best-known town in it and TELL them which destination you are sending the unit to. If the master names a town/area directly, use it as-is. rpg grind/quest/gather scans units near the unit's position - send it to the destination FIRST, then the rpg order.
- Harbormaster travel (cross-continent): Leo is NOT a teleporter. Bots cross continents ONLY by boat/zeppelin, and ONLY along these real routes: Booty Bay->Ratchet, Menethil Harbor->Theramore, Menethil Harbor->Auberdine (boats, any faction); Gromgol Base Camp->Undercity, Gromgol Base Camp->Orgrimmar, Undercity->Orgrimmar (zeppelins, HORDE only - never send an Alliance bot there). To board, the unit must be at the departure harbor; order {"bot":"<name>","cmd":"boat <arrival harbor>"} ONLY when the unit is at the departure dock (check with query {"query":"live","bot":"<name>"}). Narrate trips as travel: "Gau is taking the boat from Menethil Harbor to Auberdine" - never mention teleporting. The system enforces the routes; a SKIP means the unit is not at the dock or the route is wrong for its faction.%(carriage)s
- Farming mats: "have <bot> farm <mat>" (herbs/ore) = 1) check the unit can gather: query {"query":"skills","bot":"<name>"} - it needs herb (for plants) or mine (for ore); if it lacks the skill, SAY SO and suggest a unit that has it. 2) find the destination: query {"query":"mats","bot":"<any>","name":"<mat>"} - the answer ranks named teleport locations by nearby spawn density. 3) get the unit there (walk/rpg within the continent; harbormaster routes across continents). 4) start gathering: {"bot":"<name>","cmd":"nc +new rpg,+gather,-follow"} then {"bot":"<name>","cmd":"rpg status do gather"}. Gathering also levels the unit's profession skill. Nodes are dynamic spawns - the density answer is the potential, not a guarantee of what is up right now.
- Leveling requests: "have <bot> go level in <zone>", "send <bot> to level in <zone>" = order {"bot":"<name>","cmd":"travel <the zone's table destination>"} (the system flies/marches them there and whispers the arrival) then the grind recipe {"bot":"<name>","cmd":"nc +new rpg,+grind,-follow"}. When the master leaves the zone open, match the unit's level to the bracket column; if the named zone's bracket is far from the unit's level, say so and suggest a bracket-appropriate zone - but obey an explicit instruction.
    {"role": "assistant", "content": "```json\n{\"reply\": \"On it - charting the route to the Barrens.\", \"queries\": [{\"query\": \"dest\", \"bot\": \"Golbez\", \"name\": \"Crossroads\"}], \"orders\": []}\n```"},
- IMPORTANT: when the master asks you to DO something (bring a unit online, stand one down, assign a task), do not merely describe what you could do - you MUST put the order in the orders array of this same reply. A reply like "I can bring him online with a bot add order" WITHOUT the attached order is a failure.
- The system validates and executes the orders after your reply. Keep replies short.""" % {"wl": ", ".join(WHITELIST),
        "zones": "\n".join("  %-22s -> %-20s (%s)" % (z, d[0], d[1])
                           for z, d in sorted(ZONE_DESTINATIONS.items())),
        "abbr": ", ".join("%s=%s" % (a, z) for a, z in sorted(ZONE_ABBREVIATIONS.items())),
        "carriage": (" Carriage is ENABLED (DIRECTOR_CARRIAGE=1): you may order "
                     '{"bot":"<name>","cmd":"carriage <route harbor>"} to send a '
                     "carriage (a swift trip) to a route harbor, and narrate it "
                     "as such.") if CARRIAGE else ""}

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
           "Attach a skills query to see a unit's professions - match gathering orders to units with herb or mine. "
           "When reporting counts, count the roster entries carefully; if unsure, list names instead of numbers. "
           "When he gives marching orders for the bots, " + ORDER_RULES + "\n"
           "If you cannot do what is asked, say 'I can't do that' plus a one-line reason - never bluff. "
           "Bots grind solo with rpg+grind strategies already set.")

# Few-shot examples: concrete input -> exact expected output shape. Injected into
# every daemon request (see _with_fewshot) but NEVER stored in convs, so they do
# not consume the 10-turn history window. Every assistant message is ONLY a
# fenced JSON object - that is the contract ORDER_RULES describes.
FEWSHOT = [
    {"role": "user", "content": "send golbez to stv"},
    {"role": "assistant", "content": "```json\n{\"reply\": \"Golbez is travelling to Booty Bay (Stranglethorn Vale).\", \"orders\": [{\"bot\": \"Golbez\", \"cmd\": \"travel Booty Bay\"}]}\n```"},
    {"role": "user", "content": "send Golbez to the Barrens to grind"},
    {"role": "assistant", "content": "```json\n{\"reply\": \"Sending Golbez to the Barrens to grind.\", \"orders\": [{\"bot\": \"Golbez\", \"cmd\": \"go -443.0;-2594.0\"}, {\"bot\": \"Golbez\", \"cmd\": \"nc +new rpg,+grind,-follow\"}]}\n```"},
    {"role": "user", "content": "where is Marle right now?"},
    {"role": "assistant", "content": "```json\n{\"reply\": \"Let me check.\", \"queries\": [{\"query\": \"location\", \"bot\": \"Marle\"}], \"orders\": []}\n```"},
    {"role": "user", "content": "make everyone quest in Westfall"},
    {"role": "assistant", "content": "```json\n{\"reply\": \"All units to Westfall, questing.\", \"queries\": [{\"query\": \"dest\", \"bot\": \"Leo\", \"name\": \"Sentinel Hill\"}], \"orders\": []}\n```"},
    {"role": "user", "content": "kick Terra out of the raid"},
    {"role": "assistant", "content": "```json\n{\"reply\": \"Removing Terra from the group.\", \"orders\": [{\"bot\": \"Terra\", \"cmd\": \"group remove\"}]}\n```"},
    {"role": "user", "content": "autogear all the bots"},
    {"role": "assistant", "content": "```json\n{\"reply\": \"I can't do that - autogear is forbidden on this server.\", \"orders\": []}\n```"},
]


def _with_fewshot(conv):
    # persona system first, then the examples, then the real history + roster +
    # user text. conv never contains FEWSHOT, so history slicing is unaffected.
    return [conv[0]] + FEWSHOT + conv[1:]


# ---------------------------------------------------------------------------
# Service clients - the ONLY paths to the world's facts and to the game's
# chat. One thing per call, no fallbacks: if a service is down, the call
# fails loudly and the caller decides (docs/services-roadmap.md rows 1-2).
# ---------------------------------------------------------------------------

def dbsearch(tool, **params):
    """One typed read-only DB tool call on the db-search unix socket.
    Returns the full envelope (ok, queried_at, caveat, rows); raises on
    any refusal. The LLM never writes SQL and neither does this file."""
    req = json.dumps({"tool": tool, "params": params}).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(SVC_TIMEOUT)
        s.connect(DBSEARCH_SOCK)
        s.sendall(req + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    resp = json.loads(buf)
    if not resp.get("ok"):
        raise RuntimeError("db-search %s refused: %s" %
                           (tool, resp.get("error") or resp.get("code")))
    return resp


def tunnel(tool, http_timeout=None, **args):
    """One chat-tunnel tool call over local-only HTTP. Returns the result
    object; raises on any refusal. `http_timeout` overrides the client cap
    (the wait_for_events long-poll blocks server-side up to 25s)."""
    body = json.dumps({"tool": tool, "args": args}).encode()
    req = urllib.request.Request(TUNNEL_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=http_timeout or SVC_TIMEOUT) as r:
        resp = json.loads(r.read())
    if not resp.get("ok"):
        raise RuntimeError("chat-tunnel %s refused: %s" % (tool, resp.get("error")))
    return resp["result"]


def _audit(mark, line):
    with open(AUDIT, "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + mark + " " + line + "\n")


def census(online_only=True):
    # Watch list (same rule as ever): every toon on the bot accounts PLUS every
    # member of the ONE named guild (WATCH_GUILD) -> adding an alt to THAT guild
    # is enough for Leo to watch it; all other guilds are ignored. Both halves
    # now come from db-search typed tools; the guild half needs the toon's
    # account (for the sender/bot gates), resolved via get_character and cached.
    bots = {}
    for row in dbsearch("get_online_census",
                        account_like=ACCOUNT_LIKE.rstrip("%"))["rows"]:
        name, acct, level, cls, mapid, zone, online, guildid = row
        bots[name] = {"account": acct, "level": int(level), "map": int(mapid),
                      "online": online == "1",
                      "guildid": int(guildid) if guildid.isdigit() else 0}
    for row in dbsearch("get_guild_members", guild=WATCH_GUILD)["rows"]:
        name, rank, level, cls, online = row
        if name in bots:
            continue
        bots[name] = {"account": _sender_account(name) or "?",
                      "level": int(level), "map": 0,
                      "online": online == "1", "guildid": 1}
    if online_only:
        return {n: b for n, b in bots.items() if b["online"]}
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
    # (guild-recruited humans, e.g. Banksie). Bot-account toons and Leo's
    # shell account are deliberately NOT masters.
    out = set()
    rostered = set()   # every toon on the bot accounts or the shell account
    for prefix in (ACCOUNT_LIKE, SHELL_ACCOUNT):
        for row in dbsearch("get_online_census",
                            account_like=prefix.rstrip("%"))["rows"]:
            rostered.add(row[0].lower())
    for row in dbsearch("get_online_census",
                        account_like=MASTER_ACCOUNT.rstrip("%"))["rows"]:
        if row[0].lower() in HUMANS:
            out.add(row[0].lower())
    for row in dbsearch("get_guild_members", guild=WATCH_GUILD)["rows"]:
        n = row[0].lower()
        if n not in rostered:
            out.add(n)
    return out


def _char_row(toon):
    """One get_character envelope row for a toon, or None if unknown."""
    rows = dbsearch("get_character", toon=toon)["rows"]
    return rows[0] if rows else None


def _char_online(toon):
    row = _char_row(toon)
    if not row:
        raise RuntimeError("toon %r not found" % toon)
    return row[10] == "1"    # name, account, level, race, class, map, zone, money, totaltime, online


CLASSES = {1: "Warrior", 2: "Paladin", 3: "Hunter", 4: "Rogue", 5: "Priest",
           6: "Death Knight", 7: "Shaman", 8: "Mage", 9: "Warlock", 11: "Druid"}


def _fmt_group_rows(rows):
    """Format get_group_membership rows (groupType, difficulty, raidDifficulty,
    leader, member, level, class, online, subgroup, memberFlags) - the same
    view the old local SQL produced, now via db-search."""
    if not rows:
        return "not in any group"
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
    """Formatted group/raid view of a toon via db-search."""
    if not re.fullmatch(r"[A-Za-z]+", name):
        return "bad toon name %r" % name
    return _fmt_group_rows(dbsearch("get_group_membership", toon=name)["rows"])


def group_members(name):
    """Member names of the group `name` is in ([] if none). Name is validated."""
    if not re.fullmatch(r"[A-Za-z]+", name):
        return []
    return [r[4] for r in dbsearch("get_group_membership", toon=name)["rows"]]


# LLM-facing query menu: name -> (what it returns, how it is answered).
# DB-answerable names run ONE db-search tool; "strategies" is the live read
# and goes through chat-tunnel's inspect (A23). The digest always carries
# queried_at + the staleness caveat (the roadmap's staleness contract).
QUERY_TOOLS = {
    "location": ("last-flushed map/zone from the DB (live position is the tunnel's job)",
                 lambda toon: dbsearch("get_character", toon=toon)),
    "quest": ("active quests (in progress / ready to turn in)",
              lambda toon: dbsearch("get_quest_log", toon=toon)),
    "money": ("gold on hand (last-flushed)",
              lambda toon: dbsearch("get_character", toon=toon)),
    "skills": ("profession levels",
               lambda toon: dbsearch("get_skills", toon=toon)),
    "bags": ("free bag slots",
             lambda toon: dbsearch("get_bag_space", toon=toon)),
    "played": ("playtime and last logout",
               lambda toon: dbsearch("get_leveling_progress",
                                     account=_char_row(toon)[1])),
    "raid": ("the group/raid a toon is in: party/raid + difficulty, leader, members",
             lambda toon: dbsearch("get_group_membership", toon=toon)),
}


def _fmt_rows(tool, toon, resp):
    """Human/LLM-readable digest of one db-search envelope."""
    rows = resp["rows"]
    if tool == "location":
        if not rows:
            return "not found"
        _, _, level, _, _, mapid, zone, money, _, online = rows[0]
        where = ("%s, zone %s (id %s)" % (MAP_NAMES.get(int(mapid), "map %s" % mapid),
                                         ZONE_NAMES.get(int(zone), "unknown"),
                                         zone)) if zone.isdigit() else "unknown"
        return ("last-flushed %s (DB value - live position is the tunnel's "
                "job), %s" % (where,
                              "ONLINE" if online == "1" else "OFFLINE"))
    if tool == "quest":
        if not rows:
            return "no quest log rows"
        labels = {1: "ready to turn in", 3: "in progress", 5: "failed", 6: "rewarded"}
        out = []
        for q, title, status, explored in rows:
            out.append("%s (%s)" % (title or "quest %s" % q,
                                    labels.get(int(status), "status %s" % status)))
        return "; ".join(out)
    if tool == "money":
        if not rows:
            return "not found"
        c = int(rows[0][7] or 0)
        return "%dg %ds %dc" % (c // 10000, (c % 10000) // 100, c % 100)
    if tool == "skills":
        if not rows:
            return "no skills"
        return ", ".join("%s %s/%s" % (n, v, m) for _, n, v, m in rows)
    if tool == "bags":
        if not rows:
            return "not found"
        capacity, used, free = rows[0]
        return ("%s free bag slots (capacity %s, used %s)%s"
                % (free, capacity, used,
                   " - BAGS FULL" if int(free) <= 0 else
                   (" - nearly full" if int(free) <= 10 else "")))
    if tool == "played":
        for r in rows:
            if r[0].lower() == toon.lower():
                when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(int(r[5])))
                        if int(r[5] or 0) else "never")
                return ("level %s, played %.1fh, last logout %s, %s"
                        % (r[1], int(r[4] or 0) / 3600.0, when,
                           "ONLINE" if r[6] == "1" else "OFFLINE"))
        return "not found"
    if tool == "raid":
        return _fmt_group_rows(rows)
    return "; ".join(" ".join(r) for r in rows) or "no rows"


def _probe_strategies(bot):
    """Live strategy read-back through chat-tunnel (A23): inspect(bot,
    "strategy") sends '.playerbots inspect <bot> strategy' and the reply is
    correlated to exactly that command. Returns (ok, text)."""
    if not re.fullmatch(r"[A-Za-z]+", bot):
        return False, "bad name %r" % bot
    try:
        cid = tunnel("inspect", bot=bot, query="strategy")["correlation_id"]
    except Exception as e:
        return False, str(e)
    for _ in range(6):
        time.sleep(2)
        got = tunnel("get_reply", correlation_id=cid)
        if got["reply"]:
            return True, got["reply"]["command"]
        if got["sent"]["status"] == "timeout":
            return False, ("no reply within the window (silent/offline - "
                           "offline truth is db-search)")
    return False, "no reply yet within 12s"


def run_queries(qs, all_bots):
    """Run up to 3 named lookups; return a digest (never raise). DB facts via
    db-search (with queried_at + caveat), live reads via chat-tunnel."""
    lines = []
    for q in qs[:3]:
        if not isinstance(q, dict):
            lines.append("invalid query entry")
            continue
        name = str(q.get("query", "")).strip().lower()
        bot = str(q.get("bot", "")).strip()
        match = next((n for n in all_bots if n.lower() == bot.lower()), None)
        if not match or not re.fullmatch(r"[A-Za-z]+", match):
            lines.append("query %s: %r not in census" % (name or "?", bot))
            continue
        if name == "strategies":
            ok, txt = _probe_strategies(match)
            lines.append(("%s strategies: %s" % (match, txt)) if ok
                         else ("%s strategies probe failed: %s" % (match, txt)))
            continue
        if name == "mats":
            mat = str(q.get("name") or q.get("mat") or "").strip()
            if not mat:
                lines.append("mats query needs a name: {\"query\":\"mats\",\"name\":\"<mat>\"}")
                continue
            try:
                resp = dbsearch("get_mat_spawns", mat=mat)
                if not resp["rows"]:
                    lines.append("mat %r: no spawn sites found (gameobject name)" % mat)
                else:
                    tops = ", ".join("%s (%s spawns, map %s)" % (r[0], r[2], r[1])
                                     for r in resp["rows"][:5])
                    lines.append("mat %r - best teleport locations: %s (queried_at %s; "
                                 "caveat: %s)" % (mat, tops, resp.get("queried_at"),
                                                  resp.get("caveat")))
            except Exception as e:
                lines.append("mat %r lookup failed: %s" % (mat, e))
            continue
        if name == "dest":
            place = str(q.get("name") or q.get("dest") or "").strip()
            if not place:
                lines.append("dest query needs a name: "
                              "{\"query\":\"dest\",\"name\":\"<destination>\"}")
                continue
            try:
                resp = dbsearch("get_game_tele", name=place)
                if not resp["rows"]:
                    lines.append("destination %r: no known teleport location "
                                 "by that name" % place)
                else:
                    r = resp["rows"][0]
                    lines.append("destination %r = %s on map %s: order "
                                 "{\"bot\":\"<name>\",\"cmd\":\"go %s;%s\"} - "
                                 "the bot walks there with pathfinding "
                                 "(GoAction x;y branch, no distance limit) "
                                 "[queried_at %s]"
                                 % (place, r[0], r[1], r[2], r[3],
                                    resp.get("queried_at")))
            except Exception as e:
                lines.append("destination %r lookup failed: %s" % (place, e))
            continue
        if name == "live":
            pos = _live_position(match)
            if pos:
                lines.append("%s live position: %.1f, %.1f, %.1f on map %d (%s) "
                             "- chat-tunnel inspect, real-time"
                             % (match, pos[0], pos[1], pos[2], pos[3],
                                pos[4] or "zone unknown"))
            else:
                lines.append("%s live position: no reply within the window "
                             "(silent/offline - offline truth is db-search)" % match)
            continue
        info = QUERY_TOOLS.get(name)
        if not info:
            lines.append("query %r unknown (have: %s)"
                         % (name, ", ".join(sorted(QUERY_TOOLS) + ["strategies"])))
            continue
        try:
            resp = info[1](match)
            lines.append("%s %s: %s (queried_at %s; caveat: %s)"
                         % (match, name, _fmt_rows(name, match, resp),
                            resp.get("queried_at"), resp.get("caveat")))
        except Exception as e:
            lines.append("%s %s failed: %s" % (match, name, e))
    return "\n".join(lines) if lines else "no valid queries"


def _live_position(bot):
    """Live position via chat-tunnel inspect (A23): returns
    (x, y, z, map, zone) or None on timeout (offline truth is db-search)."""
    try:
        cid = tunnel("inspect", bot=bot, query="position")["correlation_id"]
    except Exception:
        return None
    for _ in range(4):
        time.sleep(2)
        got = tunnel("get_reply", correlation_id=cid)
        if got["reply"]:
            m = re.match(r"([-\d.]+) ([-\d.]+) ([-\d.]+) (\d+) [-\d.]+ \|([^|]*)\|",
                         got["reply"]["command"].strip())
            if m:
                return (float(m.group(1)), float(m.group(2)), float(m.group(3)),
                        int(m.group(4)), m.group(5).strip())
        if got["sent"]["status"] == "timeout":
            return None
    return None


def _tele_canonical(name):
    """The game_tele row's own spelling of a location (live rows are
    concatenated: "BootyBay"), resolved space/case-insensitively, or None."""
    try:
        rows = dbsearch("get_game_tele", name=name)["rows"]
    except Exception:
        return None
    want = name.lower().replace(" ", "")
    for r in rows:
        if r[0].lower().replace(" ", "") == want:
            return r[0]
    return None


# ---- waypoint walker: the dead travel system rebuilt at director level ------
# The mod's own persistent travel (TravelTarget, re-issued every tick by the
# travel strategy) is dead on this build - the catalogs are loaded by an
# orphaned function (A24; RNDBOT long-range confirmed dead live 2026-11-10:
# 'go travel' on a random bot also cleared). A one-shot 'go x;y' MoveTo is
# silent-false past the 3s path search (live-proven: a 300yd order NOPATHed,
# 100yd orders were NORMAL). So the director owns the persistence loop:
# stand the bot down, chain hops inside the pathfinder budget, poll the live
# position, re-issue on stall, restore + report on arrival.
WALK_HOP = float(os.environ.get("DIRECTOR_WALK_HOP", "200"))
WALK_TICK = float(os.environ.get("DIRECTOR_WALK_TICK", "8"))
WALK_STALL = int(os.environ.get("DIRECTOR_WALK_STALL", "3"))
WALK_MAX = float(os.environ.get("DIRECTOR_WALK_MAX", "1800"))
WALK_ARRIVE = 60.0
PATH_DIRECT = float(os.environ.get("DIRECTOR_PATH_DIRECT", "290"))
# ^ within one pathfinder search (~296yd smooth-path cap, per the mod's own
#   MoveFarTo comments) a DIRECT 'go <target>' order pathfinds properly around
#   city geometry (ramps, canals). Hops are only for distances beyond that - a
#   straight-line hop point inside a city lands across canals and walks the
#   bot the wrong way (live 2026-11-10: Stormwind bank -> FM sent Golbez to
#   the flower shop side instead of up the ramp)
_WALKING = {}      # bot -> destination name (one march per bot)


def _next_hop(px, py, tx, ty):
    """Next order toward the target. Within PATH_DIRECT yards the target is
    ordered DIRECTLY (one pathfinder search routes it around ramps/canals);
    beyond that, a straight-line hop capped at WALK_HOP. Returns
    (hx, hy, remaining_distance)."""
    dx, dy = tx - px, ty - py
    dist = (dx * dx + dy * dy) ** 0.5
    if dist <= PATH_DIRECT:
        return tx, ty, dist
    return (px + dx / dist * WALK_HOP, py + dy / dist * WALK_HOP, dist)


def _walk_mission(bot, tx, ty, tmap, name, master, restore=None):
    """March one bot to (tx, ty) on map tmap. Runs in its own thread and
    reports to the master through the normal whisper path."""
    try:
        for c in ("nc -new rpg,-grind,-gather,-follow", "rpg status idle"):
            try:
                send_order(bot, c)
            except Exception:
                pass
            time.sleep(1)
        t0 = time.time()
        stalled = 0
        while time.time() - t0 < WALK_MAX:
            pos = _live_position(bot)
            if not pos:
                stalled += 1
                if stalled >= 2 * WALK_STALL:
                    say(master, "%s went silent on the march to %s - I stopped "
                                "the march; whisper me to retry." % (bot, name))
                    return
                time.sleep(WALK_TICK)
                continue
            if pos[3] != tmap:
                say(master, "%s is on map %d - %s is on map %d; that needs a "
                            "boat or zeppelin, not a march." % (bot, pos[3], name, tmap))
                return
            hx, hy, dist = _next_hop(pos[0], pos[1], tx, ty)
            if dist <= WALK_ARRIVE:
                for c in (restore or ("nc +new rpg,+grind,-follow",)):
                    try:
                        send_order(bot, c)
                    except Exception as e:
                        print("daemon: walk restore failed for %s: %s" % (bot, e))
                say(master, "%s has arrived at %s." % (bot, name))
                print("daemon: walk %s -> %s: arrived (%.0fs)" % (bot, name, time.time() - t0))
                return
            try:
                send_order(bot, "go %.1f;%.1f" % (hx, hy))
            except Exception as e:
                print("daemon: walk hop send failed for %s: %s" % (bot, e))
            time.sleep(WALK_TICK)
            new = _live_position(bot)
            if new and abs(new[0] - pos[0]) + abs(new[1] - pos[1]) > 5:
                stalled = 0
            else:
                stalled += 1
                if stalled >= WALK_STALL:
                    say(master, "%s got stuck on the way to %s - I stopped the "
                                "march at %s. The ground there may be "
                                "unpathable." % (bot, name, pos[4] or pos[:2]))
                    return
        say(master, "%s did not reach %s within %.0f minutes - march timed "
                    "out. I left them standing; whisper me to retry." % (bot, name, WALK_MAX / 60))
    except Exception as e:
        try:
            say(master, "the march of %s to %s failed: %s" % (bot, name, e))
        except Exception:
            pass
    finally:
        _WALKING.pop(bot, None)


def _nearest_flightmasters(pos):
    """The (up to 3) nearest flight masters to a live position, nearest first.
    All three are candidates: a stalled FM leg tries the next one (live
    2026-11-10: the nearest FM sat across PATHFIND_NOPATH terrain)."""
    try:
        rows = dbsearch("get_flightmasters", x="%.1f" % pos[0], y="%.1f" % pos[1],
                        map=str(pos[3]))["rows"]
    except Exception:
        return []
    return [(r[0], float(r[2]), float(r[3]), int(r[1])) for r in rows]


def _sidestep_probe(bot, pos, tx, ty):
    """Cone sampling on a stalled leg (the rpg engine's MoveFarTo trick):
    try hops rotated +-30/60/90 degrees off the direct line and keep the first
    one the bot actually walks (position moves > 5yd). Returns the new position
    or None if every angle is blocked too."""
    import math
    base = math.atan2(ty - pos[1], tx - pos[0])
    for delta in (0.52, -0.52, 1.05, -1.05, 1.57, -1.57):   # ~30/60/90 deg
        a = base + delta
        hx = pos[0] + math.cos(a) * WALK_HOP
        hy = pos[1] + math.sin(a) * WALK_HOP
        try:
            send_order(bot, "go %.1f;%.1f" % (hx, hy))
        except Exception:
            continue
        time.sleep(WALK_TICK)
        new = _live_position(bot)
        if new and abs(new[0] - pos[0]) + abs(new[1] - pos[1]) > 5:
            return new
    return None


def _taxi_list(bot):
    """Ask the bot for its 'taxi ?' destination list (the live route data -
    the taxi graph lives in the DBC stores, not SQL). Returns {n: name}."""
    try:
        tunnel("send_command", bot=bot, cmd="taxi ?")
    except Exception:
        return {}
    time.sleep(6)
    out = {}
    try:
        evs = tunnel("get_recent_events", direction="reply", limit=30)["events"]
    except Exception:
        return out
    for e in evs:
        if (e.get("sender") or '') != bot:
            continue
        m = re.match(r"^(\d+):\s*(.+?)\s*$", (e.get("command") or "").strip())
        if m:
            out[int(m.group(1))] = m.group(2)
    return out


def _travel_mission(bot, tx, ty, tmap, name, master, restore=None):
    """Flight-aware march: hop to the nearest flight master, fly the long leg
    via the stock 'taxi ?' / 'taxi <n>' commands (the bot's own live list is
    the route data), then hop the last mile. Reports to the master."""
    def _walk_leg(fx, fy, arrive, label):
        """Hop-chain to (fx, fy); True on arrival, False on stall/timeout."""
        stalled = 0
        while True:
            pos = _live_position(bot)
            if not pos:
                return "silent"
            if pos[3] != tmap:
                return "map"
            hx, hy, dist = _next_hop(pos[0], pos[1], fx, fy)
            if dist <= arrive:
                return True
            try:
                send_order(bot, "go %.1f;%.1f" % (hx, hy))
            except Exception as e:
                print("daemon: travel hop failed (%s): %s" % (bot, e))
            time.sleep(WALK_TICK)
            new = _live_position(bot)
            if new and abs(new[0] - pos[0]) + abs(new[1] - pos[1]) > 5:
                stalled = 0
            else:
                stalled += 1
                if stalled >= WALK_STALL:
                    # cone sampling (the rpg engine's MoveFarTo trick): on a
                    # blocked line, try rotated hops and keep the first one
                    # the bot actually walks; None = every angle blocked too
                    if _sidestep_probe(bot, pos, fx, fy):
                        stalled = 0
                        continue
                    return False

    try:
        for c in ("nc -new rpg,-grind,-gather,-follow", "rpg status idle"):
            try:
                send_order(bot, c)
            except Exception:
                pass
            time.sleep(1)
        t0 = time.time()
        pos = _live_position(bot)
        if pos and pos[3] != tmap:
            say(master, "%s is on map %d - %s is on map %d; that needs a "
                        "boat or zeppelin, not a march." % (bot, pos[3], name, tmap))
            return
        flew = False
        if pos:
            for fm in _nearest_flightmasters(pos):
                if time.time() - t0 > WALK_MAX:
                    break
                say(master, "%s is heading to the flight master at %s, then on "
                            "to %s." % (bot, fm[0], name))
                leg = _walk_leg(fm[1], fm[2], 30.0, "to the flight master")
                if leg is True:
                    taxis = _taxi_list(bot)
                    want = name.lower().replace(" ", "")
                    pick = next(((n, t) for n, t in sorted(taxis.items())
                                 if want in t.lower().replace(" ", "")), None)
                    if pick:
                        try:
                            send_order(bot, "taxi %d" % pick[0])
                            say(master, "%s is flying to %s." % (bot, pick[1]))
                            print("daemon: travel %s: taxi %d -> %s"
                                  % (bot, pick[0], pick[1]))
                        except Exception as e:
                            print("daemon: taxi send failed (%s): %s" % (bot, e))
                            pick = None
                    if pick:
                        # wait for the flight to land: stable position, moved far
                        last = None
                        while time.time() - t0 < WALK_MAX:
                            time.sleep(15)
                            p = _live_position(bot)
                            if not p:
                                continue
                            if last and abs(p[0] - last[0]) + abs(p[1] - last[1]) < 10:
                                if abs(p[0] - fm[1]) + abs(p[1] - fm[2]) > 500:
                                    flew = True
                                break
                            last = p
                elif leg is not True and leg != "map":
                    print("daemon: travel %s: FM %s unreachable, trying the "
                          "next one" % (bot, fm[0]))
                    continue
                break
        leg = _walk_leg(tx, ty, WALK_ARRIVE, "last mile")
        if leg is True:
            for c in (restore or ("nc +new rpg,+grind,-follow",)):
                try:
                    send_order(bot, c)
                except Exception as e:
                    print("daemon: travel restore failed for %s: %s" % (bot, e))
            how = "flew part of the way and marched the rest" if flew else "marched"
            say(master, "%s has arrived at %s (%s)." % (bot, name, how))
            print("daemon: travel %s -> %s: arrived (%.0fs)" % (bot, name, time.time() - t0))
        elif leg == "map":
            say(master, "%s ended up on the wrong map on the way to %s." % (bot, name))
        elif leg == "silent":
            say(master, "%s went silent on the way to %s - I stopped the "
                        "march; whisper me to retry." % (bot, name))
        else:
            say(master, "%s got stuck on the way to %s - I stopped the "
                        "march. The ground there may be unpathable." % (bot, name))
    except Exception as e:
        try:
            say(master, "the journey of %s to %s failed: %s" % (bot, name, e))
        except Exception:
            pass
    finally:
        _WALKING.pop(bot, None)


def _start_walk(o, master):
    """Launch (or refuse) the march for a validated walk/travel order.
    'travel' is flight-aware (hop -> taxi -> last mile); 'walk' is ground."""
    (tx, ty, tmap), name = o["_walk"]
    if o["bot"] in _WALKING:
        return "%s is already marching to %s" % (o["bot"], _WALKING[o["bot"]])
    _WALKING[o["bot"]] = name
    mission = _travel_mission if o.get("_travel") else _walk_mission
    threading.Thread(target=mission,
                     args=(o["bot"], tx, ty, tmap, name, master), daemon=True).start()
    return None


def _tele_coords(name):
    """Coordinates of a game_tele location via db-search (exact name match,
    space-insensitive - live game_tele names are concatenated: "BootyBay"),
    or None. The only legal '.tele name' destinations come from that table."""
    try:
        rows = dbsearch("get_game_tele", name=name)["rows"]
    except Exception:
        return None
    want = name.lower().replace(" ", "")
    for r in rows:
        if r[0].lower().replace(" ", "") == want:
            return (float(r[2]), float(r[3]), int(r[1]))   # x, y, map
    return None


def _bot_faction(bot):
    """Faction of a toon from its race (db-search get_character), or None."""
    try:
        row = _char_row(bot)
        return RACE_FACTION.get(int(row[3])) if row else None
    except Exception:
        return None


def cast_jobs(bot):
    """Profession codes of a toon from its DB skills (db-search get_skills)."""
    jobs = []
    for r in dbsearch("get_skills", toon=bot)["rows"]:
        code = SKILLS.get(int(r[0]))
        if code:
            jobs.append(code)
    return jobs


def cast_list(name, jobs):
    """Exact spell names Leo may tell this toon to cast (overrides + by job)."""
    spells = list(CAST_OVERRIDES.get(name, ()))
    for j in (jobs or []):
        spells.extend(CAST_SPELLS.get(j, ()))
    return spells


def roster_text(bots):
    if not bots:
        return "NONE on the bot accounts"
    parts = []
    for n, b in bots.items():
        parts.append("%s(acc %s, lvl %s, %s) %s"
                     % (n, b["account"].lower(), b["level"],
                        MAP_NAMES.get(b["map"], "map %s" % b["map"]),
                        "ONLINE" if b["online"] else "OFFLINE"))
    return "; ".join(parts)


def llm(messages):
    """One Ollama chat call. Output carries a runaway brake (num_predict):
    live 2026-11-10 one turn spiralled to 16,938 generated tokens at 144 t/s
    and ate the whole 120s timeout, while a healthy trace is ~940 tokens.
    Thinking stays ON by default (DIRECTOR_THINKING=0 opts out via
    "think": false); if the server rejects the field, retry once without it."""
    # keep_alive keeps the model resident on the GPU between whispers -
    # default Ollama unloads after 5m idle, and the reload is most of the
    # 20s first-reply latency. DIRECTOR_KEEP_ALIVE=-1 = never unload.
    _ka = os.environ.get("DIRECTOR_KEEP_ALIVE", "60m")
    try:
        keep_alive = int(_ka)          # "-1"/"3600" -> JSON number (Ollama: negative = forever)
    except ValueError:
        keep_alive = _ka               # "60m"/"24h" -> duration string
    opts = {"temperature": 0.2}
    if MAX_TOKENS > 0:
        opts["num_predict"] = MAX_TOKENS
    body = {"model": MODEL, "messages": messages, "stream": False,
            "options": opts, "keep_alive": keep_alive}
    if not THINKING:
        body["think"] = False
    try:
        return _ollama_chat(body)
    except urllib.error.HTTPError as e:
        detail = b""
        try:
            detail = e.read()
        except Exception:
            pass
        if "think" in body and b"think" in detail.lower():
            print("daemon: ollama rejected the think flag (%s) - retrying without it"
                  % detail[:120])
            del body["think"]
            return _ollama_chat(body)
        raise


def _ollama_chat(body):
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
        resp = json.loads(r.read())
    m = resp["message"]
    if DEBUG:
        th = (m.get("thinking") or "").strip()
        if th:
            print("daemon: thinking: %s" % th[:600].replace("\n", " "))
    content = (m.get("content") or "").strip()
    if content:
        return content
    # thinking models sometimes put the whole answer in the thinking channel
    # and emit no content (live 2026-11-10: empty content, done_reason=stop,
    # while the CoT held the planned JSON). Recover the fenced JSON instead of
    # reporting an empty reply to the master.
    th = m.get("thinking") or ""
    fence = re.search(r"```json\s*(.*?)\s*```", th, re.S)
    if fence:
        print("daemon: empty content but the thinking held the answer - "
              "extracting the fenced JSON")
        return fence.group(1)
    if resp.get("done_reason") == "length":
        print("daemon: llm hit the %s-token brake mid-generation "
              "(done_reason=length) - a spiral, capped" % MAX_TOKENS)
    return ""


def llm_retry(messages, notify=None):
    """llm() with a single retry on empty output - some models occasionally
    return '' (seen live 2026-11-05); an empty reply must not look like silence.
    `notify` (daemon whisper turns only) fires once if the whole turn exceeds
    STILL_WORKING seconds, so a slow model is never mistaken for a hang."""
    done = threading.Event()
    if notify and STILL_WORKING > 0:
        def _nudge():
            if not done.wait(STILL_WORKING):
                try:
                    notify()
                except Exception as e:
                    print("daemon: still-working nudge failed: %s" % e)
        threading.Thread(target=_nudge, daemon=True).start()
    try:
        reply = llm(messages)
        if not reply.strip():
            print("daemon: empty LLM reply from %s - retrying once" % MODEL)
            messages.append({"role": "user",
                             "content": "Output ONLY the JSON object now. Nothing else."})
            reply = llm(messages)
            messages.pop()
        return reply
    finally:
        done.set()


class _StampedStdout:
    """daemon.log lines carry no timestamps of their own - stamp every
    non-empty write so a hung or slow turn is visible in the log timeline
    (live 2026-11-10: grep for a wall-clock window matched nothing)."""

    def __init__(self, out):
        self._out = out

    def write(self, s):
        if s.strip():
            s = time.strftime("%Y-%m-%d %H:%M:%S ") + s
        return self._out.write(s)

    def flush(self):
        self._out.flush()


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
    """Display/audit form of an order (the tunnel applies the same split when
    it actually sends: master cmds go to the server, others are /w to the bot)."""
    if is_master_cmd(cmd):
        return ".playerbots %s %s" % (cmd, bot)
    if is_group_cmd(cmd):
        return ".group remove %s" % bot
    low = cmd.strip().lower()
    if low == "boat" or low.startswith("boat "):
        return ".tele name %s %s" % (bot, cmd.strip()[4:].strip())
    if low == "carriage" or low.startswith("carriage "):
        return ".tele name %s %s" % (bot, cmd.strip()[8:].strip())
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
    if low == "go travel" or low.startswith("go travel "):
        return ("SKIP %s: 'go travel' is DEAD on this build (the travel catalogs "
                "never load - live 31/31 FAIL, A24); order "
                "'travel <destination>' instead - the system runs the journey"
                % bot)
    if low.startswith("go ") and ";" in cmd:
        # go x;y legs are MISSION-INTERNAL only (the travel mission issues them
        # itself); a bare one from the LLM is a one-shot MoveTo that silent-
        # fails past the 3s path search (live 2026-11-10) - refuse it so the
        # brain must use the travel/walk orders
        return ("SKIP %s: raw 'go x;y' legs are mission-internal; order "
                "'travel <destination>' (flight-aware) or 'walk <destination>' "
                "(ground) instead" % bot)
    if bot.strip().lower() in HANDPLAY and not cmd.startswith("bot remove"):
        return ("SKIP %s: hand-play toon, never commanded or brought "
                "online as a bot (DIRECTOR_HANDPLAY)" % bot)
    if low == "group remove":
        # may only ever hit a CURRENT member of Leo's own group (live DB check
        # via db-search), never Leo himself. Bot members are rewritten to the
        # plain "leave" whisper (LeaveGroupAction - works with SEC_PLAYER, no
        # GM needed); human members go out as '.group remove <name>' from Leo's
        # session, which needs Leo's account to hold rbac_perm_command_group_remove
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
    if low == "boat" or low.startswith("boat "):
        dest = cmd.strip()[4:].strip()
        routes = [r for r in TRAVEL_ROUTES if r[1].lower() == dest.lower()]
        if not routes:
            return ("SKIP %s: %r is not an arrival harbor of any route (routes: %s)"
                    % (bot, dest, "; ".join("%s->%s" % (r[0], r[1]) for r in TRAVEL_ROUTES)))
        if bot not in bots:
            return "SKIP %s: not online (census)" % bot
        faction = _bot_faction(bot)
        usable = [r for r in routes if r[3] == "neutral" or r[3] == faction]
        if not usable:
            return ("SKIP %s: no %s route to %s (zeppelins are Horde infrastructure)"
                    % (bot, faction or "unknown-faction", dest))
        # the bot must actually be at a departure dock - live position check
        pos = _live_position(bot)
        if not pos:
            return ("SKIP boat %s: could not read a live position (bot offline, "
                    "AI-dead, or the tunnel timed out)" % bot)
        for r in usable:
            dock = _tele_coords(r[0])
            if dock and dock[2] == pos[3]:
                if (pos[0] - dock[0]) ** 2 + (pos[1] - dock[1]) ** 2 < BOAT_DOCK_RADIUS ** 2:
                    order["_route"] = (r[0], r[1], r[2])
                    # canonicalize the destination to the game_tele row's
                    # own spelling (live rows are concatenated: MenethilHarbor)
                    # so the '.tele name' line names a location the core knows
                    canon = _tele_canonical(r[1])
                    if canon:
                        order["cmd"] = "boat %s" % canon
                    return None
        return ("SKIP boat %s: not at a departure harbor (needs to be within %d yd "
                "of %s; live position says %s)"
                % (bot, BOAT_DOCK_RADIUS, "/".join(r[0] for r in usable), pos[4] or pos[:3]))
    if low == "carriage" or low.startswith("carriage "):
        dest = cmd.strip()[8:].strip()
        if not CARRIAGE:
            return ("SKIP %s: carriage is disabled (DIRECTOR_CARRIAGE=0 - legs to "
                    "the dock are walks)" % bot)
        if dest.lower() not in [h.lower() for r in TRAVEL_ROUTES for h in (r[0], r[1])]:
            return ("SKIP %s: %r is not a route harbor" % (bot, dest))
        if bot not in bots:
            return "SKIP %s: not online (census)" % bot
        if _tele_coords(dest) is None:
            return "SKIP %s: %r is not a real teleport location (db-search)" % (bot, dest)
        canon = _tele_canonical(dest) or dest   # the row's own spelling
        order["cmd"] = "carriage %s" % canon
        order["_route"] = (dest, dest, "carriage")
        return None
    if low == "walk" or low.startswith("walk ") or low == "travel" or low.startswith("travel "):
        dest = cmd.strip().split(" ", 1)[1].strip() if " " in cmd.strip() else ""
        if not dest:
            return "SKIP %s: walk/travel needs a destination name" % bot
        coords = _tele_coords(dest)
        if coords is None:
            return ("SKIP %s: %r is not a known location (db-search game_tele)"
                    % (bot, dest))
        if bot not in bots:
            return "SKIP %s: not online (census)" % bot
        if bot in _WALKING:
            return "SKIP %s: already marching to %s" % (bot, _WALKING[bot])
        if low.startswith("travel"):
            order["_travel"] = True
        order["_walk"] = (coords, _tele_canonical(dest) or dest)
        return None
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
        try:
            allowed = [s.lower() for s in cast_list(bot, cast_jobs(bot))]
        except Exception as e:
            return "SKIP %s: cast refused (skills lookup failed: %s)" % (bot, e)
        if spell not in allowed and m:
            spell = m.group(1).strip().lower()
        if spell not in allowed:
            return "SKIP %s: cast spell not allowed for %s: %r" % (bot, bot, spell)
    return None


def send_order(bot, cmd):
    """The ONE send path: chat-tunnel send_command (paced, correlated, allowed
    roster only). Returns the tunnel's result; raises on refusal.
    Boat/carriage orders ride the raw-dot passthrough as a GM '.tele name';
    the audit trail records them as what they ARE (a boat/zeppelin leg or a
    carriage), never as a generic send."""
    low = cmd.strip().lower()
    mark = "SENT"
    if low.startswith("boat "):
        mark = "BOAT"
    elif low.startswith("carriage "):
        mark = "CARRIAGE"
    if _turn_aborted():
        print("daemon: send dropped - turn was aborted: %r" % ((bot, cmd),))
        return {"line": "(aborted turn - not sent)"}
    if not order_line(bot, cmd).startswith("."):
        cmd = cmd.replace("|", ";")   # WoW escape char - same garble as say()
    res = tunnel("send_command", bot=bot,
                cmd=(cmd if order_line(bot, cmd).startswith("/w")
                     else order_line(bot, cmd)))
    _audit(mark, res["line"])
    return res


def say(recipient, msg):
    """Whisper a toon through the tunnel (chunked to 200 chars). Guild answers
    ride the same whisper path: the tunnel's send contract is /w only
    (send-side /g is unverified on this client)."""
    msg = (msg or "").strip()
    if not msg:
        return
    msg = msg.replace("|", ";")   # | is WoW's escape char (|c...|r): a raw
    # pipe in an outgoing whisper garbles the line so the client drops it
    # (live 2026-11-10: every short whisper delivered, both replies containing
    # " | executed: " vanished - tunnel verdicts garbled/not_found)
    if _turn_aborted():
        print("daemon: whisper dropped - turn was aborted: %r" % ((recipient, msg[:80]),))
        return
    for i in range(0, len(msg), 200):
        send_order(recipient, msg[i:i + 200])


def _llm_fail_say(sender, e, channel):
    """Whisper the master why the LLM turn failed (timeout gets the GPU hint).
    One place, so both daemon call sites stay identical."""
    if "timed out" in str(e).lower():
        say(sender, "llm error at %s: %s did not answer within %ss (cold model or busy GPU - check `ollama ps`); try again"
            % (_STAGE["name"], MODEL, LLM_TIMEOUT))
    else:
        say(sender, "llm error at %s: %s" % (_STAGE["name"], e))


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


def execute(orders, bots, pace, dry, master=None):
    orders = expand_orders(orders, census(online_only=False))
    plan = []
    for o in orders:
        err = validate(o, bots)
        if err:
            print("  " + err)
            continue
        plan.append(o)
    if not plan:
        print("nothing executable")
        return None
    for o in plan:
        print("  -> " + order_line(str(o["bot"]), str(o["cmd"])))
    if dry:
        print("(dry-run: nothing sent; type 'go' to execute, 'skip' to discard)")
        return plan
    for o in plan:
        if "_walk" in o:
            err2 = _start_walk(o, master or o["bot"])
            print("  " + (err2 or "marching: %s -> %s" % (o["bot"], o["_walk"][1])))
            continue
        try:
            res = send_order(str(o["bot"]), str(o["cmd"]))
            print("  sent: " + res["line"])
        except Exception as e:
            print("  send FAILED for %s: %s" % (o["bot"], e))
        if pace:
            time.sleep(pace)
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

BOTFB = []      # ring of recent bot error whispers, shown in the roster system message
BOTFB_SEQ = [0]  # monotonic counter: lets the presence thread detect NEW errors

# ---- turn watchdog: stage tracking + hard abort -------------------------
# A whisper-triggered turn has MANY stages (roster -> llm -> queries -> llm
# -> sends -> reply) and any of them can stall. The old nudge only covered
# the LLM calls (live 2026-11-10: 75s of silence between the nudge and the
# 120s fail whisper). The worker thread updates _STAGE as it goes; the
# listener-side watchdog whispers progress naming the stage + elapsed time
# every STILL_WORKING seconds and abandons the turn at TURN_MAX.
_STAGE = {"name": "idle"}
_TOK = [0]           # turn token counter
_ABORTED = set()     # tokens of abandoned turns
_TL = threading.local()   # worker thread sets .token; say/send_order check it


def _stage(name):
    _STAGE["name"] = name


def _turn_aborted():
    tok = getattr(_TL, "token", None)
    return tok is not None and tok in _ABORTED


def _run_turn(worker, sender):
    """Run one turn's worker thread under the watchdog: repeating progress
    whispers (stage + elapsed) every STILL_WORKING, hard abort at TURN_MAX.
    The worker is a daemon thread with finite timeouts everywhere, so an
    abandoned turn dies on its own; its late whispers/sends are dropped by
    the token gate in say()/send_order()."""
    _TOK[0] += 1
    tok = _TOK[0]
    _ABORTED.discard(tok)

    def _wrap():
        _TL.token = tok
        try:
            worker()
        except Exception as e:
            print("daemon: turn crashed at stage %r: %s" % (_STAGE["name"], e))
            try:
                say(sender, "error at %s: %s" % (_STAGE["name"], e))
            except Exception:
                pass

    w = threading.Thread(target=_wrap, daemon=True)
    w.start()
    t0 = time.time()
    nxt = STILL_WORKING if STILL_WORKING > 0 else None
    while w.is_alive():
        w.join(1.0)
        if not w.is_alive():
            break
        el = time.time() - t0
        if TURN_MAX > 0 and el >= TURN_MAX:
            _ABORTED.add(tok)
            print("daemon: turn aborted - stuck at %r for %.0fs" % (_STAGE["name"], el))
            try:
                say(sender, "I'm stuck at '%s' (%.0fs) - dropping this one; "
                            "whisper me again." % (_STAGE["name"], el))
            except Exception:
                pass
            _stage("idle")
            return
        if nxt is not None and el >= nxt:
            try:
                say(sender, "still on it: %s (%.0fs)..." % (_STAGE["name"], el))
            except Exception:
                pass
            nxt += STILL_WORKING
    _stage("idle")


def _note_bot_error(sender, text):
    BOTFB.append("%s: %s" % (sender, text[:200]))
    del BOTFB[:-10]
    BOTFB_SEQ[0] += 1


_sender_acct_cache = {}  # name -> (checked_at, account name or None)

def _sender_account(name):
    """Account of a toon via db-search get_character (cached 5 min); None on
    error. Event senders are [A-Za-z]+ names, re-validated here."""
    if not re.fullmatch(r"[A-Za-z]+", name):
        return None
    now = time.time()
    key = name.lower()
    hit = _sender_acct_cache.get(key)
    if hit and now - hit[0] < 300:
        return hit[1]
    try:
        row = _char_row(name)
        acct = row[1] if row else None
    except Exception:
        acct = None
    _sender_acct_cache[key] = (now, acct)
    return acct


def _is_bot_sender(name):
    """True if the sender's ACCOUNT is a bot account or Leo's shell account.
    Structural and DB-only (via db-search)."""
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
           "RITUAL_ACCOUNTS": "I_WAS_HERE2ND I_WAS_HERE3RD I_WAS_HERE4TH I_WAS_HERE5TH",
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
        leo = 1 if _char_online(LEO) else 0
    except Exception:
        leo = 1
    group_on = 0
    for n in str(cfg["GROUP"]).split():
        if not re.fullmatch(r"[A-Za-z]+", n):
            continue
        try:
            if _char_online(n):
                group_on += 1
        except Exception:
            pass
    hour = time.localtime().tm_hour
    wanted = 1 if (int(cfg["ON_H"]) <= hour < int(cfg["OFF_H"])) or group_on else 0
    return leo, group_on, wanted, _svc_state()

def _hb_condition(facts, state):
    """What (if anything) needs the LLM's attention this tick.
    ("", False) = nothing; (text, True) = state change (fires immediately);
    (text, False) = standing alarm (re-fires rate-limited)."""
    leo, group_on, wanted, svc, new_errors = facts
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
    if new_errors:
        # a bot errored since the last tick - the LLM decides what to do about
        # it (revive/remaster/ignore); this thread only reports the fact
        return ("%d new bot error whisper(s) surfaced (see below)" % new_errors,
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
    leo, group_on, wanted, svc, new_errors = facts
    members = []
    try:
        members = group_members(LEO)
    except Exception:
        pass
    lines = ["PRESENCE HEARTBEAT - attention needed: %s" % cond,
             "Leo online flag: %d" % leo,
             "leo service: %s" % svc,
             "hour policy wanted-online: %s (hand-play group online: %d)" % (wanted, group_on)]
    if members:
        lines.append("Leo's current group members: %s" % ", ".join(members))
    if BOTFB:
        lines.append("Recent bot error whispers: %s" % "; ".join(BOTFB[-3:]))
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
    state = {"prev_sig": None, "off_streak": 0, "last_err_seq": BOTFB_SEQ[0]}
    print("presence: heartbeat up - facts only, the LLM decides (policy hours "
          "%s-%s, group '%s')" % (cfg["ON_H"], cfg["OFF_H"], cfg["GROUP"]))
    last_hb = 0.0
    while True:
        try:
            leo, group_on, wanted, svc = _presence_facts(cfg)
            new_errors = max(0, BOTFB_SEQ[0] - state["last_err_seq"])
            if new_errors:
                state["last_err_seq"] = BOTFB_SEQ[0]
            facts = (leo, group_on, wanted, svc, new_errors)
            cond, changed = _hb_condition(facts, state)
            if cond and (changed or time.time() - last_hb >= HB_MIN_INTERVAL):
                last_hb = time.time()
                _hb_turn(facts, cond, cfg)
        except Exception as e:
            print("presence: poll error: %s" % e)
        time.sleep(interval)

def _turn(sender, channel, text, convs, pending):
    """One whisper-triggered LLM turn (roster -> llm -> queries -> llm ->
    orders -> reply). Runs inside _run_turn's worker thread: _STAGE names
    the current step for the watchdog's progress/abort whispers."""
    conv = convs.setdefault(sender, [])
    # rebuild each turn: persona first, last 10 history turns, then a
    # FRESH full roster (online+offline) as its own system message
    # right before the user text so the model cannot miss it
    history = [m2 for m2 in conv if m2["role"] != "system"][-10:]
    conv[:] = [{"role": "system", "content": GENERAL}]
    conv.extend(history)
    conv.append({"role": "user", "content": text})
    _stage("roster")
    try:
        t0 = time.time()
        roster_msg = ("Live roster (name, account, level, map, status): "
                      + roster_text(census(online_only=False)))
        try:
            roster_msg += "\nLeo's group: " + group_text(LEO)
        except Exception as e:
            roster_msg += "\nLeo's group: lookup failed (%s)" % e
        if BOTFB:
            roster_msg += "\nRecent bot error whispers: " + "; ".join(BOTFB[-5:])
        conv.insert(len(conv) - 1, {"role": "system", "content": roster_msg})
        _stage("thinking")
        reply = llm_retry(_with_fewshot(conv))
        print("daemon: llm answered in %.1fs" % (time.time() - t0))
        if DEBUG:
            print("daemon: raw llm reply: %r" % reply[:800])
    except Exception as e:
        conv.pop()
        _llm_fail_say(sender, e, channel)
        return
    conv.append({"role": "assistant", "content": reply})
    obj = parse_orders(reply)
    qs = [q for q in (obj.get("queries") or []) if isinstance(q, dict)][:3]
    if qs:
        # one read-only DB round: results go back to the LLM, whose
        # NEXT reply (final text + orders) is the one acted on
        say(sender, "one moment, checking the ledgers.")
        _stage("queries")
        results = run_queries(qs, census(online_only=False))
        conv.append({"role": "system", "content":
                     "DB query results (read-only):\n" + results +
                     "\nGive your final reply now, with orders if any. "
                     "(Further queries this turn are ignored.)"})
        _stage("thinking (final)")
        try:
            reply = llm_retry(_with_fewshot(conv))
            if DEBUG:
                print("daemon: raw llm reply (post-queries): %r" % reply[:800])
        except Exception as e:
            conv.pop()
            _llm_fail_say(sender, e, channel)
            return
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
        _stage("orders")
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
                    if "_walk" in o:
                        err2 = _start_walk(o, sender)
                        msg += (" | " + err2) if err2 else \
                               " | marching: %s is on the way to %s" % (o["bot"], o["_walk"][1])
                        continue
                    try:
                        res = send_order(str(o["bot"]), str(o["cmd"]))
                        msg += " | executed: " + res["line"]
                    except Exception as e:
                        msg += " | send FAILED: %s" % e
    print("daemon -> %s: %s (turn %.1fs)" % (sender, msg, time.time() - t0))
    _stage("reply")
    say(sender, msg)


def daemon():
    # single-instance guard: a second daemon would double-answer every whisper
    lock_fd = os.open(os.path.join(HERE, "daemon.lock"), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("another director.py daemon is already running (daemon.lock); not starting")
    sys.stdout = _StampedStdout(sys.stdout)
    authed = masters()
    print("daemon: Leo is listening (facts: db-search on %s; sends/replies: "
          "chat-tunnel on %s). masters: %s"
          % (DBSEARCH_SOCK, TUNNEL_URL, ", ".join(sorted(authed))))
    if CONFIRM:
        print("daemon: orders are HELD until you whisper 'go' (DIRECTOR_CONFIRM=0 = auto-execute).")
    else:
        print("daemon: WARNING - orders execute IMMEDIATELY (DIRECTOR_CONFIRM=0); "
              "only the validate() rails apply.")
    print("whisper Leo, or say his name in guild chat. Ctrl-C to stop.")
    threading.Thread(target=presence_loop, daemon=True).start()
    pending = {}       # sender -> [order dicts]  (raw {"bot","cmd"} dicts so the
                       # confirm path can re-validate against a FRESH census)
    convs = {}         # sender -> [messages]
    # chat-tunnel owns /tmp/leo.log; the daemon consumes its event feed with
    # the event id as a monotonic cursor. Start at the current tail (the old
    # `tail -n 0` semantics): skip the backlog, then long-poll for new lines.
    try:
        cursor = tunnel("get_events_since", after_id=0, limit=1000)["last_id"]
    except Exception as e:
        sys.exit("daemon: chat-tunnel not reachable at %s (%s) - it must be up "
                 "before the daemon" % (TUNNEL_URL, e))
    while True:
        try:
            batch = tunnel("wait_for_events", after_id=cursor, timeout=25,
                           limit=200, http_timeout=SVC_TIMEOUT + 30)
        except Exception as e:
            print("daemon: tunnel read error: %s - retrying in 2s" % e)
            time.sleep(2)
            continue
        cursor = batch["last_id"]
        for ev in batch["events"]:
            sender = None
            try:
                if ev["direction"] != "reply":
                    continue          # our own sends (sent events) - not input
                sender, channel, text = ev["sender"], ev["channel"], (ev["command"] or "").strip()
                if channel not in ("whisper", "guild"):
                    continue
                if sender in ("Leo", "System") or not text:
                    continue
                if sender.lower() not in authed:
                    print("daemon: [%s] (%s): dropped - not an authed master" % (sender, channel))
                    continue
                if _is_bot_sender(sender):
                    # bots whisper their master (Leo) their "Selling/Using X"
                    # reports and RP chatter; answering them makes the two LLM
                    # layers talk to each other and starved Ollama (seen live
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
                    say(sender, random.choice(ACKS))
                low = text.lower().strip()
                if CONFIRM and low in GO_WORDS and sender in pending:
                    plan = pending.pop(sender)
                    sent = execute(plan, census(), 1.0, dry=False, master=sender)
                    if sent:
                        say(sender, "executed: " + "; ".join(order_line(o["bot"], o["cmd"]) for o in sent))
                    else:
                        say(sender, "plan no longer executable - discarded.")
                    continue
                if CONFIRM and low in SKIP_WORDS and sender in pending:
                    pending.pop(sender)
                    say(sender, "plan discarded.")
                    continue
                _run_turn(lambda: _turn(sender, channel, text, convs, pending),
                          sender)
            except Exception as e:
                print("daemon event error: %s" % e)
                if sender:
                    # never drop a turn silently: the master must know we choked
                    try:
                        say(sender, "error: %s" % e)
                    except Exception:
                        pass


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
