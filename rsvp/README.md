# Team RSVP

Every team gets its own secret link (`/t/<token>`). Anyone who opens it can mark each
teammate as **Going** or **Not going**, or set the whole team at once. Admins sign in at
`/admin` to see the totals, see each team's replies, copy team links and export a CSV.

It's a small Flask app (`app.py`). On Vercel it runs as one serverless function with a
Postgres database. Locally it can also run on a SQLite file with no setup.

```
app.py            Flask app (Vercel entrypoint) + local CLI: set-password / import / links / serve
pages/            HTML pages returned by the app
public/static/    CSS and JS, served by Vercel's CDN
vercel.json       pins the Flask preset
requirements.txt  Python dependencies (Vercel installs these)
.env.example      template for the settings below
```

## Settings and secrets

All settings come from environment variables. Locally, the app also reads them from
`rsvp/.env` (copy `.env.example`). On Vercel you set them in the project settings.
If a variable is set in both places, the real environment variable wins.

| Variable | What it is |
| --- | --- |
| `ADMIN_PASSWORD_HASH` | sha256 of the admin password. `python3 app.py set-password` writes it to `.env` and prints it. |
| `DATABASE_URL` | Postgres connection string. Vercel sets it when you connect a database. If it's missing, the app uses SQLite (`rsvp.db`). |
| `RSVP_BASE_URL` | Public URL used in team links, e.g. `https://your-project.vercel.app`. |
| `RSVP_EVENT` | Event name shown on the pages (default `ContextCon Deccan CTF`). |

`.env`, CSVs and databases are listed in `.gitignore` and `.vercelignore`, so they are
never committed or uploaded.

## Deploy on Vercel

Everything after deploying happens in the browser: in the Vercel dashboard and on this
site's `/admin` page. The only command you run is creating the password hash, which you
do **before** deploying.

### Before you deploy: make the password hash

On any machine with Python 3:

```bash
python3 -c "import hashlib, getpass; print(hashlib.sha256(getpass.getpass('Admin password: ').encode()).hexdigest())"
```

Keep the 64-character hash it prints. The password itself isn't stored anywhere. If you
lose it, make a new hash and replace the variable.

### 1. Create the project

In Vercel, choose **Add New → Project** and import the `website_rsvp` repo from GitHub. It's private, so if it isn't listed, click **Adjust GitHub App Permissions** and give Vercel access to it.

- **Root Directory:** `rsvp`
- **Framework Preset:** Flask (pinned by `vercel.json`; nothing else to set)

Click **Deploy**. This first deploy has no database or password yet, so continue.

### 2. Add a database

In the project, open **Storage → Create Database → Neon (Postgres)**, create it and connect
it to **Production** (and **Preview** if you want). This sets `DATABASE_URL` for you.
The app creates its tables itself.

### 3. Add the password

Open **Settings → Environment Variables** and add:

| Name | Value |
| --- | --- |
| `ADMIN_PASSWORD_HASH` | the hash from above |
| `RSVP_BASE_URL` | optional: your custom domain, e.g. `https://rsvp.example.com` |

If `RSVP_BASE_URL` isn't set, team links use whatever domain you open `/admin` on.

Then go to **Deployments**, open the latest deployment's **⋯** menu and click **Redeploy**.
New variables only apply to new deployments.

### 4. Load the teams (from /admin)

1. Open `https://<your-project>.vercel.app/admin` and sign in.
2. Click **Import CSV** and select the registrations CSV and the invitations CSV. You can
   pick both at once. The raw exports work as they are, and so does the merged
   `name,email,team` file. Any CSV with an `email` column and a `team`/`team_name` column works.
3. Click **Team links** to download a CSV of every team, its member emails and its unique
   link, ready for sending out.

Importing again later (e.g. a newer registrations export) only adds new people and new
teams. Existing links and replies stay as they are.

### 5. Check it

- `https://<your-project>.vercel.app/healthz` returns `{"ok": true}`.
- `/admin` shows 273 teams and 735 people.
- Open one link from the team links file, change a reply, and click **Refresh** on the dashboard.

**Export replies** on the dashboard downloads everyone's current reply at any time.

The dashboard also shows whether each team has **opened its link**: when it was first and
last opened, and how many views it has. Visits by signed-in admins, link scanners and chat-app
previews aren't counted, so "Not opened" means no teammate has loaded the page yet. The
"Link not opened yet" filter lists teams you may want to remind.

## Run locally

```bash
cd rsvp
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                    # then fill in values, or just run set-password
.venv/bin/python app.py set-password
.venv/bin/python app.py import /path/to/registrations.csv   # keep CSVs outside the repo
.venv/bin/python app.py serve           # http://127.0.0.1:8000
```

## Notes

- Admin sessions are signed cookies that last 12 hours. Changing the password logs everyone out.
- Team pages hide most of each email address (`ab•••@domain`). The admin page shows the full addresses.
- Each team link works like a password (16 random characters), so keep `team-links.csv` private.
