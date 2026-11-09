#!/usr/bin/env bash
# rtt-engine proof harness - runs WITHOUT a live worldserver.
# Starts the service on a temp unix socket with the MOCK backend, drives the
# stable JSON interface through the real client path, then demonstrates the
# backend-swap seam (mock -> cmdbus stub -> unconfigured soap) with the
# response envelope UNCHANGED.
#
#   bash services/rtt-engine/prove.sh
#
# Every check prints PASS/FAIL; exit 0 only if all pass.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
TMP=$(mktemp -d)
SOCK="$TMP/rtt-test.sock"
PID=""
FAILS=0

say() { printf '%s\n' "$*"; }
pass() { say "PASS: $*"; }
fail() { say "FAIL: $*"; FAILS=$((FAILS+1)); }

start_engine() {  # $1 = backend
  RTT_SOCK="$SOCK" RTT_BACKEND="$1" python3 "$HERE/rtt_engine.py" serve \
    >"$TMP/engine-$1.out" 2>&1 &
  PID=$!
  for _ in $(seq 1 50); do
    [ -S "$SOCK" ] && break
    sleep 0.1
  done
  [ -S "$SOCK" ] || { fail "service did not open $SOCK (backend $1)"; return 1; }
}

stop_engine() {
  [ -n "$PID" ] && kill "$PID" 2>/dev/null
  wait "$PID" 2>/dev/null
  PID=""
  rm -f "$SOCK"
}

q() {  # $1 = request JSON (uses RTT_SOCK from the environment)
  python3 "$HERE/rtt_engine.py" query --socket "$SOCK" "$1"
}

# $1 = python expression over the envelope dict 'e'; $2 = request JSON
check() {
  local expr="$1" req="$2" env_backend="${3:-mock}"
  local out
  out=$(RTT_BACKEND="$env_backend" q "$req" 2>&1) || { fail "$req -> client error: $out"; return 1; }
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

say "== rtt-engine proof harness (mock backend, no worldserver) =="

start_engine mock || { rm -rf "$TMP"; exit 1; }

# --- interface shape (stable envelope, identical keys for every op/backend) ---
check "e['interface']=='rtt-engine/1' and e['backend']=='mock' and e['ok'] is True and e['op']=='ping' and isinstance(e['latency_ms'],int) and 'queried_at' in e and e['error'] is None and e['result']['alive'] is True" \
  '{"op":"ping"}'

# --- op catalog: what routes here, and what is refused to db-search ---
check "set(o['op'] for o in e['result']['ops']) == {'catalog','ping','server_info','pinfo','console'} and e['result']['refused']['route_hint'].startswith('db-search')" \
  '{"op":"catalog"}'

# --- tick-sensitive reads ---
check "e['ok'] and e['result']['online_players']==17 and e['result']['uptime'].startswith('3 hour')" \
  '{"op":"server_info"}'
check "e['ok'] and e['result']['online'] is True and e['result']['map']==1 and e['result']['zone']=='Dun Morogh' and e['result']['position']==[-8925.06,133.77,83.33]" \
  '{"op":"pinfo","name":"Vivi"}'
check "e['ok'] and e['result']['online'] is False" \
  '{"op":"pinfo","name":"Cecil"}'

# --- console passthrough: allowlist gate ---
check "e['ok'] and e['result']['reply'].startswith('Player limit')" \
  '{"op":"console","command":".server plimit"}'
check "e['ok'] is False and e['error']['code']=='refused_console'" \
  '{"op":"console","command":".kick Vivi"}'

# --- routing rule: DB-answerable asks are refused with a db-search hint ---
check "e['ok'] is False and e['error']['code']=='wrong_service' and e['error']['route_hint']=='db-search'" \
  '{"op":"raid"}'
check "e['ok'] is False and e['error']['code']=='unknown_op' and e['error']['route_hint'].startswith('db-search')" \
  '{"op":"bogus_op"}'

# --- bad params ---
check "e['ok'] is False and e['error']['code']=='bad_params'" \
  '{"op":"pinfo"}'
# raw non-JSON line straight at the socket: the server answers an envelope
if printf 'not json at all\n' | python3 -c "
import json, socket, sys
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
  fail "raw non-JSON line -> bad_params envelope: $(cat "$TMP/last.json" | tr -d '\n')"
fi

stop_engine

say
say "== backend-swap seam: same interface, different backend =="

start_engine cmdbus || { rm -rf "$TMP"; exit 1; }
check "e['interface']=='rtt-engine/1' and e['backend']=='cmdbus' and e['ok'] is False and e['error']['code']=='backend_unavailable' and 'not wired' in e['error']['message']" \
  '{"op":"ping"}' cmdbus
check "e['backend']=='cmdbus' and e['ok'] is False and e['error']['code']=='backend_unavailable'" \
  '{"op":"pinfo","name":"Vivi"}' cmdbus
stop_engine

start_engine soap || { rm -rf "$TMP"; exit 1; }
# soap backend with no ~/.soap-director in this harness env: graceful
# backend_unavailable, SAME envelope keys as the mock run above.
check "e['interface']=='rtt-engine/1' and e['backend']=='soap' and e['ok'] is False and e['error']['code']=='backend_unavailable' and 'SOAP not configured' in e['error']['message']" \
  '{"op":"ping"}' soap
check "e['backend']=='soap' and e['ok'] is False and e['error']['code']=='backend_unavailable' and 'route_hint' not in e['error']" \
  '{"op":"pinfo","name":"Vivi"}' soap
# routing rule holds on EVERY backend:
check "e['ok'] is False and e['error']['code']=='wrong_service' and e['error']['route_hint']=='db-search'" \
  '{"op":"money"}' soap
stop_engine

rm -rf "$TMP"
say
if [ "$FAILS" -eq 0 ]; then
  say "ALL CHECKS PASSED"
  exit 0
fi
say "$FAILS CHECK(S) FAILED"
exit 1
