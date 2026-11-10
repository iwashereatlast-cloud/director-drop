# Tests

Pinned harnesses (run on serverpc or in the sandbox):

```
for t in tests/test_shell*.sh; do printf '%-24s ' "$(basename "$t")"; bash "$t" 2>&1 | tail -1; done
python3 tests/test_presence.py          # heartbeat conditions + service allowlist
python3 tests/test_services_wiring.py   # director -> db-search + chat-tunnel wiring
```

- `test_presence.py` — the presence thread is a HAND: drives `_hb_condition`
  (when the LLM gets asked) and `_hb_exec_service` (the only executor, strict
  `{"service":"leo"}` allowlist) with mocked facts. No DB, no LLM.
- `test_services_wiring.py` — the row-5 wiring proof: runs the REAL db-search
  (SQLite fixture) and REAL chat-tunnel (temp log/FIFO) offline, then drives
  director.py's census/masters/sender gates, group lookups, query digest
  (queried_at + caveat), the validated order pipeline sending through the
  tunnel, live strategies via inspect, and the presence fact gatherer; also
  asserts the raw paths (mysql CLI, FIFO writes, log tailing, botcmd probes,
  8888) are gone from director.py.

- `test_shell24.sh` — PR #42 `botcmd doctor`: wiring, safety contract (no
  `game_send` in doctor.sh; `states verify all force` without `remaster`),
  dead-environment run (sections print, rc=1, no crash, no FIFO created with
  `--quick`), `--bundle`, `--help`, docs row counts, shellcheck clean.
  Self-locating: `REPO` is derived from the script path, so it runs from any
  clone.

History note: harnesses `test_shell.sh`..`test_shell23.sh`, `test_states.py`,
`test_raid.py` and `audit_smoke.sh` lived in `/tmp` only and were lost when the
sandbox was wiped (2026-11-07). Their assertions are described in the merged PR
descriptions (#29–#42). `/tmp` is ephemeral — anything that must survive goes in
this repo, never in `/tmp`.
