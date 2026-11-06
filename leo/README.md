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

ON_H=8                                          # server-local hour to come online
OFF_H=22                                        # server-local hour to go offline
GROUP="Cecil Rydia Shadow Vivi Aeris Aerith"  # space-separated toon names (both Aeri* spellings until verified in the DB: SELECT name FROM acore_characters.characters WHERE name LIKE 'Aeri%')
AUTORITUAL=1                                    # 1 = ritual after Leo logs in
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
  ritual. Until a toon has bot AI it will never answer `/w <toon> rpg status`
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

## botcmd UI: states picker, zone travel, blank-Enter-back

- Menus: blank Enter (or `b`/`back`) = up one screen, everywhere.
- States -> "Pick a bot -> set its default state": paginated list of every
  watched alt bot (bot accounts + watched guild, minus G1 hand-play minus Leo),
  10 per page - keys `1-9` or `10+` pick a slot on the page, `0`/`n` next page, `p` prev, blank = back, or type a toon name (G1 names get a specific refusal).
  The state menu shows `[x]` on the bot's current conf state; picking a preset
  writes `Name: preset` into ~/.botcmd-states.conf (apply with `botcmd states apply`).
- `botcmd states apply` does NOT gate on `characters.online`: every conf bot gets
  its `/w` lines, because a whisper to a toon not in the world is a harmless no-op
  and the DB flag is not ground truth anyway (worldserver boot resets ALL flags to
  0 - Main.cpp:520 - and real logins rewrite them async - CharacterHandler.cpp:921).
  The flag is printed inline as info only; `botcmd states verify` (rpg status
  read-back) is the source of truth for who actually received the orders.
  `botcmd states verify all force` additionally probes bots the DB flags offline.
- `botcmd states probe [bot|all] [force] [cmd...]` is a raw whisper passthrough
  (default `nc ?`): Leo sends `/w <bot> <cmd>` and ALL reply lines per bot are
  printed (verify keeps only the last line per bot - probe shows error whispers
  too, e.g. `No quest available; use 'do quest <id>'`). Source notes:
  `nc ?`/`co ?` list ALL registered strategies of that engine and reply via
  TellMaster to the MASTER only (Engine.cpp:695); `rpg status do quest` needs a
  quest with a POI in the bot's log or it refuses (NewRpgAction.cpp:183-215);
  on IDLE the new-rpg engine re-rolls the status by RpgStatusProbWeight
  (NewRpgAction.cpp:250-256) - that is the REST/GO_GRIND/WANDER flip-flopping.
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
  (grind|quest|gather|follow|idle) into the conf for EVERY watched alt bot
  (G1 and Leo excluded) and applies it to all online bots in one go. With no
  argument it opens a `[x]` picker in the same style as the per-bot state menu
  (the box is ticked when every watched bot currently has that same state).
- After a set-all / apply-all, Leo automatically whispers each online bot
  `rpg status` and botcmd tabulates the replies (`botcmd states verify
  [bot|all]`, States menu -> 8): conf vs live `Status: GO_GRIND / DO_QUEST /
  DO_GATHER / IDLE ...` with OK / MISMATCH / NO REPLY per bot. Source-verified:
  `rpg status` with no args whispers back `NewRpgInfo::ToString()`
  (NewRpgAction.cpp, TellRpgStatusAction). The full nc strategy set itself has
  no read-only view (`.playerbots bot` is in-game only).
  Any `[name] [whisper]:` reply line is captured (not just `Status:` lines);
  `BOTCMD_VERIFY_DEBUG=1` additionally dumps the raw new log lines, and
  `BOTCMD_VERIFY_WAIT=<seconds>` changes the default 20s collect window.
  The table also shows each bot's live GROUP/LEADER (read-only DB
  `groups`/`group_member`, backticked - RESERVED word in MySQL 8) plus Leo's
  own group, because silence is usually a security-level issue, not missing
  AI: `LevelFor` (PlayerbotSecurity.cpp) gives Leo ALLOW_ALL only as the
  bot's master, INVITE if the bot is groupless or leads its group, else
  TALK; bot senders get no deny whisper (PlayerbotSecurity.cpp:190) and
  under-level queued commands are erased silently (PlayerbotAI.cpp:642-647).
  Verdict tags: `[no-ai]` = no bot AI attached (groupless bot should answer -
  run `botcmd roster ritual`); `[group]` = bot sits in a group Leo is not in
  / is not leader of; `[master]` = same group as Leo but master != Leo, or
  the `rpg status` probe answered but the ALLOW_ALL set was dropped.
  Fix for [group]/[master]: grant gmlevel on `I_WAS_CMD` (CanBeGameMaster ->
  ALLOW_ALL from every bot, PlayerbotSecurity.cpp:31, needs RBAC_PERM_COMMAND_GM)
  and/or `botcmd roster fix` after Leo re-masters them. Master churn:
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
