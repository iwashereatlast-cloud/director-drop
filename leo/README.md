# Leo presence control (systemd user units)

Leo's headless world session becomes a managed service with a watchdog:

- log in:  systemctl --user start leo
- log out: systemctl --user stop leo
- watchdog poll (every 2 min): starts Leo when wanted, stops him when not,
  zombie-kills a hung session (2 consecutive offline readings), and - default
  on - queues the bot-start ritual right after Leo logs in.

Wanted-online = server-local hour in [ON_H, OFF_H) (default 08-22), OR any
toon in GROUP online (default: the G1 manual-leveling five). Outside both,
Leo is logged out. Manual start/stop works, but the next tick re-applies
policy - being online in-game as Cecil/Rydia/Shadow/Vivi/Aeris overrides
off-hours.

## Install (once)

0. pkill -f 'shell.py'                       # retire the manual shell first
1. mkdir -p $HOME/.config/systemd/user
2. ln -sf $HOME/downloads/director-drop/leo/leo.service $HOME/.config/systemd/user/leo.service
   ln -sf $HOME/downloads/director-drop/leo/leo-watchdog.service $HOME/.config/systemd/user/leo-watchdog.service
   ln -sf $HOME/downloads/director-drop/leo/leo-watchdog.timer $HOME/.config/systemd/user/leo-watchdog.timer
3. systemctl --user daemon-reload
4. sudo loginctl enable-linger iwashere      # user units survive boot without a login
5. systemctl --user enable --now leo-watchdog.timer

Do NOT enable leo.service itself - the watchdog owns when Leo runs.

## Knobs (optional): create $HOME/.leo-watchdog.conf

# botcmd UI knob (goes in ~/.botcmd.conf, not here):
#   BOTCMD_NO_REFRESH=1   menus append (scroll) instead of redrawing in place.
#   Default: menus redraw via cls() - terminfo 'clear' when TERM is usable,
#   raw ANSI fallback when TERM is unset/dumb (Windows ssh clients), and no
#   output at all when piped (captured output stays clean).

ON_H=8                                          # server-local hour to come online
OFF_H=22                                        # server-local hour to go offline
GROUP="Cecil Rydia Shadow Vivi Aeris Aerith"  # space-separated toon names (both Aeri* spellings until verified in the DB: SELECT name FROM acore_characters.characters WHERE name LIKE 'Aeri%')
AUTORITUAL=1                                    # 1 = ritual after Leo logs in
AUTORITUAL_CLEAR=1                              # 1 = on the next tick after the auto-ritual, /w 'leave' every non-G1 member of Leo's auto-formed group (the mod auto-joins bots on login - see "Auto raid" below)
RITUAL_ACCOUNTS="I_WAS_HERE2ND I_WAS_HERE3RD I_WAS_HERE4TH I_WAS_HERE5TH"
RITUAL_BOTS="Golbez"                              # only for bots NOT on a RITUAL_ACCOUNTS account (covered bots are skipped automatically)
# NEVER add the account you hand-play on to RITUAL_ACCOUNTS - hand-play toons
# are never bots. (Default master account is I_WAS_HERE1ST - see 'botcmd master'.)

## Receipts

- systemctl --user status leo
- journalctl --user -n 20 -u leo-watchdog.service     # watchdog decisions
- python3 director.py census                          # roster online states
- tail -n 20 daemon.log                               # director banner

## Notes

- Hours use the SERVER clock. Run date once: if it prints UTC, either
  sudo timedatectl set-timezone America/Vancouver or shift ON_H/OFF_H.
- The watchdog reads characters.online via the read-only MySQL identity -
  same grants director.py uses, no new credentials.
- Re-added bots likely need nc strategy re-issue during sprees (known
  mod-playerbots behavior) - the ritual brings them ONLINE, not back to work.
- `bot addaccount` / `bot add` SKIP toons that are already online
  ("player already logged in" - PlayerbotMgr.cpp ProcessBotCommand); bot AI
  is only attached on a fresh login. `botcmd roster ritual` therefore prints
  the toons currently ONLINE and warns: log them out first, then re-run the
  ritual. Until a toon has bot AI it will never answer `/w <toon> nc ?`
  (`botcmd states verify` shows NO REPLY for it).
- WHY offline is required (module design, not our choice): a playerbot is a
  separate headless WorldSession the worldserver creates; `add`/`addaccount`/
  `login` refuse any toon already in the world (`FindPlayer(guid)` check in
  PlayerbotMgr.cpp) and there is no path that attaches bot AI to a live human
  session. `bot remove` works while online because it logs the bot session
  out (LogoutPlayerBot) - so for any REGISTERED bot, `botcmd roster remove
  <toon>` is the graceful way down, no kick needed. `nc +`/`co +` etc. also
  work live on a running bot via whisper - offline is only needed to CONVERT
  a plain online toon into a bot. `botcmd roster down <toon|all>` automates
  that: `bot remove` first, and only if the toon is STILL online afterwards
  (plain session, remove answers "not your bot") it falls back to SOAP `.kick`
  (Console::Yes, needs gmlevel 3 on I_WAS_CMD, same grant as `.group remove`).
  `botcmd roster fix` = down all -> ritual -> verify, one command.
- director.py daemon (Leo's brain) runs as leo-director.service (see below).
- Bot-sender gate (2026-11-05 fix): senders on bot accounts (i_was_here% except
  the master account) or on Leo's shell account (i_was_cmd) are dropped by a
  read-only DB account check, and bot master-report lines ("Selling |c...",
  "=== ... ===") are dropped on sight. Only DIRECTOR_HANDPLAY toons on the
  master account plus guild members on non-bot accounts are authed. This does
  not need SOAP: '.playerbots inspect' does not exist in mod-playerbots
  @ e88cc40 and '.playerbots bot' needs an active session, so neither is
  usable from the SOAP console.

## The director daemon (leo-director.service)

The daemon under systemd too - pinned working directory, log always at
downloads/director-drop/daemon.log, restarts itself, starts at boot.

Defaults since the 2026-11 review fix: validated orders are HELD until the
master whispers 'go' (set DIRECTOR_CONFIRM=0 in the unit's Environment= to
auto-execute again); the purity rails (autogear/maintenance/equip
upgrade/reset botAI/drop/sell/buy/repair/bank/...) are FORBIDDEN in
validate() regardless of CONFIRM; audit.log/daemon.log/daemon.lock are
gitignored - never `git add -A` them into the public repo.

Install:

1. pkill -f 'director.py daemon'          # stop the manual daemon
2. ln -sf $HOME/downloads/director-drop/leo/leo-director.service $HOME/.config/systemd/user/leo-director.service
3. systemctl --user daemon-reload && systemctl --user enable --now leo-director

Receipts:

- systemctl --user status leo-director
- journalctl --user -n 30 -u leo-director        # everything stdout says
- tail -n 30 $HOME/downloads/director-drop/daemon.log

The watchdog only manages leo.service (the shell). leo-director always runs;
it is harmless when Leo is offline (its tail -F just waits for the log).

## Raid / group awareness (who is in the raid Leo leads)

Leo reads group membership straight from the characters DB (read-only):
`groups` + `group_member` are written live by the worldserver on create /
join / leave / leader-change / raid-convert (Group.cpp: CHAR_INS_GROUP,
CHAR_DEL_GROUP_MEMBER, CHAR_UPD_GROUP_LEADER, CHAR_UPD_GROUP_TYPE), so the DB
view matches the in-memory group for normal parties/raids (BG groups are not
persisted). NOTE: `groups` is a RESERVED word in MySQL 8 - backtick-quote it
in any hand-written query (use single quotes around the whole -e argument).

- CLI:      botcmd director raid [toon]        (default Leo)
  botcmd raid show [toon] / botcmd raid clear [toon] - clear = /w 'leave' every
  non-G1 member of the group (leave is an unsecured bot command,
  PlayerbotAI.cpp:1008); Leo and G1 are never touched; leaving keeps bot AI -
  re-mastering is `botcmd roster fix`. As leader Leo can also `.group remove <name>`
  from his own client.
            python3 director.py group Leo
- In-game:  the daemon appends "Leo's group: ..." to the roster message on
  every turn, and the LLM can attach the read-only query
  {"query":"raid","bot":"Leo"} (or any watched toon) to answer
  "who is in the raid / who leads it".
- Live server-side view over SOAP:  botcmd system cmd '.group list Leo'
  ('.group list' is Console::Yes, so SOAP can run it).

Removing someone from the raid Leo leads:

- BOT member:  order {"bot":"<name>","cmd":"group remove"} - validate()
  rewrites it to the plain "leave" whisper (/w <bot> leave ->
  LeaveGroupAction::Leave -> LeaveOrDisbandGroup). No GM level needed.
- HUMAN member: the same order is sent as the server command
  `.group remove <name>` from Leo's own session (FIFO line). '.group remove'
  is Console::No in AzerothCore, so SOAP CANNOT run it (SOAP queues console
  commands). Leo's account (I_WAS_CMD) needs the RBAC perm
  rbac_perm_command_group_remove - gmlevel 3 (Administrator) has all perms:
      mysql> SELECT id FROM acore_auth.account WHERE username='I_WAS_CMD';
      mysql> UPDATE acore_auth.account_access SET gmlevel=3 WHERE id=<id> AND RealmID=-1;
      -- or if no row exists:
      mysql> INSERT INTO acore_auth.account_access (id, gmlevel, RealmID) VALUES (<id>, 3, -1);
      then: systemctl --user restart leo.service   # security is cached at login
  Until then bot members still leave via 'leave'; only human-member removal
  needs the perm.

Hard rails (validate()): "group remove" only ever targets a CURRENT member of
Leo's own group (live DB membership check), never Leo himself, never the
DIRECTOR_HANDPLAY toons, never bystanders. The headless chat client cannot
send CMSG_GROUP_UNINVITE itself (chat-only client, no group opcodes), which
is why removal goes through the bot 'leave' or the '.group remove' command.

## Auto raid: why Leo's raid keeps re-forming, and the auto-clear

The mod ITSELF pulls every bot into Leo's group on login - there is no conf
key that disables it:

- `PlayerbotMgr::HandlePlayerBotLoginCallback` (PlayerbotMgr.cpp:591-619): on
  EVERY bot login, if its master (Leo) is online and in a group, a
  `GroupInviteOperation(master, bot)` is queued; if Leo has NO group, one is
  created and the bot added.
- `GroupInviteOperation::Execute` (PlayerbotOperations.h:37-90): creates the
  group if needed, `ConvertToRaid()` when members >= 5 (a party only holds 5),
  `AddMember(target)`, and if `SummonWhenGroup=1` (default, playerbots.conf.dist:193)
  summons the bot to Leo beyond SightDistance.
- The conf group options do NOT gate this: GroupInvitationPermission=1 (:150),
  KeepAltsInGroup=0 (:155), LogInGroupOnly=1 (:2564) are about other flows.
  Bots DO auto-leave when the master leaves (KeepAltsInGroup=0 ->
  LeaveOrDisbandGroup, PlayerbotMgr.cpp:~560), so the raid dissolves on Leo
  logout - the problem is only while Leo is online.
- Why it matters beyond questing comfort: open-world kill XP uses
  `xp_in_group_rate` (Formulas.h:119-150) = 1.4 TOTAL for >=5 members and is
  then SPLIT across all members in reward distance (KillRewarder.cpp:264-274)
  - in a 17-man raid each bot gets ~1.4/17 (~8%) of a solo kill. (`isRaid`
  rate 1.0 applies only inside raid-instance maps.) Quest turn-in XP is NOT
  split (RewardPlayerAndGroupAtEvent, Player.cpp:13054).

So director-drop undoes the auto-join on both bring-online paths:

- CLI: `botcmd roster ritual` (and `roster fix`) now wait for the login wave
  to settle (online set stable across a poll, max `BOTCMD_RITUAL_SETTLE`
  seconds, default 60, in ~/.botcmd.conf), then `/w <m> leave` every non-G1,
  non-Leo member of Leo's group (`_ritual_settle_clear` in lib/botcmd/roster.sh,
  reusing `_group_members_of`/`_clear_group_whispers` from director.sh).
  `BOTCMD_RITUAL_AUTOCLEAR=0` disables it.
- Watchdog: the auto-ritual tick sets `ritual_pending=1` in
  ~/.leo-watchdog.state; the next tick (Leo online) sends `/w <m> leave` to
  every non-G1 member of Leo's group from the DB. `AUTORITUAL_CLEAR=0` in
  ~/.leo-watchdog.conf disables it.
- LLM: "invite" and "give leader" were removed from director.py's WHITELIST -
  the director may only SHRINK Leo's group ("leave" / membership-checked
  "group remove"), never grow one.

'leave' keeps bot AI (PlayerbotAI.cpp:1008 unsecured command) - re-mastering
stays with `botcmd roster fix`. Manual teardown stays `botcmd raid clear`.

## Master account rotation (botcmd master)

Which i_was_here* account is the HUMAN account is a runtime setting. One-time
install of the env plumbing:

    cp leo/director.env.example ~/.director.env && chmod 600 ~/.director.env
    mkdir -p ~/.config/systemd/user/leo-director.service.d
    cp leo/leo-director.service.d/override.conf ~/.config/systemd/user/leo-director.service.d/
    systemctl --user daemon-reload && systemctl --user restart leo-director

Then rotate with:  botcmd -> 9. Master   (or one-shot: `botcmd master set I_WAS_HERE5TH`)

It shows a table like:

     current: I_WAS_HERE1ST   (env file: /home/iwashere/.director.env)
      1. [X] I_WAS_HERE1ST    toons: Aeris Cecil* Rydia Shadow* Vivi
      2. [ ] I_WAS_HERE2ND    toons: ...
      3. [ ] I_WAS_HERE3RD    toons: ...
      4. [ ] I_WAS_HERE4TH    toons: ...
      5. [ ] I_WAS_HERE5TH    toons: Golbez*

Selecting one writes DIRECTOR_MASTER_ACCOUNT + DIRECTOR_HUMANS +
DIRECTOR_HANDPLAY (auto-filled from that account's toons, minus
BOTCMD_BOT_TOONS in ~/.botcmd.conf) into ~/.director.env, mirrors them into
~/.botcmd.conf, and restarts leo-director. So the "never command / never
bot-add the group I'm hand-playing" rule follows the rotation automatically.

RULE: only select a HUMAN-ONLY account. Every other i_was_here% account is a
bot account - the daemon drops its toons as senders (DB account check, no
SOAP needed) and never auths them.

## Moving a toon to another account (e.g. Golbez -> I_WAS_HERE5TH)

One-time admin steps - YOU run these against the DB; the tools stay read-only:

    # 1. create the account (worldserver console, or first client login):
    .account create I_WAS_HERE5TH <password>
    # 2. make sure the toon is OFFLINE.
    # 3. point the character at the new account:
    mysql> SELECT id FROM acore_auth.account WHERE username='I_WAS_HERE5TH';
    mysql> UPDATE acore_characters.characters SET account=<id> WHERE name='Golbez';
    # 4. I_WAS_HERE5TH is already in RITUAL_ACCOUNTS defaults now; if you keep
    #    a ~/.leo-watchdog.conf, add it there too. Verify:
    python3 director.py census        # Golbez listed under I_WAS_HERE5TH

## Account linking (trusted accounts) - required before addaccount of another account
- Cross-account altbots need a LINK: `add`/`addaccount` of a toon on ANOTHER
  account is refused unless that account is linked to the master's
  ("Failure: You are not allowed to control bot <name>") -
  PlayerbotMgr.cpp:110-116 and :747. Gate flag: `Playerbots.AllowTrustedAccountBots=1`
  (default on - playerbots.conf.dist:167, PlayerbotAIConfig.cpp:186).
- Links/keys live in the playerbots DB (`PlayerbotsDatabaseInfo`, dist default
  `acore_playerbots`, playerbots.conf.dist:2527):
  - `playerbots_account_links(account_id, linked_account_id)` - `link` inserts
    BOTH directions (PlayerbotMgr.cpp:1957-1963); `IsAccountLinked` reads it (:244-250).
  - `playerbots_account_keys(account_id, security_key)` - key stored as SHA-256 hex
    (PlayerbotMgr.cpp:1894-1913).
- In-game commands, SEC_PLAYER + Console::No (PlayerbotCommandScript.cpp:29-32),
  run from ANY toon in a client:
    .playerbots account setKey <key>           # on a toon of the account TO BE linked
    .playerbots account link <ACCOUNT> <key>   # from Leo (I_WAS_CMD): links ACCOUNT in
    .playerbots account linkedAccounts         # list (system message to the issuer)
    .playerbots account unlink <ACCOUNT>       # removes both directions (:2011-2020)
- botcmd wrappers (queued to Leo's FIFO): `botcmd roster link` (list),
  `roster link check <ACCT>`, `roster link <ACCT> <KEY>`, `roster link setkey <KEY>`,
  `roster link unlink <ACCT>` (Roster menu -> 8). The ritual warns per
  RITUAL_ACCOUNT when it is not linked to the shell account. Read-only DB view:
  acore_playerbots.playerbots_account_links.
- Typical fix for "Golbez (on I_WAS_HERE5TH) never gets bot AI":
    1) client-login any I_WAS_HERE5TH toon -> `.playerbots account setKey mykey` -> logout
    2) `botcmd roster link I_WAS_HERE5TH mykey`      (queued to Leo)
    3) `botcmd roster link check I_WAS_HERE5TH`      -> IS linked
    4) `botcmd roster ritual`                        -> addaccount I_WAS_HERE5TH attaches AI

## botcmd UI: states picker, zone travel, blank-Enter-back

- Menus: blank Enter (or `b`/`back`) = up one screen, everywhere.
- States -> "Pick a bot -> set its default state": paginated list of every
  watched alt bot (bot accounts + watched guild, minus G1 hand-play minus Leo),
  10 per page - keys `1-9` or `10+` pick a slot on the page, `0`/`n` next page, `p` prev, blank = back, or type a toon name (G1 names get a specific refusal).
  The state menu shows `[x]` on the bot's current conf state; picking a preset
  writes `Name: preset` into ~/.botcmd-states.conf (apply with `botcmd states apply`).
- `botcmd states apply` does NOT gate on `characters.online` and is read-modify-write
  since PR #34 (probe `nc ?`/`co ?` first, whisper only the diff, full push on no
  reply): every conf bot gets
  its `/w` lines, because a whisper to a toon not in the world is a harmless no-op
  and the DB flag is not ground truth anyway (worldserver boot resets ALL flags to
  0 - Main.cpp:520 - and real logins rewrite them async - CharacterHandler.cpp:921).
  The flag is printed inline as info only; `botcmd states verify` (pure 'nc ?'/'co ?'
  whisper read-back - the state table is built from the replies alone) is the source of
  truth for who actually received the orders.
  `botcmd states verify all force` additionally probes bots the DB flags offline.
- `botcmd states probe [bot|all] [force] [cmd...]` is a raw whisper passthrough
  (default `nc ?`): Leo sends `/w <bot> <cmd>` and ALL reply lines per bot are
  printed (verify reads the `Strategies:` reply line for its verdict - probe shows ALL
  lines, e.g. `No quest available; use 'do quest <id>'`). Source notes:
  `nc ?`/`co ?` list the ENABLED strategies of that engine (Engine.cpp:524
  ListStrategies; +/- at :676-694) and reply via TellMaster to the MASTER only
  (Engine.cpp:695); `rpg status do quest` needs a
  quest with a POI in the bot's log or it refuses (NewRpgAction.cpp:183-215);
  on IDLE the new-rpg engine re-rolls the status by RpgStatusProbWeight
  (NewRpgAction.cpp:250-256) - that is the REST/GO_GRIND/WANDER flip-flopping.
- Presets toggle the nc-engine strategies (`nc +new rpg,+quest,-grind,...` etc.;
  `quest`/`grind`/`gather` are real nc strategies, `new rpg` drives the rpg state
  machine and includes questing - see the `nc ?` list). Presets are nc-only since
  PR #34 - `rpg status do quest` is NOT sent anymore (it only SETS when a quest
  with a POI is already in the log - by id, or the first log quest - and refuses
  with `No quest available` otherwise, NewRpgAction.cpp:183-215 - a dead
  round-trip that stalled reply collection) -
  which is why the old status-based verify showed MISMATCH on bots with empty
  quest logs (verify now checks `nc ?` membership instead). See the
  actual quest log per bot, read-only from the DB: `botcmd states quests [bot|all]`
  (character_queststatus; 1=COMPLETE 3=INCOMPLETE 5=FAILED 6=REWARDED).
- Zone travel (verified vs mod-playerbots @ e88cc40): `go travel <zone>` matches
  destination titles by name (case-insensitive) and the engine level-filters
  destinations (TravelMgr level ranges vs bot level); rpg grind/quest/gather
  scans units near the bot's position, so send the bot to the zone FIRST:
    botcmd go <toon> <zone...>        # walk one bot now (G1 refused)
    botcmd states zones               # vanilla level->zone table
    conf recipe:  Gau: go travel Westfall ;; nc +new rpg,+grind,-follow
    via Leo:      /w Leo "send Gau to Westfall to grind"  (ORDER_RULES carries the table)
- Leo's instant ack whisper is now one of 24 random lines per message
  (customize: DIRECTOR_ACK="a;b;c" in ~/.director.env; empty disables).
- Leo says "I can't do that - <reason>" when a request is outside the
  whitelist/roster instead of improvising (ORDER_RULES).
- Master screen quits on Q (blank Enter is "back" in every submenu; at the
  master screen Enter just redraws, so you can spam Enter safely).
- `botcmd states setall [preset]` (or States menu -> 3) writes one preset
  (grind|quest|gather|leveling|follow|idle) into the conf for EVERY watched alt bot
  (G1 and Leo excluded) and applies it to all online bots in one go. With no
  argument it opens a `[x]` picker in the same style as the per-bot state menu
  (the box is ticked when every watched bot currently has that same state).
- After a set-all / apply-all, Leo automatically whispers each online bot
  `nc ?` (plus `co ?` when the bot's conf uses `co`), and botcmd tabulates the
  replies (`botcmd states verify [bot|all]`, States menu -> 8). The table is
  built PURELY from those whisper replies - no DB group map, no group column,
  no group banner, and no `rpg status` whisper (`rpg status` is a TRANSIENT
  state that re-rolls on IDLE by RpgStatusProbWeight, NewRpgAction.cpp:250-256
  - it never reflected the strategy set anyway). The VERDICT comes from `nc ?`
  strategy MEMBERSHIP vs the conf: `nc ?` whispers `ListStrategies()` = the
  ENABLED strategies only (Engine.cpp:524-538; +/- add/removeStrategy at
  :676-694) via TellMaster to the bot's MASTER (Engine.cpp:695,
  PlayerbotAI.cpp:3170) - so a `Strategies:` reply also proves Leo is that
  bot's master. Presets are strategy SETS, not rpg states: conf `quest` =
  `nc +new rpg,+quest,-grind,...` shows up as `quest`+`new rpg` present and
  `grind`/`gather`/`follow` absent in the `nc ?` list. Columns:
  BOT / CONF / NC(live) / CO(live) / VERDICT. Verdicts per bot:
  `OK (nc: <conf> on)` / `MISMATCH (nc: missing [..] still on [..]) - push
  with: botcmd states apply` / `NO REPLY [no-ai] ...` / custom confs show
  `custom conf - replies shown`; `verify all force` also probes DB-offline
  bots (row marked `db:offline - no reply expected...`).
  `botcmd states verify all remaster` additionally re-masters every SILENT bot
  first: ONLINE silent bots get `.playerbots bot remove <bot>` + `.playerbots bot add <bot>`;
  with `force`, DB-offline silent bots get a plain `.playerbots bot add <bot>` (already
  offline, so no remove needed - this repairs bots whose add never landed, e.g. after a
  pre-#38 remaster race). Leo's shell runs them,
  (remove works on any online registered bot - PlayerbotMgr.cpp:756-764; the add then
  attaches AI with master := the adder = Leo - OnBotLoginInternal SetMaster,
  PlayerbotMgr.cpp:1704). The remove is ASYNC (BotLogoutGroupCleanupOperation queued on the
  world thread, PlayerbotMgr.cpp:397-399), so the tool polls the DB until the toon is actually
  offline (`BOTCMD_REMASTER_OFFLINE_WAIT`, default 15s) BEFORE the add - otherwise AddPlayerBot
  silently no-ops (PlayerbotMgr.cpp:96-98) or refuses 'player already logged in' (:734-735) -
  then polls until the bot login lands (`BOTCMD_REMASTER_WAIT`, default 30s; the login is async
  via a DB query-holder callback, :162+), then a RECALL pass re-sends `nc ?/'co ?`
  in a fresh byte window; remastered rows are tagged `[remastered]` in the VERDICT.
  Why this exists: a bot with a NULL master is totally silent on `nc ?` (TellMaster:
  `if (!master) return false`, PlayerbotAI.cpp:3173-3177) - Leo logging out nulls his
  bots' master (RandomPlayerbotMgr.cpp:2677) and it is only re-set by re-adding the bot
  (:1704) or by a real player in the same GROUP logging in (RandomPlayerbotMgr.cpp:2748);
  since #33 the ritual clears the group, so after a `systemctl --user restart leo` the
  bots sit master-less until remastered. G1 is never remastered.
  `BOTCMD_VERIFY_DEBUG=1` additionally dumps the raw new `[name] [whisper]:`
  log lines, and `BOTCMD_VERIFY_WAIT=<seconds>` changes the default 20s
  collect window. A silent bot that answered whispers before is a
  master/level issue, not missing AI: `LevelFor` (PlayerbotSecurity.cpp)
  gives Leo ALLOW_ALL only as the bot's master; under-level queued commands
  are erased silently (PlayerbotAI.cpp:642-647) and bot senders get no deny
  whisper (PlayerbotSecurity.cpp:190). Fix: `botcmd roster fix` to re-master
  via the ritual, and/or grant gmlevel 3 on `I_WAS_CMD` (CanBeGameMaster ->
  ALLOW_ALL from every bot, PlayerbotSecurity.cpp:31). Master churn:
  Leo's logout nulls his bots' master (RandomPlayerbotMgr.cpp:2677) and a
  human logging into their group takes master (RandomPlayerbotMgr.cpp:2748).
- Leo can broadcast too: /w Leo "send everyone to Barrens to grind" -> the
  order {"bot":"ALL","cmd":"go travel Barrens"} is expanded by the daemon to
  one validated order per unit (never hand-play, never Leo). The daemon now
  paces orders at 1s instead of 2s, so a 25-bot broadcast no longer blocks
  the whisper loop for ~50s.
- Duplicate-instance guards: only ONE interactive botcmd menu can run at a
  time - a kernel flock on ~/.botcmd.lock (kill-safe: no stale lock files).
  One-shot commands (`botcmd status`, `director watch`, `roster census`, ...)
  are deliberately NOT locked and always work. `botcmd watchdog procs`
  (or Watchdog menu -> 4) is a live audit: daemon + leo shell process counts
  and whether the menu lock is held, each with a fix hint. leo-watchdog also
  logs a WARNING into the journal when it sees more than one daemon or more
  than one leo shell.

## Quest & RPG whisper commands (wiki Playerbot-Commands + source @ e88cc40)
- `rpg status do quest [id]` is NOT deprecated at e88cc40: TellRpgStatusAction
  (NewRpgAction.cpp:45-225) implements it (RPG_DO_QUEST branch :183-215), but SETs
  need master/GM (:60-69, else "Only your master or a GM can change my rpg status.")
  and the quest must already be in the bot's log with status INCOMPLETE/COMPLETE,
  else "Invalid quest" / "No quest available; use 'do quest <id>'" whispered to the issuer.
  Bare `rpg status` (read) is prefix-listed in unsecuredCommands (PlayerbotAI.cpp:1011)
  -> INVITE level, replies to the issuer. Every other command needs ALLOW_ALL at
  execution (PlayerbotAI.cpp:1092-1096: requiredLevel = unsecured-prefix ? INVITE : ALLOW_ALL;
  re-checked silently on execution, :642-647).
- `rpg do quest <id|quest-link>` is a SEPARATE command (StartRpgDoQuestAction,
  NewRpgAction.cpp:227-244): ChangeToDoQuest WITHOUT requiring the quest in the log,
  whispers "Start to do quest <id>" to the issuer. Despite the wiki's note it is still
  ALLOW_ALL at e88cc40 ("rpg do quest" is not in unsecuredCommands) - master/GM only.
- Both drive the NEW rpg engine (NewRpgInfo) - they only take effect while the
  new-rpg strategy is enabled; with it toggled off the state machine is not ticking.
- Quest log via whisper (TellMaster - MASTER-only reply, ListQuestsActions.cpp:11-40):
    quests                -> "--- Summary --- Total: X / 25 (incompleted: .., completed: ..)"
    quests all            -> full log with links (Incompleted / Completed sections)
    quests co | quests in -> completed / incompleted only
    quests travel         -> per-quest destinations/points summary (where the bot can go)
    quests travel detail  -> full detail incl. "[Active] traveling <dist> to <title>"
  Live from Leo: /w Locke quests all   |   via botcmd: botcmd states probe Locke "quests all"
  Fleet-wide read-only from the DB instead: botcmd states quests [bot|all]
  (character_queststatus; 1=COMPLETE 3=INCOMPLETE 5=FAILED 6=REWARDED).
- Other wiki quest commands (ChatTriggerContext.h: accept/drop/talk/rpg do quest):
  `accept [quest|*]`, `drop [quest]` (DropQuestAction), `r [item]` (choose reward),
  `[quest link]` (objectives status), `talk` (selected NPC), `u [game object]`,
  plus `los` (visible objects/creatures), `stats`, `who`, `spells` - all ALLOW_ALL.

## Apply = read-modify-write (`botcmd states apply [bot|all]`, States menu 4/5)

Since PR #34 the set-state path IS read-modify-write - the old separate
`states tune` is just an alias of `states apply` and the States menu has no
separate tune entry anymore. `states apply` does what you'd do by hand: Leo
whispers `/w <bot> nc ?` (and `co ?` when the bot's conf uses `co`) first, the
tool reads back the ENABLED strategy list (`Strategies: ...` via TellMaster ->
master Leo; the `nc` engine needs ALLOW_ALL = master/GM, Engine.cpp:524/676-695,
PlayerbotAI.cpp:1092-1096), diffs it against the conf's wants/nots (same
`_preset_wants_nots` table `verify` uses), and whispers ONLY the needed toggles,
e.g. `/w Gau nc +quest,-grind`. Bots already in sync get NO nc whisper; bots that
don't answer (offline / no bot AI / not master) get the full push as a fallback
(a whisper to an offline toon is a no-op anyway - still no characters.online
gate). Non-strategy conf lines you write explicitly (`go travel ...`, raw
commands) are always sent as-is. Runs `states verify` afterwards.
Knobs: `BOTCMD_APPLY_WAIT` (default 5s; old name `BOTCMD_TUNE_WAIT` still honored)
= per-pass reply wait; `BOTCMD_APPLY_FULL=1` = skip probing, blind full push
(also the automatic fallback when /tmp/leo.log is unreadable).

Presets are nc-only since PR #34: the `quest` preset no longer sends
`rpg status do quest` (see above). Questing = the nc strategies (`new rpg` +
`quest`); to force a specific quest by id use `rpg do quest <id>` (probe or raw
conf line, NewRpgAction.cpp:227-244).

## Mail / vendor / bank whisper commands (source-verified @ e88cc40)

Send from the human CLI with `botcmd states probe <bot> "<cmd>"` (the probe charset
now accepts item links: `|cff..|Hitem:..|h[Name]|h|r`). All replies go to the
bot's MASTER (Leo) via TellMaster. All of these need ALLOW_ALL (master/GM) at
execution EXCEPT `sendmail`, which is in `unsecuredCommands` (INVITE,
PlayerbotAI.cpp:1002-1012).

- `mail ?` — list mailbox: `=== Mailbox ===` then `#n subject, items/money, days`
  (MailAction.cpp TellMailProcessor). Needs a mailbox gameobject nearby, else
  "There is no mailbox nearby" (TellError).
- `mail take <filter>` / `mail delete <filter>` / `mail read <filter>` —
  filter = empty/`*` (all), `1-3` (1-based index range), `1,3` (index list)
  (MailProcessor::filterList). `take` also grabs COD money and checks bag space.
- `sendmail <item link>` — mail ONE item to the issuer's MASTER (Leo) —
  SendMailAction; needs `Playerbots.BotSendMailEnabled=1` (default 1,
  playerbots.conf.dist:798) and a mailbox nearby ("There is no mailbox nearby").
  `sendmail <money>` (e.g. `10g`, ChatHelper::parseMoney) sends money instead.
  More than one item -> "You can not request more than one item".
  So: `botcmd states probe Locke "sendmail |cffffffff|Hitem:16896:0:0:0:0:0:0:0:0|h[Major Soulstone]|h|r"`
  ships one item to Leo. NOTE: mail goes TO LEO (the master); there is no
  "sendmail <item> <other player>" form in this build.
- `s gray` | `s *` — sell all poor-quality items (SellAction). `s vendor` — sell
  items the vendor actually buys (SellVendorItemsVisitor). `s <quality> [all]`
  e.g. `s green all` (parseItemQuality). `s <item link>` — sell a specific item.
  Needs a vendor NPC nearby (UNIT_NPC_FLAG_VENDOR); receipts via TellMaster.
  Usage error text: `s gray/*/vendor/<quality> [all]/[item link]`. (`s` is the
  short trigger, `sell` the full one.)
- `repair` — RepairAllAction at a repair vendor (UNIT_NPC_FLAG_REPAIR nearby).
- `bank` / `bank ?` — list bank contents (BankAction; needs a banker nearby,
  "Cannot find banker nearby"); deposit/withdraw per BankAction.
- `wts [item link]` — "what to sell": item-usage advice (WtsAction).
- NO auction-house command in this build (no `ah` trigger in
  ChatCommandHandlerStrategy).

Purity note: director.py's FORBIDDEN list blocks `sell`/`buy`/`repair`/`bank`/
`destroy` from LLM-issued orders (Baron rails), so Leo-the-LLM won't vendor on his
own; `sendmail` and `mail` ARE whitelisted, so Leo can mail via chat orders. The
`botcmd states probe` passthrough above is human-driven and not gated by that
whitelist — the in-game master/GM gate still applies.
