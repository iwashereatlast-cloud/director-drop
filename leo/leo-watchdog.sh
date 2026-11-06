#!/bin/bash
# Leo presence watchdog for Chazbot - one poll, run every 2 min by
# leo-watchdog.timer. Trusts the SERVER only (DB characters.online via the
# read-only MySQL identity), never the process table.
#
# Wanted online = server-local hour in [ON_H, OFF_H) OR any GROUP toon online.
# Zombie kill   = leo service active but server says Leo offline twice in a row.
# Auto-ritual   = on Leo's off->on transition, queue the bot-start ritual into
#                 the FIFO. NEVER I_WAS_HERE1ST - the G1 five are hand-play only.

MYCNF=$HOME/.my.cnf-director
STATE=$HOME/.leo-watchdog.state
CONF=$HOME/.leo-watchdog.conf
ON_H=8
OFF_H=22
# Both Aeris/Aerith spellings until verified in the DB (SELECT ... LIKE 'Aeri%').
GROUP="Cecil Rydia Shadow Vivi Aeris Aerith"
AUTORITUAL=1
RITUAL_ACCOUNTS="I_WAS_HERE2ND I_WAS_HERE3RD I_WAS_HERE4TH I_WAS_HERE5TH"
# only needed for bots NOT on a RITUAL_ACCOUNTS account (covered bots are skipped)
RITUAL_BOTS="Golbez"
FIFO=/tmp/leo.in
[ -f "$CONF" ] && . "$CONF"

prev_leo=1
prev_off_streak=0
[ -f "$STATE" ] && . "$STATE"

q() { mysql --defaults-file="$MYCNF" -N -B -e "$1" 2>/dev/null; }

hour=$((10#$(date +%H)))
leo=$(q "SELECT online FROM acore_characters.characters WHERE name='Leo'")
[ -n "$leo" ] || leo=1
# empty answer = DB hiccup: assume online, never zombie-kill blind

group_on=0
for n in $GROUP; do
  c=$(q "SELECT COUNT(*) FROM acore_characters.characters WHERE name='$n' AND online=1")
  [ -n "$c" ] || c=0
  group_on=$((group_on + c))
done

wanted=0
if [ "$hour" -ge "$ON_H" ] && [ "$hour" -lt "$OFF_H" ]; then wanted=1; fi
if [ "$group_on" -gt 0 ]; then wanted=1; fi

svc=$(systemctl --user is-active leo 2>/dev/null)

if [ "$wanted" = "1" ]; then
  if [ "$svc" != "active" ]; then
    systemctl --user start leo
    echo "watchdog: leo started (hour $hour, group_online $group_on)"
  elif [ "$leo" = "0" ]; then
    prev_off_streak=$((prev_off_streak + 1))
    if [ "$prev_off_streak" -ge 2 ]; then
      systemctl --user restart leo
      echo "watchdog: zombie kill - service active but server says Leo offline (x2)"
      prev_off_streak=0
    fi
  else
    prev_off_streak=0
  fi
else
  if [ "$svc" = "active" ]; then
    systemctl --user stop leo
    echo "watchdog: leo stopped (off-hours, group offline)"
  fi
  prev_off_streak=0
fi

if [ "$AUTORITUAL" = "1" ] && [ "$leo" = "1" ] && [ "$prev_leo" = "0" ]; then
  for a in $RITUAL_ACCOUNTS; do printf '%s\n' ".playerbots bot addaccount $a"; done > "$FIFO"
  for b in $RITUAL_BOTS; do
    case "$b" in *[!A-Za-z0-9]*) echo "watchdog: skip ritual bot '$b' (bad name)"; continue ;; esac
    # addaccount covers every toon on a RITUAL_ACCOUNTS account - skip bots already covered
    bacct=$(q "SELECT LOWER(a.username) FROM acore_characters.characters c JOIN acore_auth.account a ON a.id=c.account WHERE c.name='$b'")
    covered=0
    for a in $RITUAL_ACCOUNTS; do
      [ "$(printf '%s' "$a" | tr '[:lower:]' '[:upper:]')" = "$(printf '%s' "$bacct" | tr '[:lower:]' '[:upper:]')" ] && covered=1
    done
    if [ "$covered" = "1" ]; then
      echo "watchdog: skip 'bot add $b' - $b is on ${bacct:-unknown}, already covered by addaccount"
    else
      printf '%s\n' ".playerbots bot add $b" >> "$FIFO"
    fi
  done
  echo "watchdog: ritual queued (accounts: $RITUAL_ACCOUNTS bots: $RITUAL_BOTS)"
fi

# Duplicate-instance audit (warn only - never kill anything from here).
n_daemon=$(pgrep -fc 'director\.py daemon' 2>/dev/null); n_daemon=${n_daemon:-0}
n_shell=$(pgrep -fc 'shell\.py' 2>/dev/null); n_shell=${n_shell:-0}
if [ "$n_daemon" -gt 1 ] 2>/dev/null; then
  echo "watchdog: WARNING - $n_daemon director.py daemon processes (pids: $(pgrep -f 'director\.py daemon' | paste -sd' ' -)); they double-answer whispers - fix: pkill -f 'director\.py daemon'; systemctl --user restart leo-director"
fi
if [ "$n_shell" -gt 1 ] 2>/dev/null; then
  echo "watchdog: WARNING - $n_shell leo shell processes (pids: $(pgrep -f 'shell\.py' | paste -sd' ' -)) - fix: systemctl --user restart leo"
fi

printf 'prev_leo=%s\nprev_off_streak=%s\n' "$leo" "$prev_off_streak" > "$STATE"
