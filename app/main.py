from __future__ import annotations

import csv
import io
import json
import sqlite3
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    abort,
    flash,
    g,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent.parent

# All writeable state (SQLite DB, uploaded RO docs) lives under DATA_DIR so AWS
# deploys can mount a persistent volume (EBS / EFS) at one path and have both
# the DB and the uploads land there. Defaults to BASE_DIR for local dev so
# running `python app/main.py` from a fresh checkout still works without env vars.
import os as _os_paths
DATA_DIR = Path(_os_paths.environ.get("SYNC_DATA_DIR", str(BASE_DIR))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(
    _os_paths.environ.get("SYNC_DB_PATH", str(DATA_DIR / "app.db"))
).resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

import os as _os

app = Flask(__name__)
app.config["SECRET_KEY"] = _os.environ.get("SYNC_SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB cap on uploads (RO docs)
# Dev login (the user-id dropdown) is on by default for local testing. In production,
# unset / set to "0" — only the Google OAuth callback should set session["user_id"].
app.config["DEV_LOGIN_ENABLED"] = _os.environ.get("SYNC_DEV_LOGIN", "1") == "1"

# RO documents live OUTSIDE the static tree so they can't be fetched without auth.
# Served through the authenticated `/ro-doc/<entry_id>` route which re-runs the
# same can_view_entry / can_view_nestle gate the dashboards use.
# Defaults to DATA_DIR/instance/uploads/ro; override with SYNC_UPLOAD_DIR on AWS
# to point at an EBS / EFS mount.
RO_UPLOAD_DIR = Path(
    _os_paths.environ.get("SYNC_UPLOAD_DIR", str(DATA_DIR / "instance" / "uploads" / "ro"))
).resolve()
RO_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class User:
    id: int
    name: str
    email: str
    role: str
    region: str | None
    manager_id: int | None
    regional_head_id: int | None
    can_delete_own_entries: bool
    archived: bool = False
    auth_provider: str = "session"
    # Active-access overlay (set when the user has chosen one of their access rows).
    active_access_id: int | None = None
    active_access_label: str | None = None
    regions_extra: list[str] | None = None
    access_count: int = 1


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_: Any) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


ENTRY_COLUMNS_V2: list[tuple[str, str]] = [
    ("confidence_pct", "REAL NOT NULL DEFAULT 0"),
    ("negotiation",    "TEXT"),
    ("is_cancelled",   "INTEGER NOT NULL DEFAULT 0"),
    ("cancelled_at",   "TEXT"),
    ("cancelled_by",   "INTEGER"),
    ("is_deleted",     "INTEGER NOT NULL DEFAULT 0"),
    ("deleted_at",     "TEXT"),
    ("deleted_by",     "INTEGER"),
    # Phase A: RO closure block
    ("deal_status",         "TEXT"),
    ("ro_number",           "TEXT"),
    ("ro_amount_excl_gst",  "REAL"),
    ("gst_value",           "REAL"),
    ("ro_total",            "REAL"),
    ("ro_file_path",        "TEXT"),
    # Phase C: Nestle is a separate book — main numbers exclude it.
    ("book",                "TEXT NOT NULL DEFAULT 'main'"),
    # GST entered as a percentage (e.g. 18). gst_value remains the absolute amount,
    # always derived as excl × pct / 100 at write time.
    ("gst_pct",             "REAL"),
]

# Nestle is a separate ledger; only Rahul makes entries, admins + Rahul can view.
NESTLE_OWNER_EMAIL = "rahul@syncmedia.io"
NESTLE_CLIENT_NAME = "Nestle"

DEAL_STATUSES = ["Pipeline", "Negotiation", "RO Received", "Closed Won", "Closed Lost"]
DEAL_STATUSES_REQUIRE_RO = {"RO Received", "Closed Won"}
RO_ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png"}
RO_MAX_BYTES = 10 * 1024 * 1024  # 10 MB

USER_COLUMNS_V2: list[tuple[str, str]] = [
    ("archived",      "INTEGER NOT NULL DEFAULT 0"),
    ("auth_provider", "TEXT NOT NULL DEFAULT 'session'"),
]


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"pragma table_info({table})").fetchall()}


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            create table if not exists users (
              id integer primary key autoincrement,
              name text not null,
              email text not null unique,
              role text not null check (role in ('salesperson', 'manager', 'regional_head', 'admin')),
              region text,
              manager_id integer references users(id),
              regional_head_id integer references users(id),
              can_delete_own_entries integer not null default 0
            );

            create table if not exists revenue_entries (
              id integer primary key autoincrement,
              client_name text not null,
              client_type text not null check (client_type in ('New', 'Existing')),
              agency_name text,
              campaign_name text,
              assigned_user_id integer not null references users(id),
              manager_id integer references users(id),
              regional_head_id integer references users(id),
              region text,
              quarter text not null check (quarter in ('Q1', 'Q2', 'Q3', 'Q4')),
              entry_date text not null,
              plan_shared integer not null default 0,
              plan_date text,
              plan_value real not null default 0,
              negotiation_stage text,
              pipeline_value real not null default 0,
              ro_date text,
              ro_value real not null default 0,
              status text,
              follow_up_date text,
              remarks text,
              created_by integer not null references users(id),
              updated_by integer not null references users(id),
              created_at text not null default current_timestamp,
              updated_at text not null default current_timestamp
            );
            """
        )
        conn.commit()

        migrate_db(conn)
        backfill_history(conn)
        # Sync the team config (org chart + access matrix) on every startup.
        # Validation errors are logged to stderr; we never crash the app on a bad config.
        try:
            cfg = load_team_config()
            errs = validate_team_config(cfg)
            if errs:
                import sys as _sys
                print("[team_config] validation errors at startup:", errs[:5], file=_sys.stderr)
            else:
                reconcile_team_config(conn, cfg)
        except Exception as exc:
            import sys as _sys
            print(f"[team_config] startup reconcile failed: {exc}", file=_sys.stderr)
    finally:
        conn.close()


def migrate_db(conn: sqlite3.Connection) -> None:
    have = _existing_columns(conn, "revenue_entries")
    for col, decl in ENTRY_COLUMNS_V2:
        if col not in have:
            conn.execute(f"alter table revenue_entries add column {col} {decl}")

    have = _existing_columns(conn, "users")
    for col, decl in USER_COLUMNS_V2:
        if col not in have:
            conn.execute(f"alter table users add column {col} {decl}")

    # Targets schema upgrade: per-user → per-access-label.
    # Detect the legacy schema (presence of user_id, absence of access_label) and drop.
    target_cols = _existing_columns(conn, "targets")
    if target_cols and "user_id" in target_cols and "access_label" not in target_cols:
        conn.execute("drop table targets")

    conn.executescript(
        """
        create table if not exists targets (
          id integer primary key autoincrement,
          access_label text not null,
          quarter text not null check (quarter in ('Q1','Q2','Q3','Q4')),
          fiscal_year integer not null,
          target_value real not null default 0,
          set_by integer not null references users(id),
          set_at text not null default current_timestamp,
          unique(access_label, quarter, fiscal_year)
        );

        create table if not exists revenue_entry_history (
          id integer primary key autoincrement,
          entry_id integer not null,
          version integer not null,
          action text not null check (action in ('create', 'update', 'cancel', 'uncancel', 'delete', 'undelete')),
          actor_id integer not null references users(id),
          actor_role text not null,
          snapshot_json text not null,
          changed_fields_json text,
          occurred_at text not null default current_timestamp
        );
        create index if not exists idx_history_entry_id on revenue_entry_history(entry_id);
        create index if not exists idx_history_occurred_at on revenue_entry_history(occurred_at desc);

        create table if not exists user_access (
          id integer primary key autoincrement,
          user_id integer not null references users(id),
          label text not null,
          role text not null check (role in ('salesperson','manager','regional_head','admin')),
          region text,
          regions_extra_json text,
          manager_id integer references users(id),
          regional_head_id integer references users(id),
          sort_order integer not null default 0,
          is_archived integer not null default 0,
          unique(user_id, label)
        );
        create index if not exists idx_user_access_user_id on user_access(user_id);

        create table if not exists target_history (
          id integer primary key autoincrement,
          access_label text not null,
          quarter text not null check (quarter in ('Q1','Q2','Q3','Q4')),
          fiscal_year integer not null,
          old_value real,
          new_value real not null,
          set_by integer not null references users(id),
          set_at text not null default current_timestamp
        );
        create index if not exists idx_target_history_label on target_history(access_label);
        create index if not exists idx_target_history_set_at on target_history(set_at desc);
        """
    )
    conn.commit()


def seed_demo_users(conn: sqlite3.Connection) -> None:
    count = conn.execute("select count(*) from users").fetchone()[0]
    if count > 0:
        return
    conn.executescript(
        """
        insert into users (id, name, email, role, region, can_delete_own_entries)
        values (1, 'National Admin', 'admin@example.com', 'admin', null, 1);

        insert into users (id, name, email, role, region, can_delete_own_entries)
        values (2, 'North RH', 'rh.north@example.com', 'regional_head', 'North', 0);

        insert into users (id, name, email, role, region, regional_head_id, can_delete_own_entries)
        values (3, 'North Manager', 'manager.north@example.com', 'manager', 'North', 2, 0);

        insert into users (id, name, email, role, region, manager_id, regional_head_id, can_delete_own_entries)
        values
          (4, 'Asha Sales', 'asha@example.com', 'salesperson', 'North', 3, 2, 1),
          (5, 'Vik Sales', 'vik@example.com', 'salesperson', 'North', 3, 2, 0);
        """
    )
    conn.commit()


def backfill_history(conn: sqlite3.Connection) -> None:
    """Synthesize a v=1 'create' snapshot for any pre-existing entry that has no history yet."""
    rows = conn.execute(
        """
        select re.* from revenue_entries re
        left join revenue_entry_history h on h.entry_id = re.id
        where h.id is null
        """
    ).fetchall()
    for r in rows:
        snapshot = {k: r[k] for k in r.keys()}
        conn.execute(
            """
            insert into revenue_entry_history
              (entry_id, version, action, actor_id, actor_role, snapshot_json, changed_fields_json, occurred_at)
            values (?, 1, 'create', ?, 'system', ?, NULL, ?)
            """,
            (
                r["id"],
                r["created_by"],
                json.dumps(snapshot, default=str),
                r["created_at"],
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Domain helpers — pipeline math, history writer, soft-delete row snapshot
# ---------------------------------------------------------------------------

EDITABLE_FIELDS = [
    "client_name", "client_type", "agency_name", "campaign_name",
    "assigned_user_id", "manager_id", "regional_head_id", "region",
    "quarter", "entry_date", "plan_shared", "plan_date", "plan_value",
    "confidence_pct", "negotiation", "pipeline_value",
    "deal_status", "ro_date", "ro_number", "ro_amount_excl_gst",
    "gst_pct", "gst_value", "ro_total", "ro_file_path", "ro_value",
    "status", "follow_up_date", "remarks",
]


def compute_pipeline(plan_value: float, confidence_pct: float) -> float:
    """Pipeline value derives from Plan x Confidence%. Single source of truth."""
    try:
        return round(float(plan_value or 0) * float(confidence_pct or 0) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def compute_ro_total(excl_gst: float, gst_value: float) -> float:
    """RO total = excl-GST amount + GST. Stored alongside the components for fast reads."""
    try:
        return round(float(excl_gst or 0) + float(gst_value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def compute_gst_value(excl_gst: float, gst_pct: float) -> float:
    """Absolute GST = excl × pct/100. Single source of truth for the rate-to-amount derivation."""
    try:
        return round(float(excl_gst or 0) * float(gst_pct or 0) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def normalize_deal_status(raw: str | None) -> str | None:
    val = (raw or "").strip()
    return val if val in DEAL_STATUSES else None


def validate_ro_block(deal_status: str | None, *, ro_number: str | None,
                       ro_amount_excl_gst: float, gst_value: float,
                       has_file: bool) -> list[str]:
    """Return a list of human-readable errors for the RO closure block."""
    errors: list[str] = []
    if deal_status not in DEAL_STATUSES_REQUIRE_RO:
        return errors
    if not (ro_number or "").strip():
        errors.append("RO number is required to mark this deal as " + deal_status + ".")
    if not (float(ro_amount_excl_gst or 0) > 0):
        errors.append("RO amount excluding GST must be greater than 0.")
    if not (float(gst_value or 0) >= 0):
        errors.append("GST value must be 0 or greater.")
    if not has_file:
        errors.append("RO document file is required to close the deal.")
    return errors


def save_ro_file(file_storage) -> tuple[str | None, str | None]:
    """
    Persist the uploaded RO document to app/static/uploads/ro/<uuid>.<ext>.
    Returns (relative_path_or_None, error_or_None).
    """
    if not file_storage or not file_storage.filename:
        return (None, None)
    original = secure_filename(file_storage.filename)
    if not original:
        return (None, "RO file has an invalid name.")
    ext = Path(original).suffix.lower()
    if ext not in RO_ALLOWED_EXT:
        return (None, f"RO file must be one of: {', '.join(sorted(RO_ALLOWED_EXT))}.")

    new_name = f"{uuid.uuid4().hex}{ext}"
    dest = RO_UPLOAD_DIR / new_name
    file_storage.save(dest)
    if dest.stat().st_size > RO_MAX_BYTES:
        dest.unlink(missing_ok=True)
        return (None, f"RO file exceeds {RO_MAX_BYTES // (1024 * 1024)} MB limit.")
    return (new_name, None)  # bare filename — served via /ro-doc/<entry_id>


def _row_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    return {col: row[col] for col in row.keys()}


def write_history(
    conn: sqlite3.Connection,
    *,
    entry_id: int,
    action: str,
    actor: User,
    snapshot: dict[str, Any],
    changed_fields: list[str] | None = None,
) -> None:
    next_v = conn.execute(
        "select coalesce(max(version), 0) + 1 from revenue_entry_history where entry_id = ?",
        (entry_id,),
    ).fetchone()[0]
    conn.execute(
        """
        insert into revenue_entry_history
          (entry_id, version, action, actor_id, actor_role, snapshot_json, changed_fields_json)
        values (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry_id,
            next_v,
            action,
            actor.id,
            actor.role,
            json.dumps(snapshot, default=str),
            json.dumps(changed_fields) if changed_fields is not None else None,
        ),
    )


def diff_editable_fields(old: sqlite3.Row, new: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for f in EDITABLE_FIELDS:
        a = old[f] if f in old.keys() else None
        b = new.get(f)
        if isinstance(a, (int, float)) or isinstance(b, (int, float)):
            try:
                if float(a or 0) != float(b or 0):
                    changed.append(f)
                continue
            except (TypeError, ValueError):
                pass
        if (a or "") != (b or ""):
            changed.append(f)
    return changed


# ---------------------------------------------------------------------------
# Targets — per-salesperson per-quarter, with auto-rollup to manager/RH/admin
# ---------------------------------------------------------------------------

def current_fiscal_year(today: date | None = None) -> int:
    """Calendar-year fiscal calendar for now. Team config can override later."""
    today = today or date.today()
    return today.year


def _descendant_salesperson_ids(user: User) -> list[int]:
    """All salesperson user_ids whose targets roll up under this user."""
    db = get_db()
    if user.role == "salesperson":
        return [user.id]
    if user.role == "manager":
        rows = db.execute("select id from users where role = 'salesperson' and archived = 0").fetchall()
        return [r["id"] for r in rows if is_descendant(user.id, r["id"])]
    if user.role == "regional_head":
        regions = _user_regions(user)
        if not regions:
            return []
        placeholders = ",".join("?" * len(regions))
        rows = db.execute(
            f"select id from users where role = 'salesperson' and region in ({placeholders}) and archived = 0",
            tuple(regions),
        ).fetchall()
        return [r["id"] for r in rows]
    # admin
    rows = db.execute("select id from users where role = 'salesperson' and archived = 0").fetchall()
    return [r["id"] for r in rows]


def labels_in_scope(user: User) -> list[str]:
    """
    Leaf-level access labels (role IN manager/salesperson) whose targets roll up under this user.
    Admin → all leaves. Regional head → leaves whose region is in the user's region(s).
    Manager / salesperson → just their own active access label.
    """
    db = get_db()
    if user.role == "admin":
        rows = db.execute(
            """
            select distinct label
            from user_access
            where role in ('manager', 'salesperson') and is_archived = 0
            """
        ).fetchall()
        return [r["label"] for r in rows]
    if user.role == "regional_head":
        regions = _user_regions(user)
        if not regions:
            return []
        placeholders = ",".join("?" * len(regions))
        rows = db.execute(
            f"""
            select distinct label
            from user_access
            where role in ('manager', 'salesperson')
              and is_archived = 0
              and region in ({placeholders})
            """,
            tuple(regions),
        ).fetchall()
        return [r["label"] for r in rows]
    if user.role in ("manager", "salesperson"):
        return [user.active_access_label] if user.active_access_label else []
    return []


def target_for_scope(user: User, quarters: list[str], fiscal_year: int) -> float:
    """
    Sum of base targets across the access labels in this user's scope, restricted to the given quarters.
    Use effective_target_for_scope() to include carryover from prior ended quarters.
    """
    if not quarters:
        return 0.0
    labels = labels_in_scope(user)
    if not labels:
        return 0.0
    db = get_db()
    placeholders_labels = ",".join("?" * len(labels))
    placeholders_q = ",".join("?" * len(quarters))
    row = db.execute(
        f"""
        select coalesce(sum(target_value), 0) as total
        from targets
        where fiscal_year = ?
          and quarter in ({placeholders_q})
          and access_label in ({placeholders_labels})
        """,
        (fiscal_year, *quarters, *labels),
    ).fetchone()
    return float(row["total"] or 0)


def achieved_for_rows(rows: list[sqlite3.Row]) -> float:
    """RO sum across non-cancelled, non-deleted rows. Uses ro_total when set, else legacy ro_value."""
    total = 0.0
    for r in rows:
        if r["is_cancelled"] or r["is_deleted"]:
            continue
        ro_total = r["ro_total"] if "ro_total" in r.keys() else None
        if ro_total is not None and ro_total > 0:
            total += float(ro_total or 0)
        else:
            total += float(r["ro_value"] or 0)
    return total


# ---------------------------------------------------------------------------
# Fiscal calendar + carryover (Phase B)
# ---------------------------------------------------------------------------

def load_fiscal_calendar(fiscal_year: int | None = None) -> dict[str, dict[str, str]]:
    """
    Read the fiscal calendar from team_config.json. Falls back to a calendar-year layout
    when the config doesn't define one.
    """
    try:
        cfg = load_team_config()
        cal = cfg.get("fiscal_calendar") or {}
        if cal:
            return cal
    except Exception:
        pass
    fy = fiscal_year or current_fiscal_year()
    return {
        "Q1": {"start": f"{fy}-04-01", "end": f"{fy}-06-30"},
        "Q2": {"start": f"{fy}-07-01", "end": f"{fy}-09-30"},
        "Q3": {"start": f"{fy}-10-01", "end": f"{fy}-12-31"},
        "Q4": {"start": f"{fy + 1}-01-01", "end": f"{fy + 1}-03-31"},
    }


def quarter_has_ended(quarter: str, fiscal_year: int, today: date | None = None) -> bool:
    today = today or date.today()
    cal = load_fiscal_calendar(fiscal_year)
    end_str = (cal.get(quarter) or {}).get("end")
    if not end_str:
        return False
    try:
        end_date = date.fromisoformat(end_str)
    except ValueError:
        return False
    return today > end_date


def achieved_for_scope_quarter(user: User, quarter: str) -> float:
    """RO booked for one quarter, scoped to the user's visible entries (cancelled/deleted excluded)."""
    rows = visible_entries(user, include_cancelled=True, include_deleted=False)
    return achieved_for_rows([r for r in rows if r["quarter"] == quarter])


def carryover_for_scope(
    user: User,
    view_quarters: list[str],
    fiscal_year: int,
    today: date | None = None,
) -> tuple[float, list[dict[str, Any]]]:
    """
    Carryover into the current view = sum over (ended quarters NOT in view) of
    max(0, base_target_q - achieved_q). Returns (total, breakdown_per_quarter).
    """
    today = today or date.today()
    if not view_quarters:
        return (0.0, [])
    view_set = set(view_quarters)
    breakdown: list[dict[str, Any]] = []
    total = 0.0
    for q in ("Q1", "Q2", "Q3", "Q4"):
        if q in view_set:
            continue
        if not quarter_has_ended(q, fiscal_year, today):
            continue
        target_q = target_for_scope(user, [q], fiscal_year)
        achieved_q = achieved_for_scope_quarter(user, q)
        shortfall = target_q - achieved_q
        if shortfall > 0:
            breakdown.append({
                "quarter": q,
                "base_target": target_q,
                "achieved": achieved_q,
                "shortfall": shortfall,
            })
            total += shortfall
    return (total, breakdown)


def effective_target_for_scope(
    user: User,
    view_quarters: list[str],
    fiscal_year: int,
    today: date | None = None,
) -> dict[str, Any]:
    """Effective target for the dashboard hero = base + carryover. Returns full breakdown."""
    base = target_for_scope(user, view_quarters, fiscal_year)
    carry, breakdown = carryover_for_scope(user, view_quarters, fiscal_year, today)
    return {
        "base": base,
        "carryover": carry,
        "effective": base + carry,
        "breakdown": breakdown,
    }


# ---------------------------------------------------------------------------
# Phase D — Region and Team (per-leaf-label) breakdowns for the dashboard.
# Achievement is attributed by holder + region (no access_label column on entries
# yet). Each leaf label has a unique (holders, region) tuple in the current matrix,
# so this is unambiguous.
# ---------------------------------------------------------------------------

def team_achieved(label: str, region: str | None, view_quarters: list[str]) -> float:
    """RO booked for a single leaf label across the given quarters (main book only)."""
    if not view_quarters:
        return 0.0
    db = get_db()
    holder_rows = db.execute(
        "select user_id from user_access where label = ? and is_archived = 0",
        (label,),
    ).fetchall()
    holder_ids = [r["user_id"] for r in holder_rows]
    if not holder_ids:
        return 0.0
    placeholders_h = ",".join("?" * len(holder_ids))
    placeholders_q = ",".join("?" * len(view_quarters))
    where = [
        "coalesce(book, 'main') = 'main'",
        "is_cancelled = 0",
        "is_deleted = 0",
        f"assigned_user_id in ({placeholders_h})",
        f"quarter in ({placeholders_q})",
    ]
    params: list[Any] = [*holder_ids, *view_quarters]
    if region:
        where.append("region = ?")
        params.append(region)
    sql = (
        "select coalesce(sum(coalesce(ro_total, ro_value, 0)), 0) as t "
        "from revenue_entries where " + " and ".join(where)
    )
    return float(db.execute(sql, tuple(params)).fetchone()["t"] or 0)


def team_breakdown(user: User, view_quarters: list[str], fiscal_year: int) -> list[dict[str, Any]]:
    """
    Per-leaf-label rows: {label, region, holders, target, achieved, pct}.
    Admin sees all leaves; RH sees leaves in region(s); manager / salesperson get [].
    """
    if user.role not in ("admin", "regional_head"):
        return []
    if not view_quarters:
        return []
    db = get_db()
    if user.role == "admin":
        meta_rows = db.execute(
            """
            select ua.label as label, ua.region as region,
                   group_concat(u.name, ' · ') as holders
            from user_access ua
            join users u on u.id = ua.user_id
            where ua.role in ('manager', 'salesperson') and ua.is_archived = 0 and u.archived = 0
            group by ua.label, ua.region
            order by ua.region is null, ua.region, ua.label
            """
        ).fetchall()
    else:
        regions = _user_regions(user)
        if not regions:
            return []
        placeholders = ",".join("?" * len(regions))
        meta_rows = db.execute(
            f"""
            select ua.label as label, ua.region as region,
                   group_concat(u.name, ' · ') as holders
            from user_access ua
            join users u on u.id = ua.user_id
            where ua.role in ('manager', 'salesperson') and ua.is_archived = 0 and u.archived = 0
              and ua.region in ({placeholders})
            group by ua.label, ua.region
            order by ua.region, ua.label
            """,
            tuple(regions),
        ).fetchall()

    placeholders_q = ",".join("?" * len(view_quarters))
    out: list[dict[str, Any]] = []
    for m in meta_rows:
        target_row = db.execute(
            f"""
            select coalesce(sum(target_value), 0) as t
            from targets
            where access_label = ? and fiscal_year = ? and quarter in ({placeholders_q})
            """,
            (m["label"], fiscal_year, *view_quarters),
        ).fetchone()
        target = float(target_row["t"] or 0)
        achieved = team_achieved(m["label"], m["region"], view_quarters)
        out.append({
            "label": m["label"],
            "region": m["region"],
            "holders": m["holders"] or "—",
            "target": target,
            "achieved": achieved,
            "delta": achieved - target,
            "pct": (achieved / target * 100) if target else 0,
        })
    return out


def region_breakdown(user: User, view_quarters: list[str], fiscal_year: int) -> list[dict[str, Any]]:
    """
    Per-region rows: {region, target, achieved, pct}.
    Admin: all distinct regions across leaf labels. RH: their _user_regions(user).
    """
    if user.role not in ("admin", "regional_head"):
        return []
    if not view_quarters:
        return []
    db = get_db()
    if user.role == "admin":
        region_rows = db.execute(
            """
            select distinct region from user_access
            where region is not null and role in ('manager', 'salesperson') and is_archived = 0
            order by region
            """
        ).fetchall()
        regions = [r["region"] for r in region_rows]
    else:
        regions = sorted(_user_regions(user))
    if not regions:
        return []

    placeholders_q = ",".join("?" * len(view_quarters))
    out: list[dict[str, Any]] = []
    for region in regions:
        # Target = sum of leaf-label targets where label belongs to this region
        target_row = db.execute(
            f"""
            select coalesce(sum(t.target_value), 0) as total
            from targets t
            where t.fiscal_year = ?
              and t.quarter in ({placeholders_q})
              and t.access_label in (
                select distinct ua.label from user_access ua
                where ua.role in ('manager', 'salesperson')
                  and ua.is_archived = 0 and ua.region = ?
              )
            """,
            (fiscal_year, *view_quarters, region),
        ).fetchone()
        target = float(target_row["total"] or 0)

        # Achieved = sum RO of active main entries in this region & quarters
        ach_row = db.execute(
            f"""
            select coalesce(sum(coalesce(ro_total, ro_value, 0)), 0) as total
            from revenue_entries
            where coalesce(book, 'main') = 'main'
              and is_cancelled = 0 and is_deleted = 0
              and region = ? and quarter in ({placeholders_q})
            """,
            (region, *view_quarters),
        ).fetchone()
        achieved = float(ach_row["total"] or 0)

        out.append({
            "region": region,
            "target": target,
            "achieved": achieved,
            "delta": achieved - target,
            "pct": (achieved / target * 100) if target else 0,
        })
    return out


def _build_user(row: sqlite3.Row, *, access_overlay: sqlite3.Row | None, access_count: int) -> User:
    cols = row.keys()
    extras: list[str] | None = None
    role = row["role"]
    region = row["region"]
    manager_id = row["manager_id"]
    regional_head_id = row["regional_head_id"]
    active_id = None
    active_label = None
    if access_overlay is not None:
        role = access_overlay["role"]
        region = access_overlay["region"]
        manager_id = access_overlay["manager_id"]
        regional_head_id = access_overlay["regional_head_id"]
        active_id = access_overlay["id"]
        active_label = access_overlay["label"]
        extras_raw = access_overlay["regions_extra_json"]
        if extras_raw:
            try:
                v = json.loads(extras_raw)
                if isinstance(v, list):
                    extras = [str(x) for x in v]
            except (TypeError, json.JSONDecodeError):
                extras = None
    return User(
        id=row["id"],
        name=row["name"],
        email=row["email"],
        role=role,
        region=region,
        manager_id=manager_id,
        regional_head_id=regional_head_id,
        can_delete_own_entries=bool(row["can_delete_own_entries"]),
        archived=bool(row["archived"]) if "archived" in cols else False,
        auth_provider=(row["auth_provider"] if "auth_provider" in cols else "session") or "session",
        active_access_id=active_id,
        active_access_label=active_label,
        regions_extra=extras,
        access_count=access_count,
    )


def get_user_accesses(user_id: int) -> list[sqlite3.Row]:
    """Active access rows for this user, ordered for the chooser screen."""
    return get_db().execute(
        "select * from user_access where user_id = ? and is_archived = 0 order by sort_order, id",
        (user_id,),
    ).fetchall()


def get_user(user_id: int, *, access_id: int | None = None) -> User | None:
    """Load a user, optionally with an explicit access overlay. Pure DB read — no session touch."""
    row = get_db().execute("select * from users where id = ?", (user_id,)).fetchone()
    if not row:
        return None
    accesses = get_user_accesses(user_id)
    overlay = None
    if access_id is not None:
        for a in accesses:
            if a["id"] == access_id:
                overlay = a
                break
    elif len(accesses) == 1:
        overlay = accesses[0]
    return _build_user(row, access_overlay=overlay, access_count=len(accesses))


def resolve_user_by_email(email: str) -> User | None:
    """Identity seam used by the future Google OAuth callback. Looks up an active user by email."""
    if not email:
        return None
    row = get_db().execute(
        "select id from users where lower(email) = lower(?) and archived = 0",
        (email.strip(),),
    ).fetchone()
    if not row:
        return None
    return get_user(int(row["id"]))


def current_user() -> User | None:
    uid = session.get("user_id")
    if not uid:
        return None
    aid = session.get("active_access_id")
    user = get_user(int(uid), access_id=int(aid) if aid else None)
    # Fail closed on archived accounts — clears any stale active session at the next request.
    if user is None or user.archived:
        return None
    return user


def needs_access_choice(user: User) -> bool:
    """True when a logged-in user has multiple accesses and hasn't picked one yet."""
    return user.access_count > 1 and user.active_access_id is None


def require_user() -> User:
    user = current_user()
    if not user:
        abort(401)
    return user


def is_descendant(manager_id: int, user_id: int) -> bool:
    db = get_db()
    curr = user_id
    while curr is not None:
        if curr == manager_id:
            return True
        row = db.execute("select manager_id from users where id = ?", (curr,)).fetchone()
        curr = row["manager_id"] if row else None
    return False


def _user_regions(user: User) -> list[str]:
    """All regions the user can see under the current active access."""
    out: list[str] = []
    if user.region:
        out.append(user.region)
    if user.regions_extra:
        out.extend(r for r in user.regions_extra if r and r not in out)
    return out


def can_view_entry(user: User, entry: sqlite3.Row) -> bool:
    if user.role == "admin":
        return True
    if user.role == "regional_head":
        regions = _user_regions(user)
        return (entry["region"] or "") in regions if regions else False
    if user.role == "manager":
        return is_descendant(user.id, entry["assigned_user_id"])
    return entry["assigned_user_id"] == user.id


def can_edit_entry(user: User, entry: sqlite3.Row) -> bool:
    if user.role == "admin":
        return True
    if user.role == "regional_head":
        regions = _user_regions(user)
        return (entry["region"] or "") in regions if regions else False
    if user.role == "manager":
        return is_descendant(user.id, entry["assigned_user_id"])
    return entry["assigned_user_id"] == user.id


def can_cancel_entry(user: User, entry: sqlite3.Row) -> bool:
    """Cancel = anyone with edit access. Cancelled rows stay visible but exit the funnel."""
    return can_edit_entry(user, entry)


def can_delete_entry(user: User, entry: sqlite3.Row) -> bool:
    """Soft-delete is admin-only by policy. Data is preserved permanently in revenue_entry_history."""
    return user.role == "admin"


def can_view_nestle(user: User) -> bool:
    """Nestle ledger is visible only to Rahul (the owner) and national admins."""
    if user.role == "admin":
        return True
    return (user.email or "").lower() == NESTLE_OWNER_EMAIL.lower()


def can_edit_nestle(user: User) -> bool:
    """Only Rahul creates/edits Nestle entries. Admins can view + delete (data preservation), not edit."""
    return (user.email or "").lower() == NESTLE_OWNER_EMAIL.lower()


def require_nestle_view() -> User:
    user = require_user()
    if not can_view_nestle(user):
        abort(403)
    return user


def get_assignable_users(user: User) -> list[sqlite3.Row]:
    db = get_db()
    if user.role == "admin":
        return db.execute("select * from users where role = 'salesperson' and archived = 0 order by name").fetchall()
    if user.role == "regional_head":
        regions = _user_regions(user)
        if not regions:
            return []
        placeholders = ",".join("?" * len(regions))
        return db.execute(
            f"select * from users where role = 'salesperson' and region in ({placeholders}) and archived = 0 order by name",
            tuple(regions),
        ).fetchall()
    if user.role == "manager":
        all_sales = db.execute("select * from users where role = 'salesperson' and archived = 0 order by name").fetchall()
        return [u for u in all_sales if is_descendant(user.id, u["id"])]
    return db.execute("select * from users where id = ?", (user.id,)).fetchall()


def visible_entries(
    user: User,
    *,
    include_cancelled: bool = True,
    include_deleted: bool | None = None,
    book: str = "main",
) -> list[sqlite3.Row]:
    """
    All rows the user can see. Cancelled rows are shown by default; deleted rows are hidden
    from non-admin. The `book` parameter isolates Nestle from the main ledger.
    """
    if include_deleted is None:
        include_deleted = False
    db = get_db()
    rows = db.execute(
        """
        select re.*, u.name as salesperson_name, m.name as manager_name, rh.name as regional_head_name
        from revenue_entries re
        join users u on u.id = re.assigned_user_id
        left join users m on m.id = re.manager_id
        left join users rh on rh.id = re.regional_head_id
        where coalesce(re.book, 'main') = ?
        order by re.entry_date desc, re.id desc
        """,
        (book,),
    ).fetchall()

    # Nestle entries skip the hierarchy/region check — they're a separate ledger
    # gated by can_view_nestle(). Anyone able to call visible_entries(book='nestle')
    # has already passed that gate (via require_nestle_view()).
    if book == "main":
        out = [r for r in rows if can_view_entry(user, r)]
    else:
        out = list(rows)
    if not include_cancelled:
        out = [r for r in out if not r["is_cancelled"]]
    if not include_deleted:
        out = [r for r in out if not r["is_deleted"]]
    return out


def is_recent_new(row: sqlite3.Row, *, now: datetime | None = None) -> bool:
    """True when the entry was created in the last 7 days. Used for the green 'NEW' highlight."""
    created_raw = row["created_at"]
    if not created_raw:
        return False
    now = now or datetime.utcnow()
    try:
        created = datetime.fromisoformat(str(created_raw).replace("Z", ""))
    except ValueError:
        return False
    return (now - created) <= timedelta(days=7)


FILTER_KEYS = ("quarter", "region", "client_type", "salesperson_id", "status_filter")


def parse_filter_args(raw: Any) -> dict[str, Any]:
    """
    Normalize either a Werkzeug ImmutableMultiDict or a plain dict into the canonical
    filter shape: quarters as list, status_filter default = 'active', everything else strings.
    """
    if hasattr(raw, "getlist"):
        quarters = [q for q in raw.getlist("quarter") if q]
        single = {k: (raw.get(k) or "") for k in raw.keys() if k != "quarter"}
    else:
        raw = raw or {}
        q = raw.get("quarter")
        if isinstance(q, list):
            quarters = [x for x in q if x]
        elif q:
            quarters = [q]
        else:
            quarters = []
        single = {k: v for k, v in raw.items() if k != "quarter"}

    valid_q = [q for q in quarters if q in ("Q1", "Q2", "Q3", "Q4")]
    status = (single.get("status_filter") or "active").strip().lower()
    if status not in ("active", "cancelled", "all"):
        status = "active"
    return {
        "quarters": valid_q,
        "region": (single.get("region") or "").strip(),
        "client_type": (single.get("client_type") or "").strip(),
        "salesperson_id": (single.get("salesperson_id") or "").strip(),
        "status_filter": status,
    }


def apply_filters(rows: list[sqlite3.Row], args: dict[str, Any]) -> list[sqlite3.Row]:
    quarters = args.get("quarters") or []
    region = args.get("region") or ""
    client_type = args.get("client_type") or ""
    salesperson_raw = args.get("salesperson_id") or ""
    status_filter = args.get("status_filter") or "active"

    out = rows
    if quarters:
        qset = set(quarters)
        out = [r for r in out if r["quarter"] in qset]
    if region:
        out = [r for r in out if (r["region"] or "") == region]
    if client_type:
        out = [r for r in out if r["client_type"] == client_type]
    if salesperson_raw:
        try:
            sid = int(salesperson_raw)
            out = [r for r in out if r["assigned_user_id"] == sid]
        except ValueError:
            pass
    if status_filter == "active":
        out = [r for r in out if not r["is_cancelled"]]
    elif status_filter == "cancelled":
        out = [r for r in out if r["is_cancelled"]]
    return out


def active_filter_count(args: dict[str, Any]) -> int:
    n = 0
    if args.get("quarters"):
        n += 1
    if args.get("region"):
        n += 1
    if args.get("client_type"):
        n += 1
    if args.get("salesperson_id"):
        n += 1
    if (args.get("status_filter") or "active") != "active":
        n += 1
    return n


def filter_options(user: User, all_rows: list[sqlite3.Row]) -> dict[str, list[dict[str, Any]]]:
    """Filter options scoped to what the role can legitimately filter on."""
    quarters = ["Q1", "Q2", "Q3", "Q4"]
    client_types = ["New", "Existing"]

    region_set = sorted({(r["region"] or "") for r in all_rows if r["region"]})
    salesperson_set = sorted(
        {(r["assigned_user_id"], r["salesperson_name"]) for r in all_rows if r["salesperson_name"]},
        key=lambda x: x[1],
    )

    show_region = user.role in ("admin", "regional_head")
    show_salesperson = user.role in ("admin", "regional_head", "manager")

    return {
        "quarters": quarters,
        "client_types": client_types,
        "regions": region_set if show_region else [],
        "salespeople": [{"id": sid, "name": name} for sid, name in salesperson_set] if show_salesperson else [],
        "show_region": show_region,
        "show_salesperson": show_salesperson,
    }


def parse_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except ValueError:
        return default


def rollup_from_rows(rows: list[sqlite3.Row]) -> dict[str, Any]:
    plan = sum(r["plan_value"] or 0 for r in rows)
    pipeline = sum(r["pipeline_value"] or 0 for r in rows)
    ro = sum(r["ro_value"] or 0 for r in rows)
    new_revenue = sum((r["ro_value"] or 0) for r in rows if r["client_type"] == "New")
    existing_revenue = sum((r["ro_value"] or 0) for r in rows if r["client_type"] == "Existing")
    pending_followups = sum(1 for r in rows if r["follow_up_date"] and (r["status"] or "").lower() != "closed")
    conversion_pct = (ro / pipeline * 100) if pipeline else 0

    return {
        "entries": len(rows),
        "plan_value": plan,
        "pipeline_value": pipeline,
        "ro_value": ro,
        "new_revenue": new_revenue,
        "existing_revenue": existing_revenue,
        "pending_followups": pending_followups,
        "conversion_pct": conversion_pct,
    }


def rollup(user: User) -> dict[str, Any]:
    return rollup_from_rows(visible_entries(user))


def aggregate_rows(rows: list[sqlite3.Row], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "entries": 0,
            "plan_value": 0.0,
            "pipeline_value": 0.0,
            "ro_value": 0.0,
            "new_revenue": 0.0,
            "existing_revenue": 0.0,
            "pending_followups": 0,
        }
    )
    for row in rows:
        label = row[key] if row[key] else "Unassigned"
        bucket = grouped[str(label)]
        bucket["entries"] += 1
        bucket["plan_value"] += row["plan_value"] or 0
        bucket["pipeline_value"] += row["pipeline_value"] or 0
        bucket["ro_value"] += row["ro_value"] or 0
        if row["client_type"] == "New":
            bucket["new_revenue"] += row["ro_value"] or 0
        else:
            bucket["existing_revenue"] += row["ro_value"] or 0
        if row["follow_up_date"] and (row["status"] or "").lower() != "closed":
            bucket["pending_followups"] += 1

    result: list[dict[str, Any]] = []
    for label, metrics in grouped.items():
        conversion_pct = (metrics["ro_value"] / metrics["pipeline_value"] * 100) if metrics["pipeline_value"] else 0
        result.append({"label": label, "conversion_pct": conversion_pct, **metrics})
    return sorted(result, key=lambda item: item["ro_value"], reverse=True)


def quarter_chart_data(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    quarters = ["Q1", "Q2", "Q3", "Q4"]
    buckets = {q: {"plan": 0.0, "pipeline": 0.0, "ro": 0.0} for q in quarters}
    for r in rows:
        q = r["quarter"]
        if q in buckets:
            buckets[q]["plan"] += r["plan_value"] or 0
            buckets[q]["pipeline"] += r["pipeline_value"] or 0
            buckets[q]["ro"] += r["ro_value"] or 0
    max_value = max(
        (max(b["plan"], b["pipeline"], b["ro"]) for b in buckets.values()),
        default=0,
    )
    out = []
    for q in quarters:
        b = buckets[q]
        out.append({
            "label": q,
            "plan": b["plan"],
            "pipeline": b["pipeline"],
            "ro": b["ro"],
            "plan_pct": (b["plan"] / max_value * 100) if max_value else 0,
            "pipeline_pct": (b["pipeline"] / max_value * 100) if max_value else 0,
            "ro_pct": (b["ro"] / max_value * 100) if max_value else 0,
        })
    return out


def breakdown_chart_data(rows: list[sqlite3.Row], key: str, top_n: int = 6) -> list[dict[str, Any]]:
    grouped: dict[str, float] = defaultdict(float)
    for r in rows:
        label = r[key] if r[key] else "Unassigned"
        grouped[str(label)] += r["ro_value"] or 0
    items = sorted(grouped.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    max_value = items[0][1] if items and items[0][1] > 0 else 0
    return [
        {"label": label, "value": value, "pct": (value / max_value * 100) if max_value else 0}
        for label, value in items
    ]


def compute_summaries(rows: list[sqlite3.Row]) -> dict[str, Any]:
    if not rows:
        return {"top_performer": None, "biggest_deal": None, "best_quarter": None}

    by_salesperson: dict[str, float] = defaultdict(float)
    for r in rows:
        by_salesperson[r["salesperson_name"] or "Unassigned"] += r["ro_value"] or 0
    top_name, top_value = max(by_salesperson.items(), key=lambda kv: kv[1])
    top_performer = (
        {"name": top_name, "value": top_value} if top_value > 0 else None
    )

    biggest = max(rows, key=lambda r: (r["ro_value"] or 0))
    biggest_deal = (
        {
            "client": biggest["client_name"],
            "salesperson": biggest["salesperson_name"],
            "value": biggest["ro_value"] or 0,
            "client_type": biggest["client_type"],
            "quarter": biggest["quarter"],
        }
        if (biggest["ro_value"] or 0) > 0
        else None
    )

    by_quarter: dict[str, dict[str, float]] = defaultdict(lambda: {"pipeline": 0.0, "ro": 0.0})
    for r in rows:
        by_quarter[r["quarter"]]["pipeline"] += r["pipeline_value"] or 0
        by_quarter[r["quarter"]]["ro"] += r["ro_value"] or 0
    quarter_scores = []
    for q, vals in by_quarter.items():
        if vals["pipeline"] > 0:
            quarter_scores.append({
                "label": q,
                "conversion_pct": vals["ro"] / vals["pipeline"] * 100,
                "ro": vals["ro"],
                "pipeline": vals["pipeline"],
            })
    best_quarter = (
        max(quarter_scores, key=lambda x: x["conversion_pct"]) if quarter_scores else None
    )

    return {
        "top_performer": top_performer,
        "biggest_deal": biggest_deal,
        "best_quarter": best_quarter,
    }


def client_mix(rows: list[sqlite3.Row]) -> dict[str, Any]:
    new_rev = sum((r["ro_value"] or 0) for r in rows if r["client_type"] == "New")
    existing_rev = sum((r["ro_value"] or 0) for r in rows if r["client_type"] == "Existing")
    total = new_rev + existing_rev
    return {
        "new_value": new_rev,
        "existing_value": existing_rev,
        "new_pct": (new_rev / total * 100) if total else 0,
        "existing_pct": (existing_rev / total * 100) if total else 0,
    }


def dashboard_context(user: User, raw_args: Any = None) -> dict[str, Any]:
    args = parse_filter_args(raw_args)
    all_rows = visible_entries(user)
    rows = apply_filters(all_rows, args)

    metrics = rollup_from_rows(rows)
    quarters = quarter_chart_data(rows)
    recent_rows = rows[:8]

    view_config: dict[str, dict[str, Any]] = {
        "salesperson": {
            "title": "Individual Dashboard",
            "subtitle": "Your personal performance and client pipeline.",
        },
        "manager": {
            "title": "Manager Dashboard",
            "subtitle": "Team performance for your reporting salespeople.",
        },
        "regional_head": {
            "title": "Regional Dashboard",
            "subtitle": "Regional roll-up with manager and salesperson breakouts.",
        },
        "admin": {
            "title": "India Admin Dashboard",
            "subtitle": "National roll-up across regions, managers, and salespeople.",
        },
    }
    config = view_config[user.role]

    options = filter_options(user, all_rows)
    current_filters = {
        "quarters": args["quarters"],
        "region": args["region"],
        "client_type": args["client_type"],
        "salesperson_id": args["salesperson_id"],
        "status_filter": args["status_filter"],
    }

    fy = current_fiscal_year()
    quarters_for_target = args["quarters"] if args["quarters"] else ["Q1", "Q2", "Q3", "Q4"]
    target_breakdown = effective_target_for_scope(user, quarters_for_target, fy)
    target_value = target_breakdown["effective"]
    target_base = target_breakdown["base"]
    target_carryover = target_breakdown["carryover"]
    target_carryover_breakdown = target_breakdown["breakdown"]
    achieved_value = achieved_for_rows(rows)
    pct_achieved = (achieved_value / target_value * 100) if target_value else 0
    cancelled_visible = sum(1 for r in all_rows if r["is_cancelled"])

    # Phase D: role-stratified breakdowns
    region_rows = region_breakdown(user, quarters_for_target, fy)
    team_rows = team_breakdown(user, quarters_for_target, fy)
    show_region_breakdown = user.role == "admin"
    show_team_breakdown = user.role in ("admin", "regional_head")

    return {
        "metrics": metrics,
        "quarters": quarters,
        "recent_rows": recent_rows,
        "dashboard_title": config["title"],
        "dashboard_subtitle": config["subtitle"],
        "filter_options": options,
        "current_filters": current_filters,
        "active_filter_count": active_filter_count(args),
        "total_visible": len(all_rows),
        "filtered_visible": len(rows),
        "target_value": target_value,
        "target_base": target_base,
        "target_carryover": target_carryover,
        "target_carryover_breakdown": target_carryover_breakdown,
        "achieved_value": achieved_value,
        "pct_achieved": pct_achieved,
        "fiscal_year": fy,
        "cancelled_count": cancelled_visible,
        "region_rows": region_rows,
        "team_rows": team_rows,
        "show_region_breakdown": show_region_breakdown,
        "show_team_breakdown": show_team_breakdown,
    }


@app.route("/")
def index() -> str:
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login() -> str:
    db = get_db()
    dev_login = app.config.get("DEV_LOGIN_ENABLED", False)

    if request.method == "POST":
        # POST is *only* the dev-login path. Real production logins go through
        # /auth/google/callback, which sets session["user_id"] directly.
        if not dev_login:
            abort(403)
        session.clear()
        try:
            target_id = int(request.form["user_id"])
        except (KeyError, ValueError):
            abort(400)
        # Refuse to impersonate an archived user.
        target_row = db.execute(
            "select id from users where id = ? and archived = 0", (target_id,)
        ).fetchone()
        if target_row is None:
            abort(404)
        session["user_id"] = int(target_row["id"])
        accesses = get_user_accesses(int(session["user_id"]))
        if len(accesses) == 1:
            session["active_access_id"] = int(accesses[0]["id"])
            return redirect(url_for("dashboard"))
        return redirect(url_for("select_access"))

    users = []
    if dev_login:
        users = db.execute(
            """
            select u.id, u.name, u.email, u.auth_provider,
                   (select count(*) from user_access ua where ua.user_id = u.id and ua.is_archived = 0) as access_count
            from users u
            where u.archived = 0
            order by u.auth_provider asc, u.name asc
            """
        ).fetchall()
    return render_template("login.html", users=users, dev_login=dev_login)


@app.route("/select-access", methods=["GET", "POST"])
def select_access() -> Any:
    uid = session.get("user_id")
    if not uid:
        return redirect(url_for("login"))
    accesses = get_user_accesses(int(uid))
    if not accesses:
        flash("This account has no access roles assigned. Contact a national admin.")
        session.clear()
        return redirect(url_for("login"))

    if request.method == "POST":
        try:
            chosen = int(request.form["access_id"])
        except (KeyError, ValueError):
            abort(400)
        if not any(a["id"] == chosen for a in accesses):
            abort(403)
        session["active_access_id"] = chosen
        return redirect(url_for("dashboard"))

    user_row = get_db().execute("select id, name, email from users where id = ?", (uid,)).fetchone()
    return render_template(
        "select_access.html",
        user_row=user_row,
        accesses=accesses,
    )


@app.route("/switch-role")
def switch_role() -> Any:
    """Drop the active access selection and go back to the chooser."""
    if not session.get("user_id"):
        return redirect(url_for("login"))
    session.pop("active_access_id", None)
    return redirect(url_for("select_access"))


@app.before_request
def _guard_access_choice() -> Any:
    """If the user is logged in but hasn't picked an access yet, route them to the chooser."""
    # Allowlist of endpoints that are reachable without an active access
    open_endpoints = {
        "static", "login", "logout", "select_access", "auth_google_start", "auth_google_callback",
    }
    endpoint = request.endpoint or ""
    if endpoint in open_endpoints:
        return None
    uid = session.get("user_id")
    if not uid:
        return None  # require_user() inside the route will handle the abort
    # Reject archived users immediately — clears their session and bounces to login.
    user_row = get_db().execute(
        "select archived from users where id = ?", (uid,)
    ).fetchone()
    if user_row is None or user_row["archived"]:
        session.clear()
        return redirect(url_for("login"))
    accesses = get_user_accesses(int(uid))
    if not accesses:
        # User exists but has no active access (e.g., all archived) — bounce them out.
        session.clear()
        return redirect(url_for("login"))
    if session.get("active_access_id") is None:
        if len(accesses) == 1:
            session["active_access_id"] = int(accesses[0]["id"])
        else:
            return redirect(url_for("select_access"))
    return None


@app.route("/logout")
def logout() -> Any:
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Google OAuth provision — STUBS for the developer to implement.
#
# When wired:
#   - ALLOWED_EMAIL_DOMAIN env var (e.g. "sync.in") gates which @-domain
#     can sign in; reject everyone else.
#   - On successful Google callback, do:
#         email = google_userinfo['email']
#         if not email.endswith('@' + ALLOWED_EMAIL_DOMAIN): abort(403)
#         user = resolve_user_by_email(email)
#         if not user: abort(403)  # not in the team_config
#         session['user_id'] = user.id
#         return redirect(url_for('dashboard'))
#
# Required env vars (keep them out of the repo):
#   GOOGLE_OAUTH_CLIENT_ID
#   GOOGLE_OAUTH_CLIENT_SECRET
#   GOOGLE_OAUTH_REDIRECT_URI   # e.g. https://app.example.com/auth/google/callback
#   ALLOWED_EMAIL_DOMAIN        # e.g. "sync.in"
#
# Suggested library: authlib (`pip install authlib`).
# The identity-resolution seam is `resolve_user_by_email(email)` — already
# implemented above.
# ---------------------------------------------------------------------------

_AUTH_STUB_BODY = (
    "<h1>Google Sign-in placeholder</h1>"
    "<p>This route is a stub. The developer should wire Google OAuth here.</p>"
    "<p>Required env vars: GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, "
    "GOOGLE_OAUTH_REDIRECT_URI, ALLOWED_EMAIL_DOMAIN.</p>"
    "<p>Identity resolution helper: <code>resolve_user_by_email(email)</code>. "
    "On success, set <code>session['user_id'] = user.id</code> and redirect to /dashboard.</p>"
)


@app.route("/auth/google/start")
def auth_google_start() -> Any:
    """Stub: redirect to Google's OAuth consent. Implementation pending."""
    response = make_response(_AUTH_STUB_BODY, 501)
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    return response


@app.route("/auth/google/callback")
def auth_google_callback() -> Any:
    """
    Stub: exchange Google's auth code for an ID token, validate the email domain,
    resolve to a local User via resolve_user_by_email(), then set session['user_id'].
    """
    response = make_response(_AUTH_STUB_BODY, 501)
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    return response


@app.route("/dashboard")
def dashboard() -> str:
    user = require_user()
    ctx = dashboard_context(user, request.args)
    return render_template("dashboard.html", user=user, **ctx)


# ---------------------------------------------------------------------------
# CSV export — main + Nestle. Honors the same filters as the entries page.
# ---------------------------------------------------------------------------

EXPORT_COLUMNS: list[tuple[str, str]] = [
    ("id",                 "ID"),
    ("book",               "Book"),
    ("client_name",        "Client"),
    ("client_type",        "Client type"),
    ("agency_name",        "Agency"),
    ("campaign_name",      "Campaign"),
    ("salesperson_name",   "Salesperson"),
    ("manager_name",       "Manager"),
    ("regional_head_name", "Regional head"),
    ("region",             "Region"),
    ("quarter",            "Quarter"),
    ("entry_date",         "Entry date"),
    ("plan_shared",        "Plan shared"),
    ("plan_date",          "Plan date"),
    ("plan_value",         "Plan value"),
    ("confidence_pct",     "Confidence %"),
    ("pipeline_value",     "Pipeline value"),
    ("negotiation",        "Negotiation"),
    ("deal_status",        "Deal status"),
    ("ro_date",            "RO date"),
    ("ro_number",          "RO number"),
    ("ro_amount_excl_gst", "RO excl GST"),
    ("gst_pct",            "GST %"),
    ("gst_value",          "GST"),
    ("ro_total",           "RO total"),
    ("ro_file_path",       "RO file"),
    ("status",             "Legacy status"),
    ("follow_up_date",     "Follow-up date"),
    ("remarks",            "Remarks"),
    ("is_cancelled",       "Cancelled?"),
    ("cancelled_at",       "Cancelled at"),
    ("is_deleted",         "Deleted?"),
    ("deleted_at",         "Deleted at"),
    ("created_at",         "Created at"),
    ("updated_at",         "Updated at"),
]


def _csv_value(row: sqlite3.Row, key: str) -> Any:
    """Coerce a row cell into a CSV-safe value (booleans, blanks, paths)."""
    if key not in row.keys():
        return ""
    val = row[key]
    if val is None:
        return ""
    if key in ("is_cancelled", "is_deleted", "plan_shared"):
        return "yes" if val else "no"
    return val


def write_entries_csv(rows: list[sqlite3.Row]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _, label in EXPORT_COLUMNS])
    for r in rows:
        writer.writerow([_csv_value(r, key) for key, _ in EXPORT_COLUMNS])
    return buf.getvalue()


def _csv_response(content: str, filename: str) -> Any:
    response = make_response(content)
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _csv_filename(book: str) -> str:
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return f"sync-{book}-entries-{stamp}.csv"


@app.route("/entries/export.csv")
def entries_export_csv() -> Any:
    user = require_user()
    args = parse_filter_args(request.args)
    all_rows = visible_entries(user, include_deleted=(user.role == "admin"))
    rows = apply_filters(all_rows, args)
    return _csv_response(write_entries_csv(rows), _csv_filename("main"))


@app.route("/nestle/entries/export.csv")
def nestle_entries_export_csv() -> Any:
    user = require_nestle_view()
    args = parse_filter_args(request.args)
    all_rows = visible_entries(user, book="nestle", include_deleted=(user.role == "admin"))
    rows = apply_filters(all_rows, args)
    return _csv_response(write_entries_csv(rows), _csv_filename("nestle"))


@app.route("/entries")
def entries() -> str:
    user = require_user()
    args = parse_filter_args(request.args)
    all_rows = visible_entries(user, include_deleted=(user.role == "admin"))
    rows = apply_filters(all_rows, args)

    options = filter_options(user, all_rows)
    current_filters = {
        "quarters": args["quarters"],
        "region": args["region"],
        "client_type": args["client_type"],
        "salesperson_id": args["salesperson_id"],
        "status_filter": args["status_filter"],
    }
    recent_ids = {r["id"]: True for r in rows if is_recent_new(r)}
    return render_template(
        "entries.html",
        user=user,
        rows=rows,
        recent_ids=recent_ids,
        filter_options=options,
        current_filters=current_filters,
        active_filter_count=active_filter_count(args),
        total_count=len(all_rows),
        filtered_count=len(rows),
        cancelled_count=sum(1 for r in all_rows if r["is_cancelled"]),
    )


def _normalize_negotiation(raw: str | None) -> str | None:
    val = (raw or "").strip()
    return val if val in ("Yes", "No") else None


def _gather_entry_form_values(form, user: User, owner: sqlite3.Row) -> dict[str, Any]:
    """Pull every editable entry field out of the POSTed form into a single dict."""
    plan_v = parse_float(form.get("plan_value", "0"))
    conf_v = parse_float(form.get("confidence_pct", "0"))
    excl_v = parse_float(form.get("ro_amount_excl_gst", "0"))
    # GST is entered as a percentage (e.g. 18 = 18%). Absolute GST is always derived.
    gst_pct_v = parse_float(form.get("gst_pct", "0"))
    gst_v = compute_gst_value(excl_v, gst_pct_v)
    return {
        "client_name": form["client_name"],
        "client_type": form["client_type"],
        "agency_name": form.get("agency_name"),
        "campaign_name": form.get("campaign_name"),
        "assigned_user_id": owner["id"],
        "manager_id": owner["manager_id"],
        "regional_head_id": owner["regional_head_id"],
        "region": owner["region"],
        "quarter": form["quarter"],
        "entry_date": form["entry_date"],
        "plan_shared": 1 if form.get("plan_shared") == "on" else 0,
        "plan_date": form.get("plan_date") or None,
        "plan_value": plan_v,
        "confidence_pct": conf_v,
        "negotiation": _normalize_negotiation(form.get("negotiation")),
        "pipeline_value": compute_pipeline(plan_v, conf_v),
        "deal_status": normalize_deal_status(form.get("deal_status")) or "Pipeline",
        "ro_date": form.get("ro_date") or None,
        "ro_number": (form.get("ro_number") or "").strip() or None,
        "ro_amount_excl_gst": excl_v,
        "gst_pct": gst_pct_v,
        "gst_value": gst_v,
        "ro_total": compute_ro_total(excl_v, gst_v),
        "follow_up_date": form.get("follow_up_date") or None,
        "remarks": form.get("remarks"),
    }


@app.route("/entries/new", methods=["GET", "POST"])
def create_entry() -> str | Any:
    user = require_user()
    # `assignable` no longer rendered as a picker — the entry is auto-assigned to
    # the current user. The list is still passed to the template for parity with
    # the old context shape (templates may display "owned by" hints later).
    assignable = get_assignable_users(user)

    if request.method == "POST":
        # Auto-assign to the logged-in user. Removes the brittle picker that
        # required a salesperson to exist in the org chart.
        db = get_db()
        owner = db.execute("select * from users where id = ?", (user.id,)).fetchone()
        if owner is None:
            abort(403)
        values = _gather_entry_form_values(request.form, user, owner)

        # Save RO file (if any) BEFORE validation so we know whether one exists.
        ro_file_rel, ro_file_err = save_ro_file(request.files.get("ro_file"))
        ro_errors = validate_ro_block(
            values["deal_status"],
            ro_number=values["ro_number"],
            ro_amount_excl_gst=values["ro_amount_excl_gst"],
            gst_value=values["gst_value"],
            has_file=bool(ro_file_rel),
        )
        if ro_file_err:
            ro_errors.insert(0, ro_file_err)

        if ro_errors:
            for msg in ro_errors:
                flash(msg)
            return render_template(
                "entry_form.html",
                user=user, assignable=assignable, entry=None,
                deal_statuses=DEAL_STATUSES, agencies=load_agencies(),
                form_values=values, form_errors=ro_errors,
            )

        db.execute(
            """
            insert into revenue_entries (
              client_name, client_type, agency_name, campaign_name,
              assigned_user_id, manager_id, regional_head_id, region,
              quarter, entry_date, plan_shared, plan_date, plan_value,
              confidence_pct, negotiation, pipeline_value,
              deal_status, ro_date, ro_number, ro_amount_excl_gst, gst_pct, gst_value, ro_total, ro_file_path,
              status, follow_up_date, remarks, book, created_by, updated_by
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values["client_name"], values["client_type"],
                values["agency_name"], values["campaign_name"],
                values["assigned_user_id"], values["manager_id"],
                values["regional_head_id"], values["region"],
                values["quarter"], values["entry_date"],
                values["plan_shared"], values["plan_date"], values["plan_value"],
                values["confidence_pct"], values["negotiation"], values["pipeline_value"],
                values["deal_status"], values["ro_date"], values["ro_number"],
                values["ro_amount_excl_gst"], values["gst_pct"], values["gst_value"], values["ro_total"],
                ro_file_rel,
                values["deal_status"],   # legacy `status` mirrors deal_status for backwards-compat
                values["follow_up_date"], values["remarks"],
                "main",
                user.id, user.id,
            ),
        )
        new_id = db.execute("select last_insert_rowid() as id").fetchone()["id"]
        snap = db.execute("select * from revenue_entries where id = ?", (new_id,)).fetchone()
        write_history(db, entry_id=new_id, action="create", actor=user, snapshot=_row_snapshot(snap))
        db.commit()
        flash("Revenue entry created.")
        return redirect(url_for("entries"))

    return render_template(
        "entry_form.html",
        user=user, assignable=assignable, entry=None,
        deal_statuses=DEAL_STATUSES, agencies=load_agencies(),
        form_values=None, form_errors=[],
    )


@app.route("/entries/<int:entry_id>/edit", methods=["GET", "POST"])
def edit_entry(entry_id: int) -> str | Any:
    user = require_user()
    db = get_db()
    entry = db.execute("select * from revenue_entries where id = ?", (entry_id,)).fetchone()
    if not entry or (entry["book"] or "main") != "main" or not can_edit_entry(user, entry):
        abort(403)

    assignable = get_assignable_users(user)

    if request.method == "POST":
        # Auto-assign edits keep the original owner; we just need the row data.
        owner = db.execute(
            "select * from users where id = ?", (entry["assigned_user_id"],)
        ).fetchone()
        if owner is None:
            owner = db.execute("select * from users where id = ?", (user.id,)).fetchone()
        new_values = _gather_entry_form_values(request.form, user, owner)

        # File handling: keep existing path unless a new file was uploaded.
        existing_path = entry["ro_file_path"] if "ro_file_path" in entry.keys() else None
        ro_file_rel, ro_file_err = save_ro_file(request.files.get("ro_file"))
        if ro_file_rel:
            new_values["ro_file_path"] = ro_file_rel
        else:
            new_values["ro_file_path"] = existing_path

        ro_errors = validate_ro_block(
            new_values["deal_status"],
            ro_number=new_values["ro_number"],
            ro_amount_excl_gst=new_values["ro_amount_excl_gst"],
            gst_value=new_values["gst_value"],
            has_file=bool(new_values["ro_file_path"]),
        )
        if ro_file_err:
            ro_errors.insert(0, ro_file_err)
        if ro_errors:
            for msg in ro_errors:
                flash(msg)
            return render_template(
                "entry_form.html",
                user=user, assignable=assignable, entry=entry,
                deal_statuses=DEAL_STATUSES,
                form_values=new_values, form_errors=ro_errors,
            )

        changed_fields = diff_editable_fields(entry, new_values)

        db.execute(
            """
            update revenue_entries
            set client_name = ?, client_type = ?, agency_name = ?, campaign_name = ?,
                assigned_user_id = ?, manager_id = ?, regional_head_id = ?, region = ?,
                quarter = ?, entry_date = ?, plan_shared = ?, plan_date = ?, plan_value = ?,
                confidence_pct = ?, negotiation = ?, pipeline_value = ?,
                deal_status = ?, ro_date = ?, ro_number = ?, ro_amount_excl_gst = ?,
                gst_pct = ?, gst_value = ?, ro_total = ?, ro_file_path = ?,
                status = ?, follow_up_date = ?, remarks = ?,
                updated_by = ?, updated_at = current_timestamp
            where id = ?
            """,
            (
                new_values["client_name"], new_values["client_type"],
                new_values["agency_name"], new_values["campaign_name"],
                new_values["assigned_user_id"], new_values["manager_id"],
                new_values["regional_head_id"], new_values["region"],
                new_values["quarter"], new_values["entry_date"],
                new_values["plan_shared"], new_values["plan_date"], new_values["plan_value"],
                new_values["confidence_pct"], new_values["negotiation"], new_values["pipeline_value"],
                new_values["deal_status"], new_values["ro_date"], new_values["ro_number"],
                new_values["ro_amount_excl_gst"], new_values["gst_pct"], new_values["gst_value"], new_values["ro_total"],
                new_values["ro_file_path"],
                new_values["deal_status"],
                new_values["follow_up_date"], new_values["remarks"],
                user.id, entry_id,
            ),
        )
        if changed_fields:
            snap = db.execute("select * from revenue_entries where id = ?", (entry_id,)).fetchone()
            write_history(
                db,
                entry_id=entry_id,
                action="update",
                actor=user,
                snapshot=_row_snapshot(snap),
                changed_fields=changed_fields,
            )
        db.commit()
        flash("Revenue entry updated.")
        return redirect(url_for("entries"))

    return render_template(
        "entry_form.html",
        user=user, assignable=assignable, entry=entry,
        deal_statuses=DEAL_STATUSES, agencies=load_agencies(),
        form_values=None, form_errors=[],
    )


# ---------------------------------------------------------------------------
# Soft-cancel and soft-delete routes — data is never destroyed.
# ---------------------------------------------------------------------------

def _load_entry_or_404(entry_id: int) -> sqlite3.Row:
    db = get_db()
    entry = db.execute("select * from revenue_entries where id = ?", (entry_id,)).fetchone()
    if not entry:
        abort(404)
    return entry


@app.route("/ro-doc/<int:entry_id>")
def ro_document(entry_id: int) -> Any:
    """
    Authenticated RO document download. Re-runs the same view-permission check the
    dashboards use; never exposes files at a publicly fetchable URL.
    """
    user = require_user()
    entry = _load_entry_or_404(entry_id)
    book = entry["book"] or "main"
    if book == "nestle":
        if not can_view_nestle(user):
            abort(403)
    else:
        if not can_view_entry(user, entry):
            abort(403)
    if entry["is_deleted"] and user.role != "admin":
        abort(404)
    raw_path = (entry["ro_file_path"] or "").strip()
    if not raw_path:
        abort(404)
    # Legacy rows may still have the old "uploads/ro/<file>" prefix from before
    # this fix moved the storage out of /static. Strip it so we end up with the bare filename.
    if raw_path.startswith("uploads/ro/"):
        raw_path = raw_path[len("uploads/ro/"):]
    raw_path = Path(raw_path).name  # belt-and-braces: drop any path traversal attempt
    candidate = RO_UPLOAD_DIR / raw_path
    if not candidate.is_file():
        abort(404)
    return send_from_directory(RO_UPLOAD_DIR, raw_path, as_attachment=False)


@app.route("/entries/<int:entry_id>/cancel", methods=["POST"])
def cancel_entry(entry_id: int) -> Any:
    user = require_user()
    entry = _load_entry_or_404(entry_id)
    if (entry["book"] or "main") != "main" or not can_cancel_entry(user, entry):
        abort(403)
    if entry["is_deleted"]:
        flash("Cannot cancel a deleted entry.")
        return redirect(url_for("entries"))
    if entry["is_cancelled"]:
        flash("Entry is already cancelled.")
        return redirect(url_for("entries"))

    db = get_db()
    db.execute(
        """
        update revenue_entries
        set is_cancelled = 1,
            cancelled_at = current_timestamp,
            cancelled_by = ?,
            updated_by = ?,
            updated_at = current_timestamp
        where id = ?
        """,
        (user.id, user.id, entry_id),
    )
    snap = db.execute("select * from revenue_entries where id = ?", (entry_id,)).fetchone()
    write_history(db, entry_id=entry_id, action="cancel", actor=user, snapshot=_row_snapshot(snap))
    db.commit()
    flash("Entry cancelled. It is excluded from the funnel but kept in the system.")
    return redirect(url_for("entries"))


@app.route("/entries/<int:entry_id>/uncancel", methods=["POST"])
def uncancel_entry(entry_id: int) -> Any:
    user = require_user()
    entry = _load_entry_or_404(entry_id)
    if (entry["book"] or "main") != "main" or not can_cancel_entry(user, entry):
        abort(403)
    if entry["is_deleted"] or not entry["is_cancelled"]:
        abort(400)

    db = get_db()
    db.execute(
        """
        update revenue_entries
        set is_cancelled = 0,
            cancelled_at = NULL,
            cancelled_by = NULL,
            updated_by = ?,
            updated_at = current_timestamp
        where id = ?
        """,
        (user.id, entry_id),
    )
    snap = db.execute("select * from revenue_entries where id = ?", (entry_id,)).fetchone()
    write_history(db, entry_id=entry_id, action="uncancel", actor=user, snapshot=_row_snapshot(snap))
    db.commit()
    flash("Entry reactivated and back in the funnel.")
    return redirect(url_for("entries"))


@app.route("/entries/<int:entry_id>/delete", methods=["POST"])
def delete_entry(entry_id: int) -> Any:
    """Soft-delete (admin only). Data persists in revenue_entry_history forever."""
    user = require_user()
    entry = _load_entry_or_404(entry_id)
    if (entry["book"] or "main") != "main" or not can_delete_entry(user, entry):
        abort(403)
    if entry["is_deleted"]:
        flash("Entry already deleted.")
        return redirect(url_for("entries"))

    db = get_db()
    db.execute(
        """
        update revenue_entries
        set is_deleted = 1,
            deleted_at = current_timestamp,
            deleted_by = ?,
            updated_by = ?,
            updated_at = current_timestamp
        where id = ?
        """,
        (user.id, user.id, entry_id),
    )
    snap = db.execute("select * from revenue_entries where id = ?", (entry_id,)).fetchone()
    write_history(db, entry_id=entry_id, action="delete", actor=user, snapshot=_row_snapshot(snap))
    db.commit()
    flash("Entry deleted (soft). Full snapshot preserved in the audit log.")
    return redirect(url_for("entries"))


@app.route("/entries/<int:entry_id>/undelete", methods=["POST"])
def undelete_entry(entry_id: int) -> Any:
    user = require_user()
    if user.role != "admin":
        abort(403)
    entry = _load_entry_or_404(entry_id)
    if (entry["book"] or "main") != "main" or not entry["is_deleted"]:
        abort(400)

    db = get_db()
    db.execute(
        """
        update revenue_entries
        set is_deleted = 0,
            deleted_at = NULL,
            deleted_by = NULL,
            updated_by = ?,
            updated_at = current_timestamp
        where id = ?
        """,
        (user.id, entry_id),
    )
    snap = db.execute("select * from revenue_entries where id = ?", (entry_id,)).fetchone()
    write_history(db, entry_id=entry_id, action="undelete", actor=user, snapshot=_row_snapshot(snap))
    db.commit()
    flash("Entry restored.")
    return redirect(url_for("entries"))


def require_admin() -> User:
    user = require_user()
    if user.role != "admin":
        abort(403)
    return user


# ---------------------------------------------------------------------------
# Team config — JSON file at config/team_config.json is the source of truth
# for the org chart. On change, we reconcile the users table additively
# (existing users with entries are NEVER deleted; missing users get archived).
# ---------------------------------------------------------------------------

TEAM_CONFIG_PATH = BASE_DIR / "config" / "team_config.json"

ALLOWED_ROLES = {"salesperson", "manager", "regional_head", "admin"}


def load_team_config() -> dict[str, Any]:
    if not TEAM_CONFIG_PATH.exists():
        return {"fiscal_year": current_fiscal_year(), "regions": [], "users": []}
    with open(TEAM_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


_FALLBACK_AGENCIES = [
    "Adglobal", "Bigg Trunk", "DDB Mudra", "Dentsu", "Dentsu / Carat",
    "Dentsu /DentsuX", "Dentsu/IProspect", "Digitas", "Direct",
    "Hashtag Orange", "Interactive Avenues", "Madison",
    "Omnicom Media Group", "Omnicom Media Group/OMD", "Omnicom Media Group/PHD",
    "Puretech Digital", "TLG", "TLG/Performics", "WPP", "WPP/ Wavemaker",
    "WPP/EssenceMediacom", "WPP/Mindshare", "WPP/Motivator", "WPP/Open Door",
    "ZenithOptimedia",
]


def load_agencies() -> list[str]:
    """Agency suggestions for the entry form datalist. Falls back to the built-in list
    when team_config.json doesn't define any."""
    try:
        cfg = load_team_config()
        ag = cfg.get("agencies") or []
        cleaned = [s for s in (str(x).strip() for x in ag) if s]
        return cleaned or list(_FALLBACK_AGENCIES)
    except Exception:
        return list(_FALLBACK_AGENCIES)


def validate_team_config(cfg: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(cfg, dict):
        return ["Top-level JSON must be an object."]
    users = cfg.get("users") or []
    if not isinstance(users, list):
        errors.append("'users' must be a list.")
        return errors

    seen_emails: set[str] = set()
    for i, u in enumerate(users):
        if not isinstance(u, dict):
            errors.append(f"users[{i}] must be an object")
            continue
        email = (u.get("email") or "").strip().lower()
        if not email:
            errors.append(f"users[{i}] missing email")
            continue
        if email in seen_emails:
            errors.append(f"duplicate email '{email}'")
        seen_emails.add(email)
        if not (u.get("name") or "").strip():
            errors.append(f"users[{i}] '{email}' missing name")

        # The access list is the source of truth for roles. Legacy 'role' is also accepted.
        access = u.get("access")
        if access is None and u.get("role"):
            # Legacy single-role shape — still valid.
            access = [{
                "label": (u.get("role") or "").strip().replace("_", " ").title(),
                "role": u.get("role"),
                "region": u.get("region"),
                "manager_email": u.get("manager_email"),
                "regional_head_email": u.get("regional_head_email"),
            }]
        if not access or not isinstance(access, list):
            errors.append(f"users[{i}] '{email}' missing 'access' list")
            continue
        seen_labels: set[str] = set()
        for j, a in enumerate(access):
            if not isinstance(a, dict):
                errors.append(f"users[{i}].access[{j}] must be an object")
                continue
            role = (a.get("role") or "").strip()
            if role not in ALLOWED_ROLES:
                errors.append(f"users[{i}] '{email}' access[{j}] invalid role '{role}'")
            label = (a.get("label") or "").strip()
            if not label:
                errors.append(f"users[{i}] '{email}' access[{j}] missing label")
            elif label in seen_labels:
                errors.append(f"users[{i}] '{email}' duplicate access label '{label}'")
            else:
                seen_labels.add(label)
            extras = a.get("regions_extra")
            if extras is not None and not isinstance(extras, list):
                errors.append(f"users[{i}] '{email}' access[{j}] regions_extra must be a list")

    # FK validation: manager_email and regional_head_email must reference seen emails
    for i, u in enumerate(users):
        for a in (u.get("access") or []):
            if not isinstance(a, dict):
                continue
            for fk in ("manager_email", "regional_head_email"):
                ref = (a.get(fk) or "")
                if ref and ref.strip().lower() not in seen_emails:
                    errors.append(f"users[{i}] '{u.get('email')}' access fk {fk}='{ref}' not found")

    # Agencies (optional) must be a list of non-empty strings if present.
    if "agencies" in cfg:
        ag = cfg.get("agencies")
        if not isinstance(ag, list):
            errors.append("'agencies' must be a list")
        else:
            for j, name in enumerate(ag):
                if not isinstance(name, str) or not name.strip():
                    errors.append(f"agencies[{j}] must be a non-empty string")
    return errors


def _normalize_access_list(u: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the user's access[] list, falling back to a single legacy entry from u['role']."""
    access = u.get("access")
    if isinstance(access, list) and access:
        return access
    if u.get("role"):
        return [{
            "label": (u.get("role") or "").strip().replace("_", " ").title(),
            "role": u.get("role"),
            "region": u.get("region"),
            "manager_email": u.get("manager_email"),
            "regional_head_email": u.get("regional_head_email"),
        }]
    return []


def reconcile_team_config(conn: sqlite3.Connection, cfg: dict[str, Any]) -> dict[str, int]:
    """
    Apply the config to the users + user_access tables.
      - Insert users by email if missing; update matched users.
      - Upsert each access entry into user_access by (user_id, label).
      - Resolve manager_email / regional_head_email to FK ids on the access row.
      - users.role/region come from the FIRST access entry (a "primary" baseline).
      - Users not in config are archived (never destroyed).
    """
    users_cfg = cfg.get("users") or []
    cfg_emails = {(u.get("email") or "").strip().lower() for u in users_cfg}

    inserted = 0
    updated = 0
    access_rows = 0

    # PASS 1 — upsert users
    for u in users_cfg:
        email = (u.get("email") or "").strip()
        access_list = _normalize_access_list(u)
        primary = access_list[0] if access_list else {"role": "salesperson", "region": None}
        existing = conn.execute("select id from users where lower(email) = lower(?)", (email,)).fetchone()
        archived_flag = 1 if u.get("archived") else 0
        if existing is None:
            conn.execute(
                """
                insert into users (name, email, role, region, can_delete_own_entries, archived, auth_provider)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    u.get("name") or email,
                    email,
                    primary.get("role"),
                    primary.get("region"),
                    1 if u.get("can_delete_own_entries") else 0,
                    archived_flag,
                    u.get("auth_provider") or "session",
                ),
            )
            inserted += 1
        else:
            conn.execute(
                """
                update users set
                  name = ?,
                  role = ?,
                  region = ?,
                  can_delete_own_entries = ?,
                  archived = ?,
                  auth_provider = ?
                where id = ?
                """,
                (
                    u.get("name") or email,
                    primary.get("role"),
                    primary.get("region"),
                    1 if u.get("can_delete_own_entries") else 0,
                    archived_flag,
                    u.get("auth_provider") or "session",
                    existing["id"],
                ),
            )
            updated += 1

    # Build email -> id map for FK resolution
    by_email: dict[str, int] = {}
    for r in conn.execute("select id, lower(email) as email from users").fetchall():
        by_email[r["email"]] = r["id"]

    # PASS 2 — set users.manager_id / regional_head_id from PRIMARY access (legacy fields)
    for u in users_cfg:
        email = (u.get("email") or "").strip().lower()
        my_id = by_email.get(email)
        if not my_id:
            continue
        access_list = _normalize_access_list(u)
        primary = access_list[0] if access_list else {}
        m_email = (primary.get("manager_email") or "").strip().lower()
        rh_email = (primary.get("regional_head_email") or "").strip().lower()
        m_id = by_email.get(m_email) if m_email else None
        rh_id = by_email.get(rh_email) if rh_email else None
        conn.execute(
            "update users set manager_id = ?, regional_head_id = ? where id = ?",
            (m_id, rh_id, my_id),
        )

    # PASS 3 — upsert user_access rows
    for u in users_cfg:
        email = (u.get("email") or "").strip().lower()
        my_id = by_email.get(email)
        if not my_id:
            continue
        access_list = _normalize_access_list(u)
        # Mark all of this user's existing access rows as archived; un-archive the ones we re-state.
        conn.execute("update user_access set is_archived = 1 where user_id = ?", (my_id,))
        for sort_order, a in enumerate(access_list):
            label = (a.get("label") or "").strip()
            if not label:
                continue
            role = (a.get("role") or "").strip()
            region = a.get("region")
            extras = a.get("regions_extra")
            extras_json = json.dumps(extras) if extras else None
            m_id = by_email.get((a.get("manager_email") or "").strip().lower())
            rh_id = by_email.get((a.get("regional_head_email") or "").strip().lower())

            existing_a = conn.execute(
                "select id from user_access where user_id = ? and label = ?",
                (my_id, label),
            ).fetchone()
            if existing_a is None:
                conn.execute(
                    """
                    insert into user_access (
                      user_id, label, role, region, regions_extra_json,
                      manager_id, regional_head_id, sort_order, is_archived
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (my_id, label, role, region, extras_json, m_id, rh_id, sort_order),
                )
            else:
                conn.execute(
                    """
                    update user_access set
                      role = ?, region = ?, regions_extra_json = ?,
                      manager_id = ?, regional_head_id = ?,
                      sort_order = ?, is_archived = 0
                    where id = ?
                    """,
                    (role, region, extras_json, m_id, rh_id, sort_order, existing_a["id"]),
                )
            access_rows += 1

    # Archive users not present in config (never delete — they may own entries).
    # Also archive their user_access rows so revocation is authoritative.
    archived_count = 0
    for r in conn.execute("select id, lower(email) as email from users").fetchall():
        if r["email"] not in cfg_emails:
            conn.execute("update users set archived = 1 where id = ?", (r["id"],))
            conn.execute("update user_access set is_archived = 1 where user_id = ?", (r["id"],))
            archived_count += 1
    conn.commit()
    return {"inserted": inserted, "updated": updated, "archived": archived_count, "access_rows": access_rows}


@app.route("/admin/team", methods=["GET", "POST"])
def admin_team() -> Any:
    user = require_admin()
    db = get_db()

    if request.method == "POST":
        # Accept either raw JSON body or a 'team_config_json' form field
        payload = (request.form.get("team_config_json") or "").strip()
        if not payload and request.is_json:
            payload = json.dumps(request.get_json(force=True))
        try:
            cfg = json.loads(payload)
        except json.JSONDecodeError as exc:
            flash(f"Invalid JSON: {exc}")
            return redirect(url_for("admin_team"))

        errors = validate_team_config(cfg)
        if errors:
            flash("Config rejected · " + " · ".join(errors[:5]))
            return redirect(url_for("admin_team"))

        TEAM_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: stage to tmp file then rename
        tmp = TEAM_CONFIG_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        tmp.replace(TEAM_CONFIG_PATH)

        counts = reconcile_team_config(db, cfg)
        flash(
            f"Team config saved. Inserted {counts['inserted']}, updated {counts['updated']}, archived {counts['archived']}."
        )
        return redirect(url_for("admin_team"))

    cfg = load_team_config()
    raw_json = json.dumps(cfg, indent=2)
    db_users = db.execute(
        """
        select id, name, email, role, region, manager_id, regional_head_id, can_delete_own_entries, archived, auth_provider
        from users order by role desc, name asc
        """
    ).fetchall()
    return render_template(
        "admin_team.html",
        user=user,
        cfg=cfg,
        raw_json=raw_json,
        db_users=db_users,
    )


@app.route("/admin/audit")
def admin_audit() -> Any:
    user = require_admin()
    db = get_db()

    tab = (request.args.get("tab") or "entries").strip()
    if tab not in ("entries", "targets"):
        tab = "entries"

    if tab == "targets":
        # Target-change history view
        rows = db.execute(
            """
            select th.*, u.name as actor_name
            from target_history th
            left join users u on u.id = th.set_by
            order by th.set_at desc, th.id desc
            limit 200
            """
        ).fetchall()
        target_changes = [{
            "id": r["id"],
            "access_label": r["access_label"],
            "quarter": r["quarter"],
            "fiscal_year": r["fiscal_year"],
            "old_value": r["old_value"],
            "new_value": r["new_value"],
            "actor_name": r["actor_name"] or f"User #{r['set_by']}",
            "set_at": r["set_at"],
        } for r in rows]
        target_total = db.execute("select count(*) as c from target_history").fetchone()["c"]
        return render_template(
            "admin_audit.html",
            user=user, tab="targets",
            target_changes=target_changes,
            target_total=target_total,
            history=[], total=0, total_pages=1, page=1,
            action_filter="", actor_filter="", entry_filter="",
            actors=[], action_options=[],
        )

    action_filter = (request.args.get("action") or "").strip()
    actor_filter = (request.args.get("actor_id") or "").strip()
    entry_filter = (request.args.get("entry_id") or "").strip()

    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    per_page = 50
    offset = (page - 1) * per_page

    where: list[str] = []
    params: list[Any] = []
    if action_filter:
        where.append("h.action = ?")
        params.append(action_filter)
    if actor_filter:
        try:
            params.append(int(actor_filter))
            where.append("h.actor_id = ?")
        except ValueError:
            pass
    if entry_filter:
        try:
            params.append(int(entry_filter))
            where.append("h.entry_id = ?")
        except ValueError:
            pass

    where_sql = ("where " + " and ".join(where)) if where else ""

    total = db.execute(
        f"select count(*) as c from revenue_entry_history h {where_sql}",
        tuple(params),
    ).fetchone()["c"]

    rows = db.execute(
        f"""
        select h.*, u.name as actor_name
        from revenue_entry_history h
        left join users u on u.id = h.actor_id
        {where_sql}
        order by h.occurred_at desc, h.id desc
        limit ? offset ?
        """,
        tuple(params) + (per_page, offset),
    ).fetchall()

    # Parse snapshots for in-page rendering
    history: list[dict[str, Any]] = []
    for r in rows:
        try:
            snapshot = json.loads(r["snapshot_json"]) if r["snapshot_json"] else {}
        except (TypeError, json.JSONDecodeError):
            snapshot = {}
        try:
            changed = json.loads(r["changed_fields_json"]) if r["changed_fields_json"] else []
        except (TypeError, json.JSONDecodeError):
            changed = []
        history.append({
            "id": r["id"],
            "entry_id": r["entry_id"],
            "version": r["version"],
            "action": r["action"],
            "actor_id": r["actor_id"],
            "actor_name": r["actor_name"] or f"User #{r['actor_id']}",
            "actor_role": r["actor_role"],
            "occurred_at": r["occurred_at"],
            "snapshot": snapshot,
            "changed_fields": changed,
        })

    actors = db.execute(
        "select id, name from users order by name"
    ).fetchall()

    total_pages = max(1, (total + per_page - 1) // per_page)

    return render_template(
        "admin_audit.html",
        user=user,
        tab="entries",
        history=history,
        page=page,
        total=total,
        total_pages=total_pages,
        per_page=per_page,
        action_filter=action_filter,
        actor_filter=actor_filter,
        entry_filter=entry_filter,
        actors=actors,
        action_options=["create", "update", "cancel", "uncancel", "delete", "undelete"],
        target_changes=[], target_total=0,
    )


@app.route("/admin/targets", methods=["GET", "POST"])
def admin_targets() -> Any:
    user = require_admin()
    db = get_db()
    fy_arg = request.values.get("fiscal_year") or ""
    try:
        fy = int(fy_arg) if fy_arg else current_fiscal_year()
    except ValueError:
        fy = current_fiscal_year()

    quarters = ["Q1", "Q2", "Q3", "Q4"]

    if request.method == "POST":
        # Iterate by index; the canonical label travels in a hidden input so spaces / parens / & survive.
        changes = 0
        i = 0
        while f"label__{i}" in request.form:
            label = (request.form.get(f"label__{i}") or "").strip()
            if not label:
                i += 1
                continue
            for q in quarters:
                key = f"target__{i}__{q}"
                if key not in request.form:
                    continue
                value = parse_float(request.form.get(key, "0"))
                # Read prior value (if any) so we can audit the change.
                prior_row = db.execute(
                    "select target_value from targets where access_label = ? and quarter = ? and fiscal_year = ?",
                    (label, q, fy),
                ).fetchone()
                prior_value = float(prior_row["target_value"]) if prior_row else None
                if prior_value is None or abs(prior_value - value) > 1e-9:
                    db.execute(
                        """
                        insert into target_history (access_label, quarter, fiscal_year, old_value, new_value, set_by)
                        values (?, ?, ?, ?, ?, ?)
                        """,
                        (label, q, fy, prior_value, value, user.id),
                    )
                    changes += 1
                db.execute(
                    """
                    insert into targets (access_label, quarter, fiscal_year, target_value, set_by)
                    values (?, ?, ?, ?, ?)
                    on conflict(access_label, quarter, fiscal_year)
                    do update set target_value = excluded.target_value, set_by = excluded.set_by, set_at = current_timestamp
                    """,
                    (label, q, fy, value, user.id),
                )
            i += 1
        db.commit()
        flash(f"Targets saved for FY {fy}. {changes} change{'s' if changes != 1 else ''} logged to audit.")
        return redirect(url_for("admin_targets", fiscal_year=fy))

    # Enumerate distinct leaf access labels with their region and the holders.
    label_rows = db.execute(
        """
        select ua.label as label,
               ua.region as region,
               group_concat(u.name, ' · ') as holders,
               count(distinct ua.user_id) as holder_count
        from user_access ua
        join users u on u.id = ua.user_id
        where ua.role in ('manager', 'salesperson') and ua.is_archived = 0 and u.archived = 0
        group by ua.label, ua.region
        order by ua.region is null, ua.region, ua.label
        """
    ).fetchall()

    target_rows = db.execute(
        "select access_label, quarter, target_value from targets where fiscal_year = ?",
        (fy,),
    ).fetchall()
    targets: dict[tuple[str, str], float] = {(r["access_label"], r["quarter"]): r["target_value"] for r in target_rows}

    matrix: list[dict[str, Any]] = []
    col_totals = {q: 0.0 for q in quarters}
    grand_total = 0.0
    for lr in label_rows:
        row = {
            "label": lr["label"],
            "region": lr["region"],
            "holders": lr["holders"] or "—",
            "cells": {},
            "total": 0.0,
        }
        for q in quarters:
            v = float(targets.get((lr["label"], q), 0) or 0)
            row["cells"][q] = v
            row["total"] += v
            col_totals[q] += v
        grand_total += row["total"]
        matrix.append(row)

    return render_template(
        "admin_targets.html",
        user=user,
        fy=fy,
        quarters=quarters,
        matrix=matrix,
        col_totals=col_totals,
        grand_total=grand_total,
    )


# ===========================================================================
# Nestle ledger — separate book, isolated from all main rollups.
# Owner: rahul@syncmedia.io. Viewers: Rahul + admins. Editor: Rahul only.
# ===========================================================================

def _nestle_summary() -> dict[str, Any]:
    """Aggregate visible Nestle entries: total RO booked, count, latest entry date."""
    db = get_db()
    rows = db.execute(
        """
        select coalesce(book, 'main') as book, ro_total, ro_value, is_cancelled, is_deleted, entry_date
        from revenue_entries where coalesce(book, 'main') = 'nestle'
        """
    ).fetchall()
    active = [r for r in rows if not r["is_cancelled"] and not r["is_deleted"]]
    total_ro = sum(float(r["ro_total"] or r["ro_value"] or 0) for r in active)
    cancelled = sum(1 for r in rows if r["is_cancelled"])
    return {
        "total_ro": total_ro,
        "active_count": len(active),
        "total_count": len(rows),
        "cancelled_count": cancelled,
    }


@app.route("/nestle")
def nestle_dashboard() -> Any:
    user = require_nestle_view()
    args = parse_filter_args(request.args)
    rows = visible_entries(user, book="nestle")
    rows = apply_filters(rows, args)
    summary = _nestle_summary()
    return render_template(
        "nestle_dashboard.html",
        user=user,
        summary=summary,
        rows=rows[:8],
        can_edit=can_edit_nestle(user),
    )


@app.route("/nestle/entries")
def nestle_entries() -> Any:
    user = require_nestle_view()
    args = parse_filter_args(request.args)
    all_rows = visible_entries(user, book="nestle", include_deleted=(user.role == "admin"))
    rows = apply_filters(all_rows, args)
    recent_ids = {r["id"]: True for r in rows if is_recent_new(r)}
    return render_template(
        "nestle_entries.html",
        user=user,
        rows=rows,
        recent_ids=recent_ids,
        can_edit=can_edit_nestle(user),
        total_count=len(all_rows),
        filtered_count=len(rows),
        cancelled_count=sum(1 for r in all_rows if r["is_cancelled"]),
        active_filter_count=active_filter_count(args),
        current_filters={
            "quarters": args["quarters"],
            "status_filter": args["status_filter"],
        },
    )


def _nestle_load_or_404(entry_id: int) -> sqlite3.Row:
    db = get_db()
    entry = db.execute("select * from revenue_entries where id = ?", (entry_id,)).fetchone()
    if not entry or (entry["book"] or "main") != "nestle":
        abort(404)
    return entry


@app.route("/nestle/entries/new", methods=["GET", "POST"])
def nestle_create_entry() -> Any:
    user = require_user()
    if not can_edit_nestle(user):
        abort(403)
    db = get_db()

    if request.method == "POST":
        excl_v = parse_float(request.form.get("ro_amount_excl_gst", "0"))
        gst_pct_v = parse_float(request.form.get("gst_pct", "0"))
        gst_v = compute_gst_value(excl_v, gst_pct_v)
        deal_status = normalize_deal_status(request.form.get("deal_status")) or "Pipeline"

        ro_file_rel, ro_file_err = save_ro_file(request.files.get("ro_file"))
        ro_errors = validate_ro_block(
            deal_status,
            ro_number=request.form.get("ro_number"),
            ro_amount_excl_gst=excl_v,
            gst_value=gst_v,
            has_file=bool(ro_file_rel),
        )
        if ro_file_err:
            ro_errors.insert(0, ro_file_err)

        if ro_errors:
            for msg in ro_errors:
                flash(msg)
            return render_template(
                "nestle_form.html",
                user=user, entry=None,
                deal_statuses=DEAL_STATUSES,
                form_values={
                    "deal_status": deal_status,
                    "ro_number": request.form.get("ro_number"),
                    "ro_date": request.form.get("ro_date"),
                    "ro_amount_excl_gst": excl_v,
                    "gst_pct": gst_pct_v,
                    "gst_value": gst_v,
                    "quarter": request.form.get("quarter"),
                    "entry_date": request.form.get("entry_date"),
                    "remarks": request.form.get("remarks"),
                },
                form_errors=ro_errors,
            )

        db.execute(
            """
            insert into revenue_entries (
              client_name, client_type, assigned_user_id,
              quarter, entry_date,
              plan_value, confidence_pct, pipeline_value,
              deal_status, ro_date, ro_number, ro_amount_excl_gst, gst_pct, gst_value, ro_total, ro_file_path,
              status, remarks, book, created_by, updated_by
            )
            values (?, 'Existing', ?, ?, ?, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'nestle', ?, ?)
            """,
            (
                NESTLE_CLIENT_NAME, user.id,
                request.form["quarter"], request.form["entry_date"],
                deal_status,
                request.form.get("ro_date") or None,
                (request.form.get("ro_number") or "").strip() or None,
                excl_v, gst_pct_v, gst_v, compute_ro_total(excl_v, gst_v), ro_file_rel,
                deal_status,
                request.form.get("remarks"),
                user.id, user.id,
            ),
        )
        new_id = db.execute("select last_insert_rowid() as id").fetchone()["id"]
        snap = db.execute("select * from revenue_entries where id = ?", (new_id,)).fetchone()
        write_history(db, entry_id=new_id, action="create", actor=user, snapshot=_row_snapshot(snap))
        db.commit()
        flash("Nestle entry created.")
        return redirect(url_for("nestle_entries"))

    return render_template(
        "nestle_form.html",
        user=user, entry=None,
        deal_statuses=DEAL_STATUSES,
        form_values=None, form_errors=[],
    )


@app.route("/nestle/entries/<int:entry_id>/edit", methods=["GET", "POST"])
def nestle_edit_entry(entry_id: int) -> Any:
    user = require_user()
    if not can_edit_nestle(user):
        abort(403)
    db = get_db()
    entry = _nestle_load_or_404(entry_id)

    if request.method == "POST":
        excl_v = parse_float(request.form.get("ro_amount_excl_gst", "0"))
        gst_pct_v = parse_float(request.form.get("gst_pct", "0"))
        gst_v = compute_gst_value(excl_v, gst_pct_v)
        deal_status = normalize_deal_status(request.form.get("deal_status")) or "Pipeline"

        existing_path = entry["ro_file_path"]
        ro_file_rel, ro_file_err = save_ro_file(request.files.get("ro_file"))
        new_file_path = ro_file_rel or existing_path

        ro_errors = validate_ro_block(
            deal_status,
            ro_number=request.form.get("ro_number"),
            ro_amount_excl_gst=excl_v,
            gst_value=gst_v,
            has_file=bool(new_file_path),
        )
        if ro_file_err:
            ro_errors.insert(0, ro_file_err)
        if ro_errors:
            for msg in ro_errors:
                flash(msg)
            return render_template(
                "nestle_form.html",
                user=user, entry=entry,
                deal_statuses=DEAL_STATUSES,
                form_values={
                    "deal_status": deal_status,
                    "ro_number": request.form.get("ro_number"),
                    "ro_date": request.form.get("ro_date"),
                    "ro_amount_excl_gst": excl_v,
                    "gst_pct": gst_pct_v,
                    "gst_value": gst_v,
                    "quarter": request.form.get("quarter"),
                    "entry_date": request.form.get("entry_date"),
                    "remarks": request.form.get("remarks"),
                },
                form_errors=ro_errors,
            )

        new_values: dict[str, Any] = {
            "client_name": NESTLE_CLIENT_NAME,
            "quarter": request.form["quarter"],
            "entry_date": request.form["entry_date"],
            "deal_status": deal_status,
            "ro_date": request.form.get("ro_date") or None,
            "ro_number": (request.form.get("ro_number") or "").strip() or None,
            "ro_amount_excl_gst": excl_v,
            "gst_pct": gst_pct_v,
            "gst_value": gst_v,
            "ro_total": compute_ro_total(excl_v, gst_v),
            "ro_file_path": new_file_path,
            "remarks": request.form.get("remarks"),
        }
        changed = diff_editable_fields(entry, new_values)

        db.execute(
            """
            update revenue_entries set
              quarter = ?, entry_date = ?,
              deal_status = ?, ro_date = ?, ro_number = ?,
              ro_amount_excl_gst = ?, gst_pct = ?, gst_value = ?, ro_total = ?, ro_file_path = ?,
              status = ?, remarks = ?,
              updated_by = ?, updated_at = current_timestamp
            where id = ?
            """,
            (
                new_values["quarter"], new_values["entry_date"],
                new_values["deal_status"], new_values["ro_date"], new_values["ro_number"],
                new_values["ro_amount_excl_gst"], new_values["gst_pct"], new_values["gst_value"],
                new_values["ro_total"], new_values["ro_file_path"],
                new_values["deal_status"], new_values["remarks"],
                user.id, entry_id,
            ),
        )
        if changed:
            snap = db.execute("select * from revenue_entries where id = ?", (entry_id,)).fetchone()
            write_history(db, entry_id=entry_id, action="update", actor=user,
                          snapshot=_row_snapshot(snap), changed_fields=changed)
        db.commit()
        flash("Nestle entry updated.")
        return redirect(url_for("nestle_entries"))

    return render_template(
        "nestle_form.html",
        user=user, entry=entry,
        deal_statuses=DEAL_STATUSES,
        form_values=None, form_errors=[],
    )


@app.route("/nestle/entries/<int:entry_id>/cancel", methods=["POST"])
def nestle_cancel_entry(entry_id: int) -> Any:
    user = require_nestle_view()
    if not can_edit_nestle(user) and user.role != "admin":
        abort(403)
    entry = _nestle_load_or_404(entry_id)
    if entry["is_deleted"] or entry["is_cancelled"]:
        flash("Already cancelled or deleted.")
        return redirect(url_for("nestle_entries"))
    db = get_db()
    db.execute(
        """update revenue_entries set is_cancelled = 1, cancelled_at = current_timestamp,
           cancelled_by = ?, updated_by = ?, updated_at = current_timestamp where id = ?""",
        (user.id, user.id, entry_id),
    )
    snap = db.execute("select * from revenue_entries where id = ?", (entry_id,)).fetchone()
    write_history(db, entry_id=entry_id, action="cancel", actor=user, snapshot=_row_snapshot(snap))
    db.commit()
    flash("Nestle entry cancelled.")
    return redirect(url_for("nestle_entries"))


@app.route("/nestle/entries/<int:entry_id>/uncancel", methods=["POST"])
def nestle_uncancel_entry(entry_id: int) -> Any:
    user = require_nestle_view()
    if not can_edit_nestle(user) and user.role != "admin":
        abort(403)
    entry = _nestle_load_or_404(entry_id)
    if entry["is_deleted"] or not entry["is_cancelled"]:
        abort(400)
    db = get_db()
    db.execute(
        """update revenue_entries set is_cancelled = 0, cancelled_at = NULL, cancelled_by = NULL,
           updated_by = ?, updated_at = current_timestamp where id = ?""",
        (user.id, entry_id),
    )
    snap = db.execute("select * from revenue_entries where id = ?", (entry_id,)).fetchone()
    write_history(db, entry_id=entry_id, action="uncancel", actor=user, snapshot=_row_snapshot(snap))
    db.commit()
    flash("Nestle entry reactivated.")
    return redirect(url_for("nestle_entries"))


@app.route("/nestle/entries/<int:entry_id>/delete", methods=["POST"])
def nestle_delete_entry(entry_id: int) -> Any:
    """Soft-delete (admin only) for Nestle ledger; data preserved in history forever."""
    user = require_user()
    if user.role != "admin":
        abort(403)
    entry = _nestle_load_or_404(entry_id)
    if entry["is_deleted"]:
        flash("Already deleted.")
        return redirect(url_for("nestle_entries"))
    db = get_db()
    db.execute(
        """update revenue_entries set is_deleted = 1, deleted_at = current_timestamp,
           deleted_by = ?, updated_by = ?, updated_at = current_timestamp where id = ?""",
        (user.id, user.id, entry_id),
    )
    snap = db.execute("select * from revenue_entries where id = ?", (entry_id,)).fetchone()
    write_history(db, entry_id=entry_id, action="delete", actor=user, snapshot=_row_snapshot(snap))
    db.commit()
    flash("Nestle entry deleted (soft). Snapshot kept in audit log.")
    return redirect(url_for("nestle_entries"))


@app.context_processor
def inject_today() -> dict[str, str]:
    css_path = BASE_DIR / "app" / "static" / "css" / "app.css"
    asset_version = str(int(css_path.stat().st_mtime)) if css_path.exists() else "0"
    cu = current_user()
    return {
        "today": str(date.today()),
        "asset_version": asset_version,
        "show_nestle_nav": bool(cu and can_view_nestle(cu)),
    }


if __name__ == "__main__":
    import os
    init_db()
    port = int(os.environ.get("PORT", 5050))
    print(f"[sync] Resolved data paths:")
    print(f"  DATA_DIR       = {DATA_DIR}")
    print(f"  DB_PATH        = {DB_PATH}")
    print(f"  RO_UPLOAD_DIR  = {RO_UPLOAD_DIR}")
    print(f"  TEAM_CONFIG    = {TEAM_CONFIG_PATH}")
    print(f"  DEV_LOGIN      = {'enabled' if app.config['DEV_LOGIN_ENABLED'] else 'disabled'}")
    app.run(host="127.0.0.1", port=port, debug=True)
