# Bot states: per-bot strategy assignments from one conf, applied as
# whisper lines (/w <bot> <cmd>) exactly like director.py order_line.
# Purity rule stays intact: this tool only SENDS the states you chose -
# it never invents them, never auto-applies on login. G1: hand-play toons
# are refused even if they appear in the conf.

states_presets() {
  cat <<'EOF'
Built-in presets (conf value = the preset word):
  grind    nc +new rpg,+grind,-quest,-gather,-follow
  quest    nc +new rpg,+quest,-grind,-gather,-follow ; rpg status do quest (best-effort: needs a quest in the log, else "No quest available")
  gather   nc +new rpg,+gather,-grind,-quest,-follow ; rpg status do gather (needs Herbalism/Mining)
  follow   nc +follow                 (follow-duty: cling to master; NOTE "+new +follow" is a silent no-op - strategies split on ',' only)
  idle     nc -new rpg,-grind,-quest,-gather,-follow ; rpg status idle   (stand down)
See what quests a bot actually has (read-only DB): botcmd states quests [bot|all]
Raw commands also work - separate multiple with ';;', e.g.:
  Crono: nc +new rpg,+grind,-follow ;; rpg status go grind
RPG semantics (mod-playerbots @ e88cc40): 'rpg status do quest' needs a quest with
a POI in the bot's log - else the bot whispers "No quest available" and keeps its
status (NewRpgAction.cpp:183-215); when the status hits IDLE the new-rpg engine
re-rolls it randomly by RpgStatusProbWeight (NewRpgAction.cpp:250-256). So a
durable 'quest' state = nc +new rpg + quests in the log; read it back with:
  botcmd states verify                        # verdict = 'nc ?' strategy membership; rpg status = info
  botcmd states probe all "nc ?"              # raw list (goes to master Leo)
  botcmd states probe all "rpg status do quest"   # shows per-bot error whispers
  botcmd states probe Locke "quests all"      # live quest log via whisper (TellMaster = master only, ListQuestsActions.cpp:11-40)
  botcmd states probe Locke "rpg do quest 153"    # set DO_QUEST by id/link WITHOUT needing it in the log (StartRpgDoQuestAction, NewRpgAction.cpp:227-244) - still ALLOW_ALL
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
# Presets toggle the nc-engine strategies (the nc ? list: 'new rpg' drives the
# rpg state machine and includes questing, 'grind'/'quest'/'gather' are the
# behavior strategies - StrategyContext.h, QuestStrategies.h, GrindingStrategy.h).
# 'rpg status do X' is kept as a best-effort nudge: it only lands when the bot has
# a quest with a POI in its log (else the bot whispers "No quest available" and
# keeps its status - NewRpgAction.cpp:183-215); on IDLE the new-rpg engine
# re-rolls the status randomly by RpgStatusProbWeight (NewRpgAction.cpp:250-256).
# 'rpg status do gather' needs Herbalism or Mining (same file).
state_expand() {  # $1 = conf value
  case "$1" in
    grind)  printf '%s\n' 'nc +new rpg,+grind,-quest,-gather,-follow' ;;
    quest)  printf '%s\n' 'nc +new rpg,+quest,-grind,-gather,-follow' 'rpg status do quest' ;;
    gather) printf '%s\n' 'nc +new rpg,+gather,-grind,-quest,-follow' 'rpg status do gather' ;;
    follow) printf '%s\n' 'nc +follow' ;;
    idle)   printf '%s\n' 'nc -new rpg,-grind,-quest,-gather,-follow' 'rpg status idle' ;;
    *)      printf '%s\n' "$1" | sed 's/ *;; */\n/g' ;;
  esac
}

# NOTE: the LIVE strategy set IS readable after all: 'nc ?' whispers
# ListStrategies() - the ENABLED strategies only (Engine.cpp:524-538; the +/-
# toggles at Engine.cpp:676-694 add/remove from that same map) - via TellMaster
# to the bot's MASTER only (Engine.cpp:695, PlayerbotAI.cpp:3170), so the
# 'Strategies:' reply also proves mastership. states_verify() uses it for the
# verdict. 'rpg status' (no args) whispers back NewRpgInfo::ToString() ->
# "Status: GO_GRIND / DO_QUEST / ... " - a TRANSIENT state that re-rolls on
# IDLE by RpgStatusProbWeight (NewRpgAction.cpp:250-256), shown as info only.
# ('.playerbots bot <name>' is Console::No - SOAP cannot run it.)

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
  warn "read back live strategies ('nc ?') + rpg status with: botcmd states verify"
}

states_apply() {  # $1 = bot name or "all" (default). NO online gate: every conf bot gets
                    # its /w lines - a whisper to a toon that is not in the world is a
                    # harmless no-op ("No player named ..."), and characters.online is
                    # NOT ground truth (worldserver boot resets all flags to 0 -
                    # Main.cpp:520 - and login rewrites them async - CharacterHandler.cpp:921).
                    # The DB flag is printed inline as info only; states_verify read-back
                    # is the source of truth for who actually received the orders.
  local target="${1:-all}"
  hdr "Apply states -> $target (whispers every conf bot; db online flag shown as info only)"
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
  okmsg "states pushed to $applied bot(s)${refused:+, $refused refused (G1)} - whispers to offline toons are no-ops; read back with: botcmd states verify"
  if [ "$target" = "all" ] && [ "${BOTCMD_NO_VERIFY:-0}" != "1" ]; then
    echo
    states_verify all
  fi
}

states_edit() {
  hdr "Edit states conf ($BOTCMD_STATES_CONF)"
  if [ ! -r "$BOTCMD_STATES_CONF" ]; then
    warn "no conf yet - creating a starter conf"
    cat > "$BOTCMD_STATES_CONF" <<'EOF'
# botcmd states conf - one line per bot:  Name: preset-or-commands
# presets: grind quest gather follow idle   (botcmd states presets)
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
    clear >/dev/null 2>&1 || :
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
    clear >/dev/null 2>&1 || :
    hdr "Default state for $bot  (conf: $BOTCMD_STATES_CONF)"
    printf 'current conf state: %s\n\n' "${cur:-(none set)}"
    i=0
    for p in grind quest gather follow idle; do
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
      1|2|3|4|5)
        case "$c" in 1) preset=grind;; 2) preset=quest;; 3) preset=gather;; 4) preset=follow;; 5) preset=idle;; esac
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
  local presets=(grind quest gather follow idle) c i p vals nbots uniq_n uniform=""
  nbots=$(_states_all_bots | wc -l)
  vals=$(_states_all_bots | while IFS=$'\t' read -r n _ _ _; do _states_conf_get "$n"; done)
  uniq_n=$(printf '%s\n' "$vals" | grep -v '^$' | sort -u | wc -l)
  if [ "$uniq_n" = "1" ] && [ "$(printf '%s\n' "$vals" | grep -cv '^$')" = "$nbots" ] && [ "$nbots" -gt 0 ]; then
    uniform=$(printf '%s\n' "$vals" | grep -v '^$' | head -n 1)
  fi
  while :; do
    clear >/dev/null 2>&1 || :
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
      1|2|3|4|5) printf '%s' "${presets[$((c - 1))]}"; return 0 ;;
      *) warn "invalid choice" >&2 ;;
    esac
  done
}

# Live group/leader map from the characters DB (read-only). `groups` is a
# RESERVED word in MySQL 8 - always backtick-quote it. Same schema as
# director.py GROUP_SQL (Group.cpp persists create/member-add/leader/type).
# prints: name<TAB>groupGuid<TAB>leaderName<TAB>groupType   (empty guid = groupless)
# args are toon names, charset-validated ([A-Za-z0-9]) before they reach SQL.
_states_group_map() {
  local -a names=() q="" n
  for n in "$@"; do
    case "$n" in
      ""|*[!A-Za-z0-9]*) errmsg "bad toon name '$n' - skipped in group lookup"; continue ;;
    esac
    names+=("$n")
  done
  [ "${#names[@]}" -eq 0 ] && return 0
  for n in "${names[@]}"; do q+="'$n',"; done
  q=${q%,}
  db_ro -N -B -e "SELECT c.name, COALESCE(g.guid,''), COALESCE(lc.name,''), COALESCE(g.groupType,'') FROM acore_characters.characters c LEFT JOIN acore_characters.group_member gm ON gm.memberGuid=c.guid LEFT JOIN acore_characters.\`groups\` g ON g.guid=gm.guid LEFT JOIN acore_characters.characters lc ON lc.guid=g.leaderGuid WHERE c.name IN ($q)" 2>/dev/null
}

# groupType -> party/raid/bg ('-' when groupless). bit 0x01 = battleground,
# bit 0x02 = raid (Group.h enum GroupType) - same decode as director.py.
_grp_kind() {
  case "${1:-}" in
    "") echo "-" ;;
    *)  if [ $(( ${1:-0} & 0x02 )) -ne 0 ]; then echo "raid"
        elif [ $(( ${1:-0} & 0x01 )) -ne 0 ]; then echo "bg"
        else echo "party"; fi ;;
  esac
}

# Read-back verification: Leo whispers 'nc ?' AND 'rpg status' to each online
# target bot (via the FIFO, exactly like an order), then we collect the
# "[Name] [whisper]: ..." replies from the shell log and tabulate conf vs live.
# Source-verified @ e88cc40: 'nc ?' whispers ListStrategies() - the ENABLED
# strategy names (Engine.cpp:524-538; +/- add/removeStrategy at :676-694) -
# via TellMaster to the bot's MASTER only (Engine.cpp:695, PlayerbotAI.cpp:3170).
# 'rpg status' (no args) whispers NewRpgInfo::ToString() ("Status: X ...") - a
# TRANSIENT rpg state (re-rolls on IDLE by RpgStatusProbWeight, NewRpgAction.cpp:
# 250-256). The verdict for a strategy preset comes from 'nc ?' MEMBERSHIP (a
# preset like 'quest' is a strategy SET, not an rpg status - DO_QUEST only shows
# while a quest POI is active, NewRpgAction.cpp:183-215, so bots with empty quest
# logs never show it); the rpg status is shown as info only.
states_verify() {  # $1 = bot name or "all" (default all online watched bots);
                    # $2 = "force" (or BOTCMD_VERIFY_FORCE=1) also probes bots the DB flags offline
  local target="${1:-all}" force="${2:-}" log="$BOTCMD_SHELL_LOG" start n online
  [ "${BOTCMD_VERIFY_FORCE:-0}" = "1" ] && force=force
  hdr "Verify live states (Leo whispers 'nc ?' + 'rpg status'; verdict = nc strategy membership)"
  if [ ! -r "$log" ]; then
    errmsg "no $log - is the Leo shell up? (botcmd leo status)"
    return 1
  fi
  local -a targets=()
  declare -A t_on=()
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
  # Live group/leader context - WHY a bot may stay silent when Leo whispers:
  # LevelFor (PlayerbotSecurity.cpp) gives Leo ALLOW_ALL only as the bot's
  # master, INVITE if the bot is groupless or leads its group, else TALK;
  # deny text is suppressed for bot senders (:190) and queued commands below
  # their required level are erased silently (PlayerbotAI.cpp:642-647).
  declare -A g_guid=() g_leader=() g_type=()
  local gm_line n2 gg gl gt
  while IFS=$'\t' read -r n2 gg gl gt; do
    [ -n "$n2" ] && { g_guid[$n2]=$gg; g_leader[$n2]=$gl; g_type[$n2]=$gt; }
  done < <(_states_group_map Leo "${targets[@]}")
  local leo_grp="${g_guid[Leo]:-}" inleo=""
  if [ "${#g_guid[@]}" -eq 0 ]; then
    warn "group info unavailable (DB read failed?) - GRP column and hints degrade to '-'"
  else
    for n in "${targets[@]}"; do
      [ -n "$leo_grp" ] && [ "${g_guid[$n]:-}" = "$leo_grp" ] && inleo+="$n "
    done
    if [ -n "$leo_grp" ]; then
      okmsg "Leo is in $(_grp_kind "${g_type[Leo]:-}") led by ${g_leader[Leo]:-?} (guid $leo_grp)${inleo:+ - also in it: $inleo}"
    else
      warn "Leo is not in any group right now"
    fi
  fi
  start=$(wc -c < "$log" 2>/dev/null); start=${start:-0}
  declare -A reply=() replies=()
  # one scan of everything appended to the log since the probe started
  scan_new() {
    tail -c +"$((start + 1))" "$log" 2>/dev/null | grep -a -o '\[\([A-Za-z][A-Za-z]*\)\] \[whisper\]: .*' || true
  }
  record() {  # $1 = raw matched line - keeps the last line AND all lines per bot
    local who txt
    who=${1%%]*}; who=${who#\[}
    txt=${1#*\[whisper\]: }
    reply[$who]="$txt"
    replies[$who]+="$txt"$'\n'
  }
  local max="${BOTCMD_VERIFY_WAIT:-20}" waited=0 line all sent=0
  # send probes one by one and scan after EACH send, so early replies are
  # never missed even when the post-send wait window is short
  for n in "${targets[@]}"; do
    game_send "/w $n nc ?" && sent=$((sent + 1))
    game_send "/w $n rpg status" && sent=$((sent + 1))
    while IFS= read -r line; do
      [ -n "$line" ] && record "$line"
    done <<< "$(scan_new)"
    sleep 1
  done
  okmsg "sent 'nc ?' + 'rpg status' (${#targets[@]} bot(s), $sent whispers) - collecting replies (up to ${max}s more)..."
  while [ "$waited" -lt "$max" ]; do
    while IFS= read -r line; do
      [ -n "$line" ] && record "$line"
    done <<< "$(scan_new)"
    all=1
    for n in "${targets[@]}"; do [ -n "${reply[$n]:-}" ] || all=0; done
    [ "$all" = "1" ] && break
    sleep 1; waited=$((waited + 1))
  done
  echo
  printf '%-12s %-8s %-14s %-40s %s\n' "BOT" "CONF" "GRP" "LIVE(reply)" "VERDICT"
  local conf txt st verdict grp strat status members norm missing extra wants nots w detail
  local -a warr=()
  # preset -> expected nc strategy membership (want ON / want OFF).
  # 'new rpg' is ONE strategy name containing a space - split on ',' only.
  for n in "${targets[@]}"; do
    conf=$(_states_conf_get "$n"); [ -n "$conf" ] || conf="-"
    txt=${replies[$n]:-}
    grp="-"
    if [ -n "${g_guid[$n]:-}" ]; then
      grp="$(_grp_kind "${g_type[$n]:-}"):${g_leader[$n]:-?}"
    fi
    strat=$(printf '%s' "$txt" | grep -a '^Strategies: ' | head -n 1)
    status=$(printf '%s' "$txt" | grep -a '^Status: ' | head -n 1)
    # LIVE column: transient rpg status if we got one (info), else the strategy list
    if [ -n "$status" ]; then
      st=$(printf '%s' "$status" | cut -c1-40); st=${st#Status: }
    elif [ -n "$strat" ]; then
      st=$(printf '%s' "$strat" | cut -c1-40)
    else
      st="-"
    fi
    if [ -z "$txt" ]; then
      if [ -n "$force" ] && [ "${t_on[$n]:-}" != "1" ]; then
        verdict="db:offline - no reply expected unless the toon is actually online as a bot"
      elif [ -z "${g_guid[$n]:-}" ]; then
        verdict="NO REPLY [no-ai] groupless, so Leo should have INVITE - bot AI off / not added as a bot? shell down? try: botcmd roster ritual"
      elif [ -n "$leo_grp" ] && [ "${g_guid[$n]}" = "$leo_grp" ]; then
        verdict="NO REPLY [master] same group as Leo but bot's master != Leo (deny text suppressed for bot senders); grant I_WAS_CMD gmlevel or botcmd roster fix"
      else
        verdict="NO REPLY [group] in a group led by ${g_leader[$n]:-?} that Leo is not in (bot not leader -> TALK -> silent); grant I_WAS_CMD gmlevel or botcmd roster fix"
      fi
    else
      case "$conf" in
        grind|quest|gather|follow|idle)
          case "$conf" in
            grind)  wants="new rpg,grind";  nots="quest,gather,follow" ;;
            quest)  wants="new rpg,quest";  nots="grind,gather,follow" ;;
            gather) wants="new rpg,gather"; nots="grind,quest,follow" ;;
            follow) wants="follow";         nots="" ;;
            idle)   wants="";               nots="new rpg,grind,quest,gather,follow" ;;
          esac
          if [ -n "$strat" ]; then
            members=${strat#Strategies: }
            norm=",$members,"
            norm=$(printf '%s' "$norm" | sed 's/, */,/g')
            missing=""; extra=""
            if [ -n "$wants" ]; then
              IFS=',' read -ra warr <<< "$wants"
              for w in "${warr[@]}"; do
                case "$norm" in *",$w,"*) : ;; *) missing+="$w " ;; esac
              done
            fi
            if [ -n "$nots" ]; then
              IFS=',' read -ra warr <<< "$nots"
              for w in "${warr[@]}"; do
                case "$norm" in *",$w,"*) extra+="$w " ;; esac
              done
            fi
            missing=$(printf '%s' "$missing" | xargs); extra=$(printf '%s' "$extra" | xargs)
            if [ -z "$missing" ] && [ -z "$extra" ]; then
              verdict="OK (nc: $conf strategies on)"
            else
              detail=""
              [ -n "$missing" ] && detail+="missing [$missing] "
              [ -n "$extra" ] && detail+="still on [$extra] "
              verdict="MISMATCH (nc: $detail)- push conf with: botcmd states apply; if STILL off after apply, the nc SET was silently erased (needs ALLOW_ALL - Leo not master/GM) [master]"
            fi
          elif [ -n "$status" ]; then
            verdict="rpg answered (INVITE ok) but 'nc ?' no reply - 'nc' needs ALLOW_ALL (master/GM), else the SET is silently erased (PlayerbotAI.cpp:642-647); or RepeatDelay dedupe on repeated text - rerun verify [master]"
          else
            verdict="reply shown (not a strategy list): $(printf '%s' "$txt" | head -n 1 | cut -c1-60)"
          fi
          ;;
        *) verdict="custom conf - replies shown" ;;
      esac
    fi
    printf '%-12s %-8s %-14s %-40s %s\n' "$n" "$conf" "$grp" "$st" "$verdict"
  done
  echo
  warn "read-back: 'nc ?' lists ENABLED strategies only (Engine.cpp:524 ListStrategies; +/- at :676-694) and replies via TellMaster to the bot's"
  warn "           MASTER (PlayerbotAI.cpp:3170) - a 'Strategies:' reply also proves Leo is that bot's master. 'rpg status' is transient"
  warn "           (re-rolls on IDLE by RpgStatusProbWeight, NewRpgAction.cpp:250-256), shown as info only; DO_QUEST needs a quest POI in the log (:183-215)."
  warn "tags: [no-ai] no bot AI attached (add/addaccount refuse online toons - PlayerbotMgr.cpp:733; bot remove works online - PlayerbotMgr.cpp:756);"
  warn "      [group]/[master] Leo's level from that bot is below ALLOW_ALL (PlayerbotSecurity.cpp LevelFor; silent erase PlayerbotAI.cpp:642-647; deny suppressed for bot senders :190)"
  warn "fix: grant gmlevel on I_WAS_CMD (CanBeGameMaster -> ALLOW_ALL, PlayerbotSecurity.cpp:31) and/or botcmd roster fix to re-master via ritual"
  if [ "${BOTCMD_VERIFY_DEBUG:-0}" = "1" ]; then
    echo
    warn "DEBUG - all new '[name] [whisper]:' lines since the probe started:"
    scan_new | sed 's/^/  /'
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
  if [ -z "$probe" ] || ! printf '%s' "$probe" | grep -qE '^[A-Za-z0-9 ?+,\.:-]+$'; then
    errmsg "probe charset: letters/digits/space and ? + - , . : only - '$probe' rejected"
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
states_setall() {  # $1 = grind|quest|gather|follow|idle (optional)
  local preset="${1:-}" n count=0
  if [ -z "$preset" ]; then
    preset=$(states_pick_preset) || { warn "cancelled - nothing written (blank Enter = back)"; return 0; }
    okmsg "picker selected: $preset"
  fi
  case "$preset" in
    grind|quest|gather|follow|idle) ;;
    *) errmsg "usage: botcmd states setall [grind|quest|gather|follow|idle]   (no arg = [x] picker)"; return 1 ;;
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
    clear >/dev/null 2>&1 || :
    hdr "States (who does what: per-bot strategies)"
    cat <<'EOF'
 1. States table (conf + online)
 2. Pick a bot -> set its default state   (paginated, [x] = current)
 3. Set ALL bots to ONE state   ([x] picker; conf + apply + verify)
 4. Apply ALL states from conf (whispers every conf bot, then verify)
 5. Apply ONE bot's state
 6. Edit states conf
 7. Show presets + level->zone table
 8. Verify live states now ('nc ?' strategy read-back + rpg status; CLI 'verify all force' also probes DB-offline bots)
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
