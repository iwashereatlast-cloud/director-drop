#!/usr/bin/env bash
# rtt-engine LIVE function check - run ON SERVERPC with Leo and bots online.
#
# Exercises every function TOOLS.md outlines against the REAL command server
# (127.0.0.1:8888) and prints PASS/FAIL per function with the full JSON
# envelopes as evidence. Paste the output back into the conversation to fill
# A19's result column and flip roadmap row 3 to LIVE-VERIFIED.
#
#   bash services/rtt-engine/live-check.sh [bot ...]
#
# With no args, picks up to 3 online watched bots from the read-only census
# (needs ~/.my.cnf-director). Requires the service running:
#   systemctl --user start rtt-engine
# (or it will start a temporary instance on /tmp/rtt-live-check.sock).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
SOCK="${RTT_SOCK:-/tmp/rtt-engine.sock}"
MYCNF="${RTT_MYCNF:-$HOME/.my.cnf-director}"
FAILS=0
TMP_PID=""

say()  { printf '%s\n' "$*"; }
pass() { say "PASS: $*"; }
fail() { say "FAIL: $*"; FAILS=$((FAILS+1)); }

cleanup() { [ -n "$TMP_PID" ] && kill "$TMP_PID" 2>/dev/null; }
trap cleanup EXIT

# start a temp service instance if the unit is not running
if ! [ -S "$SOCK" ]; then
  say "note: $SOCK not up - starting a temporary instance"
  RTT_SOCK="$SOCK" python3 "$HERE/rtt_engine.py" serve >"$HERE/live-check.log" 2>&1 &
  TMP_PID=$!
  for _ in $(seq 1 30); do [ -S "$SOCK" ] && break; sleep 0.1; done
  [ -S "$SOCK" ] || { fail "service did not come up (see live-check.log)"; exit 1; }
fi

# pick bots: args (capped at 5 - this is a spot check, not a fleet sweep),
# else up to 3 online GUILD members. The guild ('Special FXc') is the main
# filter - the roster IS the guild; never sweep every toon on the server
# (live finding 2026-11-10: a quoted blob of names bypassed an arg-count
# cap, so the cap counts WORDS and given names are filtered through the
# watch list before anything is queried).
#
# normalize args to a word list first: "A B C" as ONE quoted argument counts
# as three names, not one.
set -- $*
if [ $# -gt 5 ]; then
  say "note: more than 5 names given - using the first 5 (spot check, not a sweep)"
  set -- $(printf '%s\n' "$@" | head -5)
fi
# is_watched <name>: the SAME watch-list gate the service enforces (guild
# 'Special FXc' + bot accounts) - live-check refuses to query off-roster
# names even when they are handed to it.
is_watched() {
  case "$1" in
    *[!A-Za-z0-9_\'-]*) return 1 ;;
  esac
  mysql --defaults-file="$MYCNF" -N -B -e \
    "SELECT c.name FROM acore_characters.characters c "
    "JOIN acore_auth.account a ON a.id=c.account "
    "LEFT JOIN acore_characters.guild_member gm ON gm.guid=c.guid "
    "WHERE c.name='$1' AND (a.username LIKE 'i_was_here%' "
    "OR gm.guildid IN (SELECT guildid FROM acore_characters.guild "
    "WHERE name='Special FXc'))" \
    2>/dev/null | grep -q .
}
if [ $# -gt 0 ]; then
  KEPT=""
  for b in "$@"; do
    if is_watched "$b"; then
      KEPT="$KEPT $b"
    else
      say "skip $b: not on the watch list (guild 'Special FXc' or a bot account)"
    fi
  done
  BOTS="${KEPT# }"
  [ -n "$BOTS" ] || { fail "none of the given names are on the watch list"; exit 1; }
else
  BOTS=$(mysql --defaults-file="$MYCNF" -N -B -e \
    "SELECT c.name FROM acore_characters.characters c "
    "JOIN acore_characters.guild_member gm ON gm.guid=c.guid "
    "WHERE c.online=1 AND gm.guildid IN "
    "(SELECT guildid FROM acore_characters.guild WHERE name='Special FXc') "
    "ORDER BY c.name LIMIT 3" \
    2>/dev/null | tr '\n' ' ')
  [ -n "$BOTS" ] || { fail "no online guild bots found (census empty "
    "- is the DB reachable via $MYCNF, and are guild bots online? "
    "or pass bot names as args)"; exit 1; }
  say "auto-picked online guild bots: $BOTS"
fi

q() {  # $1 = request JSON; prints the raw envelope JSON
  python3 "$HERE/rtt_engine.py" query --socket "$SOCK" "$1"
}

check() {  # $1 = python expr over envelope 'e'; $2 = request JSON
  local expr="$1" req="$2" out
  out=$(q "$req" 2>&1) || { fail "$req -> client error: $out"; return; }
  say "  envelope: $(printf '%s' "$out" | tr -d '\n' | head -c 400)"
  if printf '%s' "$out" | python3 -c "
import json, sys
e = json.load(sys.stdin)
sys.exit(0 if ($expr) else 1)
"; then pass "$req"; else fail "$req -> $expr did not hold"; fi
}

say "== rtt-engine LIVE function check (command server 127.0.0.1:8888) =="

# stale-daemon detection: the running service must know the current op set
if ! q '{"op":"catalog"}' | grep -q get_live_status; then
  fail "running daemon is STALE CODE (catalog lacks get_live_status) -"
  say "  systemctl --user restart rtt-engine   (started before the last git pull)"
  exit 1
fi

say "-- function: ping (live path up) --"
check "e['ok'] and e['result']['alive'] is True" '{"op":"ping"}'

for b in $BOTS; do
  say "-- function: pinfo / get_current_position (live position of $b) --"
  check "e['ok'] and e['result']['online'] is True and e['result']['position'] is not None" \
    "{\"op\":\"pinfo\",\"name\":\"$b\"}"
  check "e['ok'] and e['result']['online'] is True and e['result']['position'] is not None" \
    "{\"op\":\"get_current_position\",\"name\":\"$b\"}"

  say "-- function: get_live_status (live state/HP of $b) --"
  check "e['ok'] and e['result']['online'] is True and e['result']['hp_pct'] is not None" \
    "{\"op\":\"get_live_status\",\"name\":\"$b\"}"
done

say "-- routing rails (refused with the owning service) --"
check "e['ok'] is False and e['error']['route_hint']=='db-search'" '{"op":"raid"}'
check "e['ok'] is False and e['error']['route_hint']=='chat-tunnel'" '{"op":"kick"}'
check "e['ok'] is False and e['error']['route_hint']=='chat-tunnel'" '{"op":"get_bot_master"}'
check "e['ok'] is False and e['error']['route_hint']=='chat-tunnel'" '{"op":"is_bot_responding"}'

say
say "A19 evidence line (fill the live result column in docs/assumptions.md):"
say "  PASS $(date +%Y-%m-%d) - live-check.sh: ping alive; pinfo/get_current_position"
say "  position non-null for online bots; get_live_status state+hp_pct; redirects correct"
if [ "$FAILS" -eq 0 ]; then
  say "ALL LIVE CHECKS PASSED"
  exit 0
fi
say "$FAILS LIVE CHECK(S) FAILED"
exit 1
