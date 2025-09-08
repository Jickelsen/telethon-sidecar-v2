
# Telethon Sidecar (FastAPI) + n8n

A small Dockerized sidecar exposing a minimal HTTP API (with Bearer auth) for Telegram **MTProto** actions using **Telethon**:
- Resolve a phone number
- Send a message to a bot *as a user* and wait for the reply
- One-shot helper: send `{phone}` to a bot and collect the reply

Designed to be orchestrated by **n8n**, so non-coders can edit the workflow while this service keeps the Telegram specifics.

## Quick Start

### 1) Prepare `.env`
Copy `.env.example` to `.env` and fill your values:

```bash
cp .env.example .env
# edit .env
```

> You must use your **Telegram API ID / Hash** (https://my.telegram.org).
> The service uses a **user** session (not a bot token).

### 2) First-time session login
Authorize a persistent Telethon session once:

```bash
docker compose run --rm telethon-sidecar-init
```

Follow the prompts (phone number, code). This will create session files in `./data/session`.

### 3) Run the sidecar
```bash
docker compose up -d telethon-sidecar
curl -H "Authorization: Bearer $AUTH_TOKEN" http://localhost:8000/health
```

### 4) n8n integration
- Use an HTTP Request node with URL `http://telethon-sidecar:8000/search_phone_via_bot`
- Add header: `Authorization: Bearer <AUTH_TOKEN>`
- Body JSON (example):
```json
{
  "phone": "+15551234567",
  "bot_username": "@a_bot",
  "message_template": "{phone}",
  "wait_seconds": 12
}
```

## Endpoints

- `GET /health` → `{ "status": "ok" }`
- `POST /resolve_phone` → `{ id, username, first_name, last_name, phone }`
- `POST /bot/send` → `{ sent: true, reply?: string }`
- `POST /search_phone_via_bot` → `{ ok, query, reply?, error? }`

All `POST` endpoints require header: `Authorization: Bearer <AUTH_TOKEN>`.

## Project Layout

```
telethon-sidecar/
├─ app.py
├─ config.py
├─ docker-compose.yml
├─ Dockerfile
├─ requirements.txt
├─ scripts/
│  ├─ init_session.py
│  └─ init_session.sh
├─ .env.example
└─ README.md
```

## Security

- Simple Bearer token (`AUTH_TOKEN`) is required for mutating endpoints.
- Session files live under `./data/session` (mounted volume). Keep that directory private.

## License

MIT
