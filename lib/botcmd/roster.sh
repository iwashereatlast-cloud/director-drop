# Roster control: live census, summon ritual, add/remove bots.
# G1 rules are baked in structurally: addaccount 1ST does not exist here,
# hand-play toons can never be added through this file.

roster_census() {
  hdr "Census (live DB, read-only) - bot accounts + $BOTCMD_WATCH_GUILD"
  local from="FROM acore_characters.characters c JOIN acore_auth.account a ON a.id=c.account LEFT JOIN acore_characters.guild_member gm ON gm.guid=c.guid"
  local where="WHERE (a.username LIKE '$BOTCMD_ACCOUNT_LIKE' OR gm.guildid IN (SELECT guildid FROM acore_characters.guild WHERE name='$BOTCMD_WATCH_GUILD'))"
  db_ro -t -e "SELECT c.name AS toon, LOWER(a.username) AS account, c.level AS lvl, CASE c.online WHEN 1 THEN 'ONLINE' ELSE 'off' END AS state, c.map AS map, CASE c.race WHEN 1 THEN 'Human' WHEN 2 THEN 'Orc' WHEN 3 THEN 'Dwarf' WHEN 4 THEN 'NightElf' WHEN 5 THEN 'Undead' WHEN 6 THEN 'Tauren' WHEN 7 THEN 'Gnome' WHEN 8 THEN 'Troll' WHEN 10 THEN 'BloodElf' WHEN 11 THEN 'Draenei' ELSE CONCAT('race', c.race) END AS race, CASE c.class WHEN 1 THEN 'Warrior' WHEN 2 THEN 'Paladin' WHEN 3 THEN 'Hunter' WHEN 4 THEN 'Rogue' WHEN 5 THEN 'Priest' WHEN 6 THEN 'DeathKnight' WHEN 7 THEN 'Shaman' WHEN 8 THEN 'Mage' WHEN 9 THEN 'Warlock' WHEN 11 THEN 'Druid' ELSE CONCAT('class', c.class) END AS class $from $where ORDER BY c.online DESC, a.username, c.name" || return 1
  local total online
  read -r total online < <(db_ro -N -B -e "SELECT COUNT(*), COALESCE(SUM(c.online),0) $from $where" 2>/dev/null || echo "? ?")
  echo
  okmsg "$online online of $total watched (accounts like '$BOTCMD_ACCOUNT_LIKE' + guild '$BOTCMD_WATCH_GUILD')"
  echo "hand-play five (G1, never bots): $BOTCMD_HANDPLAY"
}

# online watched bot toons, one per line (minus G1 minus Leo's shell account).
# 'bot addaccount' / 'bot add' SKIP online toons ("player already logged in",
# PlayerbotMgr.cpp ProcessBotCommand) - bot AI is only attached on a fresh
# login, so the ritual cannot fix a toon that is already online as a plain
# character. Such toons never answer '/w <toon> rpg status' (verify: NO REPLY).
_roster_online_watched() {
  local from="FROM acore_characters.characters c JOIN acore_auth.account a ON a.id=c.account LEFT JOIN acore_characters.guild_member gm ON gm.guid=c.guid"
  local where="WHERE (a.username LIKE '$BOTCMD_ACCOUNT_LIKE' OR gm.guildid IN (SELECT guildid FROM acore_characters.guild WHERE name='$BOTCMD_WATCH_GUILD')) AND a.username NOT LIKE '${BOTCMD_SHELL_ACCOUNT}%' AND c.online=1"
  db_ro -N -B -e "SELECT c.name $from $where ORDER BY c.name" 2>/dev/null \
    | awk -v hp="$BOTCMD_HANDPLAY" 'BEGIN{n=split(hp,a," "); for(i=1;i<=n;i++) e[tolower(a[i])]=1} !e[tolower($1)] {print}'
}

# The mod AUTO-JOINS every freshly logged-in bot into Leo's group:
# HandlePlayerBotLoginCallback (PlayerbotMgr.cpp:591-619) queues a
# GroupInviteOperation per bot when its master is online; Execute()
# (PlayerbotOperations.h:37-90) creates the group if Leo has none,
# ConvertToRaid()s at >=5 members, AddMember, and summons the bot when
# SummonWhenGroup=1 (default, playerbots.conf.dist:193). NO conf key
# disables that auto-join - so after queueing the ritual logins we wait for
# the login wave to settle, then /w each non-G1 member of Leo's group
# 'leave' (unsecured bot command, PlayerbotAI.cpp:1008; leaving keeps bot
# AI). Raid membership is not cosmetic: open-world kill XP is multiplied by
# xp_in_group_rate=1.4 (>=5 members, Formulas.h:119-150) and then split
# across all members in reward range (KillRewarder.cpp:264-274) - in a
# 17-man raid each bot gets ~1.4/17 of a solo kill. Quest turn-in XP is NOT
# split (RewardPlayerAndGroupAtEvent, Player.cpp:13054).
_ritual_settle_clear() {  # $1 = toon name (default Leo)
  local leo="${1:-${BOTCMD_LEO:-Leo}}" settle poll=5 waited=0 prev cur members oneline
  settle="${BOTCMD_RITUAL_SETTLE:-60}"
  case "$settle" in *[!0-9]*|"") settle=60 ;; esac
  hdr "Settle + clear: the mod auto-joined the fresh logins into $leo's group - undoing it"
  prev=$(_roster_online_watched); cur="$prev"
  while [ "$waited" -lt "$settle" ]; do
    sleep "$poll"; waited=$((waited + poll))
    cur=$(_roster_online_watched)
    [ "$cur" = "$prev" ] && break   # online set stable across a poll = login wave settled
    prev="$cur"
  done
  sleep 2   # grace for the QUEUED GroupInviteOperations to land in the world thread
  oneline=$(printf '%s ' $cur | sed 's/ *$//')
  okmsg "settled after ${waited}s - online watched now: ${oneline:-<none>}"
  members=$(_group_members_of "$leo")
  if [ -z "$members" ]; then
    okmsg "$leo is not in any group - nothing to clear (auto-join did not fire)"
    return 0
  fi
  warn "$leo is grouped with: $(printf '%s ' $members | sed 's/ *$//')"
  _clear_group_whispers "$leo"
}

roster_ritual() {
  hdr "Summon full roster (ritual)"
  warn "G1 baked in: accounts $BOTCMD_RITUAL_ACCOUNTS"
  warn "             plus bots: $BOTCMD_RITUAL_BOTS - account I_WAS_HERE1ST is never touched"
  local a b on
  on=$(_roster_online_watched)
  if [ -n "$on" ]; then
    warn "ONLINE right now - addaccount SKIPS online toons ('player already logged in'):"
    warn "  $(printf '%s ' $on)"
    warn "log those toons out first, then re-run the ritual - until they have bot AI they never answer /w"
  fi
  for a in $BOTCMD_RITUAL_ACCOUNTS; do
    _roster_link_ok "$a"; local lrc=$?
    if [ "$lrc" = "1" ]; then
      warn "NOT LINKED to $BOTCMD_SHELL_ACCOUNT - 'addaccount $a' will be refused ('You are not allowed to control bot ...', PlayerbotMgr.cpp:110-116,747)"
      warn "  fix: setKey on $a (on one of its toons in a client: .playerbots account setKey <KEY>), then: botcmd roster link $a <KEY>"
    elif [ "$lrc" = "2" ]; then
      warn "cannot verify link of $a (playerbots DB unreadable?) - in-game as Leo: .playerbots account linkedAccounts"
    fi
    if game_send ".playerbots bot addaccount $a"; then okmsg "queued: addaccount $a"; sleep 1; fi
  done
  for b in $BOTCMD_RITUAL_BOTS; do
    case "$b" in *[!A-Za-z0-9]*) errmsg "bad ritual bot name '$b' - skipped"; continue ;; esac
    # addaccount already adds EVERY toon on a RITUAL_ACCOUNTS account - a single
    # 'bot add' is only needed for bots on accounts NOT in that list (e.g. a bot
    # on the 1ST/master account). Skip covered bots to avoid console noise.
    local bacct covered=0 a2
    bacct=$(db_ro -N -B -e "SELECT LOWER(a.username) FROM acore_characters.characters c JOIN acore_auth.account a ON a.id=c.account WHERE c.name='$b'" 2>/dev/null | head -n 1)
    for a2 in $BOTCMD_RITUAL_ACCOUNTS; do
      [ "$(printf '%s' "$a2" | tr '[:lower:]' '[:upper:]')" = "$(printf '%s' "$bacct" | tr '[:lower:]' '[:upper:]')" ] && covered=1
    done
    if [ "$covered" = "1" ]; then
      warn "skip 'bot add $b': $b is on ${bacct:-unknown}, already covered by addaccount in this ritual"
      continue
    fi
    if game_send ".playerbots bot add $b"; then okmsg "queued: bot add $b"; sleep 1; fi
  done
  warn "bots come up IDLE - strategies stay a manual spree (purity rule)"
  if [ "${BOTCMD_RITUAL_AUTOCLEAR:-1}" = "1" ]; then
    _ritual_settle_clear "${BOTCMD_LEO:-Leo}"
  else
    warn "BOTCMD_RITUAL_AUTOCLEAR=0 - skipping the post-ritual group clear (bots will sit in ${BOTCMD_LEO:-Leo}'s raid)"
  fi
}

roster_add() {  # $1 = toon name
  local name="${1:-}"
  [ -z "$name" ] && { errmsg "usage: botcmd roster add <name>"; return 1; }
  case "$name" in *[!A-Za-z0-9]*) errmsg "toon names are letters/digits only - '$name' rejected"; return 1 ;; esac
  if [ "$(db_ro -N -B -e "SELECT online FROM acore_characters.characters WHERE name='$name'" 2>/dev/null | head -n 1)" = "1" ]; then
    warn "$name is ONLINE - 'bot add' is refused for online toons ('player already logged in');"
    warn "log the toon out first; bot AI attaches on the fresh login via add/addaccount"
  fi
  if is_handplay "$name"; then
    errmsg "REFUSED: $name is hand-play (G1) - never brought online as a bot."
    warn "   if $name is wrongly online as a bot, use REMOVE to stand them down"
    return 1
  fi
  game_send ".playerbots bot add $name" && okmsg "queued: bot add $name"
}

roster_remove() {  # $1 = toon name (allowed even for hand-play - remediation)
  local name="${1:-}"
  [ -z "$name" ] && { errmsg "usage: botcmd roster remove <name>"; return 1; }
  case "$name" in *[!A-Za-z0-9]*) errmsg "toon names are letters/digits only - '$name' rejected"; return 1 ;; esac
  game_send ".playerbots bot remove $name" && okmsg "queued: bot remove $name"
}

roster_go() {  # botcmd go <toon> <zone words...> -> /w <toon> go travel <zone>
  local name="${1:-}"
  [ $# -ge 2 ] || { errmsg "usage: botcmd go <toon> <zone...>   e.g.: botcmd go Gau Westfall"; return 1; }
  case "$name" in *[!A-Za-z0-9]*) errmsg "toon names are letters/digits only - '$name' rejected"; return 1 ;; esac
  if is_handplay "$name"; then errmsg "REFUSED: $name is hand-play (G1) - never commanded"; return 1; fi
  shift
  local zone="$*"
  game_send "/w $name go travel $zone" && okmsg "queued: /w $name go travel $zone"
  warn "receipt: botcmd leo chat 40  (bot answers 'Traveling to <zone>'; destinations are level-filtered by the engine)"
}

# Kick a toon's session via SOAP '.kick' (Console::Yes, rbac command_kick -
# needs gmlevel 3 on I_WAS_CMD, same grant as '.group remove'). This logs a
# toon out WITHOUT the game client or a worldserver restart, so the ritual's
# addaccount can attach bot AI on the fresh login.
_roster_kick() {  # $1 = toon name
  local out code
  if out=$(_soap_run ".kick $1" 2>/dev/null); then
    code=$(printf '%s\n' "$out" | head -n 1)
    if [ "$code" = "200" ]; then
      okmsg "kicked $1 via SOAP .kick - offline now, the ritual can add them as a bot"
      return 0
    fi
    warn "SOAP .kick $1 -> HTTP $code: $(printf '%s\n' "$out" | tail -n +2 | head -c 120)"
    return 1
  fi
  # SOAP gate not armed - fall back to Leo's own session (same rbac perm needed)
  if game_send ".kick $1"; then
    okmsg "queued via Leo's session: .kick $1   (result shows in the worldserver console)"
    return 0
  fi
  errmsg "kick $1 failed - arm SOAP (botcmd system arm) and check Leo is up (botcmd leo status)"
  return 1
}

# Graceful first: '.playerbots bot remove <toon>' works on any REGISTERED bot
# while online (LogoutPlayerBot logs the bot session out). Only if the toon is
# STILL online afterwards (plain character session, no bot AI - remove answers
# "not your bot") do we kick via SOAP, so the ritual's addaccount can attach
# bot AI on the fresh login.
_roster_down_one() {  # $1 = toon name
  local n="$1" on
  game_send ".playerbots bot remove $n" && okmsg "queued: bot remove $n (graceful - works if $n is a registered bot)"
  sleep "${BOTCMD_DOWN_WAIT:-3}"
  on=$(db_ro -N -B -e "SELECT online FROM acore_characters.characters WHERE name='$n'" 2>/dev/null | head -n 1)
  if [ "$on" = "1" ]; then
    warn "$n still online after 'bot remove' (not a registered bot - plain session) -> SOAP .kick"
    _roster_kick "$n"
  else
    okmsg "$n is offline now - the ritual can add them as a bot"
  fi
}

roster_down() {  # $1 = toon name or "all" - get online toons offline so addaccount can bot-ify them
  local target="${1:-}" n on acct rc=0
  [ -z "$target" ] && { errmsg "usage: botcmd roster down <toon|all>"; return 1; }
  if [ "$target" = "all" ]; then
    on=$(_roster_online_watched)   # G1 and Leo's shell account already excluded
    if [ -z "$on" ]; then okmsg "no online watched toons to take down right now"; return 0; fi
    for n in $on; do _roster_down_one "$n" || rc=1; done
    echo
    if [ "$rc" = "0" ]; then
      okmsg "$(printf '%s ' $on | wc -w) toon(s) handled - now run: botcmd roster ritual"
    else
      warn "some toons could not be taken down - see above; re-run the ritual for the rest"
    fi
    return $rc
  fi
  case "$target" in *[!A-Za-z0-9]*) errmsg "toon names are letters/digits only - '$target' rejected"; return 1 ;; esac
  if is_handplay "$target"; then
    errmsg "REFUSED: $target is hand-play (G1) - use 'botcmd roster remove $target' for the bot-remediation case; down never touches G1"
    return 1
  fi
  acct=$(db_ro -N -B -e "SELECT LOWER(a.username) FROM acore_characters.characters c JOIN acore_auth.account a ON a.id=c.account WHERE c.name='$target'" 2>/dev/null | head -n 1)
  case "$acct" in "${BOTCMD_SHELL_ACCOUNT}"*) errmsg "REFUSED: $target is on Leo's shell account ($acct) - never kicked"; return 1 ;; esac
  _roster_down_one "$target"
}

# Is account $1 linked to the shell account in the playerbots DB?
# rc: 0 = linked, 1 = not linked, 2 = cannot tell (DB/table unreadable).
# Source: IsAccountLinked (PlayerbotMgr.cpp:244-250) reads
# acore_playerbots.playerbots_account_links(account_id, linked_account_id);
# the access check that needs it is PlayerbotMgr.cpp:110-116 (+:747 addaccount).
_roster_link_ok() {
  local acct="$1" q
  case "$acct" in ""|*[!A-Za-z0-9_]*) return 2 ;; esac
  q=$(db_ro -N -B -e "SELECT COUNT(*) FROM acore_playerbots.playerbots_account_links l JOIN acore_auth.account a ON a.id=l.account_id JOIN acore_auth.account s ON s.id=l.linked_account_id WHERE (UPPER(a.username)=UPPER('$acct') AND UPPER(s.username)=UPPER('$BOTCMD_SHELL_ACCOUNT')) OR (UPPER(s.username)=UPPER('$acct') AND UPPER(a.username)=UPPER('$BOTCMD_SHELL_ACCOUNT'))" 2>/dev/null | head -n 1)
  [ -z "$q" ] && return 2
  case "$q" in *[!0-9]*) return 2 ;; esac
  [ "$q" -ge 1 ] 2>/dev/null && return 0
  return 1
}

# Account linking for cross-account altbots ("trusted accounts"):
#   Playerbots.AllowTrustedAccountBots=1 (default: playerbots.conf.dist:167,
#   PlayerbotAIConfig.cpp:186). add/addaccount of a toon on ANOTHER account is
#   allowed only if that account is linked to the master's (PlayerbotMgr.cpp:110-116,747).
#   Links/keys live in the playerbots DB (PlayerbotsDatabaseInfo, dist: acore_playerbots,
#   playerbots.conf.dist:2527): playerbots_account_links (both directions inserted,
#   PlayerbotMgr.cpp:1957-1963) and playerbots_account_keys (SHA-256 hex, :1894-1913).
#   In-game commands are SEC_PLAYER + Console::No (PlayerbotCommandScript.cpp:29-32):
#     .playerbots account setKey <key>           - run on a toon of the account TO BE linked
#     .playerbots account link <ACCOUNT> <key>   - run from Leo; links ACCOUNT into I_WAS_CMD
#     .playerbots account linkedAccounts         - list (PSendSysMessage to issuer)
#     .playerbots account unlink <ACCOUNT>       - deletes both directions (:2011-2020)
roster_link() {  # botcmd roster link [list] | check <ACCT> | <ACCT> <KEY> | setkey <KEY> | unlink <ACCT>
  local sub="${1:-list}" acct="${2:-}" key="${3:-}" rows a rc
  case "$sub" in
    list|"")
      hdr "Account links (read-only: acore_playerbots.playerbots_account_links + acore_auth.account)"
      rows=$(db_ro -t -e "SELECT a.username AS account, la.username AS linked_to FROM acore_playerbots.playerbots_account_links l JOIN acore_auth.account a ON a.id=l.account_id LEFT JOIN acore_auth.account la ON la.id=l.linked_account_id ORDER BY a.username, la.username" 2>/dev/null)
      if [ -z "$rows" ]; then
        warn "no rows - or acore_playerbots not readable with the RO identity (PlayerbotsDatabaseInfo default: acore_playerbots, playerbots.conf.dist:2527)"
        warn "live check instead, in-game as Leo: .playerbots account linkedAccounts (PlayerbotCommandScript.cpp:31)"
        return 1
      fi
      printf '%s\n' "$rows"
      echo
      for a in $BOTCMD_RITUAL_ACCOUNTS; do
        _roster_link_ok "$a"; rc=$?
        case "$rc" in
          0) okmsg "$a: linked to $BOTCMD_SHELL_ACCOUNT - addaccount will attach its toons" ;;
          1) warn "$a: NOT linked to $BOTCMD_SHELL_ACCOUNT - 'addaccount $a' refused; fix: botcmd roster link $a <KEY> (setKey on $a first)" ;;
          *) warn "$a: link state unknown (playerbots DB unreadable)" ;;
        esac
      done
      return 0
      ;;
    check)
      [ -n "$acct" ] || { errmsg "usage: botcmd roster link check <ACCOUNT>"; return 1; }
      case "$acct" in *[!A-Za-z0-9_]*) errmsg "account names are letters/digits/underscore - '$acct' rejected"; return 1 ;; esac
      hdr "Link check: $acct vs shell account $BOTCMD_SHELL_ACCOUNT"
      _roster_link_ok "$acct"; rc=$?
      case "$rc" in
        0) okmsg "$acct IS linked to $BOTCMD_SHELL_ACCOUNT (both directions stored, PlayerbotMgr.cpp:1957-1963) - add/addaccount from Leo passes the linked-account check (PlayerbotMgr.cpp:110-116, :747)"; return 0 ;;
        1) warn "$acct is NOT linked to $BOTCMD_SHELL_ACCOUNT - add/addaccount of its toons is refused ('Failure: You are not allowed to control bot ...')"
           warn "fix: 1) on a toon of $acct in a client: .playerbots account setKey <KEY>   (SHA-256 stored, PlayerbotMgr.cpp:1894-1913)"
           warn "     2) from Leo: botcmd roster link $acct <KEY>   (or in-game: .playerbots account link $acct <KEY>)"
           warn "     3) verify: botcmd roster link check $acct  /  in-game .playerbots account linkedAccounts"
           return 1 ;;
        *) warn "cannot read acore_playerbots.playerbots_account_links with the RO identity - in-game as Leo: .playerbots account linkedAccounts"; return 1 ;;
      esac
      ;;
    setkey)
      [ -n "$acct" ] || { errmsg "usage: botcmd roster link setkey <KEY>"; return 1; }
      case "$acct" in *[!A-Za-z0-9_.-]*) errmsg "key must be letters/digits/_/./- only - rejected"; return 1 ;; esac
      [ "${#acct}" -ge 4 ] || { errmsg "key must be at least 4 chars - rejected"; return 1; }
      hdr "Set security key on Leo's account ($BOTCMD_SHELL_ACCOUNT)"
      warn "this lets OTHER accounts link INTO $BOTCMD_SHELL_ACCOUNT - keep the key private"
      if game_send ".playerbots account setKey $acct"; then okmsg "queued: .playerbots account setKey (SHA-256 hex into acore_playerbots.playerbots_account_keys, PlayerbotMgr.cpp:1894-1913)"; return 0; fi
      return 1
      ;;
    unlink)
      [ -n "$acct" ] || { errmsg "usage: botcmd roster link unlink <ACCOUNT>"; return 1; }
      case "$acct" in *[!A-Za-z0-9_]*) errmsg "account names are letters/digits/underscore - '$acct' rejected"; return 1 ;; esac
      hdr "Unlink $acct from $BOTCMD_SHELL_ACCOUNT"
      if game_send ".playerbots account unlink $acct"; then okmsg "queued: .playerbots account unlink $acct (both directions deleted, PlayerbotMgr.cpp:2011-2020)"; return 0; fi
      return 1
      ;;
    *)
      # link <ACCT> <KEY>
      case "$sub" in *[!A-Za-z0-9_]*) errmsg "account names are letters/digits/underscore - '$sub' rejected"; return 1 ;; esac
      if [ "$(printf '%s' "$sub" | tr '[:lower:]' '[:upper:]')" = "$(printf '%s' "$BOTCMD_SHELL_ACCOUNT" | tr '[:lower:]' '[:upper:]')" ]; then
        errmsg "REFUSED: linking the shell account to itself is a no-op"; return 1
      fi
      [ -n "$acct" ] || { errmsg "usage: botcmd roster link <ACCOUNT> <KEY>"; return 1; }
      case "$acct" in *[!A-Za-z0-9_.-]*) errmsg "key must be letters/digits/_/./- only - rejected"; return 1 ;; esac
      [ "${#acct}" -ge 4 ] || { errmsg "key must be at least 4 chars - rejected"; return 1; }
      hdr "Link $sub into Leo's account ($BOTCMD_SHELL_ACCOUNT)"
      warn "prereq: the key must already be set ON $sub - on one of its toons in a client: .playerbots account setKey $acct"
      warn "        (else the link answers 'Invalid security key', PlayerbotMgr.cpp:1929-1949)"
      if game_send ".playerbots account link $sub $acct"; then
        okmsg "queued: .playerbots account link $sub <key> - inserts BOTH directions (PlayerbotMgr.cpp:1957-1963)"
        warn "verify: botcmd roster link check $sub   (in-game as Leo: .playerbots account linkedAccounts)"
        warn "then:   botcmd roster ritual  -> addaccount $sub attaches bot AI to its toons"
        return 0
      fi
      return 1
      ;;
  esac
}

# one-command repair: kick every online watched toon (they cannot have bot AI
# while their human session is up), then ritual (attaches AI on fresh login),
# then verify the live rpg statuses
roster_fix() {
  hdr "Roster fix: kick online watched toons -> ritual -> verify"
  warn "kicks every ONLINE watched toon (never G1, never Leo) - the only way to make"
  warn "addaccount attach bot AI: add/addaccount refuse online toons (module design)"
  roster_down all || warn "some kicks failed - continuing anyway"
  sleep 3
  roster_ritual
  echo
  states_verify all
}

roster_menu() {
  local c n z
  while :; do
    cls
    hdr "Roster (bots + watched guild)"
    cat <<'EOF'
 1. Who's online (census)
 2. Summon full roster (ritual)
 3. Add a bot
 4. Remove a bot
 5. Send a bot to a zone (go travel)
 6. Kick a toon out (SOAP .kick - no game client needed)
 7. FIX roster: kick all online bots + ritual + verify
 8. Account links (list | check ACCT | ACCT KEY | setkey KEY | unlink ACCT)
 9. Return to main menu   (blank Enter = back)
EOF
    printf '%s' "choice: "; IFS= read -r c
    case "$c" in
      ""|b|back) return 0 ;;
      1) roster_census; pause ;;
      2) roster_ritual; pause ;;
      3) printf '%s' "toon to add: "; IFS= read -r n; roster_add "$n"; pause ;;
      4) printf '%s' "toon to remove: "; IFS= read -r n; roster_remove "$n"; pause ;;
      5) printf '%s' "toon: "; IFS= read -r n
         printf '%s' "zone (e.g. Westfall, Barrens, Feralas - botcmd states zones): "; IFS= read -r z
         [ -n "$n" ] && [ -n "$z" ] && roster_go "$n" $z
         pause ;;
      6) printf '%s' "toon to kick (or 'all'): "; IFS= read -r n; roster_down "$n"; pause ;;
      7) roster_fix; pause ;;
      8) printf '%s' "link args (list | check I_WAS_HERE5TH | I_WAS_HERE5TH mykey | setkey mykey | unlink I_WAS_HERE5TH): "; IFS= read -r lnk
         roster_link $lnk; pause ;;
      9) return 0 ;;
      *) warn "invalid choice" ;;
    esac
  done
}
