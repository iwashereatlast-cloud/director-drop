#!/usr/bin/env python3
"""db-search - premade read-only query catalog for the director LLM (service 2/4).

The LLM never writes SQL. It picks a typed tool from the catalog below, the
service runs ONE fixed, pre-reviewed SELECT under the SELECT-only MySQL
identity (~/.my.cnf-director pattern, same as director.py), with a per-query
timeout, and returns a JSON envelope that always carries `queried_at` and a
per-query staleness `caveat` (the roadmap's staleness contract, A3/A5).

Interface: JSON over a local-only socket - one request per line, one response
per line. Default AF_UNIX /tmp/db-search.sock; set DBSEARCH_ADDR=host:port to
listen on 127.0.0.1 instead (never expose it - the socket IS the allowlist).

Request:  {"tool": "<name>", "params": {...}, "id": "optional-correlation"}
Response: {"ok": true, "tool": ..., "id": ..., "queried_at": ..., "caveat": ...,
           "rows": [...], "row_count": N, "duration_ms": M}
          {"ok": false, "tool": ..., "id": ..., "error": "...", "code": "..."}

Adapters:
  mysql    the real one (serverpc): mysql CLI with --defaults-file, SELECT-only
           grants, subprocess timeout = the tool's per-query timeout.
  sqlite   the proof harness one: same fixed SQL against a SQLite fixture DB
           (fixtures/fixture.sql), so every tool is proofable with no
           worldserver running.

Modes:
  python3 dbsearch.py serve                run the socket service (DBSEARCH_ADAPTER)
  python3 dbsearch.py query <tool> [k=v..]   one-shot query, print the envelope
  python3 dbsearch.py catalog             print the tool catalog as JSON
"""
import json, os, re, socket, socketserver, sqlite3, subprocess, sys, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ADAPTER = os.environ.get("DBSEARCH_ADAPTER", "mysql")
SOCKET_PATH = os.environ.get("DBSEARCH_SOCKET", "/tmp/db-search.sock")
ADDR = os.environ.get("DBSEARCH_ADDR", "")          # host:port -> TCP on 127.0.0.1 only
MYCNF = os.path.expanduser(os.environ.get("DBSEARCH_MYCNF", "~/.my.cnf-director"))
FIXTURE_DB = os.environ.get("DBSEARCH_FIXTURE_DB", os.path.join(HERE, "fixtures", "fixture.db"))
DEFAULT_TIMEOUT = float(os.environ.get("DBSEARCH_TIMEOUT", "10"))

# ------------------------------------------------------------- toon scope ----
# Subject-level gate (the function allowlist bounds HOW the LLM can call;
# this bounds WHO the queries can be about): a toon is queryable only if it
# is on the watched account family OR a member of the watched guild (the
# census watch rule - covers Leo and G1 via guild membership, the bots via
# accounts). Guild queries are limited to the watched guild; account-prefix
# params must be a prefix of the configured family. Set DBSEARCH_SCOPE=0 to
# disable (e.g. for one-off admin lookups).
SCOPE = os.environ.get("DBSEARCH_SCOPE", "1") not in ("0", "no", "off")
WATCH_GUILD = os.environ.get("DBSEARCH_WATCH_GUILD", "Special FXc")
ACCOUNT_PREFIX = os.environ.get("DBSEARCH_ACCOUNT_PREFIX", "i_was_here")
SHELL_ACCOUNT = os.environ.get("DBSEARCH_SHELL_ACCOUNT", "i_was_cmd")

def _sqlq(s):
    return s.replace("'", "''")

# One fixed SELECT (same single-SELECT audit as the catalog).
SCOPE_SQL = ("SELECT COUNT(*) FROM acore_characters.characters c "
             "JOIN acore_auth.account a ON a.id = c.account "
             "LEFT JOIN acore_characters.guild_member gm ON gm.guid = c.guid "
             "WHERE c.name = '{toon}' "
             "AND (a.username LIKE '{prefix}%' "
             "OR gm.guildid = (SELECT guildid FROM acore_characters.guild "
             "WHERE name = '{guild}'))")

def assert_scope_toon(adapter, toon):
    if not SCOPE:
        return
    sql = (SCOPE_SQL.replace("{toon}", _sqlq(toon))
                     .replace("{prefix}", _sqlq(ACCOUNT_PREFIX))
                     .replace("{guild}", _sqlq(WATCH_GUILD)))
    if adapter.run(sql, DEFAULT_TIMEOUT).strip() != "1":
        raise ToolError("toon %r is out of scope: not on the %s%% account family "
                        "and not a member of the guild '%s' (DBSEARCH_SCOPE=0 "
                        "disables this gate)" % (toon, ACCOUNT_PREFIX, WATCH_GUILD),
                        "out_of_scope")

def assert_scope_account(account):
    if not SCOPE:
        return
    a = account.lower()
    ok = any(p.lower().startswith(a) or a.startswith(p.lower())
             for p in (ACCOUNT_PREFIX, SHELL_ACCOUNT))
    if not ok:
        raise ToolError("account prefix %r is out of scope: must be within the "
                        "configured family '%s' or the shell account '%s'"
                        % (account, ACCOUNT_PREFIX, SHELL_ACCOUNT),
                        "out_of_scope")

def assert_scope_guild(guild):
    if not SCOPE:
        return
    if guild.lower() != WATCH_GUILD.lower():
        raise ToolError("guild %r is out of scope: only the watched guild '%s' "
                        "is queryable" % (guild, WATCH_GUILD), "out_of_scope")

# ---------------------------------------------------------------- params ----
# Every interpolable param is allowlisted per tool AND regex-validated here;
# there is no code path that puts client text into SQL unvalidated.
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")   # coordinates / map ids for the
# flightmaster lookup (no SQL metacharacters possible by construction)

def _p(tool, params, key, required=True, pattern=None):
    v = (params or {}).get(key)
    if v is None or v == "":
        if required:
            raise ToolError("missing param '%s'" % key, "bad_param")
        return None
    v = str(v)
    if not (pattern or NAME_RE).fullmatch(v):
        raise ToolError("param '%s' must match %s (got %r)" % (key, (pattern or NAME_RE).pattern, v[:40]), "bad_param")
    return v

# guild names can contain spaces (the watched guild does); still no quotes or
# SQL metacharacters - leading/trailing space refused too.
GUILD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]{0,30}[A-Za-z0-9]$")

# --------------------------------------------------------------- catalog ----
# sql: ONE static SELECT. Placeholders {x} are filled only from _p()-validated
# values. can/cannot feed the catalog docs (README + `catalog` tool).
CATALOG = {

  "get_online_census": {
    "sql": ("SELECT c.name, a.username AS account, c.level, c.class, c.map, "
            "c.zone, c.online, gm.guildid "
            "FROM acore_characters.characters c "
            "JOIN acore_auth.account a ON a.id = c.account "
            "LEFT JOIN acore_characters.guild_member gm ON gm.guid = c.guid "
            "WHERE a.username LIKE '{account_like}' "
            "ORDER BY a.username, c.name"),
    "params": {"account_like": "required - account name prefix, e.g. i_was_here% is not allowed; pass the bare prefix (e.g. i_was_here)"},
    "timeout": 10,
    "can": "which toons exist on a set of accounts, their level/class/map/zone, the DB `online` flag, and guild membership",
    "cannot": "whether a bot actually has AI / a master right now (live mastership probe via chat-tunnel); whether an online=1 row is truly logged in (A5)",
    "caveat": "characters.online is reset to 0 at worldserver boot and set asynchronously on login (A5), and bot logins/logouts are queued async (A3): online flags can lag reality by tens of seconds either way. Treat online as a hint; live whisper probes are ground truth for session state.",
    "fill": lambda p: {"account_like": _p("get_online_census", p, "account_like") + "%"},
  },

  "get_group_membership": {
    "sql": ("SELECT g.groupType, g.difficulty, g.raidDifficulty, "
            "lc.name AS leader, c.name AS member, c.level, c.class, "
            "c.online, gm2.subgroup, gm2.memberFlags "
            "FROM acore_characters.group_member gm "
            "JOIN acore_characters.`groups` g ON g.guid = gm.guid "
            "JOIN acore_characters.characters lc ON lc.guid = g.leaderGuid "
            "JOIN acore_characters.group_member gm2 ON gm2.guid = g.guid "
            "JOIN acore_characters.characters c ON c.guid = gm2.memberGuid "
            "WHERE gm.memberGuid = "
            "(SELECT guid FROM acore_characters.characters WHERE name = '{toon}') "
            "ORDER BY gm2.subgroup, c.name"),
    "params": {"toon": "required - exact toon name, letters/digits/underscore"},
    "timeout": 10,
    "can": "the party/raid a toon belongs to: type (party/raid/bg bits), difficulty, leader, every member with level/class/subgroup/flags/DB-online",
    "cannot": "battleground groups (not persisted); whether a member is in whisper range; who the bot's AI master is",
    "caveat": "group rows are written live by the worldserver on group events, BUT fresh bot logins auto-join the leader's group (A6) until cleared, and async login/logout (A3) can leave rows briefly stale around bot add/remove. A row here is not proof a member is online right now (A5).",
    "fill": lambda p: {"toon": _p("get_group_membership", p, "toon")},
  },

  "get_character": {
    "sql": ("SELECT c.name, a.username AS account, c.level, c.race, c.class, "
            "c.map, c.zone, c.money, c.totaltime, c.online "
            "FROM acore_characters.characters c "
            "JOIN acore_auth.account a ON a.id = c.account "
            "WHERE c.name = '{toon}'"),
    "params": {"toon": "required - exact toon name"},
    "timeout": 10,
    "can": "account, level, race, class, last map/zone, money, total played time, DB online flag for one toon",
    "cannot": "live position (this is the last flushed value); gear/quest progress (separate tools); AI/mastership",
    "caveat": "map/zone/money/totaltime are flushed to the DB at logout or periodic save - they lag live state by minutes. online is boot-reset/async (A5). Do not use position data to reason about where a toon is RIGHT NOW.",
    "fill": lambda p: {"toon": _p("get_character", p, "toon")},
  },

  "get_account_links": {
    # live schema (DESCRIBE 2026-11-10): id, account_id, linked_account_id,
    # created_at - the mod stores every link in BOTH directions (live 2026-11-10:
    # 10 rows for 5 links), so dedupe to one row per pair via account_id <
    # linked_account_id; the pair is mutual, column order is by account id.
    "sql": ("SELECT a1.username AS account, a2.username AS linked_account, "
            "l.created_at "
            "FROM acore_playerbots.playerbots_account_links l "
            "JOIN acore_auth.account a1 ON a1.id = l.account_id "
            "JOIN acore_auth.account a2 ON a2.id = l.linked_account_id "
            "WHERE l.account_id < l.linked_account_id "
            "AND (a1.username LIKE '{account}%' OR a2.username LIKE '{account}%') "
            "ORDER BY a1.username, a2.username"),
    "params": {"account": "required - account name prefix to filter by (pass the bare prefix)"},
    "timeout": 10,
    "can": "which accounts are linked to which, one row per pair (cross-account addaccount is refused unless linked, A12) + link creation times; the mod writes both directions into the table, this tool dedupes to the mutual pair",
    "cannot": "whether any given toon of a linked account is online or has AI (that is census + live probe)",
    "caveat": "the link table only changes via .playerbots account link/unlink commands, so it is near-real-time stable; but a link existing says nothing about current sessions (A3/A5 staleness applies to the toons, not the rows).",
    "fill": lambda p: {"account": _p("get_account_links", p, "account")},
  },

  "get_quest_log": {
    "sql": ("SELECT cqs.quest, qt.LogTitle, cqs.status, cqs.explored "
            "FROM acore_characters.character_queststatus cqs "
            "LEFT JOIN acore_world.quest_template qt ON qt.ID = cqs.quest "
            "WHERE cqs.guid = (SELECT guid FROM acore_characters.characters WHERE name = '{toon}') "
            "ORDER BY cqs.status, cqs.quest"),
    "params": {"toon": "required - exact toon name"},
    "timeout": 10,
    "can": "the toon's quest log rows with status codes (1=COMPLETE, 3=INCOMPLETE, 5=FAILED, 6=REWARDED per the repo audit; 0 = in the log with no recorded terminal state, seen live) and titles when acore_world is granted",
    "cannot": "live progress within an incomplete quest (mob kill counts live in memory until save); whether the bot's rpg engine will accept it (needs a POI - A8)",
    "caveat": "quest status rows flush with the character save - an in-flight quest can be further along than this shows. REWARDED/FAILED rows are stable; INCOMPLETE progress can lag by minutes.",
    "fill": lambda p: {"toon": _p("get_quest_log", p, "toon")},
  },

  "get_equipped_gear": {
    # equipped gear sits at bag=255, slot 0-18 (director.py:530 audit: bags are
    # bag=255 slot 19-22, backpack is bag=0 slot 23-38); items are GUIDs into
    # item_instance, resolved to templates in acore_world (needs its grant).
    "sql": ("SELECT ci.slot, "
            "CASE ci.slot WHEN 0 THEN 'head' WHEN 1 THEN 'neck' "
            "WHEN 2 THEN 'shoulders' WHEN 3 THEN 'shirt' WHEN 4 THEN 'chest' "
            "WHEN 5 THEN 'waist' WHEN 6 THEN 'legs' WHEN 7 THEN 'feet' "
            "WHEN 8 THEN 'wrists' WHEN 9 THEN 'hands' WHEN 10 THEN 'finger1' "
            "WHEN 11 THEN 'finger2' WHEN 12 THEN 'trinket1' WHEN 13 THEN 'trinket2' "
            "WHEN 14 THEN 'back' WHEN 15 THEN 'mainhand' WHEN 16 THEN 'offhand' "
            "WHEN 17 THEN 'ranged' WHEN 18 THEN 'tabard' "
            "ELSE 'slot' || ci.slot END, "
            "it.name, it.Quality, it.ItemLevel, it.RequiredLevel "
            "FROM acore_characters.character_inventory ci "
            "JOIN acore_characters.item_instance ii ON ii.guid = ci.item "
            "JOIN acore_world.item_template it ON it.entry = ii.itemEntry "
            "WHERE ci.guid = (SELECT guid FROM acore_characters.characters WHERE name = '{toon}') "
            "AND ci.bag = 255 AND ci.slot BETWEEN 0 AND 18 "
            "ORDER BY ci.slot"),
    "params": {"toon": "required - exact toon name"},
    "timeout": 10,
    "can": "what a toon has equipped, per slot (slot id + name: head..tabard) with item name, quality, item level and required level",
    "cannot": "bag contents (separate); enchants/gems (not joined); what was equipped seconds ago mid-session before the save",
    "caveat": "equipment rows flush with the character save - a just-equipped or just-swapped item can lag by minutes. Empty slots simply mean nothing equipped at last save.",
    "fill": lambda p: {"toon": _p("get_equipped_gear", p, "toon")},
  },

  "get_leveling_progress": {
    "sql": ("SELECT name, level, xp, leveltime, totaltime, logout_time, online, "
            "COALESCE(ROUND(xp * 3600.0 / leveltime), 0) AS xp_per_hour "
            "FROM acore_characters.characters c "
            "JOIN acore_auth.account a ON a.id = c.account "
            "WHERE a.username LIKE '{account}%' "
            "ORDER BY level DESC, name"),
    "params": {"account": "required - account name prefix (pass the bare prefix)"},
    "timeout": 10,
    "can": "per-toon leveling KPIs on a set of accounts: level, current-level xp, time spent THIS level (leveltime), total time, last logout, DB online, and derived xp/hour for the current level (xp / leveltime); logout_time shows who has not been played in days",
    "cannot": "xp gained over past days (point-in-time only - trends need snapshots, deliberately not built); whether a toon is leveling right now",
    "caveat": "xp/leveltime/totaltime/logout_time flush with the character save - current-level progress can lag by minutes; xp_per_hour is 0 until leveltime accumulates. online is boot-reset/async (A5).",
    "fill": lambda p: {"account": _p("get_leveling_progress", p, "account")},
  },

  "get_group_xp_penalty": {
    # A7: in a group of >=5 the mod converts to RAID, kill XP is x1.4 then
    # split across members in range - so grouping 5+ hurts leveling.
    "sql": ("SELECT COUNT(*) AS members, "
            "COALESCE(MAX(g.groupType & 2), 0) AS is_raid "
            "FROM acore_characters.group_member gm "
            "JOIN acore_characters.`groups` g ON g.guid = gm.guid "
            "WHERE gm.guid = (SELECT guid FROM acore_characters.group_member "
            "WHERE memberGuid = "
            "(SELECT guid FROM acore_characters.characters WHERE name = '{toon}')) "
            "HAVING COUNT(*) > 0"),
    "params": {"toon": "required - exact toon name"},
    "timeout": 10,
    "can": "one row: how many members the toon's group has and whether it is a raid (groupType bit 0x02) - the two facts behind the A7 XP penalty; 0 rows = not in a group",
    "cannot": "the exact XP multiplier per kill (depends on member levels and in-range members, which the DB does not store); battleground groups (not persisted)",
    "caveat": "group rows are live-written but async login/logout (A3) and the login auto-join (A6) can leave them briefly stale; a members count of >=5 means raid XP splitting applies (A7).",
    "fill": lambda p: {"toon": _p("get_group_xp_penalty", p, "toon")},
  },

  "get_quest_difficulty": {
    "sql": ("SELECT cqs.quest, qt.LogTitle, qt.QuestLevel, c.level, "
            "qt.QuestLevel - c.level AS level_diff, cqs.status, "
            "CASE WHEN qt.QuestLevel - c.level >= 4 THEN 'red' "
            "WHEN qt.QuestLevel - c.level = 3 THEN 'orange' "
            "WHEN qt.QuestLevel - c.level BETWEEN -2 AND 2 THEN 'yellow' "
            "WHEN qt.QuestLevel - c.level BETWEEN -5 AND -3 THEN 'green' "
            "ELSE 'gray' END AS band "
            "FROM acore_characters.character_queststatus cqs "
            "JOIN acore_characters.characters c "
            "ON c.guid = cqs.guid "
            "LEFT JOIN acore_world.quest_template qt ON qt.ID = cqs.quest "
            "WHERE c.name = '{toon}' "
            "ORDER BY level_diff DESC, cqs.quest"),
    "params": {"toon": "required - exact toon name"},
    "timeout": 10,
    "can": "the toon's quest log with each quest's level vs the toon's level, the difference, and an approximate color band (red >= +4, orange +3, yellow -2..+2, green -3..-5, gray <= -6) - answers 'are Locke's quests all gray, should we move zones'",
    "cannot": "quest availability from givers (not DB-truth); whether the rpg engine accepts it (needs a POI, A8); exact XP per quest",
    "caveat": "quest rows flush with the character save and LogTitle/QuestLevel need the acore_world.quest_template grant (without it those columns are NULL but the band still computes). Band thresholds are approximate WoW conventions, not per-quest XP math.",
    "fill": lambda p: {"toon": _p("get_quest_difficulty", p, "toon")},
  },

  "get_skills": {
    "sql": ("SELECT cs.skill, "
            "CASE cs.skill "
            "WHEN 43 THEN 'swords' WHEN 44 THEN 'axes' WHEN 45 THEN 'bows' "
            "WHEN 46 THEN 'guns' WHEN 54 THEN 'maces' "
            "WHEN 55 THEN 'two-handed swords' WHEN 95 THEN 'defense' "
            "WHEN 98 THEN 'language: common' WHEN 118 THEN 'dual wield' "
            "WHEN 129 THEN 'firstaid' WHEN 134 THEN 'feral combat' "
            "WHEN 136 THEN 'staves' WHEN 148 THEN 'riding' "
            "WHEN 160 THEN 'two-handed maces' WHEN 162 THEN 'unarmed' "
            "WHEN 164 THEN 'bs' WHEN 165 THEN 'lw' WHEN 171 THEN 'alchemy' "
            "WHEN 172 THEN 'two-handed axes' WHEN 173 THEN 'daggers' "
            "WHEN 176 THEN 'thrown' WHEN 182 THEN 'herb' WHEN 185 THEN 'cook' "
            "WHEN 186 THEN 'mine' WHEN 197 THEN 'tailor' "
            "WHEN 226 THEN 'crossbows' WHEN 229 THEN 'polearms' "
            "WHEN 333 THEN 'enchant' WHEN 356 THEN 'fish' WHEN 393 THEN 'skin' "
            "WHEN 413 THEN 'cloth' WHEN 414 THEN 'leather' WHEN 415 THEN 'mail' "
            "WHEN 416 THEN 'plate' WHEN 433 THEN 'shield' "
            "WHEN 473 THEN 'fist weapons' "
            "WHEN 755 THEN 'jewel' WHEN 762 THEN 'eng' WHEN 773 THEN 'scribe' "
            "ELSE 'skill' || cs.skill END AS name, "
            "cs.value, cs.max "
            "FROM acore_characters.character_skills cs "
            "WHERE cs.guid = (SELECT guid FROM acore_characters.characters WHERE name = '{toon}') "
            "ORDER BY cs.skill"),
    "params": {"toon": "required - exact toon name"},
    "timeout": 10,
    "can": "the toon's skills with names - professions (firstaid, riding, bs, lw, alchemy, herb, cook, mine, tailor, enchant, fish, skin, jewel, eng, scribe), weapons (swords, axes, maces, two-handed swords/maces/axes, daggers, staves, polearms, bows, crossbows, guns, thrown, unarmed, fist weapons), armor (cloth, leather, mail, plate, shield), defense, dual wield - with value/max; answers 'can Gau gather while grinding' and 'has Crono trained two-handed maces'",
    "cannot": "weapon skills vs class miss chance (weapon skill ids exist but are not named here); live skill-ups mid-session",
    "caveat": "skill values flush with the character save - a just-leveled skill can lag by minutes.",
    "fill": lambda p: {"toon": _p("get_skills", p, "toon")},
  },

  "get_bag_space": {
    # director.py:528-540 audit: capacity = 16 backpack slots +
    # ContainerSlots of equipped bags (bag=255 slot 19-22); used = backpack
    # items (bag=0 slot 23-38) + items inside equipped bags.
    "sql": ("SELECT t.capacity, t.used, t.capacity - t.used AS free_slots "
            "FROM (SELECT "
            "16 + (SELECT COALESCE(SUM(it.ContainerSlots), 0) "
            "FROM acore_characters.character_inventory ci "
            "JOIN acore_characters.item_instance ii ON ii.guid = ci.item "
            "JOIN acore_world.item_template it ON it.entry = ii.itemEntry "
            "WHERE ci.guid = (SELECT guid FROM acore_characters.characters WHERE name = '{toon}') "
            "AND ci.bag = 255 AND ci.slot BETWEEN 19 AND 22) AS capacity, "
            "(SELECT COUNT(*) FROM acore_characters.character_inventory "
            "WHERE guid = (SELECT guid FROM acore_characters.characters WHERE name = '{toon}') "
            "AND ((bag = 0 AND slot BETWEEN 23 AND 38) OR bag IN "
            "(SELECT item FROM acore_characters.character_inventory "
            "WHERE guid = (SELECT guid FROM acore_characters.characters WHERE name = '{toon}') "
            "AND bag = 255 AND slot BETWEEN 19 AND 22))) AS used) t"),
    "params": {"toon": "required - exact toon name"},
    "timeout": 10,
    "can": "one row: bag capacity (16 + equipped bags' ContainerSlots, needs the acore_world.item_template grant), used slots, and free slots",
    "cannot": "what the items ARE (this is a count only); bank/vault space",
    "caveat": "inventory flushes with the character save - loot gained seconds ago may not be counted; without the item_template grant capacity falls back to 16 and undercounts.",
    "fill": lambda p: {"toon": _p("get_bag_space", p, "toon")},
  },

  "get_mount_items": {
    # mount ITEMS: item_template class 15 (Misc) subclass 5 (Mount) per the
    # 3.3.5 item class table - NEEDS LIVE CONFIRMATION on this build (first
    # live run should eyeball the class/subclass of a known mount).
    "sql": ("SELECT ci.bag, ci.slot, it.name, it.Quality, it.ItemLevel, "
            "it.RequiredLevel "
            "FROM acore_characters.character_inventory ci "
            "JOIN acore_characters.item_instance ii ON ii.guid = ci.item "
            "JOIN acore_world.item_template it ON it.entry = ii.itemEntry "
            "WHERE ci.guid = (SELECT guid FROM acore_characters.characters WHERE name = '{toon}') "
            "AND it.class = 15 AND it.subclass = 5 "
            "ORDER BY it.name"),
    "params": {"toon": "required - exact toon name"},
    "timeout": 10,
    "can": "mount ITEMS the toon is carrying (bag/slot, name, quality, required level) - the chestnut/brown horse you shoved in the bags shows here; needs the acore_world.item_template grant",
    "cannot": "mounts LEARNED as spells (character_spell) - spell names are not in the world DB (DBC-only), so learned mounts can only be counted as raw spell ids, deliberately not exposed; riding skill level comes from get_skills (id 148)",
    "caveat": "inventory flushes with the character save - a just-looted mount can lag by minutes. The class 15 / subclass 5 mount mapping is the 3.3.5 standard, not yet live-verified on this build.",
    "fill": lambda p: {"toon": _p("get_mount_items", p, "toon")},
  },

  "get_talents": {
    # live schema (DESCRIBE 2026-11-10): character_talent (guid, spell,
    # specMask) - talents are stored as SPELLS with a spec bitmask. This build
    # has NO world talent/talent_tab tables (confirmed live: SHOW TABLES empty)
    # and spell names are DBC-only, so only ids + specMask can be returned.
    "sql": ("SELECT ct.spell, ct.specMask "
            "FROM acore_characters.character_talent ct "
            "WHERE ct.guid = (SELECT guid FROM acore_characters.characters WHERE name = '{toon}') "
            "ORDER BY ct.specMask, ct.spell"),
    "params": {"toon": "required - exact toon name"},
    "timeout": 10,
    "can": "the toon's learned talents as (spell id, specMask) rows - talent COUNT and spec spread (how many points per specMask bit); useful for 'has X spent talents / how deep in each spec'",
    "cannot": "talent NAMES or tree positions - spell names and talent trees are DBC-only on this build (no acore_world.talent table, confirmed live 2026-11-10); use the bot's live 'talents' whisper via chat-tunnel for a named list; what specMask bits mean exactly (raw bitmask, live-unverified)",
    "caveat": "talents flush with the character save - a just-spent point can lag by minutes. Rows are raw spell ids; do not treat them as game-object ids for anything else.",
    "fill": lambda p: {"toon": _p("get_talents", p, "toon")},
  },

  "get_wallets": {
    "sql": ("SELECT c.name, a.username AS account, c.level, c.money "
            "FROM acore_characters.characters c "
            "JOIN acore_auth.account a ON a.id = c.account "
            "WHERE a.username LIKE '{account}%' "
            "ORDER BY c.money DESC, c.name"),
    "params": {"account": "required - account name prefix (pass the bare prefix)"},
    "timeout": 10,
    "can": "money (copper) per toon on a set of accounts, richest first - answers 'can everyone afford training/repair'",
    "cannot": "mail with COD attached (character_mail not joined); guild bank money; pending vendor sales",
    "caveat": "money flushes with the character save - recent purchases/loot can lag by minutes.",
    "fill": lambda p: {"account": _p("get_wallets", p, "account")},
  },

  "get_game_tele": {
    # live game_tele names are concatenated ("BootyBay", "MenethilHarbor" -
    # live 2026-11-10: LIKE '%Booty Bay%' returned 0 rows while '%Booty%' hit
    # "BootyBay"), so match space-insensitively on both sides
    "sql": ("SELECT name, map, position_x, position_y, position_z "
            "FROM acore_world.game_tele "
            "WHERE REPLACE(name, ' ', '') LIKE "
            "CONCAT('%', REPLACE('{name}', ' ', ''), '%') ORDER BY name LIMIT 20"),
    "params": {"name": "required - (partial) teleport location name, letters/digits/spaces"},
    "timeout": 10,
    "can": "the core's named teleport locations (game_tele): exact name, map, coordinates - the ONLY names legal in a GM '.tele name' command",
    "cannot": "arbitrary coordinates (no such table); whether a spot is safe/faction-appropriate for a given bot",
    "caveat": "static core data - effectively never stale. A 0-row answer means the location name does not exist; never guess a tele name the LLM has not seen in these rows.",
    "fill": lambda p: {"name": _p("get_game_tele", p, "name", pattern=GUILD_RE)},
  },

  "get_flightmasters": {
    # UNIT_NPC_FLAG_FLIGHTMASTER = 8192 (0x2000). Nearest flight masters to
    # a live position: the travel mission hops to one, reads the bot's own
    # 'taxi ?' list, and flies the long leg (the taxi graph itself lives in
    # the DBC stores, not SQL - the bot's live list IS the route data).
    "sql": ("SELECT ct.name, c.map, c.position_x, c.position_y, c.position_z, "
            "CAST((c.position_x - {x}) * (c.position_x - {x}) "
            "+ (c.position_y - {y}) * (c.position_y - {y}) AS INTEGER) AS dist "
            "FROM acore_world.creature c "
            "JOIN acore_world.creature_template ct ON ct.entry = c.id "
            "WHERE (ct.npcflag & 8192) != 0 AND c.map = {map} "
            "ORDER BY dist LIMIT 3"),
    "params": {"x": "required - live position x", "y": "required - live position y",
                "map": "required - map id"},
    "timeout": 10,
    "can": "the 3 nearest flight masters to a live position on the same map (name, map, coordinates) - the departure points for the stock 'taxi ?' / 'taxi <n>' whisper commands",
    "cannot": "the taxi graph itself (DBC data, not SQL - ask the bot with 'taxi ?'); flight masters on another map; whether the bot knows a node",
    "caveat": "static spawn data - effectively never stale. The bot must be near the flight master (FarDistance) for 'taxi' to work.",
    "fill": lambda p: {"x": _p("get_flightmasters", p, "x", pattern=NUM_RE),
                      "y": _p("get_flightmasters", p, "y", pattern=NUM_RE),
                      "map": _p("get_flightmasters", p, "map", pattern=NUM_RE)},
  },

  "get_mat_spawns": {
    # herbs and veins are GAMEOBJECT_TYPE_CHEST (type 3) spawns; ranking =
    # spawn count within 1000 yards of each named teleport location, so the
    # answer arrives as a teleportable destination, not raw coordinates
    "sql": ("SELECT t.name, t.map, COUNT(*) AS spawns "
            "FROM acore_world.gameobject go "
            "JOIN acore_world.gameobject_template gt ON gt.entry = go.id "
            "JOIN acore_world.game_tele t ON t.map = go.map "
            "WHERE gt.type = 3 AND gt.name LIKE '%{mat}%' "
            "AND (go.position_x - t.position_x) * (go.position_x - t.position_x) "
            "+ (go.position_y - t.position_y) * (go.position_y - t.position_y) < 1000000 "
            "GROUP BY t.name, t.map ORDER BY spawns DESC LIMIT 10"),
    "params": {"mat": "required - (partial) gameobject name, e.g. Peacebloom or Copper Vein"},
    "timeout": 10,
    "can": "which named teleport locations have the most spawns of a farmable mat (herb/vein) within 1000 yards, ranked - answers 'where do I send a bot to farm peacebloom' with a destination name usable in .tele name",
    "cannot": "live node availability (nodes spawn/despawn on a timer - this is the spawn table, not what is up right now); skinning/mob drops (not gameobjects); exact in-range density at radius edges",
    "caveat": "static world data (effectively never stale), but nodes are shared dynamic spawns: the table is the potential, not the current world. The 1000-yard radius around each teleport point is a density proxy, not a guarantee.",
    "fill": lambda p: {"mat": _p("get_mat_spawns", p, "mat", pattern=GUILD_RE)},
  },

  "get_guild_members": {
    "sql": ("SELECT c.name, g_r.rname AS `rank`, c.level, c.class, c.online "
            "FROM acore_characters.guild g "
            "JOIN acore_characters.guild_member gm ON gm.guildid = g.guildid "
            "JOIN acore_characters.characters c ON c.guid = gm.guid "
            "JOIN acore_characters.`guild_rank` g_r ON g_r.guildid = g.guildid AND g_r.rid = gm.rank "
            "WHERE g.name = '{guild}' ORDER BY c.name"),
    "params": {"guild": "required - exact guild name; spaces allowed (e.g. 'Special FXc'), no quotes"},
    "timeout": 10,
    "can": "every member of one guild with rank, level, class, DB online flag",
    "cannot": "guilds other than the one named; guild bank contents; online-truth (A5)",
    "caveat": "guild membership/rank rows persist on change and are stable, but the online column is the boot-reset/async flag (A5) - a member shown online may have crashed without a logout write.",
    "fill": lambda p: {"guild": _p("get_guild_members", p, "guild", pattern=GUILD_RE)},
  },
}

# ------------------------------------------------------------- redirects ----
# Deliberately NOT here: names the LLM is likely to reach for that belong to
# chat-tunnel's live-read path (.playerbots inspect, A23 - rtt-engine retired
# 2026-11-10). Instead of a bare "unknown tool", these answer code=redirect
# with the owning service and a route_hint - a SUGGESTION to the caller, never
# an auto-executed read (single-brain: db-search is a hand, the LLM decides).
# A timed-out inspect means offline truth is db-search: fall back to the
# db_fallback tool and say so via its caveat - never guess (A21: a missing
# transcript row is not proof of anything either).
_CT = "POST http://127.0.0.1:8765/ (local-only JSON, one request per call)"
REDIRECTS = {
  "get_current_position": {
    "service": "chat-tunnel",
    "reason": "live position is tick-time state; the DB only has the last flushed map/zone",
    "route_hint": _CT + ': {"tool": "inspect", "args": {"bot": "<name>", "query": "position"}} -> '
      '{"ok": true, "result": {"correlation_id": "..."}}; then '
      '{"tool": "get_reply", "args": {"correlation_id": "..."}} -> {sent, reply} '
      'with the raw inspect answer (coordinates + zone); a pending reply '
      '(reply: null) is normal within the first seconds - poll get_reply '
      'again. A live inspect is "as of the inspect, seconds fresh".',
    "db_fallback": "get_character (map/zone are the LAST-FLUSHED values, minutes-to-logout stale - say so, never present them as live)"},
  "get_live_status": {
    "service": "chat-tunnel",
    "reason": "live HP/combat state never lives in the DB",
    "route_hint": _CT + ': {"tool": "inspect", "args": {"bot": "<name>", "query": "hp"}} '
      '(or "state"/"movement"/"target"/"action"), then get_reply with the '
      'correlation_id - a pending reply (reply: null) is normal within the '
      'first seconds, poll again; the matched reply is the raw inspect '
      'answer, seconds fresh.',
    "db_fallback": "get_character (online flag is boot-reset/async, A5 - a hint, never ground truth)"},
  "get_bot_master": {
    "service": "chat-tunnel",
    "reason": "who a bot's AI master is right now is live session state (SetMaster runs at bot login); the DB has no mastership row",
    "route_hint": _CT + ': send a mastership-proving whisper (e.g. "nc ?") via '
      'chat-tunnel with a correlation id and read the reply - a reply proves '
      'the sender is the bot\'s master; silence is ambiguous (A21: bot-originated '
      'reply types are partially chat-logged, so a missing transcript row '
      'proves nothing).',
    "db_fallback": "none - the DB has no mastership row; report unknown rather than guess"},
  "is_bot_responding": {
    "service": "chat-tunnel",
    "reason": "session liveness is proven by a live probe with correlation, never by DB flags (A5: online is boot-reset/async)",
    "route_hint": _CT + ': send any whitelisted whisper to the bot via chat-tunnel, '
      'then get_reply / verify the correlation_id - "delivered but silent" '
      '(bot not answering) vs "never sent" (transport down). A21: a missing '
      'transcript row is not proof of anything.',
    "db_fallback": "get_online_census (online flag is a hint only, A5 - and a DB-offline bot may still answer)"},
}

META_TOOLS = ("catalog",)

class ToolError(Exception):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code

# ------------------------------------------------------------- adapters ----
class MysqlAdapter:
    """Real adapter: mysql CLI under the SELECT-only identity (director.py pattern)."""
    def __init__(self, mycnf):
        self.mycnf = mycnf
        if not os.path.exists(mycnf):
            raise SystemExit("db-search: no MySQL defaults file at %s (DBSEARCH_MYCNF)" % mycnf)

    def run(self, sql, timeout):
        env = dict(os.environ)
        env.pop("MYSQL_PWD", None)
        try:
            out = subprocess.run(
                ["mysql", "--defaults-file=" + self.mycnf, "-N", "-B", "-e", sql],
                capture_output=True, text=True, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ToolError("query timed out after %ss" % timeout, "timeout")
        except FileNotFoundError:
            raise ToolError("mysql client not on PATH", "backend")
        if out.returncode != 0:
            raise ToolError(out.stderr.strip()[:200] or "mysql failed", "backend")
        return out.stdout.strip()

class SqliteAdapter:
    """Harness adapter: the SAME fixed SQL against SQLite fixture DBs that
    mirror the server's separate databases (acore_characters / acore_auth /
    acore_playerbots / acore_world) via ATTACH, so the catalog SQL runs
    unmodified."""
    SCHEMAS = ("acore_characters", "acore_auth", "acore_playerbots", "acore_world")

    def __init__(self, path):
        self.path = path   # fixture dir: fixture_acore_<schema>..db siblings

    def run(self, sql, timeout):
        deadline = time.monotonic() + timeout
        base = os.path.dirname(self.path)
        con = sqlite3.connect(":memory:")
        try:
            for s in self.SCHEMAS:
                con.execute("ATTACH DATABASE ? AS %s" % s,
                            (os.path.join(base, "fixture_%s.db" % s),))
            cur = con.execute(sql)
            rows = cur.fetchall()
            if time.monotonic() > deadline:
                raise ToolError("query timed out after %ss" % timeout, "timeout")
            return "\n".join("\t".join("" if v is None else str(v) for v in r) for r in rows)
        finally:
            con.close()

def make_adapter():
    if ADAPTER == "sqlite":
        return SqliteAdapter(FIXTURE_DB)
    return MysqlAdapter(MYCNF)

# --------------------------------------------------------------- engine ----
SELECT_ONLY_RE = re.compile(r"^\s*SELECT\b", re.IGNORECASE)

def assert_readonly_catalog():
    """Every registered statement (incl. the scope gate) is one SELECT."""
    for name, sql in [(n, s["sql"]) for n, s in CATALOG.items()] + [("scope_gate", SCOPE_SQL)]:
        if not SELECT_ONLY_RE.match(sql) or ";" in sql[7:]:
            raise SystemExit("db-search: catalog entry %s is not a single SELECT" % name)

def run_tool(adapter, tool, params):
    """Returns the response dict (envelope). The ONLY path to the DB."""
    if tool == "catalog":
        return {"ok": True, "tool": "catalog",
                "queried_at": _now(),
                "caveat": "static catalog - no DB query, no staleness",
                "rows": ([{"name": n, "params": s["params"], "timeout": s["timeout"],
                           "can": s["can"], "cannot": s["cannot"], "caveat": s["caveat"]}
                          for n, s in sorted(CATALOG.items())]
                         + [{"name": n, "redirect_to": r["service"],
                             "reason": r["reason"], "route_hint": r["route_hint"],
                             "db_fallback": r["db_fallback"]}
                          for n, r in sorted(REDIRECTS.items())]),
                "row_count": len(CATALOG) + len(REDIRECTS), "duration_ms": 0}
    if tool in REDIRECTS:
        r = REDIRECTS[tool]
        return {"ok": False, "tool": tool, "queried_at": _now(), "code": "redirect",
                "redirect_to": r["service"], "route_hint": r["route_hint"],
                "db_fallback": r["db_fallback"], "error": r["reason"],
                "caveat": "no DB query - a route hint to the caller (single-brain: "
                          "db-search suggests, the LLM executes); on an inspect "
                          "timeout the offline truth is this catalog's "
                          "db_fallback - use it and its caveat, never guess"}
    if tool not in CATALOG:
        raise ToolError("unknown tool %r - allowed: %s (redirects: %s)" %
                        (tool, ", ".join(sorted(CATALOG) + list(META_TOOLS)),
                         ", ".join(sorted(REDIRECTS))), "unknown_tool")
    spec = CATALOG[tool]
    values = spec["fill"](params or {})
    if "toon" in values:
        assert_scope_toon(adapter, values["toon"])
    if "guild" in values:
        assert_scope_guild(values["guild"])
    for key in ("account", "account_like"):
        if key in values:
            assert_scope_account(values[key].rstrip("%"))
    sql = spec["sql"]
    for k, v in values.items():
        sql = sql.replace("{%s}" % k, v)
    if "{" in sql:
        raise SystemExit("db-search: unfilled placeholder in %s" % tool)
    t0 = time.monotonic()
    raw = adapter.run(sql, spec["timeout"])
    ms = int((time.monotonic() - t0) * 1000)
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        rows.append(line.split("\t"))
    return {"ok": True, "tool": tool, "queried_at": _now(), "caveat": spec["caveat"],
            "rows": rows, "row_count": len(rows), "duration_ms": ms}

def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def handle(adapter, request):
    if not isinstance(request, dict) or not isinstance(request.get("tool"), str):
        return {"ok": False, "error": "request must be {\"tool\": ..., \"params\": {...}}", "code": "bad_request"}
    rid = request.get("id")
    try:
        resp = run_tool(adapter, request["tool"], request.get("params"))
    except ToolError as e:
        resp = {"ok": False, "error": str(e), "code": e.code}
    except Exception as e:                                   # noqa: BLE001
        resp = {"ok": False, "error": "internal error: %s" % e, "code": "internal"}
    if rid is not None:
        resp["id"] = rid
    return resp

# --------------------------------------------------------------- server ----
class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        adapter = self.server.adapter
        for line in self.rfile:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                resp = {"ok": False, "error": "request is not JSON", "code": "bad_request"}
            else:
                resp = handle(adapter, req)
            try:
                self.wfile.write((json.dumps(resp) + "\n").encode())
            except (BrokenPipeError, ConnectionResetError):
                return  # client hung up before reading the reply - not an error

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

def serve():
    assert_readonly_catalog()
    adapter = make_adapter()
    if ADDR:
        host, _, port = ADDR.partition(":")
        if host not in ("", "127.0.0.1", "localhost", "::1"):
            raise SystemExit("db-search: refusing to bind %r - local-only by contract" % host)
        srv = Server(("127.0.0.1", int(port or 8765)), Handler)
        where = "127.0.0.1:%s" % srv.server_address[1]
    else:
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        class UnixServer(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
        srv = UnixServer(SOCKET_PATH, Handler)
        os.chmod(SOCKET_PATH, 0o600)
        where = SOCKET_PATH
    srv.adapter = adapter
    sys.stderr.write("db-search: serving %d tools (+%d redirects) on %s (adapter=%s)\n" %
                     (len(CATALOG) + len(META_TOOLS), len(REDIRECTS), where, ADAPTER))
    srv.serve_forever()

def one_shot(tool, kv):
    adapter = make_adapter()
    params = {}
    for item in kv:
        k, _, v = item.partition("=")
        params[k] = v
    print(json.dumps(handle(adapter, {"tool": tool, "params": params}), indent=2))

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if mode == "serve":
        serve()
    elif mode == "query":
        one_shot(sys.argv[2], sys.argv[3:])
    elif mode == "catalog":
        print(json.dumps(run_tool(None, "catalog", {}), indent=2))
    else:
        raise SystemExit(__doc__)

if __name__ == "__main__":
    main()
