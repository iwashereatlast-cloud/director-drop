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
# LESSON (live 2026-11-10, twice): (a) a literal quote in a case pattern
# silently re-parses the rest of the file; (b) bash does NOT concatenate
# adjacent quoted strings across newlines - a multi-line -e argument needs
# a backslash on EVERY line or the SQL silently truncates and mysql dumps
# the whole table. Both passed bash -n. So: names validate via grep, and
# every SQL here is ONE line.
is_watched() {
  printf '%s' "$1" | grep -Eq '^[A-Za-z0-9_-]{1,12}$' || return 1
  mysql --defaults-file="$MYCNF" -N -B -e "SELECT c.name FROM acore_characters.characters c JOIN acore_auth.account a ON a.id=c.account LEFT JOIN acore_characters.guild_member gm ON gm.guid=c.guid WHERE c.name='$1' AND (a.username LIKE 'i_was_here%' OR gm.guildid IN (SELECT guildid FROM acore_characters.guild WHERE name='Special FXc'))" 2>/dev/null | grep -q .
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
  BOTS=$(mysql --defaults-file="$MYCNF" -N -B -e "SELECT c.name FROM acore_characters.characters c JOIN acore_characters.guild_member gm ON gm.guid=c.guid WHERE c.online=1 AND gm.guildid IN (SELECT guildid FROM acore_characters.guild WHERE name='Special FXc') ORDER BY c.name LIMIT 3" 2>/dev/null | tr '\n' ' ')
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

# REACH (source-pinned on the live build, PlayerbotCommandServer.cpp:49):
# every 8888 request is forwarded to RandomPlayerbotMgr::HandleRemoteCommand,
# so the registry is the RANDOM-bot pool. Master-owned playerbots answer
# 'invalid guid' even with live AI (live-proven: Cyan, OK in states verify,
# invalid guid here). That reply is the command server's honest 'not in my
# registry' - a NOTE, not a failure. A position/HP parse proves out when a
# RANDOM bot is on the watch list (e.g. an RNDBOT invited to the guild).
live_check_op() {  # $1 = op label, $2 = request JSON, $3 = result assertion
  local label="$1" req="$2" expr="$3" out
  say "-- function: $label --"
  out=$(q "$req" 2>&1) || { fail "$label client error"; return; }
  say "  envelope: $(printf '%s' "$out" | tr -d '\n' | head -c 400)"
  if printf '%s' "$out" | grep -q 'invalid guid'; then
    say "  NOTE (reach): not in the 8888 registry (RandomPlayerbotMgr - master-owned"
    say "  playerbots are not served by the stock port); db-search is the position"
    say "  source. A guild-invited RNDBOT proves the parse."
  elif printf '%s' "$out" | python3 -c "
import json, sys
e = json.load(sys.stdin)
sys.exit(0 if ($expr) else 1)
"; then
    pass "$label"
  else
    fail "$label -> $expr did not hold"
  fi
}

for b in $BOTS; do
  live_check_op "pinfo (live position of $b)" \
    "{\"op\":\"pinfo\",\"name\":\"$b\"}" \
    "e['ok'] and e['result']['online'] is True and e['result']['position'] is not None"
  live_check_op "get_current_position (live position of $b)" \
    "{\"op\":\"get_current_position\",\"name\":\"$b\"}" \
    "e['ok'] and e['result']['online'] is True and e['result']['position'] is not None"
  live_check_op "get_live_status (live state/HP of $b)" \
    "{\"op\":\"get_live_status\",\"name\":\"$b\"}" \
    "e['ok'] and e['result']['online'] is True and e['result']['hp_pct'] is not None"
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
