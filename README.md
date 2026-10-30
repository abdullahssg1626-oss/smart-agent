# WhatsApp Business Agent

Django receives signed OpenWA webhooks, generates replies with Groq, and sends
them through the OpenWA REST API. The Django webhook endpoint is
`/api/webhook/`.

## Configure Django

Copy `.env.example` to `.env` and configure:

- `DJANGO_SECRET_KEY` and `GROQ_API_KEY`.
- `OPENWA_API_URL`, normally `http://127.0.0.1:2785/api` for local Docker.
- `OPENWA_API_KEY` using an OpenWA API key with permission to send messages.
- `OPENWA_SESSION_ID` for the WhatsApp session to use.
- `OPENWA_WEBHOOK_SECRET` with a random value at least 16 characters long.
- `DATABASE_URL` for persistent PostgreSQL in production. A blank value uses
  SQLite locally.
- `DJANGO_ALLOWED_HOSTS`, including `.ngrok-free.dev` when using an ngrok
  domain.

## Run locally

1. Start OpenWA from its included Docker project:

   ```powershell
   cd .\baileys_bridge\OpenWA
   docker compose -f docker-compose.dev.yml up -d
   ```

   OpenWA's API and dashboard are available at `http://localhost:2785`. Create
   and start a WhatsApp session, then set the API key and session ID in the
   Django `.env`.

2. Run Django from the project root:

   ```powershell
   .\venv\Scripts\python.exe manage.py migrate
   .\venv\Scripts\python.exe manage.py runserver
   ```

3. Configure an OpenWA webhook for the session. Subscribe to
   `message.received`, set its secret to the same `OPENWA_WEBHOOK_SECRET`, and
   point its URL to `https://<your-ngrok-domain>.ngrok-free.dev/api/webhook/`.
   The ngrok domain must be included in `DJANGO_ALLOWED_HOSTS`.

OpenWA signs each webhook's raw JSON body with HMAC-SHA256. Django verifies the
`X-OpenWA-Signature` header before parsing or processing the event. Replies use
OpenWA's `/sessions/{sessionId}/messages/send-text` endpoint and `X-API-Key`.

## Deployment

The Render blueprint provisions Django and PostgreSQL only. OpenWA must run
separately in Docker or another persistent environment. Configure the Render
service's `OPENWA_API_URL`, `OPENWA_API_KEY`, `OPENWA_SESSION_ID`, and
`OPENWA_WEBHOOK_SECRET` to reach that OpenWA instance, and configure its
webhook URL to the Django deployment's `/api/webhook/` endpoint. Use HTTPS for
public webhook URLs and keep API keys and webhook secrets private.

Conversation history stores the last 20 completed message/reply pairs for
Groq context. This is contextual memory, not model training.
