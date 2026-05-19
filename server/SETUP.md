# Kanban Backend — Setup Guide

The Kanban frontend can run two ways:

- **Offline mode** (no setup): open `kanban.html` from disk or GitHub Pages — data is saved in your browser's `localStorage`. No Google sync.
- **Connected mode** (this guide): a tiny Flask server stores tasks in a database and syncs them with Google Tasks, Google Calendar, and Gmail.

The whole stack is **free** to run for personal use: Render's free web tier + Neon's free Postgres + Google APIs (no charge for a single user).

---

## What you'll need before starting

A Google account, a GitHub account (this repo), and ~30 minutes for first-time setup. Subsequent deploys are automatic on `git push`.

---

## Step 1 — Create a Google Cloud project & OAuth credentials

1. Go to https://console.cloud.google.com and create a new project (any name; e.g. *kanban-personal*).
2. **APIs & Services → Library** — enable each of these (search and click "Enable"):
   - Google Tasks API
   - Google Calendar API
   - Gmail API
3. **APIs & Services → OAuth consent screen**:
   - User type: **External**
   - App name: *Kanban*
   - User support email: your email
   - Developer contact: your email
   - **Scopes**: click "Add or Remove Scopes" and add:
     - `.../auth/userinfo.email`
     - `.../auth/userinfo.profile`
     - `.../auth/tasks`
     - `.../auth/calendar`
     - `.../auth/gmail.readonly`
     - `.../auth/gmail.labels`
   - **Test users**: add your own Google email. (While the app is in "Testing" mode only test users can sign in. You do **not** need to submit it for verification for personal use.)
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Web application**
   - Name: *Kanban*
   - Authorized redirect URIs:
     - `http://localhost:5000/api/auth/callback` (for local dev)
     - `https://YOUR-APP.onrender.com/api/auth/callback` (add this after Step 3)
   - Save. Copy the **Client ID** and **Client secret** — you'll need them.

---

## Step 2 — Create a free Postgres database (Neon)

Render's free tier doesn't persist a disk, so SQLite would reset on every restart. Neon's free Postgres has no expiration.

1. Sign up at https://neon.tech (free, GitHub login works).
2. Create a project — name *kanban*, region near you.
3. On the project dashboard, copy the **connection string** (starts with `postgresql://...`). Keep it handy.

---

## Step 3 — Deploy to Render

1. Sign up at https://render.com (free, GitHub login works).
2. **New → Blueprint** → connect this GitHub repo. Render will detect `render.yaml` and propose a service. Click **Apply**.
3. The first build will fail because the env vars aren't set yet. That's fine — go to the service's **Environment** tab and add:
   - `GOOGLE_CLIENT_ID` — from Step 1
   - `GOOGLE_CLIENT_SECRET` — from Step 1
   - `OAUTH_REDIRECT_URI` — `https://YOUR-APP.onrender.com/api/auth/callback` (your service's URL is shown at the top of the page)
   - `DATABASE_URL` — the Neon string from Step 2
4. **Manual Deploy → Clear build cache & deploy**. Wait ~2 minutes.
5. Go back to **Google Cloud → Credentials → your OAuth client** and add `https://YOUR-APP.onrender.com/api/auth/callback` to **Authorized redirect URIs**. Save.

Open `https://YOUR-APP.onrender.com/kanban.html`. You should see the board with a **SIGN IN** pill in the top-right.

---

## Step 4 — Sign in and try it

1. Click **SIGN IN**, complete the Google consent screen, accept the scopes.
2. You'll land back on the board, pill now reads your email in green.
3. Add a task. Click **SYNC**. Within seconds:
   - The task appears in https://tasks.google.com under a new list called *Kanban*.
   - If the task has a due date, an event appears in Google Calendar under a new calendar called *Kanban Tasks*.
   - The server scans recent unread email and creates suggestions (badge appears on **SUGGESTIONS**).

---

## How Gmail integration works

Two modes run simultaneously:

- **Auto-create from labeled emails.** Apply the Gmail label `kanban` to any email — on the next sync, it becomes a task in the **To Do** column. (Create the label once in Gmail: Settings → Labels → New label → `kanban`.)
- **Suggest from inbox.** Recent unread emails that look task-shaped (contain phrases like *please*, *can you*, *by Friday*, *deadline*, etc.) appear in the **SUGGESTIONS** drawer. Click **ACCEPT** to convert one to a task or **DISMISS** to hide it.

Already-converted or dismissed emails are remembered so they don't re-appear on subsequent syncs.

---

## Local development

```bash
cd server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your GOOGLE_CLIENT_ID / SECRET / etc.
python app.py
# Open http://localhost:5000/kanban.html
```

Local mode uses SQLite (`server/kanban.db`) by default. Override with `DATABASE_URL` to point at Postgres.

---

## Troubleshooting

- **"redirect_uri_mismatch"**: The URL in the OAuth Credentials must *exactly* match `OAUTH_REDIRECT_URI`, including `https://` vs `http://`.
- **"Access blocked: This app's request is invalid"**: You're not in the test-users list on the OAuth consent screen.
- **Free Render service is slow on first request**: it spins down after 15 min of inactivity. The first hit after wake-up takes ~30s.
- **Tasks didn't sync**: Click **SYNC** explicitly. Sync is on-demand, not automatic.
- **Want to wipe everything and start over**: in Neon, drop and recreate the database; redeploy on Render.

---

## What this server does *not* do

- No two-way sync. Edits made in Google Tasks / Calendar are not pulled back into the Kanban board.
- No multi-user support. The server stores **one** Google account's credentials. Fine for personal use.
- No background scanning. Gmail is only scanned when you click **SYNC**.

These are deliberate scope cuts for the MVP. Either is straightforward to add later.
