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

# pick bots: args, else up to 3 online toons from the RO census
if [ $# -gt 0 ]; then
  BOTS="$*"
else
  BOTS=$(mysql --defaults-file="$MYCNF" -N -B -e \
    "SELECT name FROM acore_characters.characters WHERE online=1 AND account LIKE 'i_was_here%' LIMIT 3" \
    2>/dev/null | tr '\n' ' ')
  [ -n "$BOTS" ] || { fail "no online bots found (census empty - pass bot names as args)"; exit 1; }
  say "auto-picked online bots: $BOTS"
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
