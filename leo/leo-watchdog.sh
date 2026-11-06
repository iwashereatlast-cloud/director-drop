#!/bin/bash
# Leo presence watchdog for Chazbot - one poll, run every 2 min by
# leo-watchdog.timer. Trusts the SERVER only (DB characters.online via the
# read-only MySQL identity), never the process table.
#
# Wanted online = server-local hour in [ON_H, OFF_H) OR any GROUP toon online.
# Zombie kill   = leo service active but server says Leo offline twice in a row.
# Auto-ritual   = on Leo's off->on transition, queue the bot-start ritual into
#                 the FIFO. NEVER I_WAS_HERE1ST - the G1 five are hand-play only.
# Auto-clear    = on the tick AFTER an auto-ritual, the mod has auto-joined every
#                 freshly logged-in bot into Leo's group (PlayerbotMgr.cpp:591-619
#                 -> queued GroupInviteOperation, PlayerbotOperations.h:37-90: creates
#                 the group, ConvertToRaid() at >=5 members, summons when
#                 SummonWhenGroup=1 conf:193 - NO conf key disables it), so ask every
#                 non-G1 member of Leo's group to 'leave' (/w <m> leave, unsecured
#                 bot command PlayerbotAI.cpp:1008; leaving keeps bot AI).
#                 Set AUTORITUAL_CLEAR=0 in ~/.leo-watchdog.conf to keep the raid.

MYCNF=$HOME/.my.cnf-director
STATE=$HOME/.leo-watchdog.state
CONF=$HOME/.leo-watchdog.conf
ON_H=8
OFF_H=22
# Both Aeris/Aerith spellings until verified in the DB (SELECT ... LIKE 'Aeri%').
GROUP="Cecil Rydia Shadow Vivi Aeris Aerith"
AUTORITUAL=1
AUTORITUAL_CLEAR=1
RITUAL_ACCOUNTS="I_WAS_HERE2ND I_WAS_HERE3RD I_WAS_HERE4TH I_WAS_HERE5TH"
# only needed for bots NOT on a RITUAL_ACCOUNTS account (covered bots are skipped)
RITUAL_BOTS="Golbez"
FIFO=/tmp/leo.in
[ -f "$CONF" ] && . "$CONF"

prev_leo=1
prev_off_streak=0
ritual_pending=0
[ -f "$STATE" ] && . "$STATE"
ritual_fired=0   # set when THIS run queues the auto-ritual - the clear runs on a LATER tick, once the fresh logins have auto-joined

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
  ritual_pending=1
  ritual_fired=1
fi

# Post-ritual clear (see header): the login wave above auto-joins every bot
# into Leo's group and the party converts to a raid at >=5 members. On this
# tick Leo is online, so undo it: /w <m> leave every non-G1 member of Leo's
# group, read live from the DB (group_member self-join). Bots auto-leave when
# the master leaves (KeepAltsInGroup=0, playerbots.conf.dist:155), so a
# pending marker is dropped without action when Leo is offline. The
# ritual_fired guard keeps this from running on the SAME tick that queued the
# ritual - the bots log in (and auto-join) after that tick's FIFO writes, so
# the group is cleared on the next tick instead.
if [ "$ritual_pending" = "1" ] && [ "$ritual_fired" = "0" ]; then
  if [ "$leo" = "1" ] && [ "$AUTORITUAL_CLEAR" = "1" ]; then
    members=$(q "SELECT c.name FROM acore_characters.characters lc JOIN acore_characters.group_member gm ON gm.memberGuid=lc.guid JOIN acore_characters.group_member gm2 ON gm2.guid=gm.guid JOIN acore_characters.characters c ON c.guid=gm2.memberGuid WHERE lc.name='Leo' ORDER BY c.name")
    n=0
    for m in $members; do
      [ "$m" = "Leo" ] && continue
      skip=0
      for g in $GROUP; do
        [ "$(printf '%s' "$m" | tr '[:upper:]' '[:lower:]')" = "$(printf '%s' "$g" | tr '[:upper:]' '[:lower:]')" ] && skip=1
      done
      if [ "$skip" = "1" ]; then
        echo "watchdog: clear: skip $m (hand-play G1 - never touched)"
        continue
      fi
      printf '/w %s leave\n' "$m" >> "$FIFO"
      n=$((n + 1))
    done
    if [ "$n" -gt 0 ]; then
      echo "watchdog: cleared Leo's auto-formed group - /w <m> leave sent to $n member(s)"
    else
      echo "watchdog: Leo has no clearable group members right now (nothing to do)"
    fi
  elif [ "$leo" = "1" ]; then
    echo "watchdog: AUTORITUAL_CLEAR=0 - leaving Leo's auto-formed group alone"
  else
    echo "watchdog: ritual pending dropped - Leo offline (bots auto-leave when the master leaves, KeepAltsInGroup=0)"
  fi
  ritual_pending=0
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

printf 'prev_leo=%s\nprev_off_streak=%s\nritual_pending=%s\n' "$leo" "$prev_off_streak" "$ritual_pending" > "$STATE"
