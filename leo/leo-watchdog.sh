#!/bin/bash
# leo-watchdog.sh - INFO-ONLY observer (single-brain rule, 2026-11-10).
#
# The LLM daemon (director.py) is the ONLY decision-maker about game state.
# This script no longer starts/stops/restarts leo, queues the bot-start
# ritual, or clears the auto-formed group - the daemon's presence manager
# does all of that (same knobs, same ~/.leo-watchdog.conf, decisions land in
# daemon.log + audit.log). What is left here is the independent pair of eyes:
# once per timer tick it reads the SAME ground truth (DB online flags, leo
# service state) and JOURNALS state changes - including the exact case that
# matters when the daemon is down: "wanted-online but Leo is offline".
# It never writes to the FIFO and never touches systemctl.
#
# Why keep it at all: when the daemon is down, the body is down BY DESIGN -
# and this journal is how you find out what happened while the brain was
# absent. journalctl --user -u leo-watchdog.service reads it back.

MYCNF=$HOME/.my.cnf-director
STATE=$HOME/.leo-watchdog.state
CONF=$HOME/.leo-watchdog.conf
ON_H=8
OFF_H=22
# Both Aeris/Aerith spellings until verified in the DB (SELECT ... LIKE 'Aeri%').
GROUP="Cecil Rydia Shadow Vivi Aeris Aerith"
[ -f "$CONF" ] && . "$CONF"

prev_leo=""
prev_svc=""
prev_wanted=""
[ -f "$STATE" ] && . "$STATE"

q() { mysql --defaults-file="$MYCNF" -N -B -e "$1" 2>/dev/null; }

hour=$((10#$(date +%H)))
leo=$(q "SELECT online FROM acore_characters.characters WHERE name='Leo'")
[ -n "$leo" ] || leo=1
# empty answer = DB hiccup: assume online, never report a phantom offline

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

# report only CHANGES, plus the standing alarm: wanted-online but not up
if [ "$leo" != "$prev_leo" ]; then
  echo "watchdog-info: Leo online flag $prev_leo -> $leo (service: $svc, wanted: $wanted)"
fi
if [ "$svc" != "$prev_svc" ]; then
  echo "watchdog-info: leo service $prev_svc -> $svc (Leo online flag: $leo, wanted: $wanted)"
fi
if [ "$wanted" = "1" ] && [ "$svc" != "active" ]; then
  echo "watchdog-info: WANTED-ONLINE but leo service is '$svc' (hour $hour, group_online $group_on) - the daemon presence manager should act on this; if it repeats, the daemon is down"
fi
if [ "$wanted" = "1" ] && [ "$leo" = "0" ] && [ "$svc" = "active" ]; then
  echo "watchdog-info: leo service active but server says Leo offline (zombie candidate - the daemon presence manager should act on this)"
fi

# Duplicate-instance audit (warn only - never kill anything from here).
n_daemon=$(pgrep -fc 'director\.py daemon' 2>/dev/null); n_daemon=${n_daemon:-0}
n_shell=$(pgrep -fc 'shell\.py' 2>/dev/null); n_shell=${n_shell:-0}
if [ "$n_daemon" -gt 1 ] 2>/dev/null; then
  echo "watchdog-info: WARNING - $n_daemon director.py daemon processes (pids: $(pgrep -f 'director\.py daemon' | paste -sd' ')); they double-answer whispers - fix: pkill -f 'director\.py daemon'; systemctl --user restart leo-director"
fi
if [ "$n_shell" -gt 1 ] 2>/dev/null; then
  echo "watchdog-info: WARNING - $n_shell leo shell processes (pids: $(pgrep -f 'shell\.py' | paste -sd' ')) - fix: systemctl --user restart leo"
fi

printf 'prev_leo=%s\nprev_svc=%s\nprev_wanted=%s\n' "$leo" "$svc" "$wanted" > "$STATE"
