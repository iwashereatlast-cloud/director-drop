-- db-search proof fixture: a miniature, fully offline snapshot of the four
-- AzerothCore databases the catalog reads. Built into SQLite DBs by the
-- harness (harness.py); column names/types match the live server so the
-- catalog SQL runs unmodified.
--
-- Cast (matches the repo's live roster lore):
--   Leo (shell account i_was_cmd) leads a raid with bots Ayla, Gau, Locke.
--   Cecil/Rydia/Shadow/Vivi/Aerith are the hand-play five (G1) on the master
--   account; Golbez is the extra bot on I_WAS_HERE5TH (live census 2026-11-10).
--   Guild "Special" holds the watched members; account links cover ALL the
--   ritual accounts including I_WAS_HERE5TH (live: 5TH IS linked).

ATTACH DATABASE 'fixture_acore_characters.db' AS acore_characters;
ATTACH DATABASE 'fixture_acore_auth.db'       AS acore_auth;
ATTACH DATABASE 'fixture_acore_playerbots.db'  AS acore_playerbots;
ATTACH DATABASE 'fixture_acore_world.db'       AS acore_world;

CREATE TABLE acore_auth.account (
  id INTEGER PRIMARY KEY, username TEXT, gmlevel INTEGER, RealmID INTEGER);
INSERT INTO acore_auth.account VALUES
  (1, 'i_was_cmd',        3, -1),   -- A13: shell account, gmlevel 3, all realms
  (2, 'i_was_here1st',     0, -1),  -- master/human account
  (3, 'i_was_here2nd',     0, -1),
  (4, 'i_was_here3rd',     0, -1),
  (5, 'i_was_here4th',     0, -1),
  (6, 'i_was_here5th',     0, -1);  -- NOT linked (A12 ritual warning case)

CREATE TABLE acore_characters.characters (
  guid INTEGER PRIMARY KEY, account INTEGER, name TEXT, race INTEGER,
  class INTEGER, level INTEGER, map INTEGER, zone INTEGER, money INTEGER,
  totaltime INTEGER, online INTEGER);
INSERT INTO acore_characters.characters VALUES
  (10, 1, 'Leo',     1, 5, 80, 0,   12, 1000000, 99999, 1),   -- shell toon, online
  (11, 2, 'Cecil',   1, 1, 42, 0,   12,   50000,  5000,  0),  -- G1, offline
  (12, 2, 'Rydia',   4, 8, 40, 1,   14,   20000,  4000,  0),
  (13, 2, 'Vivi',   7, 9, 35, 530,  12,   10000,  3000,  0),
  (20, 3, 'Ayla',    2, 2, 30, 0,   12,    5000,  2000,  1),  -- ritual bots, online
  (21, 4, 'Gau',     8, 7, 28, 1,   14,    3000,  1500,  1),
  (22, 5, 'Locke',  2, 4, 32, 0,   12,    7000,  2500,  0),  -- async logout in flight (A3)
  (23, 6, 'Golbez',  1, 6, 55, 0,   12,   80000,  6000,  1);  -- extra bot, online

CREATE TABLE acore_characters.`groups` (
  guid INTEGER PRIMARY KEY, leaderGuid INTEGER, groupType INTEGER,
  difficulty INTEGER, raidDifficulty INTEGER);
INSERT INTO acore_characters.`groups` VALUES
  (100, 10, 0x02, 0, 1);   -- Leo's raid (groupType bit 0x02 = raid)

CREATE TABLE acore_characters.group_member (
  guid INTEGER, memberGuid INTEGER, subgroup INTEGER, memberFlags INTEGER);
INSERT INTO acore_characters.group_member VALUES
  (100, 10, 0, 0),   -- Leo (leader, subgroup 1 assumed 0 for fixture)
  (100, 20, 1, 0),   -- Ayla
  (100, 21, 1, 0),   -- Gau
  (100, 22, 2, 0);   -- Locke - async logout: still in the raid rows (A3)

CREATE TABLE acore_characters.guild (
  guildid INTEGER PRIMARY KEY, name TEXT);
INSERT INTO acore_characters.guild VALUES (7, 'Special');

CREATE TABLE acore_characters.guild_member (
  guildid INTEGER, guid INTEGER, rank INTEGER);
INSERT INTO acore_characters.guild_member VALUES
  (7, 11, 0),   -- Cecil, guild master rank
  (7, 12, 1),
  (7, 23, 2);

CREATE TABLE acore_characters.`guild_rank` (
  guildid INTEGER, rid INTEGER, rname TEXT);
INSERT INTO acore_characters.`guild_rank` VALUES
  (7, 0, 'Guild Master'), (7, 1, 'Officer'), (7, 2, 'Member');

CREATE TABLE acore_characters.character_queststatus (
  guid INTEGER, quest INTEGER, status INTEGER, explored INTEGER);
INSERT INTO acore_characters.character_queststatus VALUES
  (22, 153, 3, 0),   -- Locke: INCOMPLETE (rpg do quest 153 target, A9)
  (22, 25,  1, 1),   -- COMPLETE, not turned in
  (22, 36,  6, 1),   -- REWARDED
  (20, 12,  5, 0);   -- Ayla: FAILED

CREATE TABLE acore_playerbots.playerbots_account_links (
  accountName TEXT, botAccount TEXT, createTime INTEGER);
INSERT INTO acore_playerbots.playerbots_account_links VALUES
  ('i_was_cmd', 'i_was_here2nd', 1730000000),
  ('i_was_cmd', 'i_was_here3rd', 1730000001),
  ('i_was_cmd', 'i_was_here4th', 1730000002),
  ('i_was_cmd', 'i_was_here5th', 1730000003);  -- live 2026-11-10: 5TH IS linked

CREATE TABLE acore_world.quest_template (
  ID INTEGER PRIMARY KEY, LogTitle TEXT);
INSERT INTO acore_world.quest_template VALUES
  (12, 'The Fargatome'),
  (25, 'A Legend of the Fall'),
  (36, 'Brotherhood of Thieves'),
  (153, 'The Everlasting Mirage');
