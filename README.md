# Sync Performance

A role-aware sales performance tool: revenue tracking, target setting, dashboards, and a versioned audit log.

- **Salesperson**: add/edit own entries; cancel own entries; never hard-deletes.
- **Manager**: sees team entries and team rollup; can cancel within scope.
- **Regional Head**: sees region entries and region rollup; can cancel within scope.
- **Admin (national)**: sees everything; sets targets at `/admin/targets`; manages the team at `/admin/team`; reviews the full mutation history at `/admin/audit`. Soft-delete is admin-only.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app/main.py
```

Open `http://127.0.0.1:5050/login` and log in as a seeded demo user.

> Note: macOS reserves port 5000 for AirPlay Receiver, so this app binds to `5050` by default. Override with `PORT=5051 python app/main.py` if needed.

## Notes

- `client_type` is mandatory and enforced in schema + forms.
- Authorization is enforced in backend route handlers before all read/write operations.
- The previous SQL RLS file remains under `db/phase1_schema_rls.sql`.

## Data model (Phase 3)

- `revenue_entries` — Pipeline derives from `Plan × Confidence% / 100` server-side. Cancellation and deletion are **soft** via `is_cancelled` / `is_deleted` flags; rows are never removed.
- `targets` — per-salesperson per-quarter target values; rollups computed at read time.
- `revenue_entry_history` — append-only audit log. Every create / update / cancel / uncancel / delete / undelete writes a versioned JSON snapshot.

## Team config

`config/team_config.json` is the source of truth for users + hierarchy + regions + fiscal calendar. The admin screen `/admin/team` edits it; on save the users table is reconciled additively (existing users with entries are never deleted, just archived if removed from the config).

## Google OAuth provision

The developer wires Google OAuth at `/auth/google/start` and `/auth/google/callback` (currently 501 stubs). Required env vars: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI`, `ALLOWED_EMAIL_DOMAIN` (e.g. `sync.in`). The identity-resolution seam is `resolve_user_by_email(email)` in `app/main.py`.

## Deploying on AWS

See **[DEPLOYMENT-AWS.md](DEPLOYMENT-AWS.md)** for the full runbook. Short version: set `SYNC_DATA_DIR=/var/lib/sync` (or wherever you mount EBS / EFS) and the SQLite DB + RO uploads both live there. Cron `scripts/backup.sh` to S3 for off-host backups.
