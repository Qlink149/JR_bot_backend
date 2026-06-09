# Jaipur Rugs Bot Backend

FastAPI backend for the Jaipur Rugs chatbot — serves both the Web channel (WebSocket) and WhatsApp channel (Gupshup webhook) from a single Vultr server using the same AI core.

## Production Architecture

![Architecture Diagram](docs/architecture.png)

Both channels use the same `chat_agent.py` (GPT-4.1-mini) and the same tools. The only difference is output rendering:
- **Web** — returns raw markdown, browser renders it
- **WhatsApp** — markdown is converted to WhatsApp format (`*bold*`, `_italic_`) and product results are sent as interactive CTA cards (image + button) via Gupshup

**Server:** `api.vultr3.qlink.in` (FastAPI · Docker · Nginx)

| Channel | Entry point | Session collection |
| --- | --- | --- |
| Web (WebSocket) | `ws_routes.py` → `/ws/user/{session_id}/...` | `JR.users` |
| WhatsApp (Gupshup) | `whatsapp_routes.py` → `POST /gupshup/message/hc` | `JR.users_whatsapp` |

## Repositories

| Repo | Purpose |
| --- | --- |
| `JR_bot_backend` | This repo — unified backend for web + WhatsApp, deployed to Vultr via GitHub Actions |
| `JR_frontend` | Web chatbot and admin dashboard UI |

## Main Backend Responsibilities

- Receive WhatsApp messages from Gupshup.
- Send WhatsApp replies through Gupshup.
- Run the OpenAI chatbot agent.
- Search products and format product replies.
- Read/write user sessions in MongoDB.
- Read system prompts from MongoDB.
- Search and update the Pinecone knowledge base.
- Expose dashboard APIs for conversations, leads, alerts, products, prompts, and WhatsApp send.
- Support human agent handoff/takeover.

## Important Routes

| Route | Purpose |
| --- | --- |
| `POST /gupshup/message/hc` | Gupshup WhatsApp webhook |
| `GET /api/conversations` | Dashboard WhatsApp conversation list |
| `GET /api/conversations/{phone}` | Dashboard message history for one WhatsApp number |
| `POST /api/whatsapp/send` | Send manual WhatsApp message from dashboard |
| `POST /api/conversations/{phone}/toggle-ai` | Toggle AI/human mode |
| `POST /api/conversations/{phone}/takeover` | Put conversation in agent mode and notify customer |
| `GET /api/alerts/all` | Agent alert list |
| `DELETE /api/alerts/{id}` | Clear alert |
| `GET /api/products` | Dashboard product list/search |
| `GET/POST /api/prompt` | Dashboard prompt read/write |
| `GET /api/cron/sync-products` | Product sync cron endpoint |

## Agent Takeover Flow

When a dashboard user clicks `Take over` in the WhatsApp chat:

1. Frontend calls `POST /api/conversations/{phone}/takeover`.
2. Backend sets `is_ai` to `False` for that WhatsApp session.
3. Backend appends a handoff message to chat history.
4. Backend sends the customer this WhatsApp message:

```text
Thank you. Our rug specialist will assist you further over a call/message.
```

After this, incoming WhatsApp messages are stored but AI does not reply while `is_ai` is false.

## Prompts And Knowledge

The bot behavior is not only controlled by code.

| Data | Location |
| --- | --- |
| System prompts | MongoDB `JR.internals`, document with `category: "system_prompt"` |
| User sessions | MongoDB `JR.users` and `JR.users_whatsapp` |
| Agent alerts | MongoDB `JR.agent_alerts` |
| KB records | Pinecone namespace configured by `PINECONE_NAMESPACE` |
| Product cache | MongoDB product collections populated from Jaipur Rugs API |

If prompt text changes but no code changes, update MongoDB through the dashboard prompt tools or a controlled script.

## Local Development

Create `.env` from `example.env` and fill required values. Never commit real secrets.

Install dependencies:

```bash
pip install -r requirements.txt
```

Run locally:

```bash
uvicorn qlink_chatbot.main:app --reload
```

Compile check:

```bash
python -m compileall qlink_chatbot
```

## Deployment Notes

Push to `main` — GitHub Actions automatically SSHs into the Vultr server, rebuilds the Docker image, and restarts the container.

After backend changes:

1. Push to `main` (or merge a branch into `main`).
2. GitHub Actions deploys to `api.vultr3.qlink.in` automatically.
3. Confirm with `GET https://api.vultr3.qlink.in/ping`.

After frontend changes:

1. Push `JR_frontend/new-changes`.
2. Deploy the frontend Vercel project.
3. Confirm `https://qlink-jr.vercel.app` is ready.

## Safety Rules

- Do not commit `.env`.
- Do not paste or store production keys in README or code.
- Do not force-push production branches.
- Backend WhatsApp production changes should go through `whatsapp-integration-updates`.
- Keep frontend API config pointed at the production WhatsApp backend unless a migration is planned.
