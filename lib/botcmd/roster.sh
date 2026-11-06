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
    clear >/dev/null 2>&1 || :
    hdr "Roster (bots + watched guild)"
    cat <<'EOF'
 1. Who's online (census)
 2. Summon full roster (ritual)
 3. Add a bot
 4. Remove a bot
 5. Send a bot to a zone (go travel)
 6. Kick a toon out (SOAP .kick - no game client needed)
 7. FIX roster: kick all online bots + ritual + verify
 8. Return to main menu   (blank Enter = back)
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
      8) return 0 ;;
      *) warn "invalid choice" ;;
    esac
  done
}
