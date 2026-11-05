# System checks: worldserver, SOAP gate, RO DB grants, remote console.
# The SOAP envelope mirrors director.py soap_command exactly.

# run one console command via SOAP; prints "code" line then stripped text.
# fails (returns 1, prints nothing) when the gate is not armed.
_soap_run() {  # $1 = console command
  local url user pass cmdxml envelope resp code body
  [ -s "$BOTCMD_SOAP_CONF" ] && [ "$(wc -l < "$BOTCMD_SOAP_CONF")" -ge 3 ] || return 1
  url=$(sed -n '1p' "$BOTCMD_SOAP_CONF")
  user=$(sed -n '2p' "$BOTCMD_SOAP_CONF")
  pass=$(sed -n '3p' "$BOTCMD_SOAP_CONF")
  cmdxml=$(printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g')
  envelope='<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns1="urn:AC"><SOAP-ENV:Body><ns1:executeCommand><command>'"$cmdxml"'</command></ns1:executeCommand></SOAP-ENV:Body></SOAP-ENV:Envelope>'
  resp=$(curl -s --max-time 10 -u "$user:$pass" \
    -H 'Content-Type: text/xml; charset=utf-8' -H 'SOAPAction: urn:AC' \
    -d "$envelope" -w $'\n%{http_code}' "$url" 2>/dev/null) || { echo "000"; return 0; }
  code=$(printf '%s' "$resp" | tail -n 1)
  body=$(printf '%s' "$resp" | sed '$d')
  printf '%s\n' "$code"
  printf '%s' "$body" | sed -e 's/<[^>]*>/ /g' | tr -s ' ' | sed 's/^ *//'
}

system_worldserver() {
  hdr "worldserver (system unit)"
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet ac-worldserver 2>/dev/null; then
    okmsg "active since: $(systemctl show ac-worldserver -p ActiveEnterTimestamp --value 2>/dev/null || echo '?')"
  else
    errmsg "ac-worldserver NOT active (or systemctl cannot see it)"
  fi
}

system_soap_status() {
  hdr "SOAP gate (bot-sender filter + live .pinfo + remote console)"
  local out code text
  if out=$(_soap_run ".server info"); then
    code=$(printf '%s\n' "$out" | head -n 1)
    text=$(printf '%s\n' "$out" | tail -n +2 | head -c 300)
    case "$code" in
      200)
        if [ -n "$(printf '%s' "$text" | tr -d ' ')" ]; then
          okmsg "SOAP works (HTTP 200) - worldserver answered:"
          echo "$text"
        else
          errmsg "HTTP 200 but empty response - odd; paste this to the chat"
        fi ;;
      401)
        errmsg "HTTP 401 Unauthorized: endpoint is UP but the credentials were REJECTED"
        warn "  AC SOAP auth = real game account: username not found or password mismatch"
        warn "  re-enter carefully: botcmd system arm" ;;
      403)
        errmsg "HTTP 403: account authenticated but gmlevel below Administrator"
        warn "  needs gmlevel 3 on the account (acore_auth.account_access)" ;;
      000|"")
        errmsg "no response (connection failed/timeout) - is worldserver up and listening?" ;;
      *)
        errmsg "HTTP $code - unexpected response:"
        echo "$text" ;;
    esac
  else
    warn "NOT armed: $BOTCMD_SOAP_CONF missing or short"
    errmsg "the director's bot-sender gate is INERT (fail-open) without it"
    echo "arm it from the menu (System -> arm SOAP) or: botcmd system arm"
  fi
}

# arbitrary worldserver console command via the SOAP gate
system_cmd() {  # $1 = console command
  local out code
  hdr "SOAP console: $1"
  if out=$(_soap_run "$1"); then
    code=$(printf '%s\n' "$out" | head -n 1)
    case "$code" in
      200) okmsg "HTTP 200:"; printf '%s\n' "$out" | tail -n +2 ;;
      401) errmsg "401 - credentials rejected (re-arm: botcmd system arm)" ;;
      403) errmsg "403 - gmlevel below Administrator" ;;
      *)   errmsg "HTTP $code:"; printf '%s\n' "$out" | tail -n +2 ;;
    esac
  else
    errmsg "SOAP not armed (botcmd system arm)"
  fi
}

system_soap_arm() {
  hdr "Arm SOAP gate (writes $BOTCMD_SOAP_CONF, perms 600)"
  local url user pass old_umask
  if [ -s "$BOTCMD_SOAP_CONF" ]; then
    warn "a config already exists - this OVERWRITES it"
  fi
  printf '%s' "SOAP URL [http://127.0.0.1:7878/]: "; IFS= read -r url
  url="${url:-http://127.0.0.1:7878/}"
  printf '%s' "game account (gmlevel >= Administrator, e.g. i_was_cmd): "; IFS= read -r user
  if [ -z "$user" ]; then errmsg "no account given - aborted"; return 1; fi
  printf '%s' "password (input hidden): "; IFS= read -rs pass; echo
  if [ -z "$pass" ]; then errmsg "no password given - aborted"; return 1; fi
  old_umask=$(umask); umask 177
  printf '%s\n' "$url" "$user" "$pass" > "$BOTCMD_SOAP_CONF" || {
    umask "$old_umask"; errmsg "write failed"; return 1; }
  umask "$old_umask"
  chmod 600 "$BOTCMD_SOAP_CONF"
  okmsg "written: $BOTCMD_SOAP_CONF"
  echo
  system_soap_status
}

system_grants() {
  hdr "RO identity grants (expect SELECT-only lines, nothing else)"
  db_ro -e "SHOW GRANTS" || return 1
  echo
  warn "still pending: GRANT SELECT ON acore_world.quest_template + item_template"
  warn "  (Leo's quest/bags queries fail audibly without them)"
}

system_menu() {
  local c cmd
  while :; do
    clear 2>/dev/null || :
    hdr "System (worldserver / SOAP / DB / console)"
    cat <<'EOF'
 1. worldserver status
 2. SOAP gate: status + live test
 3. Arm SOAP gate
 4. RO DB grants
 5. Run a console command via SOAP
 6. Return to main menu
EOF
    printf '%s' "choice: "; IFS= read -r c
    case "$c" in
      1) system_worldserver; pause ;;
      2) system_soap_status; pause ;;
      3) system_soap_arm; pause ;;
      4) system_grants; pause ;;
      5) printf '%s' "console command: "; IFS= read -r cmd
         [ -n "$cmd" ] && system_cmd "$cmd"; pause ;;
      6) return 0 ;;
      *) warn "invalid choice" ;;
    esac
  done
}
