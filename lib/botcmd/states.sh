# Bot states: per-bot strategy assignments from one conf, applied as
# whisper lines (/w <bot> <cmd>) exactly like director.py order_line.
# Purity rule stays intact: this tool only SENDS the states you chose -
# it never invents them, never auto-applies on login. G1: hand-play toons
# are refused even if they appear in the conf.

states_presets() {
  cat <<'EOF'
Built-in presets (conf value = the preset word):
  grind    nc +new rpg,+grind,-follow
  quest    nc +new rpg,-follow ; rpg status do quest
  gather   nc +new rpg,+gather,-follow ; rpg status do gather
  follow   nc +follow                 (follow-duty: cling to master; NOTE "+new +follow" is a silent no-op - strategies split on ',' only)
  idle     nc -new rpg,-grind,-gather ; rpg status idle   (stand down)
Raw commands also work - separate multiple with ';;', e.g.:
  Crono: nc +new rpg,+grind,-follow ;; rpg status go grind
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
state_expand() {  # $1 = conf value
  case "$1" in
    grind)  printf '%s\n' 'nc +new rpg,+grind,-follow' ;;
    quest)  printf '%s\n' 'nc +new rpg,-follow' 'rpg status do quest' ;;
    gather) printf '%s\n' 'nc +new rpg,+gather,-follow' 'rpg status do gather' ;;
    follow) printf '%s\n' 'nc +follow' ;;
    idle)   printf '%s\n' 'nc -new rpg,-grind,-gather' 'rpg status idle' ;;
    *)      printf '%s\n' "$1" | sed 's/ *;; */\n/g' ;;
  esac
}

# NOTE: there is NO read-only way to see a bot's LIVE strategy set:
# '.playerbots inspect' does not exist in mod-playerbots @ e88cc40 and
# '.playerbots bot <name> ...' is Console::No (needs an active in-game session),
# so SOAP cannot run it either. The old _states_inspect() called a nonexistent
# command AND duplicated the SOAP envelope code - removed. states_show shows
# your conf; verify live behavior in the chat log (botcmd leo chat 40).

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
  warn "no read-only live-strategy view exists (.playerbots bot is in-game only) - verify live in: botcmd leo chat 40"
}

states_apply() {  # $1 = bot name or "all" (default)
  local target="${1:-all}"
  hdr "Apply states -> $target"
  if ! _states_conf_entries > /tmp/.botcmd-apply.$$ 2>/dev/null; then
    warn "no conf: $BOTCMD_STATES_CONF (botcmd states edit)"
    return 1
  fi
  local n v online cmd applied=0 skipped=0
  while IFS=$'\t' read -r n v; do
    if [ "$target" != "all" ] && [ "$(printf '%s' "$n" | tr '[:upper:]' '[:lower:]')" != "$(printf '%s' "$target" | tr '[:upper:]' '[:lower:]')" ]; then
      continue
    fi
    if is_handplay "$n"; then
      errmsg "REFUSED: $n is hand-play (G1) - never commanded"
      continue
    fi
    online=$(_states_online "$n")
    if [ "$online" != "1" ]; then
      warn "skip $n: not online"
      skipped=$((skipped + 1))
      continue
    fi
    while IFS= read -r cmd; do
      if game_send "/w $n $cmd"; then okmsg "queued: /w $n $cmd"; sleep 1; fi
    done < <(state_expand "$v")
    applied=$((applied + 1))
  done < /tmp/.botcmd-apply.$$
  rm -f /tmp/.botcmd-apply.$$
  echo
  okmsg "states pushed to $applied bot(s), $skipped skipped (offline)"
  warn "bots answer strategy changes in chat - check: botcmd leo chat 40"
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
    clear 2>/dev/null || :
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
    clear 2>/dev/null || :
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

states_menu() {
  local c n
  while :; do
    clear 2>/dev/null || :
    hdr "States (who does what: per-bot strategies)"
    cat <<'EOF'
 1. States table (conf + online)
 2. Pick a bot -> set its default state   (paginated, [x] = current)
 3. Apply ALL states (online bots only)
 4. Apply ONE bot's state
 5. Edit states conf
 6. Show presets + level->zone table
 7. Return to main menu   (blank Enter = back)
EOF
    printf '%s' "choice: "; IFS= read -r c
    case "$c" in
      ""|b|back) return 0 ;;
      1) states_show; pause ;;
      2) states_pick_and_set; pause ;;
      3) states_apply all; pause ;;
      4) printf '%s' "bot name: "; IFS= read -r n; states_apply "$n"; pause ;;
      5) states_edit ;;
      6) states_presets; echo; states_zones; pause ;;
      7) return 0 ;;
      *) warn "invalid choice" ;;
    esac
  done
}
