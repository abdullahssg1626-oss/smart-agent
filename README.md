# WhatsApp Business Agent

Django receives signed OpenWA webhooks, generates replies with Groq, and sends
them through the OpenWA REST API. The Django webhook endpoint is
`/api/webhook/`.

## Configure Django

Copy `.env.example` to `.env` and configure:

- `DJANGO_SECRET_KEY` and `GROQ_API_KEY`.
- `GROQ_MODEL` is optional; it defaults to `openai/gpt-oss-20b`. Set it to
  another currently supported Groq model ID to override the default.
- `OPENWA_API_URL`, normally `http://127.0.0.1:2785/api` for local Docker.
- `OPENWA_API_KEY` using an OpenWA API key with permission to send messages.
- `OPENWA_SESSION_ID` for the WhatsApp session to use.
- `OPENWA_WEBHOOK_SECRET` with a random value at least 16 characters long.
  Production webhook requests are rejected if this secret is missing or too
  short; local `DEBUG=True` requests do not require signatures.
- `DATABASE_URL` for persistent PostgreSQL in production. A blank value uses
  SQLite locally.
- `DJANGO_ALLOWED_HOSTS`, as comma-separated hostnames (schemes and root paths
  are also accepted). Localtunnel (`.loca.lt`) and ngrok (`.ngrok-free.app`
  and `.ngrok-free.dev`) tunnel hostnames are allowed automatically, including
  their dynamic subdomains; other custom domains can be added here.

Vercel (`VERCEL_URL`, `VERCEL_PROJECT_PRODUCTION_URL`) and Render
(`RENDER_EXTERNAL_HOSTNAME`) deployment hostnames are added automatically.

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
   point its URL to the current tunnel URL, for example
   `https://<your-tunnel>.loca.lt/api/webhook/` or
   `https://<your-tunnel>.ngrok-free.app/api/webhook/`. Tunnel subdomains are
   allowed automatically, so they do not need to be updated in `.env` when
   the tunnel domain changes.

OpenWA signs each webhook's raw JSON body with HMAC-SHA256. Django verifies the
`X-OpenWA-Signature` header before parsing or processing the event. Replies use
OpenWA's `/sessions/{sessionId}/messages/send-text` endpoint and `X-API-Key`.

## Deployment

The Render blueprint provisions Django and PostgreSQL only. OpenWA must run
separately in Docker or another persistent environment. Configure the Render
service's `OPENWA_API_URL`, `OPENWA_API_KEY`, `OPENWA_SESSION_ID`, and
`OPENWA_WEBHOOK_SECRET` to reach that OpenWA instance. Set the Render secret to
a random value of at least 16 characters, then configure the OpenWA webhook
with the same secret, subscribe to `message.received`, and set the target URL
to `https://<your-render-host>/api/webhook/`. Render's service hostname is
allowed automatically. Use HTTPS for public webhook URLs and keep API keys and
webhook secrets private.

Conversation history stores the last 20 completed message/reply pairs for
Groq context. This is contextual memory, not model training.
