# WhatsApp Business Agent

Django receives messages from a persistent Baileys bridge, generates replies
with Groq, and asks the bridge to send them over WhatsApp. Baileys requires a
long-running Node.js process and persistent session storage; it cannot run as a
serverless function.

## Configure Django

Copy `.env.example` to `.env` and set:

- `DJANGO_SECRET_KEY` to a long random value.
- `WHATSAPP_BRIDGE_URL` to the private or HTTPS URL of the bridge.
- `WHATSAPP_BRIDGE_TOKEN` to a long random secret shared with the bridge.
- `GROQ_API_KEY` and, optionally, `GROQ_MODEL`.
- `DATABASE_URL` to persistent PostgreSQL in production. A blank value uses
  SQLite locally.

## Run locally

1. Install Node.js 20 or newer. In `baileys_bridge`, copy `.env.example` to
   `.env` and set `WHATSAPP_BRIDGE_TOKEN` to the same value as Django's.
   `DJANGO_WEBHOOK_URL` should point to the Django server's
   `/webhook/baileys/` route.
2. Install and start the bridge:

   ```powershell
   cd .\baileys_bridge
   npm install
   npm start
   ```

3. Scan the QR code shown in the bridge terminal using WhatsApp's linked-device
   settings. Session credentials are saved in `baileys_bridge/auth_info/`; keep
   that folder private and persistent.
4. In another terminal, from the project root, install the Python dependencies,
   migrate the database, and start Django:

   ```powershell
   .\venv\Scripts\python.exe manage.py migrate
   .\venv\Scripts\python.exe manage.py runserver
   ```

The bridge's `/health` endpoint reports whether its WhatsApp connection is
open. Django's incoming endpoint is `/webhook/baileys/`; it only accepts
requests authenticated with the shared bearer token.

## Production deployment

Run the Baileys bridge as a persistent Node.js service with a persistent volume
for `baileys_bridge/auth_info/`. Configure its environment from
`baileys_bridge/.env.example`. Set Django's `WHATSAPP_BRIDGE_URL` to the
bridge's reachable `/` base URL and configure the same
`WHATSAPP_BRIDGE_TOKEN` in both services. Keep the bridge endpoint private or
behind HTTPS and network access controls; do not expose an unauthenticated
plain-HTTP endpoint to the internet.

Deploy Django with persistent PostgreSQL and set `DATABASE_URL` to its
connection URL, then run migrations:

```powershell
.\venv\Scripts\python.exe manage.py migrate
```

Conversation history stores the last 20 completed message/reply pairs for
Groq context. This is contextual memory, not model training.
