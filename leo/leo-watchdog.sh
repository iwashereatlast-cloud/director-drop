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
RITUAL_ACCOUNTS="I_WAS_HERE2ND I_WAS_HERE3RD I_WAS_HERE4TH"
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
  for b in $RITUAL_BOTS; do printf '%s\n' ".playerbots bot add $b"; done >> "$FIFO"
  echo "watchdog: ritual queued (accounts: $RITUAL_ACCOUNTS bots: $RITUAL_BOTS)"
fi

printf 'prev_leo=%s\nprev_off_streak=%s\n' "$leo" "$prev_off_streak" > "$STATE"
