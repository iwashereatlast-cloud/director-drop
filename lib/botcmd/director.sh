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
  echo "== presence commands (Leo runs himself, not /w to a bot) =="
  awk '/^MASTER_CMDS = \(/{f=1} f{print} f && /\)$/{exit}' "$d" \
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
    clear 2>/dev/null || :
    hdr "Director (LLM daemon: leo-director.service)"
    cat <<'EOF'
 1. Restart daemon
 2. Status (systemctl)
 3. Tail daemon.log (last 40 lines)
 4. Deploy update (git pull + compile check + restart)
 5. Leo's command list (what he can order)
 6. Return to main menu
EOF
    printf '%s' "choice: "; IFS= read -r c
    case "$c" in
      1) director_restart; pause ;;
      2) director_status; pause ;;
      3) director_log 40; pause ;;
      4) director_deploy; pause ;;
      5) director_commands; pause ;;
      6) return 0 ;;
      *) warn "invalid choice" ;;
    esac
  done
}
