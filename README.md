# Sync Performance

A role-aware sales performance tool for Nestle: revenue tracking, target setting, dashboards, RO document management, and a versioned audit log.

## Roles

| Role | Scope | Key actions |
|------|-------|-------------|
| **Salesperson** | Own entries | Add, edit, cancel own entries |
| **Manager** | Team | View team entries + rollup, cancel within scope |
| **Regional Head** | Region | View region entries + rollup, cancel within scope |
| **Admin** | National | Full access: set targets, manage team, audit log, soft-delete |

---

## Quick start (local dev)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app/main.py
```

Open `http://127.0.0.1:5050/login`. Dev mode (`SYNC_DEV_LOGIN=1`, the default) shows a user picker with seeded demo accounts.

> macOS reserves port 5000 for AirPlay — this app uses **5050** by default. Override with `PORT=5051 python app/main.py`.

---

## Environment variables

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `5050` | Server port |
| `SYNC_DEV_LOGIN` | `1` | **Set to `0` in production** — disables the dev user picker |
| `SYNC_SECRET_KEY` | `dev-secret-change-me` | Flask session signing. **Change in production** (`python -c "import secrets; print(secrets.token_hex(32))"`) |
| `SYNC_DATA_DIR` | repo root | Root for SQLite DB + local file uploads |
| `SYNC_DB_PATH` | `${SYNC_DATA_DIR}/app.db` | SQLite database path |
| `SYNC_UPLOAD_DIR` | `${SYNC_DATA_DIR}/instance/uploads/ro` | Local RO uploads (only used when S3 is not configured) |

### S3 (RO document storage)

| Variable | Default | Description |
|----------|---------|-------------|
| `SYNC_S3_BUCKET` | _(empty)_ | **Set to enable S3 uploads.** RO docs go to `s3://{bucket}/{prefix}/{uuid}.ext` |
| `SYNC_S3_PREFIX` | `ro-docs` | Key prefix inside the bucket |

When `SYNC_S3_BUCKET` is set, RO documents are uploaded to S3 and served via **15-minute presigned URLs**. When unset, files are stored on local disk.

### Google OAuth (production auth)

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_OAUTH_CLIENT_ID` | _(empty)_ | Google OAuth client ID from [Cloud Console](https://console.cloud.google.com/apis/credentials) |
| `GOOGLE_OAUTH_CLIENT_SECRET` | _(empty)_ | Google OAuth client secret |
| `ALLOWED_EMAIL_DOMAIN` | `syncmedia.io` | Only Google accounts with this domain can sign in |

---

## Google OAuth setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services → Credentials
2. Configure the **OAuth consent screen** (External or Internal)
3. Create **OAuth 2.0 Client ID** → Web application
4. Add authorized redirect URI: `https://your-domain.com/auth/google/callback`
5. Set the env vars and flip `SYNC_DEV_LOGIN=0`

Users must already exist in `config/team_config.json`. The OAuth flow matches by email via `resolve_user_by_email()`.

---

## S3 bucket setup

```bash
# Create bucket
aws s3 mb s3://sync-perf-ro-docs

# Block public access (presigned URLs handle auth)
aws s3api put-public-access-block --bucket sync-perf-ro-docs \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

The EC2 instance IAM role needs `s3:PutObject` and `s3:GetObject` on the bucket.

---

## Data model

- **`revenue_entries`** — Sales pipeline entries. Pipeline = Plan x Confidence%. Soft-cancelled via `is_cancelled`, soft-deleted via `is_deleted`. Rows are never removed.
- **`targets`** — Per-salesperson per-quarter targets. Rollups computed at read time.
- **`revenue_entry_history`** — Append-only audit log. Every create/update/cancel/uncancel/delete/undelete writes a versioned JSON snapshot.
- **`ro_documents`** — RO files attached to entries. Stored in S3 or local disk depending on config.

## Team config

`config/team_config.json` is the source of truth for users, hierarchy, regions, and fiscal calendar. Edit via the admin screen at `/admin/team` or directly. On save, the users table is reconciled additively — existing users with entries are archived, never deleted.

---

## Project structure

```
app/
  main.py              ← Flask app (routes, DB, auth, everything)
  static/              ← CSS, JS, images
  templates/           ← Jinja2 HTML templates
config/
  team_config.json     ← Users, hierarchy, regions
scripts/
  backup.sh            ← Hourly SQLite + uploads backup to S3
requirements.txt
DEPLOYMENT-AWS.md      ← Full AWS deployment runbook
```

---

## Deploying on AWS

See **[DEPLOYMENT-AWS.md](DEPLOYMENT-AWS.md)** for the full runbook including:

- Single EC2 + EBS topology (~$15-30/mo)
- ECS Fargate + RDS topology for scale
- Backup/restore procedures
- Security checklist
- nginx/systemd config

### Minimal production env vars

```bash
SYNC_DEV_LOGIN=0
SYNC_SECRET_KEY=<random-32-byte-hex>
SYNC_S3_BUCKET=sync-perf-ro-docs
GOOGLE_OAUTH_CLIENT_ID=xxx.apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=GOCSPX-xxx
```
