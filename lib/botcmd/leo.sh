# Leo control: the game client shell (leo.service).
# The shell connects Leo's character to the worldserver and reads the FIFO.

leo_on() {
  hdr "Leo - bring online"
  systemctl --user start leo
  sleep 1
  if [ "$(user_unit_state leo)" = active ]; then
    okmsg "leo service active (shell reading $BOTCMD_FIFO)"
  else
    errmsg "leo service not active - check: systemctl --user status leo"
  fi
  warn "presence policy: the watchdog may stop leo off-window unless a group toon is online"
}

leo_off() {
  hdr "Leo - log off"
  systemctl --user stop leo && okmsg "leo stopped" || errmsg "stop failed"
  warn "the watchdog may bring leo back on a later tick if the on-window wants it online"
}

leo_status() {
  hdr "Leo status (systemctl)"
  systemctl --user status leo --no-pager -n 3 || true
}

leo_chat() {  # $1 = number of lines
  local n="${1:-40}"
  hdr "Leo chat log - last $n lines ($BOTCMD_SHELL_LOG)"
  if [ -r "$BOTCMD_SHELL_LOG" ]; then
    tail -n "$n" "$BOTCMD_SHELL_LOG"
  else
    errmsg "no $BOTCMD_SHELL_LOG yet (shell has not run since boot?)"
  fi
}

leo_menu() {
  local c
  while :; do
    clear >/dev/null 2>&1 || :
    hdr "Leo control (game client shell)"
    cat <<'EOF'
 1. Bring Leo online
 2. Log Leo off
 3. Status (systemctl)
 4. Tail chat log (last 40 lines)
 5. Return to main menu
EOF
    printf '%s' "choice: "; IFS= read -r c
    case "$c" in
      1) leo_on; pause ;;
      2) leo_off; pause ;;
      3) leo_status; pause ;;
      4) leo_chat 40; pause ;;
      5) return 0 ;;
      ""|b|back) return 0 ;;
      *) warn "invalid choice" ;;
    esac
  done
}
