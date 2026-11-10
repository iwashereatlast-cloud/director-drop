# botcmd common: paths, palette, helpers.
# Sourced by bin/botcmd - not runnable on its own.

[ -n "${BOTCMD_COMMON:-}" ] && return 0
BOTCMD_COMMON=1

# ---- paths & knobs (override any of these in ~/.botcmd.conf) ----
BOTCMD_REPO="${BOTCMD_REPO:-$HOME/downloads/director-drop}"
BOTCMD_FIFO="${BOTCMD_FIFO:-/tmp/leo.in}"
BOTCMD_SHELL_LOG="${BOTCMD_SHELL_LOG:-/tmp/leo.log}"
BOTCMD_DIRECTOR_LOG="$BOTCMD_REPO/daemon.log"
BOTCMD_WATCHDOG_CONF="${BOTCMD_WATCHDOG_CONF:-$HOME/.leo-watchdog.conf}"
BOTCMD_MYCNF="${BOTCMD_MYCNF:-$HOME/.my.cnf-director}"
BOTCMD_ACCOUNT_LIKE="${BOTCMD_ACCOUNT_LIKE:-i_was_here%}"
BOTCMD_MASTER_ACCOUNT="${BOTCMD_MASTER_ACCOUNT:-I_WAS_HERE1ST}"
# ^ the account currently treated as the HUMAN/master account (rotated via
#   'botcmd master'). Mirror of DIRECTOR_MASTER_ACCOUNT in ~/.director.env.
BOTCMD_BOT_TOONS="${BOTCMD_BOT_TOONS:-}"
# ^ space-separated toon names that are BOTS even if they sit on the selected
#   master account - excluded from the auto human list when rotating (e.g. "Golbez")
BOTCMD_WATCH_GUILD="${BOTCMD_WATCH_GUILD:-Special FXc}"
BOTCMD_SHELL_ACCOUNT="${BOTCMD_SHELL_ACCOUNT:-i_was_cmd}"
# ^ Leo's headless-shell account - its toons (Leo) are excluded from bot pickers.
BOTCMD_STATES_CONF="${BOTCMD_STATES_CONF:-$HOME/.botcmd-states.conf}"
# Post-ritual auto-group clear: on EVERY bot login the mod pulls the bot into
# Leo's group (creating it if Leo has none) and converts to raid at >=5
# members - PlayerbotMgr.cpp:591-619 HandlePlayerBotLoginCallback -> queued
# GroupInviteOperation (PlayerbotOperations.h:37-90; SummonWhenGroup=1,
# playerbots.conf.dist:193). No conf key disables that auto-join, so
# roster_ritual waits for the login wave to settle, then /w each non-G1
# member 'leave' (leaving keeps bot AI). 0 = leave the auto-formed group alone.
BOTCMD_RITUAL_AUTOCLEAR="${BOTCMD_RITUAL_AUTOCLEAR:-1}"
# seconds roster_ritual waits for the ritual logins to settle before clearing
BOTCMD_RITUAL_SETTLE="${BOTCMD_RITUAL_SETTLE:-60}"

# G1 hard rule (mirrors DIRECTOR_HANDPLAY in director.py): these five are
# hand-play only - NEVER summoned, never commanded. bot remove stays
# allowed (remediation if one is wrongly online as a bot).
# Both Aeris/Aerith spellings are listed until verified in the DB:
#   SELECT name FROM acore_characters.characters WHERE name LIKE 'Aeri%';
BOTCMD_HANDPLAY="Cecil Rydia Shadow Vivi Aeris Aerith"
# Summon ritual (mirrors the watchdog AUTORITUAL): account I_WAS_HERE1ST is
# deliberately absent. Do not add it.
BOTCMD_RITUAL_ACCOUNTS="I_WAS_HERE2ND I_WAS_HERE3RD I_WAS_HERE4TH I_WAS_HERE5TH"
# single extra bots for the ritual - ONLY needed for bots on an account NOT in
# BOTCMD_RITUAL_ACCOUNTS (e.g. a bot on the 1ST/master account, which is never
# addaccount'd). roster_ritual skips any bot already covered by addaccount.
BOTCMD_RITUAL_BOTS="Golbez"

[ -r "$HOME/.botcmd.conf" ] && . "$HOME/.botcmd.conf"

# ---- palette (plain when not a tty, e.g. piped/scripted) ----
if [ -t 1 ]; then
  C_B=$'\e[1m'; C_C=$'\e[36m'; C_G=$'\e[32m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_N=$'\e[0m'
else
  C_B=; C_C=; C_G=; C_Y=; C_R=; C_N=
fi

hdr()    { printf '%s\n' "${C_C}${C_B}== $* ==${C_N}"; }
okmsg()  { printf '%s\n' "${C_G}$*${C_N}"; }
warn()   { printf '%s\n' "${C_Y}$*${C_N}"; }
errmsg() { printf '%s\n' "${C_R}$*${C_N}"; }
pause()  { printf '%s' "${C_N}press Enter... "; IFS= read -r _unused; }

# ---- menu screen redraw ----
# Plain 'clear || :' silently no-ops when TERM is unset/dumb or terminfo is
# missing (common with Windows ssh clients) - menus then SCROLL instead of
# refreshing. cls prefers terminfo 'clear' on a real tty, falls back to raw
# ANSI (xterm & co), and emits NOTHING when stdout is piped (keeps captured
# output clean). BOTCMD_NO_REFRESH=1 forces scroll/append mode on purpose.
cls() {
  [ "${BOTCMD_NO_REFRESH:-0}" = "1" ] && return 0
  [ -t 1 ] || return 0
  if [ -n "${TERM:-}" ] && [ "$TERM" != "dumb" ] && command -v clear >/dev/null 2>&1 && clear >/dev/null 2>&1; then
    return 0
  fi
  printf '\033[H\033[2J\033[3J'
}

# ---- helpers ----

# send one chat/console line to Leo's FIFO. Recreates the FIFO after a
# reboot (tmpfs wipe). The 3s timeout mirrors director.py send_line: a
# FIFO with no reader would otherwise block the menu forever.
game_send() {
  local line="$1"
  if [ ! -p "$BOTCMD_FIFO" ]; then
    warn "FIFO $BOTCMD_FIFO missing - creating (reboot wipes /tmp)"
    mkfifo "$BOTCMD_FIFO" || { errmsg "cannot create $BOTCMD_FIFO"; return 1; }
  fi
  timeout 3 bash -c 'printf "%s\n" "$1" > "$2"' _ "$line" "$BOTCMD_FIFO" || {
    errmsg "no reader on the FIFO - is the Leo shell up? (botcmd leo status)"
    return 1
  }
}

# read-only MySQL via the RO identity. NEVER point this at a write-capable
# defaults file - the whole tool is human-driven, but the rule is structural.
db_ro() {
  command -v mysql >/dev/null 2>&1 || {
    errmsg "mysql not found on PATH"
    return 1
  }
  env -u MYSQL_PWD mysql --defaults-file="$BOTCMD_MYCNF" "$@" || {
    errmsg "mysql failed - check $BOTCMD_MYCNF and the SELECT grants (botcmd system grants)"
    return 1
  }
}

# case-insensitive membership in the G1 hand-play list
is_handplay() {
  local n lc
  lc=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
  for n in $BOTCMD_HANDPLAY; do
    [ "$lc" = "$(printf '%s' "$n" | tr '[:upper:]' '[:lower:]')" ] && return 0
  done
  return 1
}

user_unit_state() {  # $1 = unit name; prints active/inactive/unknown
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user is-active "$1" 2>/dev/null || true
  else
    echo unknown
  fi
}

# generic "key<sep>value" ini setter used by master.sh (_conf_set) and
# states.sh (_states_conf_set): updates in place or appends; empty value
# removes the key. awk-based so values may contain sed-special chars.
_ini_set() {  # $1=file $2=key $3=value (empty removes) $4=separator (default "=")
  local f="$1" k="$2" v="$3" sep="${4:-=}" tmp
  touch "$f" 2>/dev/null || return 1
  tmp=$(mktemp) || return 1
  awk -v k="$k" -v v="$v" -v sep="$sep" '
    BEGIN { pat = "^" k "[[:space:]]*" sep; line = (sep == "=" ? k sep v : k sep " " v) }
    $0 ~ pat { if (v != "") print line; found = 1; next }
    { print }
    END { if (!found && v != "") print line }
  ' "$f" > "$tmp" && cat "$tmp" > "$f"
  local rc=$?
  rm -f "$tmp"
  return $rc
}
