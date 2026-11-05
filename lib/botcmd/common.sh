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
BOTCMD_SOAP_CONF="${BOTCMD_SOAP_CONF:-$HOME/.soap-director}"
BOTCMD_MYCNF="${BOTCMD_MYCNF:-$HOME/.my.cnf-director}"
BOTCMD_ACCOUNT_LIKE="${BOTCMD_ACCOUNT_LIKE:-i_was_here%}"
BOTCMD_WATCH_GUILD="${BOTCMD_WATCH_GUILD:-Special FXc}"
BOTCMD_STATES_CONF="${BOTCMD_STATES_CONF:-$HOME/.botcmd-states.conf}"

# G1 hard rule (mirrors DIRECTOR_HANDPLAY in director.py): these five are
# hand-play only - NEVER summoned, never commanded. bot remove stays
# allowed (remediation if one is wrongly online as a bot).
# Both Aeris/Aerith spellings are listed until verified in the DB:
#   SELECT name FROM acore_characters.characters WHERE name LIKE 'Aeri%';
BOTCMD_HANDPLAY="Cecil Rydia Shadow Vivi Aeris Aerith"
# Summon ritual (mirrors the watchdog AUTORITUAL): account I_WAS_HERE1ST is
# deliberately absent. Do not add it.
BOTCMD_RITUAL_ACCOUNTS="I_WAS_HERE2ND I_WAS_HERE3RD I_WAS_HERE4TH"
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
