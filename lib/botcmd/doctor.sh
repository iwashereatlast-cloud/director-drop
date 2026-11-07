# Doctor: READ-ONLY evidence bundle for the whole stack.
#
# Safety contract: doctor NEVER sends a mutating game line. The only game
# traffic it can generate is the read-only strategy query ('nc ?'/'co ?')
# whispers issued inside states_verify - and doctor calls states_verify
# WITHOUT 'remaster' (no bot remove/add, no state changes, no group touches).
# Everything else is: systemctl is-active, process/log inspection, conf greps,
# read-only SQL. Exit code: 1 if any FAIL, else 0 (usable as a gate).
#
# Output: sections S1..S6 with PASS/FAIL/INFO lines + a summary. Use
#   botcmd doctor --bundle ~/doctor-$(date +%Y%m%d-%H%M%S).log
# and paste the bundle back - that is the evidence for docs/assumptions.md.

DOCTOR_PASS=0
DOCTOR_FAIL=0
DOCTOR_INFO=0

_dpass() { DOCTOR_PASS=$((DOCTOR_PASS + 1)); printf 'PASS  %s\n' "$*"; }
_dfail() { DOCTOR_FAIL=$((DOCTOR_FAIL + 1)); printf 'FAIL  %s\n' "$*"; }
_dinfo() { DOCTOR_INFO=$((DOCTOR_INFO + 1)); printf 'INFO  %s\n' "$*"; }
_dchk() { # $1 = label, $2 = condition rc (0 = pass)
  if [ "$2" -eq 0 ]; then _dpass "$1"; else _dfail "$1"; fi
}

# actual value of a playerbots.conf key (spaces/quotes stripped); empty = absent
_pb_conf_key() { # $1 = file, $2 = key
  [ -r "$1" ] || return 1
  grep -E "^[[:space:]]*$2[[:space:]]*=" "$1" 2>/dev/null | head -n 1 \
    | sed 's/^[^=]*=//; s/^[[:space:]]*//; s/[[:space:]]*$//; s/^"//; s/"$//'
}

_pb_conf_find() { # prints path of the live playerbots.conf (not .dist)
  local c
  if [ -n "${BOTCMD_PB_CONF:-}" ] && [ -r "$BOTCMD_PB_CONF" ]; then
    printf '%s\n' "$BOTCMD_PB_CONF"; return 0
  fi
  c=$(find "$HOME" -maxdepth 7 -name playerbots.conf -not -name '*.dist' 2>/dev/null | head -n 1)
  if [ -n "$c" ]; then printf '%s\n' "$c"; return 0; fi
  c=$(find "$HOME" -maxdepth 7 -name playerbots.conf.dist 2>/dev/null | head -n 1)
  [ -n "$c" ] && printf '%s\n' "$c"
  [ -n "$c" ]
}

_doctor_s1_stack() {
  hdr "S1 stack: units, FIFO, processes, logs"
  local st
  if command -v systemctl >/dev/null 2>&1; then
    st=$(systemctl is-active ac-worldserver 2>/dev/null || echo unknown)
    _dchk "ac-worldserver system unit active (got: $st)" $([ "$st" = active ]; echo $?)
    st=$(user_unit_state leo)
    _dchk "leo shell unit active (got: $st)" $([ "$st" = active ]; echo $?)
    st=$(user_unit_state leo-director)
    _dchk "leo-director unit active (got: $st)" $([ "$st" = active ]; echo $?)
    st=$(user_unit_state leo-watchdog.timer)
    _dchk "leo-watchdog.timer active (got: $st)" $([ "$st" = active ]; echo $?)
  else
    _dinfo "no systemctl on PATH - skipping unit checks"
  fi
  _dchk "FIFO $BOTCMD_FIFO exists" $([ -p "$BOTCMD_FIFO" ]; echo $?)
  local n
  n=$(pgrep -fc 'shell\.py' 2>/dev/null || echo 0)
  _dchk "Leo headless client process running (count=$n)" $([ "${n:-0}" -ge 1 ]; echo $?)
  _dchk "shell log $BOTCMD_SHELL_LOG readable" $([ -r "$BOTCMD_SHELL_LOG" ]; echo $?)
  _dchk "daemon log $BOTCMD_DIRECTOR_LOG readable" $([ -r "$BOTCMD_DIRECTOR_LOG" ]; echo $?)
  if [ -r "$BOTCMD_SHELL_LOG" ]; then
    _dinfo "last 5 whisper lines in leo.log:"
    grep -E '\[(whisper|guild)\]:' "$BOTCMD_SHELL_LOG" 2>/dev/null | tail -n 5 | sed 's/^/      /'
  fi
  if [ -r "$BOTCMD_DIRECTOR_LOG" ]; then
    n=$(grep -c 'bot error surfaced to LLM context' "$BOTCMD_DIRECTOR_LOG" 2>/dev/null || true)
    _dinfo "daemon.log: $n bot-error lines surfaced to LLM context since #41 (grep 'bot error surfaced')"
    _dinfo "daemon.log last 3 lines:"
    tail -n 3 "$BOTCMD_DIRECTOR_LOG" 2>/dev/null | sed 's/^/      /'
  fi
}

_doctor_s2_pbconf() {
  hdr "S2 playerbots.conf keys we depend on"
  local f
  if ! f=$(_pb_conf_find); then
    _dfail "playerbots.conf not found (set BOTCMD_PB_CONF in ~/.botcmd.conf)"
    return 0
  fi
  _dinfo "conf file: $f"
  # key|expected|why
  local rows=(
    "SummonWhenGroup|1|bots summoned into master group on join (post-ritual clear depends on it)"
    "KeepAltsInGroup|0|bots auto-leave when master leaves"
    "AllowAccountBots|1|'bot addaccount' works"
    "AllowGuildBots|1|guild bots addable"
    "AllowTrustedAccountBots|1|cross-account addaccount after roster link"
    "MaxAddedBots|40|headroom for the roster (>=16 toons)"
    "WhisperDistance|6000|bot whisper chatter range"
    "RandomBotSayWithoutMaster|0|null master => total silence (why NO REPLY happens)"
    "RandomBotCountMode|0|static online count, no rotation (Nov 5 build)"
    "PlayerbotsDatabaseInfo|acore_playerbots|where account links/keys live"
  )
  local r key exp why actual
  for r in "${rows[@]}"; do
    key=${r%%|*}; rest=${r#*|}; exp=${rest%%|*}; why=${rest#*|}
    actual=$(_pb_conf_key "$f" "$key")
    if [ -z "$actual" ]; then
      _dinfo "$key absent (dist default expected: $exp) - $why"
    elif [ "$actual" = "$exp" ]; then
      _dpass "$key=$actual - $why"
    else
      _dfail "$key=$actual (expected $exp) - $why"
    fi
  done
  # CommandSeparator: conf holds "\\" (escaped backslash) - accept \ or \\
  actual=$(_pb_conf_key "$f" "CommandSeparator")
  if [ -z "$actual" ]; then
    _dinfo "CommandSeparator absent (dist default: backslash)"
  elif [ "$actual" = '\' ] || [ "$actual" = '\\' ]; then
    _dpass "CommandSeparator=$actual"
  else
    _dfail "CommandSeparator=$actual (expected backslash) - multi-cmd ';;' splitting in conf values depends on it"
  fi
}

_doctor_s3_db() {
  hdr "S3 DB ground truth (read-only)"
  _dchk "mysql client on PATH" $(command -v mysql >/dev/null 2>&1; echo $?)
  _dchk "RO defaults file $BOTCMD_MYCNF readable" $([ -r "$BOTCMD_MYCNF" ]; echo $?)
  if ! command -v mysql >/dev/null 2>&1 || [ ! -r "$BOTCMD_MYCNF" ]; then
    _dinfo "skipping all SQL sections (no mysql / no RO identity)"
    return 0
  fi
  _dinfo "accounts (username, gmlevel, RealmID - note RealmID -1 means ALL realms, not a bug):"
  db_ro -t -e "SELECT username, gmlevel, RealmID FROM acore_auth.account WHERE username LIKE '$BOTCMD_ACCOUNT_LIKE' OR username LIKE '${BOTCMD_SHELL_ACCOUNT}%' ORDER BY username" 2>/dev/null | sed 's/^/      /' || _dfail "acore_auth.account query failed"
  local gm
  gm=$(db_ro -N -B -e "SELECT COALESCE(MAX(gmlevel),0) FROM acore_auth.account WHERE username LIKE '${BOTCMD_SHELL_ACCOUNT}%'" 2>/dev/null || echo 0)
  _dchk "shell account ${BOTCMD_SHELL_ACCOUNT}% gmlevel >= 3 for .playerbots bot commands (got: $gm)" $([ "${gm:-0}" -ge 3 ]; echo $?)
  _dinfo "census (watched accounts + guild '$BOTCMD_WATCH_GUILD'):"
  if roster_census; then _dpass "census query ok"; else _dfail "census query failed"; fi
  local on n
  on=$(_roster_online_watched | grep -c . || true)
  _dinfo "DB-online watched bot toons right now: $on"
  printf '%s\n' "$(_roster_online_watched)" | sed 's/^/      /'
  if db_ro -N -B -e "SELECT COUNT(*) FROM acore_playerbots.playerbots_account_links" >/dev/null 2>&1; then
    _dpass "acore_playerbots.playerbots_account_links readable"
    _dinfo "account links (both directions are inserted on link):"
    db_ro -t -e "SELECT * FROM acore_playerbots.playerbots_account_links LIMIT 20" 2>/dev/null | sed 's/^/      /'
  else
    _dfail "acore_playerbots.playerbots_account_links NOT readable with the RO identity (roster link needs it)"
  fi
  _dinfo "Leo's group/raid from DB:"
  if director_raid "${BOTCMD_LEO:-Leo}"; then _dpass "group query ok"; else _dfail "group query failed (director.py group via RO mysql)"; fi
}

_doctor_s4_statesconf() {
  hdr "S4 states conf layout ($BOTCMD_STATES_CONF)"
  if states_show; then _dpass "states conf readable"; else _dfail "no states conf at $BOTCMD_STATES_CONF (botcmd states edit)"; fi
}

_doctor_s5_live() {
  hdr "S5 live whisper read-back (states verify all force - READ-ONLY, no remaster)"
  local tmp
  tmp=$(mktemp /tmp/.doctor-verify.XXXXXX)
  if states_verify all force >"$tmp" 2>&1; then :; fi
  cat "$tmp"
  local ok mis nr off
  ok=$(grep -c 'OK (' "$tmp" || true)
  mis=$(grep -c 'MISMATCH' "$tmp" || true)
  nr=$(grep -c 'NO REPLY' "$tmp" || true)
  off=$(grep -c 'db:offline' "$tmp" || true)
  _dinfo "verify tally: OK=$ok MISMATCH=$mis NO REPLY=$nr db:offline=$off"
  if [ "$((mis + nr))" -gt 0 ]; then
    _dfail "$((mis + nr)) conf bot(s) not matching live state (MISMATCH/NO REPLY) - fix: botcmd states apply [all]; silent bots: botcmd states verify all remaster"
  else
    _dpass "all probed bots match their conf state"
  fi
  [ "$off" -gt 0 ] && _dinfo "$off bot(s) DB-offline (not probed without force semantics; add them: botcmd roster add <toon> or verify all force remaster)"
  rm -f "$tmp"
}

doctor_run() {
  local bundle="" quick=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --bundle) bundle="${2:-}"; [ -n "$bundle" ] || { errmsg "--bundle needs a file path"; return 2; }; shift 2 ;;
      --quick) quick=1; shift ;;
      -h|--help)
        cat <<'EOF'
botcmd doctor [--quick] [--bundle FILE]
  READ-ONLY evidence bundle. Never sends a mutating game command; the only
  game traffic is the read-only strategy query whispers inside
  'states verify all force' (no remaster). Sections:
    S1 stack   units, FIFO, leo shell process, logs (incl. #41 bot-error lines)
    S2 conf    playerbots.conf keys we depend on vs expected values
    S3 DB      accounts/gmlevel, census, online watched, account links, Leo group
    S4 states  ~/.botcmd-states.conf layout
    S5 live    states verify all force table (verdict vs conf)   [skip with --quick]
    S6 summary PASS/FAIL/INFO counts; exit 1 if any FAIL
  --bundle FILE  also append everything to FILE (paste it back as evidence)
EOF
        return 0 ;;
      *) errmsg "doctor: unknown arg '$1' (try --help)"; return 2 ;;
    esac
  done
  if [ -n "$bundle" ]; then
    exec > >(tee -a "$bundle") 2>&1
    echo "== doctor bundle -> $bundle =="
  fi
  hdr "botcmd doctor - $(date '+%a %Y-%m-%d %H:%M:%S')"
  _dinfo "code revision: $(git -C "$BOTCMD_REPO" rev-parse --short HEAD 2>/dev/null || echo unknown) ($BOTCMD_REPO)"
  _dinfo "host: $(hostname 2>/dev/null || echo ?)  user: $(id -un 2>/dev/null || echo ?)"
  _doctor_s1_stack
  _doctor_s2_pbconf
  _doctor_s3_db
  _doctor_s4_statesconf
  [ -z "$quick" ] && _doctor_s5_live
  hdr "S6 DOCTOR SUMMARY"
  printf 'PASS %d   FAIL %d   INFO %d\n' "$DOCTOR_PASS" "$DOCTOR_FAIL" "$DOCTOR_INFO"
  if [ "$DOCTOR_FAIL" -gt 0 ]; then
    _dfail "$DOCTOR_FAIL check(s) failed - see FAIL lines above"
  else
    _dpass "all checks passed"
  fi
  _dinfo "assumption register: docs/assumptions.md - every bot-behavior assumption + the exact command that confirms it + a result column to fill from runs like this one"
  _dinfo "next: fix FAILs, then botcmd states apply [all] + botcmd states verify (read-back), or botcmd states verify all remaster for silent bots"
  [ "$DOCTOR_FAIL" -gt 0 ] && return 1
  return 0
}
