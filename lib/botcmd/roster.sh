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

roster_ritual() {
  hdr "Summon full roster (ritual)"
  warn "G1 baked in: accounts $BOTCMD_RITUAL_ACCOUNTS"
  warn "             plus bots: $BOTCMD_RITUAL_BOTS - account I_WAS_HERE1ST is never touched"
  local a b
  for a in $BOTCMD_RITUAL_ACCOUNTS; do
    if game_send ".playerbots bot addaccount $a"; then okmsg "queued: addaccount $a"; sleep 1; fi
  done
  for b in $BOTCMD_RITUAL_BOTS; do
    if game_send ".playerbots bot add $b"; then okmsg "queued: bot add $b"; sleep 1; fi
  done
  warn "bots come up IDLE - strategies stay a manual spree (purity rule)"
}

roster_add() {  # $1 = toon name
  local name="${1:-}"
  [ -z "$name" ] && { errmsg "usage: botcmd roster add <name>"; return 1; }
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
  game_send ".playerbots bot remove $name" && okmsg "queued: bot remove $name"
}

roster_go() {  # botcmd go <toon> <zone words...> -> /w <toon> go travel <zone>
  local name="${1:-}"
  [ $# -ge 2 ] || { errmsg "usage: botcmd go <toon> <zone...>   e.g.: botcmd go Gau Westfall"; return 1; }
  if is_handplay "$name"; then errmsg "REFUSED: $name is hand-play (G1) - never commanded"; return 1; fi
  shift
  local zone="$*"
  game_send "/w $name go travel $zone" && okmsg "queued: /w $name go travel $zone"
  warn "receipt: botcmd leo chat 40  (bot answers 'Traveling to <zone>'; destinations are level-filtered by the engine)"
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
 6. Return to main menu   (blank Enter = back)
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
      6) return 0 ;;
      *) warn "invalid choice" ;;
    esac
  done
}
