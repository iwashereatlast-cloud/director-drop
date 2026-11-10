# Bot states: per-bot strategy assignments from one conf, applied as
# whisper lines (/w <bot> <cmd>) exactly like director.py order_line.
# Purity rule stays intact: this tool only SENDS the states you chose -
# it never invents them, never auto-applies on login. G1: hand-play toons
# are refused even if they appear in the conf.

states_presets() {
  cat <<'EOF'
Built-in presets (conf value = the preset word):
  grind    nc +new rpg,+grind,-quest,-gather,-follow
  quest    nc +new rpg,+quest,-grind,-gather,-follow     (nc-only since PR #34: 'rpg status do quest' removed - only works with a quest+POI already in the log, else it just asks for an id)
  gather   nc +new rpg,+gather,-grind,-quest,-follow ; rpg status do gather (needs Herbalism/Mining)
  leveling nc +new rpg,+quest,+grind,+gather,-follow   (quest+grind+gather combined; the gather
           strategy no-ops on bots without Herbalism/Mining, so it is safe fleet-wide)
  follow   nc +follow                 (follow-duty: cling to master; NOTE "+new +follow" is a silent no-op - strategies split on ',' only)
  idle     nc -new rpg,-grind,-quest,-gather,-follow ; rpg status idle   (stand down)
See what quests a bot actually has (read-only DB): botcmd states quests [bot|all]
Raw commands also work - separate multiple with ';;', e.g.:
  Crono: nc +new rpg,+grind,-follow ;; rpg status go grind
RPG semantics (mod-playerbots @ e88cc40): presets are nc-only - 'rpg status do
quest' is NOT sent anymore (it needs a quest with a POI in the bot's log - else
"No quest available" - NewRpgAction.cpp:183-215 - so it was a dead round-trip);
when the status hits IDLE the new-rpg engine re-rolls it randomly by
RpgStatusProbWeight (NewRpgAction.cpp:250-256). A durable 'quest' state = the nc
strategies (new rpg + quest) + quests in the log; read it back with:
  botcmd states verify                        # verdict = 'nc ?'/'co ?' strategy membership vs conf (table built from the replies alone)
  botcmd states apply [bot|all]               # set-state = read-modify-write: probe 'nc ?'/'co ?', whisper ONLY the diff (full push on no reply), then verify
  botcmd states probe all "nc ?"              # raw list (goes to master Leo)
  botcmd states probe all "rpg status do quest"   # MANUAL only - shows per-bot error whispers (not sent by presets anymore)
  botcmd states probe Locke "quests all"      # live quest log via whisper (TellMaster = master only, ListQuestsActions.cpp:11-40)
  botcmd states probe Locke "rpg do quest 153"    # set DO_QUEST by id/link WITHOUT needing it in the log (StartRpgDoQuestAction, NewRpgAction.cpp:227-244) - still ALLOW_ALL
Mail/vendor/bank (probe passes item links; all ALLOW_ALL except sendmail which is
unsecured/INVITE - replies go to master Leo; see README 'Mail / vendor / bank'):
  botcmd states probe all "mail ?"               # list each bot's mailbox (needs mailbox nearby)
  botcmd states probe Locke "sendmail |cffffffff|Hitem:16896:0:0:0:0:0:0:0:0|h[Major Soulstone]|h|r"   # mail ONE item to Leo
  botcmd states probe all "s vendor"             # sell what the vendor buys; also: s gray | s green all | s <item link>
  botcmd states probe Locke "repair"             # repair at a repair vendor; bank via "bank ?" (banker nearby)
Gotcha (receipt 2026-11-03): "new rpg" is ONE strategy name with a space -
remove it as the single token "-new rpg", never "-new,-rpg".
Zone recipes (go travel FIRST - the engine level-filters destinations and rpg
scans near the bot's position; see 'botcmd states zones' for the table):
  Gau:  go travel Westfall ;; nc +new rpg,+grind,-follow
  Marle: go travel Feralas ;; nc +new rpg,+gather,-follow ;; rpg status do gather
  (one-shot instead: botcmd go Gau Westfall)
EOF
}

# expand a conf value into one whisper command per line
# Presets are nc-ONLY (since PR #34): they toggle the nc-engine strategies (the
# nc ? list: 'new rpg' drives the rpg state machine and includes questing,
# 'grind'/'quest'/'gather' are the behavior strategies - StrategyContext.h,
# QuestStrategies.h, GrindingStrategy.h). 'rpg status do quest' was REMOVED from
# the quest preset: it only lands when the bot already has a quest with a POI in
# its log, otherwise the bot just whispers back "No quest available; use 'do quest
# <id>'" - a dead round-trip per bot that stalls apply/verify reply collection
# (NewRpgAction.cpp:183-215). It still works as a raw conf line or a manual probe;
# 'rpg do quest <id>' sets a quest by id without needing it in the log
# (NewRpgAction.cpp:227-244). 'rpg status do gather' needs Herbalism or Mining.
# On IDLE the new-rpg engine re-rolls the status randomly by RpgStatusProbWeight
# (NewRpgAction.cpp:250-256).
state_expand() {  # $1 = conf value
  case "$1" in
    grind)  printf '%s\n' 'nc +new rpg,+grind,-quest,-gather,-follow' ;;
    quest)  printf '%s\n' 'nc +new rpg,+quest,-grind,-gather,-follow' ;;
    gather) printf '%s\n' 'nc +new rpg,+gather,-grind,-quest,-follow' 'rpg status do gather' ;;
    leveling) printf '%s\n' 'nc +new rpg,+quest,+grind,+gather,-follow' ;;
    follow) printf '%s\n' 'nc +follow' ;;
    idle)   printf '%s\n' 'nc -new rpg,-grind,-quest,-gather,-follow' 'rpg status idle' ;;
    *)      printf '%s\n' "$1" | sed 's/ *;; */\n/g' ;;
  esac
}

# preset word -> expected nc strategy membership as "wants<TAB>nots" (csv).
# 'new rpg' is ONE strategy name containing a space - tokens split on ',' only.
# Used by states_verify (verdict) and states_tune (diff-apply) so both agree.
_preset_wants_nots() {  # $1 = preset word; prints "wants|nots"; rc 1 = not a preset
  # NOTE: separator is '|' not TAB - under 'IFS=$'\t' read' consecutive tabs
  # collapse (tab is IFS whitespace), which would swap empty fields (idle preset).
  case "$1" in
    grind)  printf 'new rpg,grind|quest,gather,follow\n' ;;
    quest)  printf 'new rpg,quest|grind,gather,follow\n' ;;
    gather) printf 'new rpg,gather|grind,quest,follow\n' ;;
    leveling) printf 'new rpg,quest,grind,gather|follow\n' ;;
    follow) printf 'follow|\n' ;;
    idle)   printf '|new rpg,grind,quest,gather,follow\n' ;;
    *) return 1 ;;
  esac
}

# ---- diff-apply (tune) helpers ----
# Parse a conf value into per-engine wants/nots from its nc/co lines.
# prints one line per engine: "nc<TAB>wants<TAB>nots" / "co<TAB>wants<TAB>nots".
# rc 1 when an engine line has a non +/- token (e.g. '~' toggle) - caller then
# pushes that engine's original lines verbatim (cannot diff a toggle).
_conf_engine_wants() {  # $1 = conf value (preset word or raw ';;'-separated cmds)
  local v="$1" line rest tok engine rc=0 pw
  local wants_nc="" nots_nc="" wants_co="" nots_co="" bad_nc=0 bad_co=0
  pw=$(_preset_wants_nots "$v")   # captured - must NOT leak to stdout
  if [ -n "$pw" ]; then
    IFS='|' read -r wants_nc nots_nc <<< "$pw"
    printf 'nc|%s|%s\n' "$wants_nc" "$nots_nc"
    return 0
  fi
  local -a toks=()
  while IFS= read -r line; do
    case "$line" in
      "nc "*) engine=nc; rest="${line#nc }" ;;
      "co "*) engine=co; rest="${line#co }" ;;
      *) continue ;;
    esac
    IFS=',' read -ra toks <<< "$rest"
    for tok in "${toks[@]}"; do
      tok=$(printf '%s' "$tok" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
      case "$tok" in
        +*) if [ "$engine" = nc ]; then wants_nc+="${wants_nc:+,}${tok#+}"; else wants_co+="${wants_co:+,}${tok#+}"; fi ;;
        -*) if [ "$engine" = nc ]; then nots_nc+="${nots_nc:+,}${tok#-}"; else nots_co+="${nots_co:+,}${tok#-}"; fi ;;
        *)  if [ "$engine" = nc ]; then bad_nc=1; else bad_co=1; fi ;;
      esac
    done
  done < <(state_expand "$v")
  [ "$bad_nc" = "1" ] && rc=1
  [ "$bad_co" = "1" ] && rc=1
  if [ -n "$wants_nc$nots_nc" ] || [ "$bad_nc" = "1" ]; then printf 'nc|%s|%s\n' "$wants_nc" "$nots_nc"; fi
  if [ -n "$wants_co$nots_co" ] || [ "$bad_co" = "1" ]; then printf 'co|%s|%s\n' "$wants_co" "$nots_co"; fi
  return $rc
}

# Compute the toggle string needed to go from an enabled set to wants/nots.
# $1 = "Strategies: a, b, c" reply (or ""), $2 = wants csv, $3 = nots csv.
# prints "+a,+b,-c" (empty = in sync). Empty $1 prints "!" (no info -> caller
# falls back to a full push).
_strategies_diff() {
  local reply="$1" wants="$2" nots="$3"
  [ -z "$reply" ] && { printf '!'; return 0; }
  local members="${reply#Strategies: }" norm w out="" missing="" extra=""
  norm=",$members,"; norm=$(printf '%s' "$norm" | sed 's/, */,/g')
  local -a warr=()
  if [ -n "$wants" ]; then
    IFS=',' read -ra warr <<< "$wants"
    for w in "${warr[@]}"; do case "$norm" in *",$w,"*) : ;; *) missing+="${missing:+,}+$w" ;; esac; done
  fi
  if [ -n "$nots" ]; then
    IFS=',' read -ra warr <<< "$nots"
    for w in "${warr[@]}"; do case "$norm" in *",$w,"*) extra+="${extra:+,}-$w" ;; esac; done
  fi
  out="$missing"; [ -n "$extra" ] && out+="${out:+,}$extra"
  printf '%s' "$out"
}

# NOTE: the LIVE strategy set IS readable after all: 'nc ?' whispers
# ListStrategies() - the ENABLED strategies only (Engine.cpp:524-538; the +/-
# toggles at Engine.cpp:676-694 add/remove from that same map) - via TellMaster
# to the bot's MASTER only (Engine.cpp:695, PlayerbotAI.cpp:3170), so the
# 'Strategies:' reply also proves mastership. states_verify() uses it for the
# verdict. 'rpg status' (no args) whispers back NewRpgInfo::ToString() ->
# "Status: GO_GRIND / DO_QUEST / ... " - a TRANSIENT state that re-rolls on
# IDLE by RpgStatusProbWeight (NewRpgAction.cpp:250-256), shown as info only.
# ('.playerbots bot <name>' is Console::No - no remote path can run it.)

_states_conf_entries() {  # prints "name<TAB>value" per conf line
  [ -r "$BOTCMD_STATES_CONF" ] || return 1
  grep -v '^[[:space:]]*#' "$BOTCMD_STATES_CONF" | grep -v '^[[:space:]]*$' | \
    while IFS=: read -r n v; do
      n=$(printf '%s' "$n" | xargs); v=$(printf '%s' "$v" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
      [ -n "$n" ] && [ -n "$v" ] && printf '%s\t%s\n' "$n" "$v"
    done
}

_states_online() {  # $1 = name -> prints 1/0/"" from live census
  db_ro -N -B -e "SELECT online FROM acore_characters.characters WHERE name='$1'" 2>/dev/null | head -n 1
}

states_show() {
  hdr "Bot states - conf + live ($BOTCMD_STATES_CONF)"
  if ! _states_conf_entries > /tmp/.botcmd-entries.$$ 2>/dev/null; then
    warn "no conf yet: $BOTCMD_STATES_CONF"
    warn "start one: botcmd states edit   (presets: botcmd states presets)"
    return 1
  fi
  local n v online g1
  printf '%-12s %-8s %-34s\n' "BOT" "STATUS" "CONF STATE"
  while IFS=$'\t' read -r n v; do
    if is_handplay "$n"; then g1=" (G1)"; else g1=""; fi
    online=$(_states_online "$n")
    [ "$online" = "1" ] && online="ONLINE" || { [ "$online" = "0" ] && online="off" || online="?"; }
    printf '%-12s %-8s %-34s\n' "$n$g1" "$online" "$(printf '%s' "$v" | cut -c1-34)"
  done < /tmp/.botcmd-entries.$$
  rm -f /tmp/.botcmd-entries.$$
  echo
  warn "purity rule: states are sent by YOU (apply), never auto-applied on login"
  warn "read back live strategies ('nc ?'/'co ?') with: botcmd states verify"
}

# Blind full push - fallback when the shell log is unreadable or BOTCMD_APPLY_FULL=1.
# NO online gate: every conf bot gets its /w lines - a whisper to a toon that is not
# in the world is a harmless no-op ("No player named ..."), and characters.online is
# NOT ground truth (worldserver boot resets all flags to 0 - Main.cpp:520 - and
# logins rewrite them async - CharacterHandler.cpp:921). The DB flag is printed
# inline as info only; states_verify read-back is the source of truth.
_states_apply_blind() {  # $1 = bot|all
  local target="${1:-all}"
  hdr "Apply states -> $target (BLIND full push - no probe)"
  if ! _states_conf_entries > /tmp/.botcmd-apply.$$ 2>/dev/null; then
    warn "no conf: $BOTCMD_STATES_CONF (botcmd states edit)"
    return 1
  fi
  local n v online cmd applied=0 refused=0
  while IFS=$'\t' read -r n v; do
    if [ "$target" != "all" ] && [ "$(printf '%s' "$n" | tr '[:upper:]' '[:lower:]')" != "$(printf '%s' "$target" | tr '[:upper:]' '[:lower:]')" ]; then
      continue
    fi
    if is_handplay "$n"; then
      errmsg "REFUSED: $n is hand-play (G1) - never commanded"
      refused=$((refused + 1))
      continue
    fi
    online=$(_states_online "$n")
    [ "$online" = "1" ] && online="db:online" || online="db:offline"
    while IFS= read -r cmd; do
      if game_send "/w $n $cmd"; then okmsg "queued: /w $n $cmd   ($online)"; sleep 1; fi
    done < <(state_expand "$v")
    applied=$((applied + 1))
  done < /tmp/.botcmd-apply.$$
  rm -f /tmp/.botcmd-apply.$$
  echo
  okmsg "states pushed to $applied bot(s)${refused:+, $refused refused (G1)} - read back with: botcmd states verify"
  return 0
}

states_apply() {  # $1 = bot|all (default all) - THE set-state path: read-modify-write.
  # Probe 'nc ?' (+'co ?' when the conf uses co) per conf bot, diff the ENABLED
  # strategy list against the conf wants/nots, whisper ONLY the delta ("nc +a,-b").
  # Bots that do not answer (offline / no bot AI / not master) get the FULL push as
  # fallback - still no online gate. Non-strategy conf lines (go travel, raw cmds,
  # explicitly written 'rpg status ...') are always sent as-is. Blind push instead
  # when the shell log is unreadable or BOTCMD_APPLY_FULL=1. Verifies after when
  # target=all. (Blind full push until PR #34; the separate 'states tune' is now an
  # alias of this - the probe+diff IS how states are set.)
  local target="${1:-all}" log="$BOTCMD_SHELL_LOG"
  if [ ! -r "$log" ] || [ "${BOTCMD_APPLY_FULL:-0}" = "1" ]; then
    [ -r "$log" ] || warn "no $log - cannot probe replies; blind full push (leo shell up? botcmd leo status)"
    [ "${BOTCMD_APPLY_FULL:-0}" = "1" ] && warn "BOTCMD_APPLY_FULL=1 - blind full push requested"
    _states_apply_blind "$target" || return 1
    if [ "$target" = "all" ] && [ "${BOTCMD_NO_VERIFY:-0}" != "1" ]; then
      echo
      states_verify all
    fi
    return 0
  fi
  hdr "Apply states -> $target (probe 'nc ?'/'co ?' first, whisper only the diff)"
  if ! _states_conf_entries > /tmp/.botcmd-apply.$$ 2>/dev/null; then
    warn "no conf: $BOTCMD_STATES_CONF (botcmd states edit)"; return 1
  fi
  local -a targets=()
  local n v
  if [ "$target" = "all" ]; then
    while IFS=$'\t' read -r n v; do targets+=("$n"); done < /tmp/.botcmd-apply.$$
  else
    case "$target" in *[!A-Za-z0-9]*) errmsg "toon names are letters/digits only - '$target' rejected"; rm -f /tmp/.botcmd-apply.$$; return 1 ;; esac
    targets+=("$target")
  fi
  local refused=0
  local -a live=()
  for n in "${targets[@]}"; do
    if is_handplay "$n"; then errmsg "REFUSED: $n is hand-play (G1) - never commanded"; refused=$((refused+1)); continue; fi
    live+=("$n")
  done
  rm -f /tmp/.botcmd-apply.$$
  [ "${#live[@]}" -eq 0 ] && { warn "no tunable bots in conf"; return 1; }

  declare -A want_co=() strat_nc=() strat_co=()
  local eng w nn
  for n in "${live[@]}"; do
    while IFS='|' read -r eng w nn; do
      [ "$eng" = co ] && want_co[$n]=1
    done < <(_conf_engine_wants "$(_states_conf_get "$n")")
  done

  # pass 1: 'nc ?' to every live bot; pass 2: 'co ?' only to bots with a co line
  local start; start=$(wc -c < "$log" 2>/dev/null); start=${start:-0}
  local max="${BOTCMD_APPLY_WAIT:-${BOTCMD_TUNE_WAIT:-5}}" waited line who txt
  apply_scan() { tail -c +"$((start + 1))" "$log" 2>/dev/null | grep -a -o '\[\([A-Za-z][A-Za-z]*\)\] \[whisper\]: Strategies: .*' || true; }
  apply_probe() {  # $1 = "nc ?"|"co ?", rest = bot names
    local cmd="$1"; shift
    local -a ns=("$@") all_done
    for n in "${ns[@]}"; do game_send "/w $n $cmd" || true; sleep 1; done
    waited=0
    while [ "$waited" -lt "$max" ]; do
      while IFS= read -r line; do
        [ -n "$line" ] || continue
        who=${line%%]*}; who=${who#\[}
        txt=${line#*\[whisper\]: }
        case "$cmd" in
          "nc ?") strat_nc[$who]="$txt" ;;
          "co ?") strat_co[$who]="$txt" ;;
        esac
      done <<< "$(apply_scan)"
      all_done=1
      for n in "${ns[@]}"; do
        if [ "$cmd" = "nc ?" ]; then [ -n "${strat_nc[$n]:-}" ] || all_done=0
        else [ -n "${strat_co[$n]:-}" ] || all_done=0; fi
      done
      [ "$all_done" = "1" ] && break
      sleep 1; waited=$((waited + 1))
    done
    start=$(wc -c < "$log" 2>/dev/null); start=${start:-0}   # next pass scans only new lines
  }
  apply_probe "nc ?" "${live[@]}"
  local -a co_names=()
  for n in "${live[@]}"; do [ -n "${want_co[$n]:-}" ] && co_names+=("$n"); done
  [ "${#co_names[@]}" -gt 0 ] && apply_probe "co ?" "${co_names[@]}"

  local applied=0 synced=0 pushed=0 online cmd delta parse_out parse_rc w_nc nn_nc w_co nn_co
  local -a lines=() nc_lines=() co_lines=() other=()
  for n in "${live[@]}"; do
    v=$(_states_conf_get "$n")
    online=$(_states_online "$n")
    [ "$online" = "1" ] && online="db:online" || online="db:offline"
    lines=(); nc_lines=(); co_lines=(); other=()
    mapfile -t lines < <(state_expand "$v")
    for cmd in "${lines[@]}"; do
      case "$cmd" in
        "nc "*) nc_lines+=("$cmd") ;;
        "co "*) co_lines+=("$cmd") ;;
        *) other+=("$cmd") ;;
      esac
    done
    parse_rc=0; parse_out=$(_conf_engine_wants "$v") || parse_rc=1
    w_nc=""; nn_nc=""; w_co=""; nn_co=""
    while IFS='|' read -r eng w nn; do
      [ "$eng" = nc ] && { w_nc="$w"; nn_nc="$nn"; }
      [ "$eng" = co ] && { w_co="$w"; nn_co="$nn"; }
    done <<< "$parse_out"
    if [ "${#nc_lines[@]}" -gt 0 ]; then
      if [ "$parse_rc" = "1" ]; then delta="!"; else delta=$(_strategies_diff "${strat_nc[$n]:-}" "$w_nc" "$nn_nc"); fi
      if [ "$delta" = "!" ]; then
        for cmd in "${nc_lines[@]}"; do game_send "/w $n $cmd" && okmsg "queued: /w $n $cmd   ($online, no/bad nc ? reply -> full push)"; sleep 1; done
        pushed=$((pushed + 1))
      elif [ -n "$delta" ]; then
        game_send "/w $n nc $delta" && okmsg "queued: /w $n nc $delta   ($online, diff from 'nc ?')"
        sleep 1; applied=$((applied + 1))
      else
        synced=$((synced + 1))
      fi
    fi
    if [ "${#co_lines[@]}" -gt 0 ]; then
      if [ "$parse_rc" = "1" ]; then delta="!"; else delta=$(_strategies_diff "${strat_co[$n]:-}" "$w_co" "$nn_co"); fi
      if [ "$delta" = "!" ]; then
        for cmd in "${co_lines[@]}"; do game_send "/w $n $cmd" && okmsg "queued: /w $n $cmd   ($online, no/bad co ? reply -> full push)"; sleep 1; done
      elif [ -n "$delta" ]; then
        game_send "/w $n co $delta" && okmsg "queued: /w $n co $delta   ($online, diff from 'co ?')"
        sleep 1
      fi
    fi
    # non-strategy lines always go through (go travel, raw cmds, explicit 'rpg status ...')
    for cmd in "${other[@]}"; do
      game_send "/w $n $cmd" && okmsg "queued: /w $n $cmd   ($online)"; sleep 1
    done
  done
  echo
  okmsg "apply done: $applied bot(s) got a diff whisper, $synced already in sync (no nc whisper), $pushed full-pushed (no reply)${refused:+, $refused refused (G1)}"
  warn "read back with: botcmd states verify"
  if [ "$target" = "all" ] && [ "${BOTCMD_NO_VERIFY:-0}" != "1" ]; then
    echo
    states_verify all
  fi
}


# alias - probe+diff IS the set-state path now (PR #34); kept so 'botcmd states tune' works
states_tune() { states_apply "$@"; }

states_edit() {
  hdr "Edit states conf ($BOTCMD_STATES_CONF)"
  if [ ! -r "$BOTCMD_STATES_CONF" ]; then
    warn "no conf yet - creating a starter conf"
    cat > "$BOTCMD_STATES_CONF" <<'EOF'
# botcmd states conf - one line per bot:  Name: preset-or-commands
# presets: grind quest gather leveling follow idle   (botcmd states presets)
# raw commands work too - separate multiple with ';;'
# Crono: grind
# Edge: follow
# Marle: nc +new rpg,+grind,-follow ;; rpg status go grind
EOF
    okmsg "starter written - edit and remove the # comments as you go"
  fi
  "${EDITOR:-nano}" "$BOTCMD_STATES_CONF"
}

# every watched alt bot except G1 hand-play and Leo's shell account:
# prints "name<TAB>level<TAB>online<TAB>account"
_states_all_bots() {
  db_ro -N -B -e "SELECT c.name, c.level, c.online, LOWER(a.username) FROM acore_characters.characters c JOIN acore_auth.account a ON a.id=c.account LEFT JOIN acore_characters.guild_member gm ON gm.guid=c.guid WHERE (a.username LIKE '$BOTCMD_ACCOUNT_LIKE' OR gm.guildid IN (SELECT guildid FROM acore_characters.guild WHERE name='$BOTCMD_WATCH_GUILD')) AND a.username NOT LIKE '${BOTCMD_SHELL_ACCOUNT}%' ORDER BY c.online DESC, c.name" 2>/dev/null \
    | awk -F'\t' -v hp="$BOTCMD_HANDPLAY" 'BEGIN{n=split(hp,a," "); for(i=1;i<=n;i++) e[tolower(a[i])]=1} !e[tolower($1)] {print}'
}

_states_conf_get() {  # $1=name -> conf value or empty
  [ -r "$BOTCMD_STATES_CONF" ] || return 0
  awk -F: -v n="$1" '!/^[[:space:]]*#/ && NF>1 {k=$1; gsub(/[[:space:]]/,"",k); if (tolower(k)==tolower(n)) {sub(/^[^:]*:[[:space:]]*/,""); gsub(/[[:space:]]+$/,""); print; exit}}' "$BOTCMD_STATES_CONF"
}

_states_conf_set() {  # $1=name $2=value (empty removes the line)
  _ini_set "$BOTCMD_STATES_CONF" "$1" "$2" ":"
}

# paginated bot picker; prints the chosen name, rc 1 = backed out (blank/b/back)
states_pick_bot() {
  local -a rows=()
  local total pagesize=10 page=0 pages i slot name lvl onl acct cur c
  mapfile -t rows < <(_states_all_bots)
  total=${#rows[@]}
  if [ "$total" -eq 0 ]; then
    errmsg "no alt bots in census (accounts like '$BOTCMD_ACCOUNT_LIKE' + guild '$BOTCMD_WATCH_GUILD', minus G1, minus Leo)"
    return 1
  fi
  pages=$(( (total + pagesize - 1) / pagesize ))
  while :; do
    cls
    hdr "Pick a bot - page $((page + 1))/$pages, $total bots  (1-9 or 10+ = slot on this page, 0 or n = next page, p = prev, blank = back, or type a name)" >&2
    i=$((page * pagesize)); slot=1
    while [ "$i" -lt "$total" ] && [ "$slot" -le "$pagesize" ]; do
      IFS=$'\t' read -r name lvl onl acct <<< "${rows[$i]}"
      cur=$(_states_conf_get "$name"); [ -n "$cur" ] || cur="-"
      [ "$onl" = "1" ] && onl="ONLINE" || onl="off"
      printf '%2d. %-12s lvl %-3s %-6s %-16s state: %s\n' "$slot" "$name" "$lvl" "$onl" "$acct" "$cur" >&2
      i=$((i + 1)); slot=$((slot + 1))
    done
    printf '%s' "choice: " >&2; IFS= read -r c
    case "$c" in
      ""|b|back) return 1 ;;
      n) [ $((page + 1)) -lt "$pages" ] && page=$((page + 1)) ;;
      p) [ "$page" -gt 0 ] && page=$((page - 1)) ;;
      [0-9]|[0-9][0-9]|[0-9][0-9][0-9])
        if [ "$c" = "0" ]; then
          # 0 = next page
          if [ $((page + 1)) -lt "$pages" ]; then
            page=$((page + 1))
          else
            warn "already on the last page ($pages/$pages)" >&2; sleep 1
          fi
        else
          # multi-digit number = slot on THIS page (10 = 10th entry)
          slot=$c
          i=$((page * pagesize + slot - 1))
          if [ "$i" -lt "$total" ]; then
            IFS=$'\t' read -r name _ _ _ <<< "${rows[$i]}"
            printf '%s' "$name"; return 0
          fi
          warn "no bot on slot $slot this page" >&2
        fi ;;
      *)
        case "$c" in *[!A-Za-z0-9]*)
          errmsg "toon names are letters/digits only - '$c' rejected" >&2; sleep 1; continue ;; esac
        if is_handplay "$c"; then
          errmsg "'$c' is G1 hand-play ($BOTCMD_HANDPLAY) - states are never set on them" >&2; sleep 1
        else
          local canon
          canon=$(printf '%s\n' "${rows[@]}" | awk -F'\t' -v n="$c" 'tolower($1)==tolower(n){print $1; exit}')
          if [ -n "$canon" ]; then
            printf '%s' "$canon"; return 0
          fi
          errmsg "'$c' is not a watched alt bot (accounts like '$BOTCMD_ACCOUNT_LIKE' + guild '$BOTCMD_WATCH_GUILD', minus G1, minus Leo)" >&2; sleep 1
        fi ;;
    esac
  done
}

# state menu for one bot, [x] marks the current conf state
states_set_bot() {  # $1 = bot name
  local bot="$1" cur c preset i p
  if is_handplay "$bot"; then errmsg "REFUSED: $bot is hand-play (G1) - never set states on them"; return 1; fi
  cur=$(_states_conf_get "$bot")
  while :; do
    cls
    hdr "Default state for $bot  (conf: $BOTCMD_STATES_CONF)"
    printf 'current conf state: %s\n\n' "${cur:-(none set)}"
    i=0
    for p in grind quest gather leveling follow idle; do
      i=$((i + 1))
      if [ "$cur" = "$p" ]; then printf '[x] %d. %s\n' "$i" "$p"
      else printf '[ ] %d. %s\n' "$i" "$p"; fi
    done
    cat <<'EOF'
 0. Remove from conf
    blank Enter = back
EOF
    printf '%s' "choice: "; IFS= read -r c
    case "$c" in
      ""|b|back) return 0 ;;
      0) _states_conf_set "$bot" "" && okmsg "$bot removed from states conf"; return 0 ;;
      1|2|3|4|5|6)
        case "$c" in 1) preset=grind;; 2) preset=quest;; 3) preset=gather;; 4) preset=leveling;; 5) preset=follow;; 6) preset=idle;; esac
        if _states_conf_set "$bot" "$preset"; then
          okmsg "$bot: $preset   (push it live with: botcmd states apply $bot)"
        else
          errmsg "write to $BOTCMD_STATES_CONF failed"
        fi
        return 0 ;;
      *) warn "invalid choice" ;;
    esac
  done
}

states_pick_and_set() {
  local bot
  bot=$(states_pick_bot) || return 0
  [ -n "$bot" ] || return 0
  states_set_bot "$bot"
}

# vanilla level -> zone table for 'go travel <zone>'
states_zones() {
  hdr "Vanilla level -> zone   (send with: botcmd go <toon> <zone>  or  /w <toon> go travel <zone>)"
  cat <<'EOF'
 1-10   Elwynn Forest, Dun Morogh, Teldrassil, Mulgore        (racial starts)
10-20   Westfall, Loch Modan, Darkshore, The Barrens
20-30   Redridge Mountains, Wetlands, Duskwood, Ashenvale, Silverpine Forest, Hillsbrad Foothills
30-40   Desolace, Arathi Highlands, Alterac Mountains, Stranglethorn Vale, Badlands, Dustwallow Marsh, Thousand Needles
40-50   Tanaris, Feralas, The Hinterlands, Swamp of Sorrows, Azshara
50-60   Western Plaguelands, Eastern Plaguelands, Burning Steppes, Silithus, Winterspring, Un'Goro Crater, Felwood
EOF
  echo "destinations match by name (case-insensitive) and are level-filtered by the engine;"
  echo "rpg grind/quest/gather scans near the bot, so go travel <zone> FIRST, then the rpg order."
}

# preset picker for setall - same [x] style as states_set_bot. The box is
# ticked when EVERY watched bot currently has that same conf state.
# Menu goes to stderr; only the chosen preset is printed on stdout (rc 1 = back).
states_pick_preset() {
  local presets=(grind quest gather leveling follow idle) c i p vals nbots uniq_n uniform=""
  nbots=$(_states_all_bots | wc -l)
  vals=$(_states_all_bots | while IFS=$'\t' read -r n _ _ _; do _states_conf_get "$n"; done)
  uniq_n=$(printf '%s\n' "$vals" | grep -v '^$' | sort -u | wc -l)
  if [ "$uniq_n" = "1" ] && [ "$(printf '%s\n' "$vals" | grep -cv '^$')" = "$nbots" ] && [ "$nbots" -gt 0 ]; then
    uniform=$(printf '%s\n' "$vals" | grep -v '^$' | head -n 1)
  fi
  while :; do
    cls
    hdr "Set ALL bots to one state ($nbots watched bots)" >&2
    if [ -n "$uniform" ]; then
      printf 'all bots currently have conf state: %s\n\n' "$uniform" >&2
    else
      printf '(bots currently have mixed or no conf states)\n\n' >&2
    fi
    i=0
    for p in "${presets[@]}"; do
      i=$((i + 1))
      if [ "$uniform" = "$p" ]; then printf '[x] %d. %s\n' "$i" "$p" >&2
      else printf '[ ] %d. %s\n' "$i" "$p" >&2; fi
    done
    printf '%s\n' "    blank Enter = back" >&2
    printf '%s' "choice: " >&2; IFS= read -r c
    case "$c" in
      ""|b|back) return 1 ;;
      1|2|3|4|5|6) printf '%s' "${presets[$((c - 1))]}"; return 0 ;;
      *) warn "invalid choice" >&2 ;;
    esac
  done
}

# Read-back verification, built PURELY from whisper replies: Leo whispers
# 'nc ?' to every target (plus 'co ?' when the bot's conf uses co), we collect
# the "[Name] [whisper]: Strategies: ..." lines from the shell log and tabulate
# conf vs live. No DB group map, no 'rpg status' (transient - re-rolls on IDLE
# by RpgStatusProbWeight, NewRpgAction.cpp:250-256).
# Source-verified @ e88cc40: 'nc ?'/'co ?' whisper ListStrategies() - the ENABLED
# strategies (Engine.cpp:524-538; +/- at :676-694) - via TellMaster to the bot's
# MASTER only (Engine.cpp:695, PlayerbotAI.cpp:3170), so a 'Strategies:' reply
# also proves Leo is that bot's master. Verdict = strategy MEMBERSHIP vs conf
# (a preset like 'quest' is a strategy SET, not an rpg status).
states_verify() {  # args: [bot|all] [force] [remaster]
                    # force (or BOTCMD_VERIFY_FORCE=1) also probes bots the DB flags offline;
                    # remaster: every SILENT ONLINE bot gets '.playerbots bot remove' + 'bot add'
                    # from Leo's shell (remove works on online registered bots - PlayerbotMgr.cpp:756;
                    # add then attaches AI with master := the adder = Leo - OnBotLoginInternal
                    # SetMaster, PlayerbotMgr.cpp:1704; a null master is why 'nc ?' is silent -
                    # TellMaster 'if (!master) return false', PlayerbotAI.cpp:3173), then a recall
                    # pass re-sends 'nc ?'/'co ?' in a fresh byte window and the table is rebuilt
  local target="all" force="" remaster="" a log="$BOTCMD_SHELL_LOG" start off n online
  for a in "$@"; do
    case "$a" in
      force) force=force ;;
      remaster) remaster=1 ;;
      *) target="$a" ;;
    esac
  done
  [ "${BOTCMD_VERIFY_FORCE:-0}" = "1" ] && force=force
  hdr "Verify live states (Leo whispers 'nc ?' + 'co ?' where the conf uses co; verdict = strategy membership vs conf${remaster:+; remaster + recall})"
  if [ ! -r "$log" ]; then
    errmsg "no $log - is the Leo shell up? (botcmd leo status)"
    return 1
  fi
  local -a targets=() co_names=() remade=()
  declare -A t_on=() want_co=() rem_on=() need_remove=()
  if [ "$target" = "all" ]; then
    while IFS=$'\t' read -r n _ online _; do
      if [ "$online" = "1" ] || [ -n "$force" ]; then
        targets+=("$n"); t_on[$n]=$online
      fi
    done < <(_states_all_bots)
  else
    case "$target" in
      *[!A-Za-z0-9]*) errmsg "toon names are letters/digits only - '$target' rejected"; return 1 ;;
    esac
    if is_handplay "$target"; then
      errmsg "REFUSED: $target is hand-play (G1) - never commanded"; return 1
    fi
    online=$(_states_online "$target")
    if [ "$online" != "1" ]; then
      if [ -z "$force" ]; then warn "$target is not online - nothing to verify (or: states verify $target force)"; return 1; fi
      warn "$target shows offline in DB - probing anyway (force)"
    fi
    targets+=("$target"); t_on[$target]=$online
  fi
  if [ "${#targets[@]}" -eq 0 ]; then
    warn "no online watched bots to verify right now"
    return 1
  fi
  # targets whose conf uses co get a 'co ?' pass too (same rule as apply)
  local eng w nn w_nc nn_nc w_co nn_co
  for n in "${targets[@]}"; do
    while IFS='|' read -r eng w nn; do
      [ "$eng" = co ] && want_co[$n]=1
    done < <(_conf_engine_wants "$(_states_conf_get "$n")")
  done
  for n in "${targets[@]}"; do [ -n "${want_co[$n]:-}" ] && co_names+=("$n"); done

  start=$(wc -c < "$log" 2>/dev/null); start=${start:-0}
  declare -A reply_nc=() reply_co=() replies=()
  scan_range() {  # $1=byte start $2=byte end (exclusive) -> new "[Name] [whisper]: ..." lines in that window
    [ "$2" -gt "$1" ] || return 0
    tail -c +"$(( $1 + 1 ))" "$log" 2>/dev/null | head -c "$(( $2 - $1 ))" \
      | grep -a -o '\[\([A-Za-z][A-Za-z]*\)\] \[whisper\]: .*' || true
  }
  record() {  # $1 = raw line, $2 = engine tag (nc|co)
    local who txt
    who=${1%%]*}; who=${who#\[}
    txt=${1#*\[whisper\]: }
    replies[$who]+="$txt"$'\n'
    case "$2" in
      nc) case "$txt" in "Strategies: "*) reply_nc[$who]="$txt" ;; esac ;;
      co) case "$txt" in "Strategies: "*) reply_co[$who]="$txt" ;; esac ;;
    esac
  }
  local max="${BOTCMD_VERIFY_WAIT:-20}" waited=0 line all sent=0 now
  # pass 1: 'nc ?' to every target - scan after EACH send so early replies are never missed
  for n in "${targets[@]}"; do
    game_send "/w $n nc ?" && sent=$((sent + 1))
    now=$(wc -c < "$log" 2>/dev/null); now=${now:-0}
    while IFS= read -r line; do [ -n "$line" ] && record "$line" nc; done <<< "$(scan_range "$start" "$now")"
    sleep 1
  done
  off=$(wc -c < "$log" 2>/dev/null); off=${off:-0}
  # one more scan of the pass-1 window (stragglers), then the co window starts at $off
  while IFS= read -r line; do [ -n "$line" ] && record "$line" nc; done <<< "$(scan_range "$start" "$off")"
  # pass 2: 'co ?' only to co-conf targets; only bytes after $off belong to this pass
  for n in "${co_names[@]}"; do
    game_send "/w $n co ?" && sent=$((sent + 1))
    now=$(wc -c < "$log" 2>/dev/null); now=${now:-0}
    while IFS= read -r line; do [ -n "$line" ] && record "$line" co; done <<< "$(scan_range "$off" "$now")"
    sleep 1
  done
  okmsg "sent 'nc ?' to ${#targets[@]} bot(s)${co_names:+ and 'co ?' to ${#co_names[@]} co-conf bot(s)} ($sent whispers) - collecting replies (up to ${max}s more)..."
  while [ "$waited" -lt "$max" ]; do
    while IFS= read -r line; do [ -n "$line" ] && record "$line" nc; done <<< "$(scan_range "$start" "$off")"
    now=$(wc -c < "$log" 2>/dev/null); now=${now:-0}
    [ "${#co_names[@]}" -gt 0 ] && while IFS= read -r line; do [ -n "$line" ] && record "$line" co; done <<< "$(scan_range "$off" "$now")"
    all=1
    for n in "${targets[@]}"; do [ -n "${reply_nc[$n]:-}" ] || all=0; done
    for n in "${co_names[@]}"; do [ -n "${reply_co[$n]:-}" ] || all=0; done
    [ "$all" = "1" ] && break
    sleep 1; waited=$((waited + 1))
  done
  # ---- remaster + recall (optional keyword): silent online bots get remove+add, then re-probe ----
  if [ -n "$remaster" ]; then
    for n in "${targets[@]}"; do
      [ -n "${replies[$n]:-}" ] && continue          # answered something -> not silent
      is_handplay "$n" && continue                    # G1: never commanded, never remastered
      if [ "${t_on[$n]:-}" = "1" ]; then
        remade+=("$n"); need_remove[$n]=1            # online silent: remove + add (master := Leo)
      elif [ -n "$force" ]; then
        remade+=("$n")                               # DB-offline (force only): plain 'bot add' - already offline
      fi
    done
    if [ "${#remade[@]}" -gt 0 ]; then
      local ow="${BOTCMD_REMASTER_OFFLINE_WAIT:-15}" i on
      okmsg "remaster: ${#remade[@]} silent bot(s) -> online ones: 'bot remove' + poll DB until offline (async logout: BotLogoutGroupCleanupOperation, PlayerbotMgr.cpp:397-399) + 'bot add'; DB-offline ones (force): plain 'bot add'. master := Leo either way..."
      for n in "${remade[@]}"; do
        if [ "${need_remove[$n]:-}" = "1" ]; then
          game_send ".playerbots bot remove $n"
          i=0
          while [ "$i" -lt "$ow" ]; do
            on=$(_states_online "$n"); [ "$on" = "0" ] && break
            sleep 1; i=$((i+1))
          done
          game_send ".playerbots bot add $n" && okmsg "  $n: remove + add queued (offline after ${i}s)"
        else
          game_send ".playerbots bot add $n" && okmsg "  $n: add queued (was DB-offline - plain add, master := Leo)"
        fi
        sleep 1
      done
      okmsg "waiting up to ${BOTCMD_REMASTER_WAIT:-30}s for the bot logins (async via DB query holder, PlayerbotMgr.cpp:162+), then recalling..."
      i=0; all=0
      while [ "$i" -lt "${BOTCMD_REMASTER_WAIT:-30}" ]; do
        all=1
        for n in "${remade[@]}"; do on=$(_states_online "$n"); rem_on[$n]=$on; [ "$on" = "1" ] || all=0; done
        [ "$all" = "1" ] && break
        sleep 1; i=$((i+1))
      done
      for n in "${remade[@]}"; do printf '  remastered %-12s online=%s\n' "$n" "${rem_on[$n]:-?}"; done
      warn "note: re-added bots come up with ResetStrategies (OnBotLoginInternal, PlayerbotMgr.cpp:1705) - run: botcmd states apply"
      off=$(wc -c < "$log" 2>/dev/null); off=${off:-0}
      local off2
      for n in "${remade[@]}"; do
        game_send "/w $n nc ?" && sent=$((sent + 1))
        now=$(wc -c < "$log" 2>/dev/null); now=${now:-0}
        while IFS= read -r line; do [ -n "$line" ] && record "$line" nc; done <<< "$(scan_range "$off" "$now")"
        sleep 1
      done
      off2=$(wc -c < "$log" 2>/dev/null); off2=${off2:-0}
      for n in "${remade[@]}"; do
        [ -n "${want_co[$n]:-}" ] || continue
        game_send "/w $n co ?" && sent=$((sent + 1))
        now=$(wc -c < "$log" 2>/dev/null); now=${now:-0}
        while IFS= read -r line; do [ -n "$line" ] && record "$line" co; done <<< "$(scan_range "$off2" "$now")"
        sleep 1
      done
      waited=0; all=0
      while [ "$waited" -lt "$max" ]; do
        while IFS= read -r line; do [ -n "$line" ] && record "$line" nc; done <<< "$(scan_range "$off" "$off2")"
        now=$(wc -c < "$log" 2>/dev/null); now=${now:-0}
        [ "${#remade[@]}" -gt 0 ] && while IFS= read -r line; do [ -n "$line" ] && record "$line" co; done <<< "$(scan_range "$off2" "$now")"
        all=1
        for n in "${remade[@]}"; do [ -n "${reply_nc[$n]:-}" ] || all=0; done
        [ "$all" = "1" ] && break
        sleep 1; waited=$((waited + 1))
      done
      okmsg "recall done for: $(printf '%s ' "${remade[@]}")"
    else
      warn "remaster: no silent online bots - nothing to remaster"
    fi
  fi
  echo
  printf '%-12s %-9s %-44s %-28s %s\n' "BOT" "CONF" "NC(live)" "CO(live)" "VERDICT"
  local conf txt strat nc_live co_live verdict v members norm missing extra detail
  local -a warr=()
  _membership() {  # $1="Strategies: ..." reply, $2=wants csv, $3=nots csv -> "OK" or "missing [..] still on [..]"
    members=${1#Strategies: }; norm=",$members,"; norm=$(printf '%s' "$norm" | sed 's/, */,/g')
    missing=""; extra=""
    if [ -n "$2" ]; then
      IFS=',' read -ra warr <<< "$2"
      for w in "${warr[@]}"; do case "$norm" in *",$w,"*) : ;; *) missing+="$w " ;; esac; done
    fi
    if [ -n "$3" ]; then
      IFS=',' read -ra warr <<< "$3"
      for w in "${warr[@]}"; do case "$norm" in *",$w,"*) extra+="$w " ;; esac; done
    fi
    missing=$(printf '%s' "$missing" | xargs); extra=$(printf '%s' "$extra" | xargs)
    if [ -z "$missing" ] && [ -z "$extra" ]; then printf 'OK'
    else
      detail=""
      [ -n "$missing" ] && detail+="missing [$missing] "
      [ -n "$extra" ] && detail+="still on [$extra] "
      printf '%s' "$detail"
    fi
  }
  for n in "${targets[@]}"; do
    conf=$(_states_conf_get "$n"); [ -n "$conf" ] || conf="-"
    txt=${replies[$n]:-}
    strat=${reply_nc[$n]:-}
    if [ -n "$strat" ]; then nc_live=$(printf '%s' "$strat" | sed 's/^Strategies: //' | cut -c1-44); else nc_live="-"; fi
    if [ -n "${want_co[$n]:-}" ] && [ -n "${reply_co[$n]:-}" ]; then
      co_live=$(printf '%s' "${reply_co[$n]}" | sed 's/^Strategies: //' | cut -c1-28)
    else
      co_live="-"
    fi
    if [ -z "$txt" ]; then
      if [ -n "$force" ] && [ "${t_on[$n]:-}" != "1" ]; then
        verdict="db:offline - no reply expected unless the toon is actually online as a bot"
      else
        verdict="NO REPLY [no-ai] bot AI off / not added / shell down - try: botcmd roster ritual; if it answered whispers before it is a master/level issue: botcmd roster fix (or gmlevel 3 on I_WAS_CMD)"
      fi
    else
      case "$conf" in
        grind|quest|gather|leveling|follow|idle)
          IFS='|' read -r wants nots <<< "$(_preset_wants_nots "$conf")"
          if [ -n "$strat" ]; then
            v=$(_membership "$strat" "$wants" "$nots")
            if [ "$v" = "OK" ]; then verdict="OK (nc: $conf on)"; else verdict="MISMATCH (nc: $v)- push with: botcmd states apply"; fi
          else
            verdict="reply shown (not a strategy list): $(printf '%s' "$txt" | head -n 1 | cut -c1-60)"
          fi
          if [ -n "${want_co[$n]:-}" ]; then
            w_nc=""; nn_nc=""; w_co=""; nn_co=""
            while IFS='|' read -r eng w nn; do
              [ "$eng" = nc ] && { w_nc="$w"; nn_nc="$nn"; }
              [ "$eng" = co ] && { w_co="$w"; nn_co="$nn"; }
            done <<< "$(_conf_engine_wants "$conf")"
            if [ -n "${reply_co[$n]:-}" ]; then
              v=$(_membership "${reply_co[$n]}" "$w_co" "$nn_co")
              if [ "$v" = "OK" ]; then verdict="$verdict; co on"; else verdict="$verdict; co: $v"; fi
            else
              verdict="$verdict; co: NO REPLY"
            fi
          fi
          ;;
        *) verdict="custom conf - replies shown" ;;
      esac
    fi
    case " ${remade[*]:-} " in
      *" $n "*)
        if [ "${rem_on[$n]:-}" = "1" ]; then verdict="$verdict [remastered]"
        else verdict="$verdict [remastered:still-offline]"; fi ;;
    esac
    printf '%-12s %-9s %-44s %-28s %s\n' "$n" "$conf" "$nc_live" "$co_live" "$verdict"
  done
  echo
  warn "read-back: 'nc ?'/'co ?' list the ENABLED strategies only (Engine.cpp:524 ListStrategies; +/- at :676-694) and reply via TellMaster to the"
  warn "           bot's MASTER (PlayerbotAI.cpp:3170) - a 'Strategies:' reply also proves Leo is that bot's master."
  warn "tags: [no-ai] no bot AI attached (add/addaccount refuse online toons - PlayerbotMgr.cpp:733; bot remove works online - PlayerbotMgr.cpp:756); a bot that"
  warn "      answered whispers before but is silent now = master/level issue (PlayerbotSecurity.cpp LevelFor; silent erase PlayerbotAI.cpp:642-647; deny suppressed :190)"
  warn "fix: botcmd roster fix to re-master via ritual, and/or gmlevel 3 on I_WAS_CMD (CanBeGameMaster -> ALLOW_ALL, PlayerbotSecurity.cpp:31)"
  if [ "${BOTCMD_VERIFY_DEBUG:-0}" = "1" ]; then
    echo
    warn "DEBUG - all new '[name] [whisper]:' lines since the probe started:"
    now=$(wc -c < "$log" 2>/dev/null); now=${now:-0}
    scan_range "$start" "$now" | sed 's/^/  /'
  fi
  echo
  warn "full reply text: botcmd leo chat 60   (daemon.log also logs these replies as dropped bot-sender lines - expected)"
}

# Read-only quest-log view from the characters DB (character_queststatus:
# guid, quest, status - QuestDef.h: 1=COMPLETE 3=INCOMPLETE 5=FAILED 6=REWARDED).
# This is how you SEE which quests a bot has - 'rpg status do quest' only SETS
# (by id, or the first log quest with a POI) and never lists them.
states_quests() {  # $1 = bot name or "all" (default all watched bots)
  local target="${1:-all}" n q s q_list=""
  hdr "Quest logs (read-only DB: character_queststatus)"
  local -a names=()
  if [ "$target" = "all" ]; then
    while IFS=$'\t' read -r n _ _ _; do names+=("$n"); done < <(_states_all_bots)
  else
    case "$target" in
      ""|*[!A-Za-z0-9]*) errmsg "toon names are letters/digits only - '$target' rejected"; return 1 ;;
    esac
    if is_handplay "$target"; then errmsg "REFUSED: $target is hand-play (G1)"; return 1; fi
    names+=("$target")
  fi
  if [ "${#names[@]}" -eq 0 ]; then warn "no watched bots in census right now"; return 1; fi
  for n in "${names[@]}"; do q_list+="'$n',"; done
  q_list=${q_list%,}
  local rows
  rows=$(db_ro -N -B -e "SELECT c.name, q.quest, q.status FROM acore_characters.character_queststatus q JOIN acore_characters.characters c ON c.guid=q.guid WHERE c.name IN ($q_list) ORDER BY c.name, q.quest" 2>/dev/null)
  if [ -z "$rows" ]; then
    warn "no quest rows for these bots - their quest logs are empty (that is why 'rpg status do quest' answers 'No quest available')"
    return 0
  fi
  printf '%-12s %-8s %-11s\n' "BOT" "QUEST" "STATUS"
  printf '%s\n' "$rows" | while IFS=$'\t' read -r n q s; do
    case "$s" in
      1) s="COMPLETE" ;;
      3) s="INCOMPLETE" ;;
      5) s="FAILED" ;;
      6) s="REWARDED" ;;
      *) s="?$s" ;;
    esac
    printf '%-12s %-8s %-11s\n' "$n" "$q" "$s"
  done
  echo
  warn "'rpg status do quest' picks the first INCOMPLETE/COMPLETE quest with a POI (NewRpgAction.cpp:183-215); bots with no rows above have empty logs"
}

# Generic whisper probe: Leo sends '/w <bot> <command>' and we print EVERY
# '[bot] [whisper]: ...' reply line from the shell log (verify keeps only the
# last line per bot - probe shows all of them, e.g. "No quest available").
# Source notes: 'nc ?'/'co ?' list ALL registered strategies of that engine and
# reply via TellMaster -> MASTER only (Engine.cpp:695) - Leo gets them because
# the ritual/raid makes Leo master (and gmlevel gives ALLOW_ALL anyway).
# 'rpg status do quest' needs a quest with a POI in the bot's log, else the bot
# whispers "No quest available; use 'do quest <id>'" and keeps its old status
# (NewRpgAction.cpp:183-215); on IDLE the new-rpg engine re-rolls the status
# randomly by RpgStatusProbWeight (NewRpgAction.cpp:250-256). NOTE: the probe
# command runs FOR REAL (rpg status do X sets it) - it is a passthrough.
states_probe() {  # $1 = bot|all (default all DB-online watched bots); $2 = force (optional); rest = command (default "nc ?")
  local target="${1:-all}" force="" log="$BOTCMD_SHELL_LOG" start n online line
  [ $# -gt 0 ] && shift
  if [ "${1:-}" = "force" ]; then force=1; shift; fi
  [ "${BOTCMD_VERIFY_FORCE:-0}" = "1" ] && force=1
  local probe="${*:-nc ?}"
  # allow-list; '[]|' added so item links pass: |cff..|Hitem:..|h[Name]|h|r.
  # Control chars are rejected FIRST: grep works line-wise, so a newline inside
  # $probe would otherwise split into two FIFO lines (= injected 2nd command).
  case "$probe" in
    ""|*$'\n'*|*$'\r'*|*$'\t'*)
      errmsg "probe must be one line without control chars - rejected"
      return 1 ;;
  esac
  if ! printf '%s' "$probe" | grep -qE '^[]|[A-Za-z0-9 ?+,.:-]+$'; then
    errmsg "probe charset: letters/digits/space and ? + - , . : | [ ] only (item links ok) - '$probe' rejected"
    return 1
  fi
  hdr "Probe: Leo whispers '/w <bot> $probe' - prints ALL whisper replies (command runs for real)"
  if [ ! -r "$log" ]; then errmsg "no $log - is the Leo shell up? (botcmd leo status)"; return 1; fi
  local -a targets=()
  if [ "$target" = "all" ]; then
    while IFS=$'\t' read -r n _ online _; do
      if [ "$online" = "1" ] || [ -n "$force" ]; then targets+=("$n"); fi
    done < <(_states_all_bots)
  else
    case "$target" in
      *[!A-Za-z0-9]*) errmsg "toon names are letters/digits only - '$target' rejected"; return 1 ;;
    esac
    if is_handplay "$target"; then errmsg "REFUSED: $target is hand-play (G1) - never commanded"; return 1; fi
    targets+=("$target")
  fi
  if [ "${#targets[@]}" -eq 0 ]; then warn "no watched bots to probe right now"; return 1; fi
  start=$(wc -c < "$log" 2>/dev/null); start=${start:-0}
  local -a raw=()
  scan_new() { tail -c +"$((start + 1))" "$log" 2>/dev/null | grep -a -o '\[\([A-Za-z][A-Za-z]*\)\] \[whisper\]: .*' || true; }
  collect() { while IFS= read -r line; do [ -n "$line" ] && raw+=("$line"); done <<< "$(scan_new)"; }
  local max="${BOTCMD_VERIFY_WAIT:-20}" waited=0 sent=0 all
  for n in "${targets[@]}"; do
    game_send "/w $n $probe" && sent=$((sent + 1))
    collect
    sleep 1
  done
  okmsg "sent '/w <bot> $probe' to $sent/${#targets[@]} bot(s) - collecting replies (up to ${max}s more)..."
  while [ "$waited" -lt "$max" ]; do
    collect
    all=1
    for n in "${targets[@]}"; do
      if [ "${#raw[@]}" -eq 0 ] || ! printf '%s\n' "${raw[@]}" | grep -q "^\[$n\] \[whisper\]:"; then all=0; fi
    done
    [ "$all" = "1" ] && break
    sleep 1; waited=$((waited + 1))
  done
  echo
  for n in "${targets[@]}"; do
    printf '%-12s ' "$n"
    if [ "${#raw[@]}" -gt 0 ] && printf '%s\n' "${raw[@]}" | grep -q "^\[$n\] \[whisper\]:"; then
      printf '%s\n' "${raw[@]}" | grep "^\[$n\] \[whisper\]:" | awk '!seen[$0]++' | sed 's/^\[[A-Za-z]*\] \[whisper\]: /  reply: /' | head -n 6
    else
      echo "NO REPLY"
    fi
  done
  echo
  warn "raw log: botcmd leo chat 60   (daemon.log logs these as dropped bot-sender lines - expected)"
}

# set EVERY watched alt bot's conf to one preset, then apply (which verifies).
# $1 = optional preset; no arg -> interactive [x] picker (states_pick_preset).
states_setall() {  # $1 = grind|quest|gather|leveling|follow|idle (optional)
  local preset="${1:-}" n count=0
  if [ -z "$preset" ]; then
    preset=$(states_pick_preset) || { warn "cancelled - nothing written (blank Enter = back)"; return 0; }
    okmsg "picker selected: $preset"
  fi
  case "$preset" in
    grind|quest|gather|leveling|follow|idle) ;;
    *) errmsg "usage: botcmd states setall [grind|quest|gather|leveling|follow|idle]   (no arg = [x] picker)"; return 1 ;;
  esac
  hdr "Set ALL alt bots to '$preset' (conf + apply online + verify)"
  while IFS=$'\t' read -r n _ _ _; do
    if _states_conf_set "$n" "$preset"; then count=$((count + 1)); fi
  done < <(_states_all_bots)
  okmsg "conf written: $count bot(s) -> $preset   (G1 hand-play and Leo are never in the list)"
  echo
  states_apply all
}

states_menu() {
  local c n
  while :; do
    cls
    hdr "States (who does what: per-bot strategies)"
    cat <<'EOF'
 1. States table (conf + online)
 2. Pick a bot -> set its default state   (paginated, [x] = current)
 3. Set ALL bots to ONE state   ([x] picker; conf + apply + verify)
 4. Apply ALL states from conf (probe 'nc ?'/'co ?' first, whisper only the diff, then verify)
 5. Apply ONE bot's state (same probe+diff)
 6. Edit states conf
 7. Show presets + level->zone table
 8. Verify live states now ('nc ?'/'co ?' read-back -> state table; CLI: 'verify all force' probes DB-offline bots, 'verify all remaster' re-masters silent bots first)
 9. Probe any command (default 'nc ?' - lists strategies; replies go to master Leo)
10. Return to main menu   (blank Enter = back)
EOF
    printf '%s' "choice: "; IFS= read -r c
    case "$c" in
      ""|b|back) return 0 ;;
      1) states_show; pause ;;
      2) states_pick_and_set; pause ;;
      3) states_setall; pause ;;
      4) states_apply all; pause ;;
      5) printf '%s' "bot name: "; IFS= read -r n; states_apply "$n"; pause ;;
      6) states_edit ;;
      7) states_presets; echo; states_zones; pause ;;
      8) states_verify all; pause ;;
      9) states_probe all; pause ;;
      10) return 0 ;;
      *) warn "invalid choice" ;;
    esac
  done
}
