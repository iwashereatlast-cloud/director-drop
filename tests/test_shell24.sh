#!/usr/bin/env bash
# Tests for PR #42: botcmd doctor (read-only evidence bundle) + docs/assumptions.md.
# Self-locating: REPO = parent of this tests/ dir. Run: bash tests/test_shell24.sh
#  - bin/botcmd wiring: sources doctor.sh, usage line, dispatch
#  - safety contract: doctor.sh itself never calls game_send (the only game
#    traffic is states_verify's read-only query whispers, called WITHOUT remaster)
#  - doctor runs in a dead environment (no mysql/systemctl/FIFO) without
#    crashing: sections S1-S4 + summary print, exit code 1 (FAILs present)
#  - --bundle writes the full output to the file
#  - docs/assumptions.md: 18 rows, evidence rule, A13 confirmed
#  - shellcheck on the new lib: no warnings except pre-existing SC2148 class
set -u
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
fails=0
check() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1 (want [$3] got [$2])"; fails=$((fails+1)); fi; }

# --- wiring greps ---
check "bin/botcmd sources doctor.sh" "$(grep -c 'BC_LIB/doctor.sh' "$REPO/bin/botcmd")" "1"
check "bin/botcmd dispatch doctor)" "$(grep -c 'doctor) doctor_run' "$REPO/bin/botcmd")" "1"
check "usage line present" "$(grep -c 'botcmd doctor \[--quick\]' "$REPO/bin/botcmd")" "1"
check "bash -n bin/botcmd" "$(bash -n "$REPO/bin/botcmd" && echo ok)" "ok"
check "bash -n doctor.sh" "$(bash -n "$REPO/lib/botcmd/doctor.sh" && echo ok)" "ok"

# --- safety contract ---
check "doctor.sh never calls game_send directly" "$(grep -c 'game_send' "$REPO/lib/botcmd/doctor.sh" || true)" "0"
check "S5 calls states_verify all force (read-only)" "$(grep -c 'states_verify all force' "$REPO/lib/botcmd/doctor.sh")" "1"
check "S5 never passes remaster" "$(grep -cE 'states_verify.*remaster' "$REPO/lib/botcmd/doctor.sh" || true)" "0"
check "header states no-mutating contract" "$(grep -c 'NEVER sends a mutating game line' "$REPO/lib/botcmd/doctor.sh")" "1"

# --- docs ---
check "assumptions.md exists" "$([ -s "$REPO/docs/assumptions.md" ] && echo ok)" "ok"
check "assumptions.md has 18 rows" "$(grep -c '^| A' "$REPO/docs/assumptions.md")" "18"
check "assumptions.md evidence rule" "$(grep -c 'only. ticket for changing behavior' "$REPO/docs/assumptions.md")" "1"
check "assumptions.md A13 pre-confirmed" "$(grep -c 'shellacct gmlevel=3 RealmID=-1' "$REPO/docs/assumptions.md")" "1"
check "outline.md exists" "$([ -s "$REPO/docs/outline.md" ] && echo ok)" "ok"
check "outline.md proof protocol" "$(grep -c 'Proof protocol' "$REPO/docs/outline.md")" "1"
check "functions doc lists doctor" "$(grep -c 'botcmd doctor' "$REPO/docs/botcmd-functions.md")" "2"

# --- run doctor in a dead environment (quick: no live whisper pass) ---
mkdir -p /tmp/doctor-test
: > /tmp/doctor-empty-states.conf
out=$(env -i PATH=/usr/bin:/bin HOME=/tmp/doctor-test \
  BOTCMD_REPO="$REPO" BOTCMD_STATES_CONF=/tmp/doctor-empty-states.conf \
  BOTCMD_FIFO=/tmp/doctor-test/leo.in BOTCMD_MYCNF=/nonexistent \
  BOTCMD_VERIFY_WAIT=1 BOTCMD_NO_REFRESH=1 \
  bash "$REPO/bin/botcmd" doctor --quick 2>&1); rc=$?
check "doctor prints S1" "$(printf '%s' "$out" | grep -c 'S1 stack')" "1"
check "doctor prints S2" "$(printf '%s' "$out" | grep -c 'S2 playerbots.conf')" "1"
check "doctor prints S3" "$(printf '%s' "$out" | grep -c 'S3 DB ground truth')" "1"
check "doctor prints S4" "$(printf '%s' "$out" | grep -c 'S4 states conf')" "1"
check "doctor prints S6 summary" "$(printf '%s' "$out" | grep -c 'S6 DOCTOR SUMMARY')" "1"
check "doctor summary counts line" "$(printf '%s' "$out" | grep -cE '^PASS [0-9]+ +FAIL [0-9]+ +INFO [0-9]+$')" "1"
check "dead env -> FAILs counted -> rc=1" "$rc" "1"
check "no FIFO created by --quick run" "$([ ! -e /tmp/doctor-test/leo.in ] && echo ok)" "ok"
check "doctor --help works" "$(env -i PATH=/usr/bin:/bin HOME=/tmp/doctor-test BOTCMD_REPO="$REPO" bash "$REPO/bin/botcmd" doctor --help 2>&1 | grep -c 'READ-ONLY evidence bundle')" "1"

# --- bundle ---
rm -f /tmp/doctor-bundle.log
env -i PATH=/usr/bin:/bin HOME=/tmp/doctor-test \
  BOTCMD_REPO="$REPO" BOTCMD_STATES_CONF=/tmp/doctor-empty-states.conf \
  BOTCMD_FIFO=/tmp/doctor-test/leo.in BOTCMD_MYCNF=/nonexistent \
  bash "$REPO/bin/botcmd" doctor --quick --bundle /tmp/doctor-bundle.log >/dev/null 2>&1
check "bundle file written" "$([ -s /tmp/doctor-bundle.log ] && echo ok)" "ok"
check "bundle contains summary" "$(grep -c 'S6 DOCTOR SUMMARY' /tmp/doctor-bundle.log)" "1"
check "bundle header line" "$(grep -c 'doctor bundle ->' /tmp/doctor-bundle.log)" "1"

# --- shellcheck on the new lib (ignore the pre-existing SC2148 class) ---
SC=$(command -v shellcheck 2>/dev/null || { [ -x /home/appuser/.local/bin/shellcheck ] && echo /home/appuser/.local/bin/shellcheck; })
if [ -n "${SC:-}" ]; then
  sc=$("$SC" -S warning "$REPO/lib/botcmd/doctor.sh" 2>&1 | grep -E 'SC[0-9]+' | grep -v 'SC2148' | wc -l)
  check "shellcheck doctor.sh (excl SC2148) clean" "$sc" "0"
else
  check "shellcheck doctor.sh (skipped: no shellcheck)" "skip" "skip"
fi

echo; [ "$fails" -eq 0 ] && echo "ALL SHELL24 TESTS PASS" || echo "SHELL24 FAILURES: $fails"
exit $fails
