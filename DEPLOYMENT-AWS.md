# Sync Performance — AWS deployment runbook

This document covers how to run the app on AWS so that **`app.db`** and **RO documents** under `instance/uploads/ro/` survive across restarts, deploys, and instance replacement.

The app intentionally stores all writeable state in **one directory** (set by `SYNC_DATA_DIR`) so a single mounted volume on AWS captures everything.

---

## TL;DR — what to mount where

```
SYNC_DATA_DIR/
├── app.db                          ← SQLite DB
├── backups/                        ← scripts/backup.sh writes here before S3 push
└── instance/
    └── uploads/
        └── ro/<uuid>.<ext>         ← RO documents
```

Point `SYNC_DATA_DIR` at a persistent mount and everything is preserved.

---

## Path overrides (env vars)

| Variable | Default | Purpose |
|----------|---------|---------|
| `SYNC_DATA_DIR` | repo root | Root of all writeable data — DB + uploads. **Set this on AWS.** |
| `SYNC_DB_PATH` | `${SYNC_DATA_DIR}/app.db` | Override the DB path explicitly (e.g. on a separate volume) |
| `SYNC_UPLOAD_DIR` | `${SYNC_DATA_DIR}/instance/uploads/ro` | Override the RO uploads dir |
| `SYNC_DEV_LOGIN` | `1` | **Set to `0` on AWS** — disables the dev user-id login picker |
| `SYNC_SECRET_KEY` | `dev-secret-change-me` | Flask session signing key. **Change on AWS** to a 32+ byte random value |
| `PORT` | `5050` | Port the dev server listens on (use gunicorn in prod) |
| `SYNC_BACKUP_BUCKET` | _(none)_ | S3 bucket for `scripts/backup.sh` |
| `SYNC_BACKUP_PREFIX` | hostname | Prefix inside the bucket |
| `SYNC_RETENTION_DAYS` | `7` | Local snapshot retention before pruning |

The app **prints the resolved paths at startup** so you can verify the mount worked before users log in.

---

## Topology A — single EC2 + EBS (cheapest, ~$15-30/mo)

Best for: pilot, internal tool, single-team usage. Single point of failure but trivially backed up.

### Architecture

```
                   Route 53 (sync.example.com)
                          │
                  ┌───────┴───────┐
                  │  ALB or ngx   │ TLS termination
                  └───────┬───────┘
                          │  :80 / :443
                  ┌───────┴───────┐
                  │  EC2 t3.small │  gunicorn :5050  → flask app
                  │               │  cron @hourly    → scripts/backup.sh
                  └───────┬───────┘
                          │ /var/lib/sync
                  ┌───────┴───────┐
                  │  EBS gp3 20GB │  ← SYNC_DATA_DIR
                  │  (encrypted)  │     · app.db
                  └───────────────┘     · instance/uploads/ro/
                          │
                          ▼
                       ┌────┐
                       │ S3 │  encrypted bucket, versioning ON, 30-day Glacier
                       └────┘
```

### One-time setup

```bash
# 1. Create + attach + mount an EBS gp3 volume
sudo mkfs -t ext4 /dev/nvme1n1
sudo mkdir -p /var/lib/sync
sudo mount /dev/nvme1n1 /var/lib/sync
sudo chown -R syncapp:syncapp /var/lib/sync

# Make the mount survive reboots
echo "/dev/nvme1n1 /var/lib/sync ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab

# 2. Install Python + sqlite + awscli + nginx
sudo dnf install -y python3.11 python3.11-pip sqlite nginx awscli
sudo pip3.11 install gunicorn

# 3. Pull the app
sudo -u syncapp git clone https://your-git-host/sync-performance.git /opt/sync
cd /opt/sync
sudo -u syncapp python3.11 -m venv .venv
sudo -u syncapp .venv/bin/pip install -r requirements.txt

# 4. Configure systemd
sudo tee /etc/systemd/system/sync.service > /dev/null <<'UNIT'
[Unit]
Description=Sync Performance Flask app
After=network.target

[Service]
Type=simple
User=syncapp
WorkingDirectory=/opt/sync
Environment=SYNC_DATA_DIR=/var/lib/sync
Environment=SYNC_DEV_LOGIN=0
Environment=SYNC_SECRET_KEY=__GENERATE_A_REAL_ONE__
Environment=PORT=5050
ExecStart=/opt/sync/.venv/bin/gunicorn -w 3 -b 127.0.0.1:5050 app.main:app
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now sync

# 5. nginx in front (TLS via Let's Encrypt or ACM if behind ALB)
sudo tee /etc/nginx/conf.d/sync.conf > /dev/null <<'NGX'
server {
    listen 80;
    server_name sync.example.com;
    client_max_body_size 12M;   # >= MAX_CONTENT_LENGTH for RO uploads
    location / { proxy_pass http://127.0.0.1:5050; proxy_set_header Host $host; }
}
NGX
sudo systemctl restart nginx

# 6. Backups
sudo tee /etc/cron.d/sync-backup > /dev/null <<'CRON'
SYNC_DATA_DIR=/var/lib/sync
SYNC_BACKUP_BUCKET=sync-perf-backups
0 * * * * syncapp /opt/sync/scripts/backup.sh >> /var/log/sync-backup.log 2>&1
CRON
```

### Day-to-day ops

| Task | Command |
|------|---------|
| Live tail | `journalctl -u sync -f` |
| Restart app | `sudo systemctl restart sync` |
| Run backup ad-hoc | `sudo -u syncapp SYNC_DATA_DIR=/var/lib/sync SYNC_BACKUP_BUCKET=sync-perf-backups /opt/sync/scripts/backup.sh` |
| EBS snapshot | enable AWS Backup with daily schedule, 14-day retention |
| Disk usage | `du -sh /var/lib/sync/*` |
| Edit team config | `sudo -u syncapp vi /opt/sync/config/team_config.json` then `sudo systemctl restart sync` (or use `/admin/team` JSON tab) |

---

## Topology B — production (ECS Fargate + EFS + RDS, ~$100-150/mo)

When you outgrow single-instance:

```
   ALB (HTTPS, ACM cert)
        │
   ┌────┴────┐
   │ Fargate │ × N tasks running gunicorn
   │  task   │
   └────┬────┘
        ├── EFS mount /mnt/sync         ← SYNC_DATA_DIR (shared between tasks)
        │   └── instance/uploads/ro/
        │
        └── RDS Postgres                ← Replace SQLite for DB
            (db.t4g.micro, multi-AZ)
```

**What changes in the code** to support this:
- The DB switches from SQLite to Postgres. Right now `sqlite3.connect(DB_PATH)` is in `get_db()` (`app/main.py:55`); replace with `psycopg2.connect(...)` and update SQL where SQLite-specific syntax appears (mostly the `pragma table_info` migration check and the `on conflict ... do update set` upserts — both have direct Postgres equivalents).
- DB connection string from `SYNC_DATABASE_URL` env var.
- Files can stay on EFS (simpler) or move to S3 with presigned URLs (cheaper at scale; see "Migration" below).

**What stays unchanged**:
- Templates, routes, permissions, audit log, target carry-forward, RO closure gate.
- All security fixes (`/ro-doc/<id>` auth, archived-user revocation, `SYNC_DEV_LOGIN=0` gate, atomic mutation+audit).

---

## Backup + restore

### Cron entry (hourly is fine for most teams)

```cron
0 * * * * syncapp SYNC_DATA_DIR=/var/lib/sync SYNC_BACKUP_BUCKET=my-sync-backups /opt/sync/scripts/backup.sh
```

### What `scripts/backup.sh` does

1. `sqlite3 app.db ".backup '...'"` — online, consistent snapshot (handles WAL mode safely; **never** just `cp app.db`).
2. `tar czf` the uploads directory.
3. `aws s3 cp` both to `s3://${SYNC_BACKUP_BUCKET}/${SYNC_BACKUP_PREFIX}/${YYYY/MM/DD}/`.
4. `aws s3 sync` the live uploads dir to `s3://.../uploads-live/` for incremental visibility.
5. Prune local snapshots older than `SYNC_RETENTION_DAYS` (default 7).

### Restore

```bash
# Stop the app first so writes don't race
sudo systemctl stop sync

# Pull the most recent DB + uploads from S3
aws s3 cp s3://my-sync-backups/<host>/2026/04/29/app-20260429T030000Z.db /tmp/restore.db
aws s3 cp s3://my-sync-backups/<host>/2026/04/29/uploads-20260429T030000Z.tar.gz /tmp/uploads.tar.gz

# Replace the DB
sudo cp /tmp/restore.db /var/lib/sync/app.db
sudo chown syncapp:syncapp /var/lib/sync/app.db

# Replace uploads
sudo rm -rf /var/lib/sync/instance/uploads/ro
sudo tar -xzf /tmp/uploads.tar.gz -C /var/lib/sync/instance/uploads/
sudo chown -R syncapp:syncapp /var/lib/sync/instance

# Start the app
sudo systemctl start sync
```

You can also restore just the uploads from `s3://.../uploads-live/` since `aws s3 sync` keeps it identical to the source.

### Why we don't rely solely on EBS snapshots

EBS snapshots are great for full-disk DR but recover slowly (10s of minutes). The S3 push gives you per-hour granularity and an off-host copy that survives an entire AZ outage. **Use both.**

---

## Future migration to S3 (objects) + RDS (DB)

If RO traffic grows or you want zero-disk-management, swap two functions in `app/main.py`:

| Function | Replace with |
|----------|--------------|
| `save_ro_file(file_storage)` (line ~365) | `boto3.client('s3').upload_fileobj(...)` returning the S3 key as `ro_file_path` |
| `ro_document(entry_id)` (line ~1980) | After permission check, return a redirect to a **15-minute presigned URL** (`s3.generate_presigned_url`) — the S3 GET still requires the signature, so the auth boundary is preserved |

For the DB:
- The schema (`app/main.py:60` area) is already plain SQL — only the column-introspection `pragma table_info` and SQLite-specific `on conflict ... do update set` upserts need Postgres equivalents.
- Use `SYNC_DATABASE_URL=postgresql://user:pw@host/db` and switch the connector in `get_db()` accordingly.
- All other code (`write_history`, `effective_target_for_scope`, etc.) is portable.

---

## Security checklist for AWS

- [ ] `SYNC_DEV_LOGIN=0` in production environment (otherwise the user-id picker is exposed)
- [ ] `SYNC_SECRET_KEY` set to a strong random value (`python -c "import secrets; print(secrets.token_hex(32))"`)
- [ ] HTTPS terminated at ALB / nginx (`Strict-Transport-Security` enabled)
- [ ] `SYNC_BACKUP_BUCKET` has versioning enabled and a 30-day lifecycle to Glacier
- [ ] EBS volumes encrypted (default-encrypt enabled at the account level)
- [ ] IAM role for the EC2 instance permits **only** `s3:PutObject` to the backup bucket — no broader access
- [ ] `client_max_body_size 12M` (or higher) in nginx so 10MB RO uploads don't 413
- [ ] Google OAuth wired (`/auth/google/callback`) before flipping `SYNC_DEV_LOGIN=0` to anything outside dev — the developer task documented in the route's docstring
- [ ] CloudWatch log shipping for `journalctl -u sync` so you can audit access
- [ ] AWS Backup daily plan for the EBS volume, 14-day retention, cross-region copy

---

## Where the app prints what it's using

When you run `python app/main.py` (or `gunicorn` with the same env vars), the startup log includes:

```
[sync] Resolved data paths:
  DATA_DIR       = /var/lib/sync
  DB_PATH        = /var/lib/sync/app.db
  RO_UPLOAD_DIR  = /var/lib/sync/instance/uploads/ro
  TEAM_CONFIG    = /opt/sync/config/team_config.json
  DEV_LOGIN      = disabled
```

Read these before letting users in to confirm the mount is correct.
