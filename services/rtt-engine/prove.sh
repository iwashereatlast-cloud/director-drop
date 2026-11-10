#!/usr/bin/env bash
# rtt-engine proof harness - runs WITHOUT a live worldserver or DB.
#
# Spins up a MOCK of the mod-playerbots command server (127.0.0.1, stock
# '<verb>,<guid>\n' line protocol, scripted from fixtures/) plus the REAL
# rtt-engine service (pbcmd backend, GUID lookups from a fixture file), then
# drives the stable rtt-engine/1 interface through the real client path.
# Asserts: envelope unchanged from rtt-engine/1 (callers must not notice the
# backend swap), live position reads, offline/invalid-guid fallback to
# db-search, the routing rule (kick -> chat-tunnel), and the GUID cache.
#
#   bash services/rtt-engine/prove.sh
#
# Every check prints PASS/FAIL; exit 0 only if all pass.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
TMP=$(mktemp -d)
SOCK="$TMP/rtt-test.sock"
PORT=18888
GUIDS="$TMP/guid-fixture.json"
PIDS=""
FAILS=0

say() { printf '%s\n' "$*"; }
pass() { say "PASS: $*"; }
fail() { say "FAIL: $*"; FAILS=$((FAILS+1)); }

cleanup() {
  for p in $PIDS; do kill "$p" 2>/dev/null; done
  rm -rf "$TMP"
}
trap cleanup EXIT

start_mock_cmdserver() {
  python3 "$HERE/fixtures/mock-cmdserver.py" "$PORT" \
    "$HERE/fixtures/cmdserver-responses.json" >"$TMP/cmdserver.out" 2>&1 &
  PIDS="$PIDS $!"
  for _ in $(seq 1 50); do
    python3 -c "import socket; socket.create_connection(('127.0.0.1', $PORT), 1).close()" \
      2>/dev/null && return 0
    sleep 0.1
  done
  fail "mock command server did not come up on $PORT"; return 1
}

start_engine() {
  RTT_SOCK="$SOCK" RTT_PBHOST=127.0.0.1 RTT_PBPORT="$PORT" \
  RTT_GUID_SOURCE=fixture RTT_GUID_FIXTURE="$GUIDS" \
    python3 "$HERE/rtt_engine.py" serve >"$TMP/engine.out" 2>&1 &
  PIDS="$PIDS $!"
  for _ in $(seq 1 50); do
    [ -S "$SOCK" ] && return 0
    sleep 0.1
  done
  fail "service did not open $SOCK"; return 1
}

q() { python3 "$HERE/rtt_engine.py" query --socket "$SOCK" "$1"; }

# $1 = python expression over the envelope dict 'e'; $2 = request JSON
check() {
  local expr="$1" req="$2"
  local out
  out=$(q "$req" 2>&1) || { fail "$req -> client error: $out"; return 1; }
  printf '%s' "$out" >"$TMP/last.json"
  if python3 -c "
import json, sys
e = json.load(open(sys.argv[1]))
sys.exit(0 if ($expr) else 1)
" "$TMP/last.json"; then
    pass "$req"
  else
    fail "$req -> $expr did not hold: $(printf '%s' "$out" | tr -d '\n')"
  fi
}

cp "$HERE/fixtures/guid-fixture.json" "$GUIDS"

say "== rtt-engine proof harness (pbcmd backend vs mock 8888 server, no worldserver) =="

start_mock_cmdserver || exit 1
start_engine || exit 1

# --- interface shape: the rtt-engine/1 envelope, key-for-key UNCHANGED ---
# (callers must not notice the backend swap)
check "set(e.keys()) == {'interface','ok','op','backend','queried_at','latency_ms','result','error'} and e['interface']=='rtt-engine/1' and e['backend']=='pbcmd' and e['ok'] is True and e['op']=='ping' and isinstance(e['latency_ms'],int) and 'queried_at' in e and e['error'] is None and e['result']['alive'] is True" \
  '{"op":"ping"}'

# --- op catalog: what routes here; kick -> chat-tunnel, DB-answerable -> db-search ---
check "set(o['op'] for o in e['result']['ops']) == {'catalog','ping','pinfo'} and e['result']['refused']['route_hints']['kick']=='chat-tunnel' and e['result']['refused']['route_hints']['raid']=='db-search'" \
  '{"op":"catalog"}'

# --- live position of an ONLINE bot via the 8888 line protocol ---
check "e['ok'] and e['result']['online'] is True and e['result']['guid']==9 and e['result']['map']==1 and e['result']['position']==[-8925.06,133.77,83.33] and e['result']['zone']=='Dun Morogh'" \
  '{"op":"pinfo","name":"Vivi"}'
check "e['ok'] and e['result']['online'] is True and e['result']['guid']==21" \
  '{"op":"pinfo","name":"Locke"}'

# --- offline bot: command server says not live -> db-search is the truth ---
check "e['ok'] and e['result']['online'] is False and 'db-search' in e['result']['caveat']" \
  '{"op":"pinfo","name":"Terra"}'
# invalid guid (empty reply, connection closed): same offline fallback
check "e['ok'] and e['result']['online'] is False and 'db-search' in e['result']['caveat']" \
  '{"op":"pinfo","name":"Cecil"}'
# name with no GUID at all: offline fallback, never an error
check "e['ok'] and e['result']['online'] is False and 'db-search' in e['result']['caveat']" \
  '{"op":"pinfo","name":"Nobody"}'

# --- routing rule: kick -> chat-tunnel (accepted gap), DB-answerable -> db-search ---
check "e['ok'] is False and e['error']['code']=='wrong_service' and e['error']['route_hint']=='chat-tunnel' and 'accepted gap' in e['error']['message']" \
  '{"op":"kick"}'
check "e['ok'] is False and e['error']['code']=='wrong_service' and e['error']['route_hint']=='db-search'" \
  '{"op":"raid"}'
check "e['ok'] is False and e['error']['code']=='unknown_op' and e['error']['route_hint'].startswith('db-search')" \
  '{"op":"bogus_op"}'

# --- bad params ---
check "e['ok'] is False and e['error']['code']=='bad_params'" \
  '{"op":"pinfo"}'

# --- raw non-JSON line straight at the socket: the server answers an envelope ---
if printf 'not json at all\n' | python3 -c "
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sys.argv[1])
s.sendall(sys.stdin.buffer.read())
print(s.recv(65536).decode())
" "$SOCK" >"$TMP/last.json" 2>&1 \
   && python3 -c "
import json, sys
e = json.load(open(sys.argv[1]))
sys.exit(0 if (e['ok'] is False and e['error']['code']=='bad_params') else 1)
" "$TMP/last.json"; then
  pass 'raw non-JSON line -> bad_params envelope'
else
  fail "raw non-JSON line -> bad_params envelope: $(tr -d '\n' <"$TMP/last.json")"
fi

# --- backend down: command server unreachable -> backend_unavailable, same envelope ---
kill $(echo $PIDS | awk '{print $1}') 2>/dev/null   # the mock command server
PIDS=$(echo $PIDS | cut -d' ' -f2-)
sleep 0.3
check "e['ok'] is False and e['error']['code']=='backend_unavailable' and set(e.keys()) == {'interface','ok','op','backend','queried_at','latency_ms','result','error'}" \
  '{"op":"ping"}'
check "e['ok'] is False and e['error']['code']=='backend_unavailable'" \
  '{"op":"pinfo","name":"Vivi"}'

# --- GUID-lookup cache: one lookup per name per TTL, even if the fixture changes ---
if python3 - "$HERE" "$GUIDS" <<'PYEOF'
import json, sys, time
sys.path.insert(0, sys.argv[1])
import os
os.environ["RTT_GUID_SOURCE"] = "fixture"
os.environ["RTT_GUID_FIXTURE"] = sys.argv[2]
import rtt_engine
b = rtt_engine.PbCmdBackend()
g1 = b.guid("Vivi")
json.dump({"Vivi": 999}, open(sys.argv[2], "w"))
g2 = b.guid("Vivi")          # cached: still the OLD guid
b2 = rtt_engine.PbCmdBackend()
g3 = b2.guid("Vivi")         # fresh resolver: sees the NEW guid
sys.exit(0 if (g1 == 9 and g2 == 9 and g3 == 999) else 1)
PYEOF
then
  pass "GUID cache: second lookup served from cache; fresh resolver sees the change"
else
  fail "GUID cache: lookup was not cached (or fresh resolver stale)"
fi

say
if [ "$FAILS" -eq 0 ]; then
  say "ALL CHECKS PASSED"
  exit 0
fi
say "$FAILS CHECK(S) FAILED"
exit 1
