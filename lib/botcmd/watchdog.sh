# Watchdog control: presence policy daemon (leo-watchdog.timer -> service).

watchdog_status() {
  hdr "Watchdog status"
  echo "timer: $(user_unit_state leo-watchdog.timer)"
  systemctl --user list-timers leo-watchdog.timer --no-pager 2>/dev/null | sed -n '1,2p'
  echo
  echo "service (last run):"
  systemctl --user status leo-watchdog.service --no-pager -n 0 2>/dev/null | sed -n '1,4p' || true
  echo
  if [ -r "$BOTCMD_WATCHDOG_CONF" ]; then
    echo "conf ($BOTCMD_WATCHDOG_CONF):"
    cat "$BOTCMD_WATCHDOG_CONF"
  else
    warn "no conf file - watchdog runs defaults (on 8, off 22, group: hand-play five, autoritual on)"
  fi
}

watchdog_journal() {  # $1 = number of lines
  local n="${1:-20}"
  hdr "Watchdog journal - last $n runs"
  journalctl --user -u leo-watchdog.service -n "$n" --no-pager || \
    errmsg "journalctl failed"
}

watchdog_conf_edit() {
  hdr "Edit watchdog conf"
  if [ ! -r "$BOTCMD_WATCHDOG_CONF" ]; then
    warn "no conf yet - this starts a new one at $BOTCMD_WATCHDOG_CONF"
    warn "supported keys: ON_H, OFF_H, GROUP (space-separated), AUTORITUAL, RITUAL_ACCOUNTS, RITUAL_BOTS"
  fi
  "${EDITOR:-nano}" "$BOTCMD_WATCHDOG_CONF"
  okmsg "saved (the watchdog re-reads the conf on every run - no restart needed)"
}

watchdog_procs() {
  hdr "Process audit (duplicate-instance check)"
  local nd ns pids
  nd=$(pgrep -fc 'director\.py daemon' 2>/dev/null); nd=${nd:-0}
  ns=$(pgrep -fc 'shell\.py' 2>/dev/null); ns=${ns:-0}
  pids=$(pgrep -f 'director\.py daemon' 2>/dev/null | paste -sd' ' -)
  echo "director.py daemon : $nd running${pids:+  (pids: $pids)}"
  if [ "$nd" -gt 1 ] 2>/dev/null; then
    warn "MORE THAN ONE daemon - duplicate daemons double-answer whispers. fix:"
    echo "    pkill -f 'director.py daemon'; systemctl --user restart leo-director"
  elif [ "$nd" = "1" ]; then
    okmsg "exactly one daemon - OK"
  else
    warn "no daemon running right now (leo-director inactive or not started)"
  fi
  pids=$(pgrep -f 'shell\.py' 2>/dev/null | paste -sd' ' -)
  echo "leo shell.py       : $ns running${pids:+  (pids: $pids)}"
  if [ "$ns" -gt 1 ] 2>/dev/null; then
    warn "MORE THAN ONE leo shell. fix: systemctl --user restart leo"
  elif [ "$ns" = "1" ]; then
    okmsg "exactly one leo shell - OK"
  else
    warn "no leo shell running right now (the watchdog timer will start it if wanted)"
  fi
  if ( flock -n 9 ) 9<>"$HOME/.botcmd.lock" 2>/dev/null; then
    okmsg "menu lock (~/.botcmd.lock): FREE - no botcmd menu open"
  else
    warn "menu lock HELD by pid $(cat "$HOME/.botcmd.lock.pid" 2>/dev/null || echo '?') - a botcmd menu is open elsewhere"
  fi
}

watchdog_menu() {
  local c
  while :; do
    clear 2>/dev/null || :
    hdr "Watchdog (presence policy: on-window / group-online)"
    cat <<'EOF'
 1. Status (timer + last run + conf)
 2. Recent journal (last 20 runs)
 3. Edit conf
 4. Process audit (duplicate daemons / leo shells / menu lock)
 5. Return to main menu (blank Enter = back)
EOF
    printf '%s' "choice: "; IFS= read -r c
    case "$c" in
      ""|b|back) return 0 ;;
      1) watchdog_status; pause ;;
      2) watchdog_journal 20; pause ;;
      3) watchdog_conf_edit ;;
      4) watchdog_procs; pause ;;
      5) return 0 ;;
      *) warn "invalid choice" ;;
    esac
  done
}
