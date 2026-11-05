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
  follow   nc +new +follow            (follow-duty: cling to master)
  idle     nc -new rpg,-grind,-gather ; rpg status idle   (stand down)
Raw commands also work - separate multiple with ';;', e.g.:
  Crono: nc +new rpg,+grind,-follow ;; rpg status go grind
Gotcha (receipt 2026-11-03): "new rpg" is ONE strategy name with a space -
remove it as the single token "-new rpg", never "-new,-rpg".
EOF
}

# expand a conf value into one whisper command per line
state_expand() {  # $1 = conf value
  case "$1" in
    grind)  printf '%s\n' 'nc +new rpg,+grind,-follow' ;;
    quest)  printf '%s\n' 'nc +new rpg,-follow' 'rpg status do quest' ;;
    gather) printf '%s\n' 'nc +new rpg,+gather,-follow' 'rpg status do gather' ;;
    follow) printf '%s\n' 'nc +new +follow' ;;
    idle)   printf '%s\n' 'nc -new rpg,-grind,-gather' 'rpg status idle' ;;
    *)      printf '%s\n' "$1" | sed 's/ *;; */\n/g' ;;
  esac
}

# live strategy via SOAP .playerbots inspect (needs the gate armed; prints
# nothing on any failure - the table then shows "-")
_states_inspect() {  # $1 = bot name, $2 = query word
  [ -s "$BOTCMD_SOAP_CONF" ] && [ "$(wc -l < "$BOTCMD_SOAP_CONF")" -ge 3 ] || return 1
  local url user pass envelope body
  url=$(sed -n '1p' "$BOTCMD_SOAP_CONF")
  user=$(sed -n '2p' "$BOTCMD_SOAP_CONF")
  pass=$(sed -n '3p' "$BOTCMD_SOAP_CONF")
  envelope='<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns1="urn:AC"><SOAP-ENV:Body><ns1:executeCommand><command>.playerbots inspect '"$1 $2"'</command></ns1:executeCommand></SOAP-ENV:Body></SOAP-ENV:Envelope>'
  body=$(curl -s --max-time 5 -u "$user:$pass" \
    -H 'Content-Type: text/xml; charset=utf-8' -H 'SOAPAction: urn:AC' \
    -d "$envelope" "$url" 2>/dev/null) || return 1
  printf '%s' "$body" | sed -e 's/<[^>]*>/ /g' | tr -s ' ' | cut -c1-46 | sed 's/^ *//'
  [ -n "$(printf '%s' "$body" | tr -d ' ')" ] || return 1
}

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
  local n v online live g1
  printf '%-12s %-8s %-30s %s\n' "BOT" "STATUS" "CONF STATE" "LIVE (SOAP inspect)"
  while IFS=$'\t' read -r n v; do
    if is_handplay "$n"; then g1=" (G1)"; else g1=""; fi
    online=$(_states_online "$n")
    [ "$online" = "1" ] && online="ONLINE" || { [ "$online" = "0" ] && online="off" || online="?"; }
    live=$(_states_inspect "$n" strategy)
    [ -n "$live" ] || live="-"
    printf '%-12s %-8s %-30s %s\n' "$n$g1" "$online" "$(printf '%s' "$v" | cut -c1-30)" "$live"
  done < /tmp/.botcmd-entries.$$
  rm -f /tmp/.botcmd-entries.$$
  echo
  warn "purity rule: states are sent by YOU (apply), never auto-applied on login"
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

states_menu() {
  local c n
  while :; do
    clear 2>/dev/null || :
    hdr "States (who does what: per-bot strategies)"
    cat <<'EOF'
 1. Show states table (conf + online + live)
 2. Apply ALL states (online bots only)
 3. Apply ONE bot's state
 4. Edit states conf
 5. Show presets
 6. Return to main menu
EOF
    printf '%s' "choice: "; IFS= read -r c
    case "$c" in
      1) states_show; pause ;;
      2) states_apply all; pause ;;
      3) printf '%s' "bot name: "; IFS= read -r n; states_apply "$n"; pause ;;
      4) states_edit ;;
      5) states_presets; pause ;;
      6) return 0 ;;
      *) warn "invalid choice" ;;
    esac
  done
}
