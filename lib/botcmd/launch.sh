# Full launch: the ordered post-reboot boot ritual, one button.
# Order matters: worldserver up -> FIFO exists -> director -> leo -> timer.

launch() {
  hdr "FULL LAUNCH (ordered boot ritual)"
  echo "1/5 worldserver check"
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet ac-worldserver 2>/dev/null; then
    okmsg "ac-worldserver active"
  else
    errmsg "ac-worldserver NOT active - fix that first (server must be up before Leo can log in)"
    return 1
  fi

  echo "2/5 FIFO"
  if [ -p "$BOTCMD_FIFO" ]; then
    okmsg "$BOTCMD_FIFO present"
  else
    mkfifo "$BOTCMD_FIFO" && okmsg "created $BOTCMD_FIFO (reboot wipes /tmp)"
  fi

  echo "3/5 director daemon (no-op if already running)"
  systemctl --user start leo-director
  echo "   leo-director: $(user_unit_state leo-director)"

  echo "4/5 leo shell"
  systemctl --user start leo
  echo "   leo: $(user_unit_state leo)"

  echo "5/5 watchdog timer"
  systemctl --user start leo-watchdog.timer
  systemctl --user enable leo-watchdog.timer >/dev/null 2>&1 && okmsg "timer enabled (boot-persistent)"
  echo "   leo-watchdog.timer: $(user_unit_state leo-watchdog.timer)"

  echo
  okmsg "launch sequence done - live census receipt:"
  echo
  sleep 2
  roster_census || true
  echo
  warn "summon ritual: the watchdog auto-queues it within ~2 min of Leo coming online"
  warn "  (or Roster -> 2 to summon right now)"
  warn "presence policy: off-window, the watchdog stops leo unless a group toon is online"
}
