#!/usr/bin/env bash
# Idempotent install of daily Product Master cron on the Vultr host.
# Usage:
#   CRON_SECRET=... sudo -E bash scripts/install-vultr-cron.sh
#   # or:
#   sudo bash scripts/install-vultr-cron.sh '<secret>'

set -euo pipefail

SECRET="${1:-${CRON_SECRET:-}}"
APP_DIR="${APP_DIR:-/opt/jr_bot_backend}"
SCRIPT_SRC="${APP_DIR}/scripts/vultr-cron-sync-products.sh"
SCRIPT_DST="/usr/local/bin/vultr-cron-sync-products.sh"
ENV_DIR="/etc/jr-bot"
ENV_FILE="${ENV_DIR}/cron.env"
LOG_FILE="/var/log/jr-sync-products.log"
CRON_LINE="0 0 * * * ${SCRIPT_DST} >> ${LOG_FILE} 2>&1"

if [[ -z "$SECRET" ]]; then
  echo "CRON_SECRET is required (arg1 or env)" >&2
  exit 1
fi

if [[ ! -f "$SCRIPT_SRC" ]]; then
  echo "Missing sync script at ${SCRIPT_SRC}" >&2
  exit 1
fi

install -m 0755 "$SCRIPT_SRC" "$SCRIPT_DST"
mkdir -p "$ENV_DIR"
umask 077
printf 'CRON_SECRET=%s\nJR_BACKEND_URL=%s\n' \
  "$SECRET" \
  "${JR_BACKEND_URL:-https://api.vultr3.qlink.in}" > "$ENV_FILE"
chmod 600 "$ENV_FILE"
touch "$LOG_FILE"
chmod 644 "$LOG_FILE"

# Install/replace only our cron line (root crontab).
existing="$(crontab -l 2>/dev/null || true)"
filtered="$(printf '%s\n' "$existing" | grep -v 'vultr-cron-sync-products.sh' || true)"
printf '%s\n%s\n' "$filtered" "$CRON_LINE" | grep -v '^$' | crontab -

echo "Installed:"
echo "  script: ${SCRIPT_DST}"
echo "  secret: ${ENV_FILE} (mode 600)"
echo "  cron:   ${CRON_LINE}"
echo "Current crontab:"
crontab -l
