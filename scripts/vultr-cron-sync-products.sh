#!/usr/bin/env bash
# Daily Product Master sync on the single Vultr backend.
# Install (on VPS):
#   sudo bash scripts/install-vultr-cron.sh
# Or via GitHub Actions deploy (sets /etc/jr-bot/cron.env + crontab).
#
# Requires CRON_SECRET (env, or /etc/jr-bot/cron.env) matching the backend container.

set -euo pipefail

if [[ -f /etc/jr-bot/cron.env ]]; then
  # shellcheck disable=SC1091
  set -a
  source /etc/jr-bot/cron.env
  set +a
fi

BASE_URL="${JR_BACKEND_URL:-https://api.vultr3.qlink.in}"
SECRET="${CRON_SECRET:-}"

if [[ -z "$SECRET" ]]; then
  echo "CRON_SECRET is required" >&2
  exit 1
fi

curl -fsS -X GET \
  -H "Authorization: Bearer ${SECRET}" \
  "${BASE_URL}/api/cron/sync-products"
echo
