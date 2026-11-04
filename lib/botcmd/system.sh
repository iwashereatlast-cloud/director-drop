# System checks: worldserver, SOAP gate, RO DB grants.
# All read-only. The SOAP envelope mirrors director.py soap_command exactly.

system_worldserver() {
  hdr "worldserver (system unit)"
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet ac-worldserver 2>/dev/null; then
    okmsg "active since: $(systemctl show ac-worldserver -p ActiveEnterTimestamp --value 2>/dev/null || echo '?')"
  else
    errmsg "ac-worldserver NOT active (or systemctl cannot see it)"
  fi
}

system_soap_status() {
  hdr "SOAP gate (bot-sender filter + live .pinfo)"
  local url user pass
  if [ -s "$BOTCMD_SOAP_CONF" ] && [ "$(wc -l < "$BOTCMD_SOAP_CONF")" -ge 3 ]; then
    url=$(sed -n '1p' "$BOTCMD_SOAP_CONF")
    user=$(sed -n '2p' "$BOTCMD_SOAP_CONF")
    pass=$(sed -n '3p' "$BOTCMD_SOAP_CONF")
    okmsg "armed: $url (user $user, perms $(stat -c '%a' "$BOTCMD_SOAP_CONF"))"
    echo
    warn "live test: .server info via SOAP..."
    local envelope body text
    envelope='<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns1="urn:AC"><SOAP-ENV:Body><ns1:executeCommand><command>.server info</command></ns1:executeCommand></SOAP-ENV:Body></SOAP-ENV:Envelope>'
    body=$(curl -s --max-time 10 -u "$user:$pass" \
      -H 'Content-Type: text/xml; charset=utf-8' -H 'SOAPAction: urn:AC' \
      -d "$envelope" "$url" 2>/dev/null || true)
    if [ -n "$body" ]; then
      text=$(printf '%s' "$body" | sed -e 's/<[^>]*>/ /g' | tr -s ' ' | head -c 300)
      if [ -n "$(printf '%s' "$text" | tr -d ' ')" ]; then
        okmsg "SOAP works - worldserver answered:"
        echo "$text"
      else
        errmsg "empty SOAP response - check worldserver SOAP bind and the account's gmlevel"
      fi
    else
      errmsg "no response - is worldserver up and listening on $url?"
    fi
  else
    warn "NOT armed: $BOTCMD_SOAP_CONF missing or short"
    errmsg "the director's bot-sender gate is INERT (fail-open) without it"
    echo "arm it from the menu (System -> arm SOAP) or: botcmd system arm"
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
  printf '%s' "game account (gmlevel >= Administrator, e.g. I_WAS_BANK): "; IFS= read -r user
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
  local c
  while :; do
    clear 2>/dev/null || :
    hdr "System (worldserver / SOAP / DB)"
    cat <<'EOF'
 1. worldserver status
 2. SOAP gate: status + live test
 3. Arm SOAP gate
 4. RO DB grants
 5. Return to main menu
EOF
    printf '%s' "choice: "; IFS= read -r c
    case "$c" in
      1) system_worldserver; pause ;;
      2) system_soap_status; pause ;;
      3) system_soap_arm; pause ;;
      4) system_grants; pause ;;
      5) return 0 ;;
      *) warn "invalid choice" ;;
    esac
  done
}
