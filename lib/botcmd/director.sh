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
 5. Return to main menu
EOF
    printf '%s' "choice: "; IFS= read -r c
    case "$c" in
      1) director_restart; pause ;;
      2) director_status; pause ;;
      3) director_log 40; pause ;;
      4) director_deploy; pause ;;
      5) return 0 ;;
      *) warn "invalid choice" ;;
    esac
  done
}
