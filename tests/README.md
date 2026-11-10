# Tests

Pinned harnesses (run on serverpc or in the sandbox):

```
for t in tests/test_shell*.sh; do printf '%-24s ' "$(basename "$t")"; bash "$t" 2>&1 | tail -1; done
```

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
