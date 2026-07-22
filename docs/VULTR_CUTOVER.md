# Vultr-only cutover checklist

Single production backend: `https://api.vultr3.qlink.in`

## 1. Gupshup webhook (Partner Subscription API)

Target URL:

```text
https://api.vultr3.qlink.in/gupshup/message/hc
```

From `JR_bot_backend` (uses `QLINK_GUPSHUP_APP_ID` + `QLINK_GUPSHUP_PARTNER_APP_TOKEN` from `.env`):

```bash
# PowerShell
$env:WEBHOOK_URL="https://api.vultr3.qlink.in/gupshup/message/hc"
python set_gupshup_webhook.py
```

Do **not** use `jaipurrugs-whatsapp-backend.vercel.app` for production.

## 2. Frontend env (Vercel project `qlink-jr`)

```text
VITE_BACKEND_URL=https://api.vultr3.qlink.in
VITE_WS_BACKEND_URL=https://api.vultr3.qlink.in
```

## 3. Daily product sync on Vultr host

**Preferred:** add GitHub Actions secret `CRON_SECRET`, then deploy `jr-production`.
Deploy SSHs to the VPS, injects `CRON_SECRET` into the Docker container, and runs
`scripts/install-vultr-cron.sh` (script + `/etc/jr-bot/cron.env` + midnight crontab).

**Manual (if you have SSH):**

```bash
# on Vultr host
cd /opt/jr_bot_backend
sudo CRON_SECRET='your-secret' bash scripts/install-vultr-cron.sh

# smoke test
sudo /usr/local/bin/vultr-cron-sync-products.sh
```

Backend container and `/etc/jr-bot/cron.env` must share the same `CRON_SECRET`.

## 4. Deploy backend

Push to branch `jr-production` → GitHub Actions → Docker on Vultr.

Confirm: `GET https://api.vultr3.qlink.in/ping`

## 5. One-time token rebuild (after tokenizer changes)

From a machine with `MONGO_URI`:

```bash
python scripts/backfill_search_tokens.py --rebuild-all --batch-size 2000
```

## 6. Stop dual-backend sync

- Do not run `sync.bat` (removed).
- Do not push production fixes to the separate `whatsapp` / Vercel backend remote.
