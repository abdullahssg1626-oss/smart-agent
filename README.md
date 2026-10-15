# WhatsApp Business Agent

Django webhook that receives WhatsApp text messages, generates replies with
Groq, and sends them through the WhatsApp Cloud API.

## Deploy to Vercel

1. Push this project to GitHub. Keep `.env` out of Git; `.gitignore` excludes it.
2. Import the repository into Vercel and leave **Root Directory** set to the
   folder containing `manage.py`.
3. Add these environment variables in **Vercel → Project Settings → Environment
   Variables**. Do not upload the local `.env` file:

   - `DJANGO_SECRET_KEY`
   - `WHATSAPP_VERIFY_TOKEN`
   - `WHATSAPP_ACCESS_TOKEN`
   - `WHATSAPP_PHONE_NUMBER_ID`
   - `WHATSAPP_APP_SECRET`
   - `GROQ_API_KEY`
   - `DATABASE_URL` (a persistent PostgreSQL connection URL; required for saved conversation memory)
   - `DJANGO_DEBUG` = `false`
   - `WHATSAPP_GRAPH_API_VERSION` = `v23.0` (or the Graph API version you use)
   - `GROQ_MODEL` = `llama-3.3-70b-versatile` (or a model enabled for your Groq account)

   `DJANGO_ALLOWED_HOSTS` is optional if using Vercel-provided domains. For a
   custom domain, set it to the comma-separated host names.
4. Deploy the project. In Meta's webhook settings, use
   `https://<your-vercel-domain>/webhook/whatsapp/` as the callback URL and use
   the same `WHATSAPP_VERIFY_TOKEN` configured in Vercel. Subscribe to the
   `messages` webhook field.

Vercel runs this Django app as a serverless function. Create a persistent
PostgreSQL database and set its connection URL as `DATABASE_URL`; Vercel's local
filesystem and SQLite database are not persistent. To create the database
tables, set the PostgreSQL URL as `DATABASE_URL` in your local `.env` file and
run the migration command from this project folder:

```powershell
.\venv\Scripts\python.exe manage.py migrate
```

Use the same database URL in Vercel's environment variables. Never share or
commit this URL; it contains database credentials.

Each phone number has a stored conversation history. The last 20 completed
message/reply pairs are passed to Groq through LangChain. This is contextual
memory, not model training; the agent does not learn new business facts unless
you add them to its system prompt or a knowledge base.

## Run locally

Create a `.env` file using `.env.example`, fill in the required values, then run:

```powershell
.\venv\Scripts\python.exe manage.py runserver
```

Locally, a blank `DATABASE_URL` uses SQLite. Create tables before the first run:

```powershell
.\venv\Scripts\python.exe manage.py migrate
```

The webhook route is `/webhook/whatsapp/`. Never upload `.env` or its secret
values to GitHub; add them to the Vercel project environment instead.
