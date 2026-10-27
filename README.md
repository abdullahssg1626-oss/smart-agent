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

## Deploy the free Render setup

1. Push the repository to GitHub and select **New → Blueprint** in Render.
   Connect the repository and let Render read the root `render.yaml`.
2. Enter your `GROQ_API_KEY` when prompted, then create the Blueprint. It
   creates the Django web service, public Baileys bridge, shared random bridge
   token, and a free PostgreSQL database in Oregon.
3. Open the `whatsapp-baileys` service logs and scan its QR code with WhatsApp
   linked-device settings. The bridge calls Django at its Render URL and Django
   sends replies back to the bridge.

This free configuration is for testing, not reliable always-on use. Render
free web services spin down after 15 minutes without inbound traffic and have
ephemeral filesystems. The Baileys service can disconnect while asleep, and its
WhatsApp login files can be lost when it restarts, so you may need to scan a
new QR code. The free PostgreSQL database expires after 30 days; upgrade it
before expiry to retain conversation history. Do not rely on this setup for
production messaging.

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
