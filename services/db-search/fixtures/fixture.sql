-- db-search proof fixture: a miniature, fully offline snapshot of the four
-- AzerothCore databases the catalog reads. Built into SQLite DBs by the
-- harness (harness.py); column names/types match the live server so the
-- catalog SQL runs unmodified.
--
-- Cast (matches the repo's live roster lore):
--   Leo (shell account i_was_cmd) leads a raid with bots Ayla, Gau, Locke.
--   Cecil/Rydia/Shadow/Vivi/Aerith are the hand-play five (G1) on the master
--   account; Golbez is the extra bot on I_WAS_HERE5TH (live census 2026-11-10).
--   Guild "Special FXc" holds the watched members; account links cover ALL the
--   ritual accounts including I_WAS_HERE5TH (live: 5TH IS linked). Link table
--   mirrors the live schema (DESCRIBE 2026-11-10): id, account_id,
--   linked_account_id, created_at - account IDs, not names.

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
  xp INTEGER, leveltime INTEGER, logout_time INTEGER, totaltime INTEGER,
  online INTEGER);
INSERT INTO acore_characters.characters VALUES
  (10, 1, 'Leo',     1, 5, 80, 0,   12, 1000000, 0,     0,      1731000000, 99999, 1),  -- shell toon, online
  (11, 2, 'Cecil',   1, 1, 42, 0,   12,   50000, 3100,  7200,   1730900000,  5000,  0), -- G1, offline days
  (12, 2, 'Rydia',   4, 8, 40, 1,   14,   20000, 5400,  9000,   1730950000,  4000,  0),
  (13, 2, 'Vivi',   7, 9, 35, 530,  12,   10000, 2200,  3600,   1730990000,  3000,  0),
  (20, 3, 'Ayla',    2, 2, 30, 0,   12,    5000, 4100,  5400,   1731000100,  2000,  1),  -- ritual bots, online
  (21, 4, 'Gau',     8, 7, 28, 1,   14,    3000, 1800,  2700,   1731000050,  1500,  1),
  (22, 5, 'Locke',  2, 4, 32, 0,   12,    7000, 7300, 10800,   1731000200,  2500,  0),  -- async logout in flight (A3)
  (23, 6, 'Golbez',  1, 6, 55, 0,   12,   80000, 9500, 14400,   1731000300,  6000,  1);  -- extra bot, online

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
INSERT INTO acore_characters.guild VALUES (7, 'Special FXc');

CREATE TABLE acore_characters.guild_member (
  guildid INTEGER, guid INTEGER, rank INTEGER);
INSERT INTO acore_characters.guild_member VALUES
  (7, 10, 3),   -- Leo, Leveling Master (live: in the watched guild)
  (7, 11, 0),   -- Cecil, guild master rank
  (7, 12, 1),
  (7, 23, 2);

CREATE TABLE acore_characters.`guild_rank` (
  guildid INTEGER, rid INTEGER, rname TEXT);
INSERT INTO acore_characters.`guild_rank` VALUES
  (7, 0, 'Guild Master'), (7, 1, 'Officer'), (7, 2, 'Member'), (7, 3, 'Leveling Master');

CREATE TABLE acore_characters.character_queststatus (
  guid INTEGER, quest INTEGER, status INTEGER, explored INTEGER);
INSERT INTO acore_characters.character_queststatus VALUES
  (22, 153, 3, 0),   -- Locke: INCOMPLETE (rpg do quest 153 target, A9)
  (22, 25,  1, 1),   -- COMPLETE, not turned in
  (22, 36,  6, 1),   -- REWARDED
  (20, 12,  5, 0);   -- Ayla: FAILED

CREATE TABLE acore_playerbots.playerbots_account_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER,
  linked_account_id INTEGER, created_at TEXT);
-- the mod writes every link in BOTH directions (live 2026-11-10: 10 rows for
-- 5 links); the tool dedupes. ALL five accounts incl. the master are linked
-- live, so the fixture models the same five mutual pairs.
INSERT INTO acore_playerbots.playerbots_account_links VALUES
  (1,  1, 2, '2026-11-02 16:31:36'), (2,  2, 1, '2026-11-02 16:31:36'),
  (3,  1, 3, '2026-11-02 16:22:40'), (4,  3, 1, '2026-11-02 16:22:40'),
  (5,  1, 4, '2026-11-02 16:22:40'), (6,  4, 1, '2026-11-02 16:22:40'),
  (7,  1, 5, '2026-11-02 16:22:40'), (8,  5, 1, '2026-11-02 16:22:40'),
  (9,  1, 6, '2026-11-05 22:12:47'), (10, 6, 1, '2026-11-05 22:12:47');

CREATE TABLE acore_world.quest_template (
  ID INTEGER PRIMARY KEY, LogTitle TEXT, QuestLevel INTEGER);
INSERT INTO acore_world.quest_template VALUES
  (12, 'The Fargatome', 30),
  (25, 'A Legend of the Fall', 26),
  (36, 'Brotherhood of Thieves', 12),
  (153, 'The Everlasting Mirage', 38);

CREATE TABLE acore_characters.character_inventory (
  guid INTEGER, bag INTEGER, slot INTEGER, item INTEGER);
CREATE TABLE acore_characters.item_instance (
  guid INTEGER PRIMARY KEY, itemEntry INTEGER);
CREATE TABLE acore_world.item_template (
  entry INTEGER PRIMARY KEY, name TEXT, Quality INTEGER,
  ItemLevel INTEGER, RequiredLevel INTEGER, ContainerSlots INTEGER,
  class INTEGER, subclass INTEGER);
-- Ayla's paperdoll (guid 20): bag=255, equipment slots 0-18 per the
-- director.py:530 audit; two slots empty to prove sparse output.
INSERT INTO acore_characters.item_instance VALUES
  (9001, 2901), (9002, 2902), (9003, 2903);
INSERT INTO acore_world.item_template VALUES
  (2901, 'Linen Robe', 1, 15, 5, 0, 4, 1),
  (2902, 'Brawler Boots', 2, 18, 7, 0, 4, 8),
  (2903, 'Worn Shortsword', 0, 5, 1, 0, 2, 8),
  (2950, 'Small Brown Pouch', 1, 5, 1, 8, 1, 0),
  (2951, 'Rough Stone', 0, 1, 1, 0, 7, 1),
  (2952, 'Linen Cloth', 0, 1, 1, 0, 7, 1),
  (2953, 'Chestnut Mare Bridle', 3, 30, 30, 0, 15, 5);
INSERT INTO acore_characters.character_inventory VALUES
  (20, 255, 4, 9001),   -- chest: Linen Robe
  (20, 255, 7, 9002),   -- feet: Brawler Boots
  (20, 255, 15, 9003);  -- mainhand: Worn Shortsword

CREATE TABLE acore_characters.character_skills (
  guid INTEGER, skill INTEGER, value INTEGER, max INTEGER);
-- Ayla (guid 20): herbalist/miner - the gather-preset match
INSERT INTO acore_characters.character_skills VALUES
  (20, 182, 150, 150),  -- herb
  (20, 186, 75, 150),   -- mine
  (20, 129, 40, 75),    -- firstaid
  (20, 148, 150, 150);  -- riding
-- Ayla's bags: an 8-slot pouch equipped (bag=255 slot 19), 3 items in the
-- backpack (bag=0 slots 23-25) -> capacity 24, used 3, free 21.
INSERT INTO acore_characters.item_instance VALUES
  (9004, 2950), (9005, 2951), (9006, 2952), (9007, 2953);
INSERT INTO acore_characters.character_inventory VALUES
  (20, 255, 19, 9004),  -- equipped bag: Small Brown Pouch (8 slots)
  (20, 0, 23, 9005),    -- backpack: Rough Stone
  (20, 0, 24, 9006),    -- backpack: Linen Cloth
  (20, 0, 25, 9007);    -- backpack: Chestnut Mare Bridle (mount item)

CREATE TABLE acore_characters.character_talent (
  guid INTEGER, spell INTEGER, specMask INTEGER,
  PRIMARY KEY (guid, spell));
-- Ayla (guid 20, druid): points spread across two specMask bits
INSERT INTO acore_characters.character_talent VALUES
  (20, 18562, 1), (20, 18571, 1), (20, 20711, 2), (20, 20719, 2);
-- Ayla's combat/armor skills: staves + cloth/leather (druid-appropriate)
INSERT INTO acore_characters.character_skills VALUES
  (20, 136, 130, 130),  -- staves
  (20, 413, 1, 1),      -- cloth
  (20, 414, 1, 1);      -- leather

-- scope-gate outsiders: an account outside the i_was_here% family and a
-- toon on it that is NOT in the watched guild.
INSERT INTO acore_auth.account VALUES (9, 'someother', 0, -1);
INSERT INTO acore_characters.characters VALUES
  (30, 9, 'Zem', 2, 3, 20, 0, 12, 100, 0, 0, 1731000000, 500, 1);

-- ---------------------------------------------------------------------------
-- game_tele + gameobject spawns (get_game_tele / get_mat_spawns tools).
-- A small slice of the core's teleport table (the harbormaster routes) and
-- herb/vein spawn rows clustered near their classic zones.
CREATE TABLE acore_world.game_tele (
  id INTEGER PRIMARY KEY, name TEXT, map INTEGER,
  position_x REAL, position_y REAL, position_z REAL, orientation REAL);
INSERT INTO acore_world.game_tele VALUES
  (1, 'Goldshire',      0, -9446.0, 64.0, 56.0, 0),
  (2, 'Auberdine',      1, 6440.0, 126.0, 6.0, 0),
  (3, 'Menethil Harbor',0, -3760.0, -868.0, 10.0, 0),
  (4, 'Theramore',      1, -4010.0, -4500.0, 10.0, 0),
  (5, 'Booty Bay',      0, -14420.0, 480.0, 26.0, 0),
  (6, 'Ratchet',        1, -870.0, -3700.0, 10.0, 0),
  (7, 'Kharanos',       0, -5690.0, -510.0, 400.0, 0);

-- herbs/veins are GAMEOBJECT_TYPE_CHEST (type 3)
CREATE TABLE acore_world.gameobject_template (
  entry INTEGER PRIMARY KEY, name TEXT, type INTEGER);
INSERT INTO acore_world.gameobject_template VALUES
  (1618, 'Peacebloom', 3), (1619, 'Peacebloom', 3),
  (1731, 'Copper Vein', 3);

CREATE TABLE acore_world.gameobject (
  guid INTEGER PRIMARY KEY, id INTEGER, map INTEGER,
  position_x REAL, position_y REAL, position_z REAL);
-- 3 Peacebloom near Goldshire (Elwynn), 1 near Kharanos (Dun Morogh)
INSERT INTO acore_world.gameobject VALUES
  (1, 1618, 0, -9400.0, 80.0, 56.0),
  (2, 1618, 0, -9500.0, 40.0, 56.0),
  (3, 1619, 0, -9450.0, 100.0, 56.0),
  (4, 1618, 0, -5700.0, -480.0, 400.0),
  (5, 1731, 0, -5650.0, -530.0, 400.0);
-- remaining harbormaster route harbors (zeppelin line; note: the game_tele
-- spelling of Gromgol is apostrophe-free - params with quotes are refused)
INSERT INTO acore_world.game_tele VALUES
  (8, 'Gromgol Base Camp', 0, -12360.0, 220.0, 12.0, 0),
  (9, 'Undercity', 0, 1595.0, 240.0, -50.0, 0),
  (10, 'Orgrimmar', 1, 1442.0, -4470.0, 12.0, 0);
