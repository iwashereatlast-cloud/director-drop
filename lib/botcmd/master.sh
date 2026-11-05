# Master account selector: which i_was_here* account is the HUMAN account.
#
# director.py reads DIRECTOR_MASTER_ACCOUNT + DIRECTOR_HUMANS from
# ~/.director.env via the leo-director.service systemd drop-in
# (leo/leo-director.service.d/override.conf). Rotating here rewrites that env
# file, mirrors the values into ~/.botcmd.conf, and restarts leo-director.
#
# Invariant: the selected master account must be HUMAN-ONLY. Every other
# i_was_here% account is a bot account - its toons are never authed and are
# dropped by the daemon's DB sender gate. Toons named in BOTCMD_BOT_TOONS are
# excluded from the auto human list even if they sit on the selected account.

DIRECTOR_ENV="${DIRECTOR_ENV:-$HOME/.director.env}"

_master_accounts() {  # one i_was_here* account per line, from acore_auth
  db_ro -N -B -e "SELECT username FROM acore_auth.account WHERE username LIKE '$BOTCMD_ACCOUNT_LIKE' ORDER BY username" 2>/dev/null
}

_master_current() {  # effective master account: env file wins, then conf
  local a=""
  if [ -r "$DIRECTOR_ENV" ]; then
    a=$(sed -n 's/^DIRECTOR_MASTER_ACCOUNT=//p' "$DIRECTOR_ENV" | tail -n 1 | tr -d '"' | xargs)
  fi
  [ -n "$a" ] || a="$BOTCMD_MASTER_ACCOUNT"
  printf '%s' "$a"
}

_master_toons() {  # $1=account -> "Name Name* ..." (* = online)
  db_ro -N -B -e "SELECT c.name, c.online FROM acore_characters.characters c JOIN acore_auth.account a ON a.id=c.account WHERE a.username='$1' ORDER BY c.name" 2>/dev/null \
    | awk -F'\t' '{printf "%s%s ", $1, ($2 == "1" ? "*" : "")} END {print ""}'
}

_conf_set() {  # $1=key $2=value -> update-or-append KEY="value" in ~/.botcmd.conf
  local f="$HOME/.botcmd.conf" k="$1" v="$2" tmp
  touch "$f"
  tmp=$(mktemp) || return 1
  if grep -q "^${k}=" "$f"; then
    sed "s|^${k}=.*|${k}=\"${v}\"|" "$f" > "$tmp"
  else
    { cat "$f"; echo "${k}=\"${v}\""; } > "$tmp"
  fi
  cat "$tmp" > "$f" && rm -f "$tmp"
}

master_show() {
  local cur acct i=0 mark
  cur=$(_master_current)
  hdr "Master account (the HUMAN account Leo listens to)"
  printf 'current: %s   (env file: %s)\n\n' "$cur" "$DIRECTOR_ENV"
  while IFS= read -r acct; do
    i=$((i + 1))
    mark="[ ]"; [ "${acct,,}" = "${cur,,}" ] && mark="[X]"
    printf '%2d. %s %-16s toons: %s\n' "$i" "$mark" "$acct" "$(_master_toons "$acct")"
  done < <(_master_accounts)
  echo
  warn "rule: select only a HUMAN-ONLY account. All other i_was_here% accounts are"
  warn "bot accounts - their toons are never authed (daemon DB sender gate drops them)."
  warn "BOTCMD_BOT_TOONS in ~/.botcmd.conf excludes known bots from the auto human list."
}

master_set() {  # $1 = account name (case-insensitive) or menu number
  local want="${1:-}" acct="" humans tmp
  [ -n "$want" ] || { errmsg "usage: botcmd master set I_WAS_HERE5TH   (or a menu number)"; return 1; }
  if [ "$want" -eq "$want" ] 2>/dev/null; then
    acct=$(_master_accounts | sed -n "${want}p")
  else
    while IFS= read -r a; do
      if [ "${a,,}" = "${want,,}" ]; then acct="$a"; break; fi
    done < <(_master_accounts)
  fi
  [ -n "$acct" ] || { errmsg "no account matching '$want' (have: $(_master_accounts | paste -sd' ' -))"; return 1; }
  # human list = toons on the account, minus BOTCMD_BOT_TOONS (case-insensitive)
  humans=$(db_ro -N -B -e "SELECT c.name FROM acore_characters.characters c JOIN acore_auth.account a ON a.id=c.account WHERE a.username='$acct' ORDER BY c.name" 2>/dev/null \
           | awk -v excl="$BOTCMD_BOT_TOONS" 'BEGIN{n=split(excl,a," "); for(i=1;i<=n;i++) e[tolower(a[i])]=1} !e[tolower($0)]' \
           | paste -sd, -)
  [ -n "$humans" ] || { errmsg "account $acct has no usable toons (none, or all in BOTCMD_BOT_TOONS) - refusing: would auth nobody"; return 1; }
  tmp=$(mktemp) || return 1
  (umask 077 && cat > "$tmp" <<EOF
# managed by: botcmd master set $acct - do not edit by hand
DIRECTOR_MASTER_ACCOUNT=$acct
DIRECTOR_HUMANS=$humans
DIRECTOR_HANDPLAY=$humans
EOF
  )
  (umask 077 && cat "$tmp" > "$DIRECTOR_ENV") && rm -f "$tmp"
  _conf_set BOTCMD_MASTER_ACCOUNT "$acct"
  _conf_set BOTCMD_HANDPLAY "$(printf '%s' "$humans" | tr ',' ' ')"
  okmsg "master account -> $acct    humans/hand-play -> $humans"
  if [ -d "$HOME/.config/systemd/user/leo-director.service.d" ]; then
    systemctl --user daemon-reload && systemctl --user restart leo-director \
      && okmsg "leo-director restarted (reads $DIRECTOR_ENV via drop-in)"
  else
    warn "systemd drop-in NOT installed - the daemon will not pick this up until you run:"
    warn "  mkdir -p ~/.config/systemd/user/leo-director.service.d"
    warn "  cp $BOTCMD_REPO/leo/leo-director.service.d/override.conf ~/.config/systemd/user/leo-director.service.d/"
    warn "  systemctl --user daemon-reload && systemctl --user restart leo-director"
  fi
  sleep 1
  tail -n 4 "$BOTCMD_DIRECTOR_LOG" 2>/dev/null || true
}

master_menu() {
  local c
  while :; do
    master_show
    printf '%s' "select account number/name to make master (0=back): "
    IFS= read -r c
    [ "$c" = "0" ] && return 0
    [ -n "$c" ] && master_set "$c"
  done
}
