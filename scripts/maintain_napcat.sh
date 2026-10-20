#!/usr/bin/env bash
set -euo pipefail

mode="${1:-check}"
napcat_service="${NAPCAT_SERVICE:-napcat.service}"
qqbot_service="${QQBOT_SERVICE:-qqbot.service}"
napcat_port="${NAPCAT_PORT:-3002}"
wait_seconds="${NAPCAT_WAIT_SECONDS:-90}"
client_limit_error="Maximum number of clients reached"

log() {
  printf '[napcat-maintenance] %s\n' "$*"
}

wait_for_napcat() {
  local attempt
  for ((attempt = 1; attempt <= wait_seconds; attempt++)); do
    if systemctl is-active --quiet "$napcat_service" \
      && [[ -n "$(ss -H -ltn "sport = :${napcat_port}")" ]]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_qqbot() {
  local attempt invocation_id
  invocation_id="$(systemctl show "$qqbot_service" -p InvocationID --value)"
  for ((attempt = 1; attempt <= wait_seconds; attempt++)); do
    if systemctl is-active --quiet "$qqbot_service" \
      && journalctl _SYSTEMD_INVOCATION_ID="$invocation_id" --no-pager \
        | grep -F 'OneBot V11 | Bot ' \
        | grep -F ' connected' >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

restart_stack() {
  local reason="$1"
  log "restarting NapCat and QQ bot: ${reason}"
  systemctl stop "$napcat_service"
  systemctl start "$napcat_service"
  if ! wait_for_napcat; then
    log "NapCat did not open port ${napcat_port} within ${wait_seconds}s"
    return 1
  fi

  systemctl restart "$qqbot_service"
  if ! wait_for_qqbot; then
    log "QQ bot did not reconnect within ${wait_seconds}s"
    return 1
  fi
  log "recovery completed"
}

has_client_limit_error() {
  local invocation_id
  invocation_id="$(systemctl show "$napcat_service" -p InvocationID --value)"
  [[ -n "$invocation_id" ]] || return 1
  journalctl _SYSTEMD_INVOCATION_ID="$invocation_id" --since '-10 minutes' --no-pager \
    | grep -F "$client_limit_error" >/dev/null
}

case "$mode" in
  check)
    if ! systemctl is-active --quiet "$napcat_service"; then
      restart_stack "NapCat is inactive"
    elif has_client_limit_error; then
      restart_stack "Xvfb client limit reached"
    fi
    ;;
  restart)
    restart_stack "scheduled preventive maintenance"
    ;;
  *)
    printf 'Usage: %s [check|restart]\n' "$0" >&2
    exit 2
    ;;
esac
