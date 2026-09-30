#!/usr/bin/env python3
"""Team RSVP site: one private link per team, plus a password-protected admin dashboard.

Runs on Vercel (Flask preset, `app` below) with Postgres, or locally with SQLite.
Once deployed, everything is done from /admin: upload the registration CSVs, watch replies,
download each team's link. The commands below are only for local use:

  python3 app.py set-password                              # store the admin password hash in .env
  python3 app.py import ../infinium-merged-2026-09-29.csv   # same as the admin page's CSV upload
  python3 app.py links --base-url https://rsvp.example.com # same as the admin page's links download
  python3 app.py serve --port 8000                         # run locally

Settings come from environment variables; locally they are also read from .env
(real environment variables win, so Vercel's project settings take precedence):
  DATABASE_URL / POSTGRES_URL  Postgres connection string (unset -> local SQLite file rsvp.db)
  ADMIN_PASSWORD_HASH          sha256 hex of the admin password
  RSVP_BASE_URL                public site URL used in team links, e.g. https://rsvp.example.com
  RSVP_EVENT                   event name shown on the pages
"""
import argparse
import csv
import getpass
import hashlib
import hmac
import io
import os
import re
import secrets
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"


def load_env(path=ENV_PATH):
    """Minimal .env reader: KEY=VALUE lines, # comments, optional quotes. Never overrides real env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        key = key.removeprefix("export ").strip()
        if not sep or not key or key.startswith("#"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def write_env_var(key, value, path=ENV_PATH):
    """Set KEY=value in .env, replacing an existing line or appending one."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    new = f"{key}={value}"
    for i, line in enumerate(lines):
        if line.strip().removeprefix("export ").split("=", 1)[0].strip() == key:
            lines[i] = new
            break
    else:
        lines.append(new)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


load_env()
PAGES_DIR = HERE / "pages"
PUBLIC_DIR = HERE / "public"
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
SQLITE_PATH = Path(os.environ.get("RSVP_DB", HERE / "rsvp.db"))
EVENT_NAME = os.environ.get("RSVP_EVENT", "ContextCon Deccan CTF")
BASE_URL = os.environ.get("RSVP_BASE_URL", "").rstrip("/")

SESSION_COOKIE = "rsvp_admin"
SESSION_TTL = 12 * 3600
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
STATUSES = ("yes", "no")

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS teams ("
    " id {pk}, name TEXT NOT NULL UNIQUE, token TEXT NOT NULL UNIQUE)",
    "CREATE TABLE IF NOT EXISTS members ("
    " id {pk}, team_id INTEGER NOT NULL REFERENCES teams(id), name TEXT NOT NULL DEFAULT '',"
    " email TEXT NOT NULL, status TEXT CHECK (status IN ('yes', 'no')), updated_at TEXT)",
    "CREATE UNIQUE INDEX IF NOT EXISTS members_email ON members (lower(email))",
    "CREATE INDEX IF NOT EXISTS members_team ON members (team_id)",
]
# Columns added after the first release; created on the fly so existing databases upgrade themselves.
MIGRATIONS = [
    ("teams", "first_opened_at", "TEXT"),
    ("teams", "last_opened_at", "TEXT"),
    ("teams", "open_count", "INTEGER NOT NULL DEFAULT 0"),
]
_schema_ready = False

# Link scanners, chat-app previews and scripts that shouldn't count as a team opening its link.
BOT_UA = re.compile(r"bot|crawl|spider|slurp|preview|headless|facebookexternalhit|whatsapp|telegram|discord|"
                    r"skype|curl|wget|python-|go-http|java/|okhttp|scanner|monitor|existence discovery", re.I)


# ---------------------------------------------------------------- database

class DB:
    """Thin wrapper so the same SQL (with ? placeholders) runs on Postgres and SQLite."""

    def __init__(self):
        self.pg = bool(DATABASE_URL)
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row
            # prepare_threshold=None keeps it compatible with pgbouncer-style poolers (Neon, Supabase).
            self.conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None)
        else:
            self.conn = sqlite3.connect(SQLITE_PATH, timeout=10)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()

    def _ensure_schema(self):
        global _schema_ready
        if _schema_ready:
            return
        pk = "SERIAL PRIMARY KEY" if self.pg else "INTEGER PRIMARY KEY"
        try:
            for stmt in SCHEMA:
                self.conn.execute(stmt.format(pk=pk))
            for table, column, decl in MIGRATIONS:
                if column not in self._columns(table):
                    exists = " IF NOT EXISTS" if self.pg else ""
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN{exists} {column} {decl}")
            self.conn.commit()
        except Exception:
            # Another instance may be creating the same tables at this moment (first requests
            # after a fresh deploy). Roll back and carry on; the next request re-checks.
            self.conn.rollback()
            return
        _schema_ready = True

    def _columns(self, table):
        if self.pg:
            rows = self.conn.execute(
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s", (table,)).fetchall()
        else:
            rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r["name"] for r in rows}

    def _sql(self, sql):
        return sql.replace("?", "%s") if self.pg else sql

    def all(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(self._sql(sql), params).fetchall()]

    def one(self, sql, params=()):
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def run(self, sql, params=()):
        self.conn.execute(self._sql(sql), params)

    def many(self, sql, seq):
        seq = list(seq)
        if not seq:
            return
        if self.pg:
            with self.conn.cursor() as cur:
                cur.executemany(self._sql(sql), seq)
        else:
            self.conn.executemany(sql, seq)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            self.conn.rollback()
        else:
            self.conn.commit()
        self.conn.close()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def admin_hash():
    return os.environ.get("ADMIN_PASSWORD_HASH", "").strip().lower()


def mask_email(email):
    local, _, domain = email.partition("@")
    return (local[:2] if len(local) > 2 else local[:1]) + "•••@" + domain


def team_by_token(db, token):
    if not TOKEN_RE.match(token or ""):
        return None
    return db.one("SELECT id, name, token FROM teams WHERE token = ?", (token,))


def team_payload(db, team):
    rows = db.all(
        "SELECT id, name, email, status FROM members WHERE team_id = ? "
        "ORDER BY name = '', lower(name), lower(email)",
        (team["id"],),
    )
    return {
        "event": EVENT_NAME,
        "team": team["name"],
        "members": [
            {"id": r["id"], "name": r["name"], "email": mask_email(r["email"]), "status": r["status"]}
            for r in rows
        ],
    }


def site_url():
    if BASE_URL:
        return BASE_URL
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    return f"{proto}://{request.host}"


def summary_payload(db, base):
    teams = db.all("SELECT id, name, token, first_opened_at, last_opened_at, open_count FROM teams ORDER BY lower(name)")
    members = db.all(
        "SELECT team_id, name, email, status, updated_at FROM members "
        "ORDER BY name = '', lower(name), lower(email)"
    )
    by_team = {}
    for m in members:
        by_team.setdefault(m["team_id"], []).append(
            {"name": m["name"], "email": m["email"], "status": m["status"], "updated_at": m["updated_at"]}
        )
    out, totals = [], {"people": 0, "yes": 0, "no": 0, "pending": 0, "teams": 0, "teams_complete": 0, "teams_opened": 0}
    for t in teams:
        ms = by_team.get(t["id"], [])
        yes = sum(1 for m in ms if m["status"] == "yes")
        no = sum(1 for m in ms if m["status"] == "no")
        pending = len(ms) - yes - no
        out.append({
            "name": t["name"], "link": f"{base}/t/{t['token']}", "members": ms,
            "yes": yes, "no": no, "pending": pending,
            "updated_at": max((m["updated_at"] for m in ms if m["updated_at"]), default=None),
            "first_opened_at": t["first_opened_at"], "last_opened_at": t["last_opened_at"],
            "open_count": t["open_count"] or 0,
        })
        totals["people"] += len(ms)
        totals["yes"] += yes
        totals["no"] += no
        totals["pending"] += pending
        totals["teams"] += 1
        totals["teams_complete"] += pending == 0
        totals["teams_opened"] += bool(t["first_opened_at"])
    return {"event": EVENT_NAME, "generated_at": now_iso(), "totals": totals, "teams": out}


def parse_people_csv(text):
    """Rows of (name, email, team) from a CSV with email and team (or team_name) columns; name is optional."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    fields = {(f or "").strip().lower() for f in reader.fieldnames or []}
    if "email" not in fields or not fields & {"team", "team_name"}:
        raise ValueError("The CSV needs an 'email' column and a 'team' (or 'team_name') column.")
    rows = []
    for raw in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if isinstance(v, str)}
        email, team = row.get("email", ""), row.get("team") or row.get("team_name", "")
        if email and team:
            rows.append((row.get("name", ""), email, team))
    return rows


def import_people(db, rows):
    """Add new teams and people; existing people keep their reply (and move if their team changed)."""
    teams = {t["name"]: t["id"] for t in db.all("SELECT id, name FROM teams")}
    new_teams = sorted({r[2] for r in rows} - set(teams))
    db.many("INSERT INTO teams (name, token) VALUES (?, ?)", [(n, secrets.token_urlsafe(12)) for n in new_teams])
    teams = {t["name"]: t["id"] for t in db.all("SELECT id, name FROM teams")}
    existing = {m["email"].lower(): m for m in db.all("SELECT id, team_id, name, email FROM members")}
    inserts, updates, seen, moved = [], [], set(), 0
    for name, email, team in rows:
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        m = existing.get(key)
        if not m:
            inserts.append((teams[team], name, email))
        elif m["team_id"] != teams[team] or (name and name != m["name"]):
            moved += m["team_id"] != teams[team]
            updates.append((teams[team], name or m["name"], m["id"]))
    db.many("INSERT INTO members (team_id, name, email) VALUES (?, ?, ?)", inserts)
    db.many("UPDATE members SET team_id = ?, name = ? WHERE id = ?", updates)
    return {
        "rows": len(rows), "new_teams": len(new_teams), "new_people": len(inserts), "moved": moved,
        "teams": db.one("SELECT count(*) AS n FROM teams")["n"],
        "people": db.one("SELECT count(*) AS n FROM members")["n"],
    }


def csv_cell(value):
    """Stop spreadsheet apps from treating attendee-supplied text as a formula."""
    value = "" if value is None else str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def csv_writer(buf):
    w = csv.writer(buf)
    return lambda row: w.writerow([csv_cell(v) for v in row])


def links_csv(db, base):
    teams = db.all("SELECT id, name, token FROM teams ORDER BY lower(name)")
    emails = {}
    for m in db.all("SELECT team_id, email FROM members ORDER BY lower(email)"):
        emails.setdefault(m["team_id"], []).append(m["email"])
    buf = io.StringIO()
    write = csv_writer(buf)
    write(["team", "members", "emails", "link"])
    for t in teams:
        es = emails.get(t["id"], [])
        write([t["name"], len(es), "; ".join(es), f"{base}/t/{t['token']}"])
    return buf.getvalue(), len(teams)


def csv_download(text, prefix):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    return Response("\ufeff" + text, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{prefix}-{stamp}.csv"'})


# ---------------------------------------------------------------- admin sessions
# Stateless signed cookies, so sessions survive across serverless instances.
# The key is derived from the password hash: changing the password logs everyone out.

def _session_key():
    return hashlib.sha256(b"rsvp-session:" + admin_hash().encode()).digest()


def make_session():
    exp = str(int(time.time()) + SESSION_TTL)
    return exp + "." + hmac.new(_session_key(), exp.encode(), hashlib.sha256).hexdigest()


def is_admin():
    exp, _, sig = (request.cookies.get(SESSION_COOKIE) or "").partition(".")
    if not admin_hash() or not exp.isdigit() or int(exp) < time.time():
        return False
    return hmac.compare_digest(sig, hmac.new(_session_key(), exp.encode(), hashlib.sha256).hexdigest())


# ---------------------------------------------------------------- web app

class RSVPFlask(Flask):
    def log_exception(self, exc_info):
        # Flask logs the full URL by default, which can contain a team's secret token.
        self.logger.error("Unhandled error in %s %s", request.method, request.endpoint, exc_info=exc_info)


app = RSVPFlask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024  # Vercel caps request bodies at 4.5 MB


@app.after_request
def security_headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
    return resp


def page(name, code=200):
    return Response((PAGES_DIR / name).read_bytes(), code, mimetype="text/html")


def error(code, message):
    return jsonify(error=message), code


@app.errorhandler(404)
def not_found(_):
    if request.path.startswith("/api/"):
        return error(404, "Not found.")
    return page("notfound.html", 404)


@app.errorhandler(413)
def too_large(_):
    return error(413, "That file is too large (the limit is 4 MB).")


@app.errorhandler(405)
def bad_method(_):
    if request.path.startswith("/api/"):
        return error(405, "Method not allowed.")
    return page("notfound.html", 404)


@app.errorhandler(500)
def server_error(_):
    if request.path.startswith("/api/"):
        return error(500, "Something went wrong on the server. Please try again in a moment.")
    return Response("Something went wrong on the server. Please try again in a moment.", 500, mimetype="text/plain")


@app.get("/")
def index():
    return page("index.html")


@app.get("/robots.txt")
def robots():
    # Team pages are private links; keep every crawler out (pages also send X-Robots-Tag: noindex).
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/t/<token>")
def team_page(token):
    with DB() as db:
        found = team_by_token(db, token) is not None
    return page("team.html") if found else page("notfound.html", 404)


@app.get("/api/team/<token>")
def team_get(token):
    with DB() as db:
        team = team_by_token(db, token)
        if not team:
            return error(404, "This link isn't valid.")
        record_open(db, team)
        return team_payload(db, team)


def record_open(db, team):
    """Count a real visit to a team page (the page's script loads this API; plain link fetches don't)."""
    if is_admin() or BOT_UA.search(request.headers.get("User-Agent", "")):
        return
    stamp = now_iso()
    db.run("UPDATE teams SET open_count = open_count + 1, last_opened_at = ?, "
           "first_opened_at = COALESCE(first_opened_at, ?) WHERE id = ?", (stamp, stamp, team["id"]))


@app.post("/api/team/<token>/rsvp")
def team_rsvp(token):
    body = request.get_json(silent=True)
    updates = body.get("updates") if isinstance(body, dict) else None
    if not isinstance(updates, list) or not updates or len(updates) > 100:
        return error(400, "Bad request.")
    clean = {}
    for u in updates:
        if not isinstance(u, dict) or type(u.get("id")) is not int or u.get("status") not in STATUSES:
            return error(400, "Bad request.")
        clean[u["id"]] = u["status"]
    with DB() as db:
        team = team_by_token(db, token)
        if not team:
            return error(404, "This link isn't valid.")
        ids = {r["id"] for r in db.all("SELECT id FROM members WHERE team_id = ?", (team["id"],))}
        if not set(clean) <= ids:
            return error(403, "That person isn't on this team.")
        stamp = now_iso()
        db.many(
            "UPDATE members SET status = ?, updated_at = ? WHERE id = ? AND status IS DISTINCT FROM ?",
            [(s, stamp, i, s) for i, s in clean.items()],
        )
        return team_payload(db, team)


@app.get("/admin")
@app.get("/admin/")
def admin_page():
    return page("admin.html")


@app.post("/api/admin/login")
def admin_login():
    body = request.get_json(silent=True)
    password = body.get("password") if isinstance(body, dict) else None
    expected = admin_hash()
    if not expected:
        return error(503, "No admin password is configured (set ADMIN_PASSWORD_HASH).")
    if not isinstance(password, str) or not hmac.compare_digest(sha256(password), expected):
        time.sleep(1)  # slow down guessing
        return error(401, "Wrong password.")
    resp = jsonify(ok=True)
    resp.set_cookie(SESSION_COOKIE, make_session(), max_age=SESSION_TTL, httponly=True, samesite="Strict",
                    secure=site_url().startswith("https://"))
    return resp


@app.post("/api/admin/logout")
def admin_logout():
    resp = jsonify(ok=True)
    resp.delete_cookie(SESSION_COOKIE, httponly=True, samesite="Strict")
    return resp


@app.get("/api/admin/summary")
def admin_summary():
    if not is_admin():
        return error(401, "Log in first.")
    with DB() as db:
        return summary_payload(db, site_url())


@app.get("/api/admin/export.csv")
def admin_export():
    if not is_admin():
        return error(401, "Log in first.")
    with DB() as db:
        data = summary_payload(db, site_url())
    buf = io.StringIO()
    write = csv_writer(buf)
    write(["team", "name", "email", "rsvp", "updated_at", "team_link", "link_first_opened", "link_last_opened", "link_views"])
    for t in data["teams"]:
        for m in t["members"]:
            write([t["name"], m["name"], m["email"], m["status"] or "pending", m["updated_at"] or "", t["link"],
                   t["first_opened_at"] or "", t["last_opened_at"] or "", t["open_count"]])
    return csv_download(buf.getvalue(), "rsvp")


@app.get("/api/admin/links.csv")
def admin_links():
    if not is_admin():
        return error(401, "Log in first.")
    with DB() as db:
        text, _ = links_csv(db, site_url())
    return csv_download(text, "team-links")


@app.post("/api/admin/import")
def admin_import():
    if not is_admin():
        return error(401, "Log in first.")
    # Requiring text/csv means a cross-site form can't post here (it would need a CORS preflight).
    if request.mimetype != "text/csv":
        return error(415, "Send the file as text/csv.")
    try:
        rows = parse_people_csv(request.get_data(as_text=True))
    except (ValueError, csv.Error) as e:
        return error(400, str(e))
    if not rows:
        return error(400, "No rows with both an email and a team were found.")
    with DB() as db:
        return import_people(db, rows)


# ---------------------------------------------------------------- commands

def cmd_import(args):
    try:
        rows = parse_people_csv(Path(args.csv).read_text(encoding="utf-8-sig"))
    except ValueError as e:
        sys.exit(str(e))
    with DB() as db:
        r = import_people(db, rows)
    where = "Postgres" if DATABASE_URL else SQLITE_PATH
    print(f"Imported {r['rows']} rows: {r['new_teams']} new teams, {r['new_people']} new people, {r['moved']} moved team.")
    print(f"Database ({where}) now has {r['teams']} teams and {r['people']} people.")


def cmd_set_password(args):
    pw = getpass.getpass("New admin password: ")
    if len(pw) < 8:
        sys.exit("Use at least 8 characters.")
    if pw != getpass.getpass("Repeat it: "):
        sys.exit("Passwords didn't match.")
    digest = sha256(pw)
    write_env_var("ADMIN_PASSWORD_HASH", digest)
    print(f"Saved ADMIN_PASSWORD_HASH to {ENV_PATH} (restart a running local server to pick it up).")
    print("For Vercel, add the same value as the ADMIN_PASSWORD_HASH environment variable:")
    print(digest)


def cmd_links(args):
    base = (args.base_url or BASE_URL).rstrip("/")
    if not base:
        sys.exit("Pass --base-url (e.g. https://rsvp.example.com) or set RSVP_BASE_URL.")
    out = Path(args.out)
    with DB() as db:
        text, count = links_csv(db, base)
    out.write_text(text, encoding="utf-8")
    os.chmod(out, 0o600)
    print(f"Wrote {count} team links to {out}")


def cmd_serve(args):
    # On Vercel the CDN serves public/; locally Flask does.
    app.add_url_rule("/static/<path:filename>", "static",
                     lambda filename: send_from_directory(PUBLIC_DIR / "static", filename))
    if not admin_hash():
        print("Warning: no admin password set; run `python3 app.py set-password`.", file=sys.stderr)
    app.run(host=args.host, port=args.port, debug=False)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("import", help="load people from a name,email,team CSV (safe to re-run)")
    s.add_argument("csv")
    s.set_defaults(func=cmd_import)
    s = sub.add_parser("set-password", help="set the admin password (stored as a sha256 hash)")
    s.set_defaults(func=cmd_set_password)
    s = sub.add_parser("links", help="write a CSV of every team's unique link")
    s.add_argument("--base-url")
    s.add_argument("--out", default=str(HERE / "team-links.csv"))
    s.set_defaults(func=cmd_links)
    s = sub.add_parser("serve", help="run the site locally")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
