# Jaipur Rugs Bot Backend

FastAPI backend for the Jaipur Rugs chatbot — **one server** for Web (WebSocket) and WhatsApp (Gupshup), same AI core.

## Production Architecture

| Piece | Host |
| --- | --- |
| **Backend (web + WhatsApp + cron target)** | `https://api.vultr3.qlink.in` |
| Frontend / admin UI | `https://qlink-jr.vercel.app` (separate repo) |
| Product catalog | Daily sync from JR Product Master API → MongoDB `JR.products` |

Do **not** run a second WhatsApp-only backend on Vercel. Point Gupshup webhook and the frontend at Vultr only.

```text
WhatsApp  → Gupshup → https://api.vultr3.qlink.in/gupshup/message/hc
Web chat  → WS/HTTP → https://api.vultr3.qlink.in
Cron      → GET       https://api.vultr3.qlink.in/api/cron/sync-products
Frontend  → Vercel static → same Vultr API
```

| Channel | Entry point | Session collection |
| --- | --- | --- |
| Web (WebSocket) | `ws_routes.py` → `/ws/user/{session_id}/...` | `JR.users` |
| WhatsApp (Gupshup) | `whatsapp_routes.py` → `POST /gupshup/message/hc` | `JR.users_whatsapp` |

## Repositories

| Repo | Purpose |
| --- | --- |
| `JR_bot_backend` | This repo — unified backend, deploy to Vultr via GitHub Actions (`jr-production`) |
| `JR_frontend` | Web chatbot + admin dashboard (Vercel) |

## Important Routes

| Route | Purpose |
| --- | --- |
| `POST /gupshup/message/hc` | Gupshup WhatsApp webhook |
| `GET /api/conversations` | Dashboard WhatsApp conversation list |
| `GET /api/conversations/{phone}` | Dashboard message history |
| `POST /api/whatsapp/send` | Manual WhatsApp send from dashboard |
| `POST /api/conversations/{phone}/toggle-ai` | Toggle AI/human mode |
| `POST /api/conversations/{phone}/takeover` | Agent takeover |
| `GET /api/alerts/all` | Agent alerts |
| `GET/POST /api/prompt` | Shared system prompt |
| `POST /api/sync-products` | Manual Product Master sync |
| `GET /api/cron/sync-products` | Scheduled sync (`Authorization: Bearer $CRON_SECRET`) |
| `POST /api/backfill-search-tokens` | One-time search token backfill |
| `GET /ping` | Health (Mongo + R2) |

## Product Catalog Sync

Source of truth: JR **Product Master** API (not live search). Sync upserts into Mongo and builds `search_tokens` locally (`build_search_tokens`).

On the Vultr host, schedule daily:

```bash
# See scripts/vultr-cron-sync-products.sh
0 0 * * * CRON_SECRET=... /usr/local/bin/vultr-cron-sync-products.sh
```

### Diagnose bot vs website (local, ~1 min)

When the bot returns the “wrong” rugs vs jaipurrugs.com, don’t guess — run:

```bash
# Website shows a rug the bot missed — is it in Mongo / Product Master?
python scripts/diagnose_catalog.py --find "laal chattan"
python scripts/diagnose_catalog.py --find "PAE-5080-0001" --full-api

# Replay the user message (same extract → Mongo → relax path)
python scripts/diagnose_catalog.py --query "show me red rugs above 15000 usd and below 20000 usd round shape"

# Prove a known SKU should have matched
python scripts/diagnose_catalog.py --query "red round above 15000 usd" --expect-sku PAE-5080-0001
```

Verdicts:

| Tag | Meaning | What to tell the client |
| --- | --- | --- |
| `API_GAP` | Not in JR Product Master API | Website CMS ≠ API feed; JR must expose the SKU |
| `API_YES_MONGO_NO` | In API, missing in Mongo | We need a catalog sync |
| `LOGIC` | In Mongo, filters/ranking miss it | Our search bug (color/shape/medium/etc.) |
| `OK` | Expected SKU returned | Behavior is correct |

### Golden search suite (no Mongo / OpenAI)

```bash
python -m pytest tests/test_jr_search_golden_queries.py -q
```

Covers soft new arrival, aurelia+red, red+round+USD (PAE-5080-class ColorFamily), medium/5×8, show-more, Hindi color, bleed, and single honesty note.

## Local Development

```bash
cp example.env .env   # fill secrets — never commit .env
pip install -r requirements.txt
uvicorn qlink_chatbot.main:app --reload
python -m compileall qlink_chatbot
```

## Deployment (Vultr)

Push / merge to branch **`jr-production`** → GitHub Actions SSHs to the VPS, rebuilds Docker, restarts the container.

1. Merge into `jr-production` and push `origin`.
2. Confirm `GET https://api.vultr3.qlink.in/ping`.
3. Gupshup webhook must be `https://api.vultr3.qlink.in/gupshup/message/hc`.

Frontend deploy is separate (Vercel). Set:

```text
VITE_BACKEND_URL=https://api.vultr3.qlink.in
VITE_WS_BACKEND_URL=https://api.vultr3.qlink.in
```

## Safety Rules

- Do not commit `.env`.
- Do not force-push production branches.
- Do not dual-push to a separate WhatsApp Vercel backend for production.
- Keep `CRON_SECRET` set in production.
