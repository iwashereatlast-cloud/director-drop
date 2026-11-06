# Director control: the LLM daemon (leo-director.service, director.py).
# Lives in the repo dir, logs to daemon.log there.

director_restart() {
  hdr "Director daemon - restart"
  systemctl --user restart leo-director && okmsg "leo-director restarted" \
    || errmsg "restart failed"
  sleep 1
  director_log 5
}

director_status() {
  hdr "Director status (systemctl)"
  systemctl --user status leo-director --no-pager -n 3 || true
}

director_log() {  # $1 = number of lines
  local n="${1:-40}"
  hdr "daemon.log - last $n lines"
  if [ -r "$BOTCMD_DIRECTOR_LOG" ]; then
    tail -n "$n" "$BOTCMD_DIRECTOR_LOG"
  else
    errmsg "no $BOTCMD_DIRECTOR_LOG yet"
  fi
}

director_raid() {  # $1 = toon name (default Leo)
  local toon="${1:-Leo}"
  hdr "Group/raid of $toon (read-only DB: groups + group_member)"
  if ! python3 "$BOTCMD_REPO/director.py" group "$toon"; then
    errmsg "group query failed (RO mysql identity $BOTCMD_MYCNF up?)"
  fi
}

# Members of <toon>'s group/raid from the live DB (group_member self-join;
# the worldserver keeps it current on create/join/leave/convert), one name
# per line, including <toon> itself. Empty output = groupless or DB down.
# NOTE: `groups` is a RESERVED word in MySQL 8 - not used here, but backtick
# it in hand-written queries against it.
_group_members_of() {  # $1 = toon name -> member names on stdout
  case "$1" in
    ""|*[!A-Za-z0-9]*) return 1 ;;
  esac
  db_ro -N -B -e "SELECT c.name FROM acore_characters.characters lc JOIN acore_characters.group_member gm ON gm.memberGuid=lc.guid JOIN acore_characters.group_member gm2 ON gm2.guid=gm.guid JOIN acore_characters.characters c ON c.guid=gm2.memberGuid WHERE lc.name='$1' ORDER BY c.name" 2>/dev/null
}

# Members of <toon>'s group that may be asked to leave: everyone except
# <toon> itself and the G1 hand-play five.
_group_clear_targets() {  # $1 = toon name -> target names on stdout
  local leo="$1" m
  _group_members_of "$leo" | while IFS= read -r m; do
    [ -z "$m" ] && continue
    [ "$m" = "$leo" ] && continue
    is_handplay "$m" && continue
    printf '%s\n' "$m"
  done
}

# Ask every clearable member of <toon>'s group to leave:
# '/w <m> leave' - 'leave' is an unsecured bot command (PlayerbotAI.cpp:1008)
# and GM/master Leo passes the gate anyway. Leaving keeps bot AI (re-mastering
# stays with roster fix). Prints per-member lines + a summary.
_clear_group_whispers() {  # $1 = toon name
  local leo="$1" m left=0 targets
  targets=$(_group_clear_targets "$leo")
  if [ -z "$targets" ]; then
    warn "$leo has no clearable members (groupless, DB down, or G1-only) - nothing to clear"
    return 1
  fi
  while IFS= read -r m; do
    [ -z "$m" ] && continue
    if game_send "/w $m leave"; then okmsg "queued: /w $m leave"; left=$((left + 1)); sleep 1; fi
  done <<< "$targets"
  echo
  okmsg "asked $left member(s) of $leo's group to leave"
  warn "bots keep their AI when leaving - verify with: botcmd director raid $leo  (DB lags a moment)"
  return 0
}

# Clear Leo's raid/party: ask every member (except Leo and G1 hand-play) to
# leave via '/w <member> leave' - 'leave' is an unsecured bot command
# (PlayerbotAI.cpp:1008) and GM/master Leo passes the gate anyway. Group
# membership is read live from the DB (groups/group_member, backticked).
# NOTE: leaving the group does NOT remove bot AI and does not by itself
# re-master bots - re-mastering is roster fix (bot remove + addaccount via Leo).
# As raid leader Leo can also kick a member from his own client: .group remove <name>
director_raid_clear() {  # $1 = toon name (default Leo)
  local leo="${1:-Leo}" members m left=0 skipped=0
  case "$leo" in
    ""|*[!A-Za-z0-9]*) errmsg "toon names are letters/digits only - '$leo' rejected"; return 1 ;;
  esac
  hdr "Clear $leo's raid/party - /w <member> leave each non-G1 member"
  members=$(_group_members_of "$leo")
  if [ -z "$members" ]; then
    warn "$leo is not in any group (or DB read failed) - nothing to clear"
    return 1
  fi
  while IFS= read -r m; do
    [ -z "$m" ] && continue
    if [ "$m" = "$leo" ]; then skipped=$((skipped + 1)); continue; fi
    if is_handplay "$m"; then
      warn "skip $m: hand-play (G1) - never touched"
      skipped=$((skipped + 1))
      continue
    fi
    if game_send "/w $m leave"; then okmsg "queued: /w $m leave"; left=$((left + 1)); sleep 1; fi
  done <<< "$members"
  echo
  okmsg "asked $left member(s) to leave, $skipped skipped (Leo/G1)"
  warn "bots keep their AI when leaving - verify with: botcmd director raid $leo  (DB lags a moment)"
}

director_deploy() {
  hdr "Deploy update (git pull + compile check + restart)"
  local before after
  before=$(git -C "$BOTCMD_REPO" rev-parse --short HEAD 2>/dev/null || echo "?")
  if ! git -C "$BOTCMD_REPO" pull -q; then
    errmsg "git pull failed - NOT deploying (live daemon keeps running)"
    return 1
  fi
  after=$(git -C "$BOTCMD_REPO" rev-parse --short HEAD)
  okmsg "repo: $before -> $after"
  if ! python3 -m py_compile "$BOTCMD_REPO/director.py"; then
    errmsg "director.py does not compile - NOT restarting (live daemon keeps running)"
    return 1
  fi
  okmsg "director.py compiles"
  systemctl --user restart leo-director && okmsg "leo-director restarted" \
    || errmsg "restart failed"
  sleep 1
  tail -n 5 "$BOTCMD_DIRECTOR_LOG" 2>/dev/null
  warn "botcmd itself may have updated in the pull - relaunch it to pick up changes"
}

# Parse the DEPLOYED director.py so the list always matches what runs live.
# Range end-patterns anchor to line END: the real tuples close with a
# trailing ")" / "}" at the end of the last line, not at line start.
director_commands() {
  local d="$BOTCMD_REPO/director.py" wl count
  hdr "Leo's command list (parsed live from director.py)"
  [ -r "$d" ] || { errmsg "no $d - deploy state unknown"; return 1; }

  wl=$(awk '/^WHITELIST = \(/{f=1} f{print} f && /\)$/{exit}' "$d" \
      | grep -o '"[^"]*"' | tr -d '"')
  count=$(printf '%s\n' "$wl" | grep -c .)
  echo "== whisper commands the LLM may order ($count, prefix-matched) =="
  printf '%s\n' "$wl" | paste -sd' ' - | fold -s -w 78
  echo
  echo "== FORBIDDEN (purity rails - validate() refuses these ALWAYS, even with CONFIRM off) =="
  awk '/^FORBIDDEN = \(/{f=1} f{print} f && /\)$/{exit}' "$d" \
      | grep -o '"[^"]*"' | tr -d '"' | paste -sd' ' - | fold -s -w 78
  echo
  echo "== presence commands (Leo runs himself, not /w to a bot) =="
  awk '/^MASTER_CMDS = \(/{f=1} f{print} f && /\)$/{exit}' "$d" \
    | grep -o '"[^"]*"' | tr -d '"' | paste -sd' ' -
  echo
  echo "== group commands ('.group ...' from Leo's session; Console::No = NOT via SOAP) =="
  awk '/^GROUP_CMDS = \(/{f=1} f{print} f && /\)$/{exit}' "$d" \
    | grep -o '"[^"]*"' | tr -d '"' | paste -sd' ' -
  echo
  echo "== allowed cast spells (per profession job; spell-gated) =="
  awk '/^CAST_SPELLS = \{/{f=1} f{print} f && /\}$/{exit}' "$d" \
    | grep -o '"[^"]*"' | tr -d '"' | paste - - | sed 's/\t/: /'
  echo
  echo "== read-only fact queries Leo can run (max 3 per turn) =="
  awk '/^DB_QUERIES = \{/{f=1} f{print} f && /^\}/{exit}' "$d" \
    | grep -oE '^ +"[a-z]+":' | tr -d ' ":'
  echo
  echo "== source-audit notes (verbatim from director.py comments) =="
  sed -n '/^# whisper-bot command prefixes/,/^WHITELIST = /p' "$d" \
    | grep '^#' | sed 's/^# \{0,1\}//'
  echo
  warn "G1 hand-play five ($BOTCMD_HANDPLAY): orders on them are refused by the director too"
  echo "task recipes (quest/grind/gather/idle): botcmd states presets"
}

director_menu() {
  local c
  while :; do
    cls
    hdr "Director (LLM daemon: leo-director.service)"
    cat <<'EOF'
 1. Restart daemon
 2. Status (systemctl)
 3. Tail daemon.log (last 40 lines)
 4. Deploy update (git pull + compile check + restart)
 5. Leo's command list (what he can order)
 6. Watch daemon.log LIVE (tail -f - Ctrl-C to stop)
 7. Leo's group/raid (leader + members, read-only DB)
 8. Return to main menu
EOF
    printf '%s' "choice: "; IFS= read -r c
    case "$c" in
      1) director_restart; pause ;;
      2) director_status; pause ;;
      3) director_log 40; pause ;;
      4) director_deploy; pause ;;
      5) director_commands; pause ;;
      6) tail -f "$BOTCMD_DIRECTOR_LOG" ;;
      7) director_raid "${BOTCMD_LEO:-Leo}"; pause ;;
      8) return 0 ;;
      ""|b|back) return 0 ;;
      *) warn "invalid choice" ;;
    esac
  done
}
