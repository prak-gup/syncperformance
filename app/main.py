from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "app.db"

app = Flask(__name__)
app.config["SECRET_KEY"] = "dev-secret-change-me"


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


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
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

    count = conn.execute("select count(*) from users").fetchone()[0]
    if count == 0:
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
    conn.close()


def get_user(user_id: int) -> User | None:
    row = get_db().execute("select * from users where id = ?", (user_id,)).fetchone()
    if not row:
        return None
    return User(
        id=row["id"],
        name=row["name"],
        email=row["email"],
        role=row["role"],
        region=row["region"],
        manager_id=row["manager_id"],
        regional_head_id=row["regional_head_id"],
        can_delete_own_entries=bool(row["can_delete_own_entries"]),
    )


def current_user() -> User | None:
    uid = session.get("user_id")
    if not uid:
        return None
    return get_user(int(uid))


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


def can_view_entry(user: User, entry: sqlite3.Row) -> bool:
    if user.role == "admin":
        return True
    if user.role == "regional_head":
        return entry["region"] == user.region
    if user.role == "manager":
        return is_descendant(user.id, entry["assigned_user_id"])
    return entry["assigned_user_id"] == user.id


def can_edit_entry(user: User, entry: sqlite3.Row) -> bool:
    if user.role == "admin":
        return True
    if user.role == "regional_head":
        return entry["region"] == user.region
    if user.role == "manager":
        return is_descendant(user.id, entry["assigned_user_id"])
    return entry["assigned_user_id"] == user.id


def can_delete_entry(user: User, entry: sqlite3.Row) -> bool:
    if user.role == "admin":
        return True
    if user.role == "regional_head":
        return entry["region"] == user.region
    if user.role == "manager":
        return is_descendant(user.id, entry["assigned_user_id"])
    return entry["assigned_user_id"] == user.id and user.can_delete_own_entries


def get_assignable_users(user: User) -> list[sqlite3.Row]:
    db = get_db()
    if user.role == "admin":
        return db.execute("select * from users where role = 'salesperson' order by name").fetchall()
    if user.role == "regional_head":
        return db.execute(
            "select * from users where role = 'salesperson' and region = ? order by name", (user.region,)
        ).fetchall()
    if user.role == "manager":
        all_sales = db.execute("select * from users where role = 'salesperson' order by name").fetchall()
        return [u for u in all_sales if is_descendant(user.id, u["id"])]
    return db.execute("select * from users where id = ?", (user.id,)).fetchall()


def visible_entries(user: User) -> list[sqlite3.Row]:
    db = get_db()
    rows = db.execute(
        """
        select re.*, u.name as salesperson_name, m.name as manager_name, rh.name as regional_head_name
        from revenue_entries re
        join users u on u.id = re.assigned_user_id
        left join users m on m.id = re.manager_id
        left join users rh on rh.id = re.regional_head_id
        order by re.entry_date desc, re.id desc
        """
    ).fetchall()
    return [r for r in rows if can_view_entry(user, r)]


def parse_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except ValueError:
        return default


def rollup(user: User) -> dict[str, Any]:
    rows = visible_entries(user)
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


def dashboard_context(user: User) -> dict[str, Any]:
    rows = visible_entries(user)
    metrics = rollup(user)
    quarter_summary = aggregate_rows(rows, "quarter")
    salesperson_summary = aggregate_rows(rows, "salesperson_name")
    manager_summary = aggregate_rows(rows, "manager_name")
    region_summary = aggregate_rows(rows, "region")
    recent_rows = rows[:10]

    view_config: dict[str, dict[str, Any]] = {
        "salesperson": {
            "title": "Individual Dashboard",
            "subtitle": "Your personal performance and client pipeline.",
            "scope_table": None,
        },
        "manager": {
            "title": "Manager Dashboard",
            "subtitle": "Team performance roll-up for reporting salespeople.",
            "scope_table": salesperson_summary,
        },
        "regional_head": {
            "title": "Regional Dashboard",
            "subtitle": "Regional roll-up with manager and salesperson breakouts.",
            "scope_table": manager_summary,
        },
        "admin": {
            "title": "India Admin Dashboard",
            "subtitle": "National roll-up across regions, managers, and salespeople.",
            "scope_table": region_summary,
        },
    }
    config = view_config[user.role]
    return {
        "metrics": metrics,
        "recent_rows": recent_rows,
        "quarter_summary": quarter_summary,
        "salesperson_summary": salesperson_summary,
        "manager_summary": manager_summary,
        "region_summary": region_summary,
        "dashboard_title": config["title"],
        "dashboard_subtitle": config["subtitle"],
        "scope_table": config["scope_table"],
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
    if request.method == "POST":
        session["user_id"] = int(request.form["user_id"])
        return redirect(url_for("dashboard"))

    users = db.execute("select id, name, role from users order by id").fetchall()
    return render_template("login.html", users=users)


@app.route("/logout")
def logout() -> Any:
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard() -> str:
    user = require_user()
    ctx = dashboard_context(user)
    return render_template("dashboard.html", user=user, **ctx)


@app.route("/entries")
def entries() -> str:
    user = require_user()
    rows = visible_entries(user)
    return render_template("entries.html", user=user, rows=rows)


@app.route("/entries/new", methods=["GET", "POST"])
def create_entry() -> str | Any:
    user = require_user()
    assignable = get_assignable_users(user)

    if request.method == "POST":
        assigned_user_id = int(request.form["assigned_user_id"])

        if user.role == "salesperson":
            assigned_user_id = user.id

        if not any(u["id"] == assigned_user_id for u in assignable):
            abort(403)

        owner = get_db().execute("select * from users where id = ?", (assigned_user_id,)).fetchone()
        db = get_db()
        db.execute(
            """
            insert into revenue_entries (
              client_name, client_type, agency_name, campaign_name,
              assigned_user_id, manager_id, regional_head_id, region,
              quarter, entry_date, plan_shared, plan_date, plan_value,
              negotiation_stage, pipeline_value, ro_date, ro_value,
              status, follow_up_date, remarks, created_by, updated_by
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request.form["client_name"],
                request.form["client_type"],
                request.form.get("agency_name"),
                request.form.get("campaign_name"),
                assigned_user_id,
                owner["manager_id"],
                owner["regional_head_id"],
                owner["region"],
                request.form["quarter"],
                request.form["entry_date"],
                1 if request.form.get("plan_shared") == "on" else 0,
                request.form.get("plan_date") or None,
                parse_float(request.form.get("plan_value", "0")),
                request.form.get("negotiation_stage"),
                parse_float(request.form.get("pipeline_value", "0")),
                request.form.get("ro_date") or None,
                parse_float(request.form.get("ro_value", "0")),
                request.form.get("status"),
                request.form.get("follow_up_date") or None,
                request.form.get("remarks"),
                user.id,
                user.id,
            ),
        )
        db.commit()
        flash("Revenue entry created.")
        return redirect(url_for("entries"))

    return render_template("entry_form.html", user=user, assignable=assignable, entry=None)


@app.route("/entries/<int:entry_id>/edit", methods=["GET", "POST"])
def edit_entry(entry_id: int) -> str | Any:
    user = require_user()
    db = get_db()
    entry = db.execute("select * from revenue_entries where id = ?", (entry_id,)).fetchone()
    if not entry or not can_edit_entry(user, entry):
        abort(403)

    assignable = get_assignable_users(user)

    if request.method == "POST":
        assigned_user_id = int(request.form["assigned_user_id"])
        if user.role == "salesperson":
            assigned_user_id = user.id
        if not any(u["id"] == assigned_user_id for u in assignable):
            abort(403)

        owner = db.execute("select * from users where id = ?", (assigned_user_id,)).fetchone()
        db.execute(
            """
            update revenue_entries
            set client_name = ?, client_type = ?, agency_name = ?, campaign_name = ?,
                assigned_user_id = ?, manager_id = ?, regional_head_id = ?, region = ?,
                quarter = ?, entry_date = ?, plan_shared = ?, plan_date = ?, plan_value = ?,
                negotiation_stage = ?, pipeline_value = ?, ro_date = ?, ro_value = ?,
                status = ?, follow_up_date = ?, remarks = ?, updated_by = ?, updated_at = current_timestamp
            where id = ?
            """,
            (
                request.form["client_name"],
                request.form["client_type"],
                request.form.get("agency_name"),
                request.form.get("campaign_name"),
                assigned_user_id,
                owner["manager_id"],
                owner["regional_head_id"],
                owner["region"],
                request.form["quarter"],
                request.form["entry_date"],
                1 if request.form.get("plan_shared") == "on" else 0,
                request.form.get("plan_date") or None,
                parse_float(request.form.get("plan_value", "0")),
                request.form.get("negotiation_stage"),
                parse_float(request.form.get("pipeline_value", "0")),
                request.form.get("ro_date") or None,
                parse_float(request.form.get("ro_value", "0")),
                request.form.get("status"),
                request.form.get("follow_up_date") or None,
                request.form.get("remarks"),
                user.id,
                entry_id,
            ),
        )
        db.commit()
        flash("Revenue entry updated.")
        return redirect(url_for("entries"))

    return render_template("entry_form.html", user=user, assignable=assignable, entry=entry)


@app.route("/entries/<int:entry_id>/delete", methods=["POST"])
def delete_entry(entry_id: int) -> Any:
    user = require_user()
    db = get_db()
    entry = db.execute("select * from revenue_entries where id = ?", (entry_id,)).fetchone()
    if not entry or not can_delete_entry(user, entry):
        abort(403)

    db.execute("delete from revenue_entries where id = ?", (entry_id,))
    db.commit()
    flash("Revenue entry deleted.")
    return redirect(url_for("entries"))


@app.context_processor
def inject_today() -> dict[str, str]:
    return {"today": str(date.today())}


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
