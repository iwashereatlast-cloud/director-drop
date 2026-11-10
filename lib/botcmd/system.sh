# System checks: worldserver, RO DB grants.
# The old console-credential gate (status/arm/console cmd) was removed in
# the end-of-life pass (docs/services-roadmap.md): no console credentials
# in the stack. Live reads route through chat-tunnel (.playerbots inspect
# via Leo's session) - no auth material to arm.

system_worldserver() {
  hdr "worldserver (system unit)"
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet ac-worldserver 2>/dev/null; then
    okmsg "active since: $(systemctl show ac-worldserver -p ActiveEnterTimestamp --value 2>/dev/null || echo '?')"
  else
    errmsg "ac-worldserver NOT active (or systemctl cannot see it)"
  fi
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
    cls
    hdr "System (worldserver / DB)"
    cat <<'EOF'
 1. worldserver status
 2. RO DB grants
 3. Return to main menu
EOF
    printf '%s' "choice: "; IFS= read -r c
    case "$c" in
      1) system_worldserver; pause ;;
      2) system_grants; pause ;;
      3) return 0 ;;
      ""|b|back) return 0 ;;
      *) warn "invalid choice" ;;
    esac
  done
}
