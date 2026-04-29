"""
End-to-end QA harness for Sync Performance.
Walks every role × every feature, prints PASS / FAIL for each scenario.
Run: .venv/bin/python _e2e_test.py
"""
import io
import json
import sqlite3
import sys
from datetime import date

sys.path.insert(0, ".")
from app.main import app, init_db


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"

results = []


def check(label, ok, detail=""):
    results.append((label, ok, detail))
    tag = PASS if ok else FAIL
    extra = f" — {detail}" if detail else ""
    print(f"  {tag} · {label}{extra}")


def section(title):
    print()
    print(f"\033[1m── {title} ──\033[0m")


# Reset DB and reconcile config
init_db()
conn = sqlite3.connect("app.db")
conn.row_factory = sqlite3.Row


def uid_of(email):
    row = conn.execute("select id from users where lower(email) = lower(?)", (email,)).fetchone()
    return row["id"] if row else None


def access_id_of(email, label):
    row = conn.execute(
        """
        select ua.id from user_access ua
        join users u on u.id = ua.user_id
        where lower(u.email) = lower(?) and ua.label = ?
        """,
        (email, label),
    ).fetchone()
    return row["id"] if row else None


def login(client, email, label=None):
    """Log in via the dev /login dropdown, then auto-pick or pick `label`."""
    uid = uid_of(email)
    if uid is None:
        return None
    client.post("/login", data={"user_id": uid}, follow_redirects=False)
    accs = conn.execute(
        "select id, label from user_access where user_id = ? order by sort_order",
        (uid,),
    ).fetchall()
    if len(accs) == 1:
        return accs[0]["id"]
    if label:
        aid = access_id_of(email, label)
        client.post("/select-access", data={"access_id": aid}, follow_redirects=False)
        return aid
    # Multi-access user without explicit label → leave on chooser
    return None


# ===========================================================================
# 1. Auth & access
# ===========================================================================
section("1. Auth & multi-access flow")

c = app.test_client()
r = c.get("/login")
check("GET /login renders", r.status_code == 200)
# Dropdown lists all 7 matrix users
body = r.get_data(as_text=True)
for em in ("anubhav@syncmedia.io", "varshesh@syncmedia.io", "rahul@syncmedia.io"):
    check(f"Login dropdown lists {em}", em in body)

# Single-access auto-pick (Anubhav)
c1 = app.test_client()
r = c1.post("/login", data={"user_id": uid_of("anubhav@syncmedia.io")}, follow_redirects=False)
check("Anubhav (single access) → /dashboard", r.headers.get("Location", "").endswith("/dashboard"))

# Multi-access redirect (Varshesh)
c2 = app.test_client()
r = c2.post("/login", data={"user_id": uid_of("varshesh@syncmedia.io")}, follow_redirects=False)
check("Varshesh (multi-access) → /select-access", r.headers.get("Location", "").endswith("/select-access"))
r = c2.get("/select-access")
labels = ["Total (West &amp; South)", "Total West", "West 2", "South 1"]
for label in labels:
    check(f"Chooser lists '{label}'", label in r.get_data(as_text=True))

# Forced redirect to chooser when accessing dashboard without active access
r = c2.get("/dashboard", follow_redirects=False)
check("Multi-access guard redirects /dashboard → /select-access",
      r.status_code in (301, 302) and r.headers.get("Location", "").endswith("/select-access"))

# Pick Total West
ws_id = access_id_of("varshesh@syncmedia.io", "Total West")
c2.post("/select-access", data={"access_id": ws_id}, follow_redirects=False)
r = c2.get("/dashboard")
check("After picking Total West, /dashboard renders", r.status_code == 200)

# Switch role drops active access
c2.get("/switch-role")
r = c2.get("/dashboard", follow_redirects=False)
check("/switch-role drops active access", r.headers.get("Location", "").endswith("/select-access"))

# Forged access_id: Varshesh tries Anubhav's access_id
forged = access_id_of("anubhav@syncmedia.io", "Pan India")
r = c2.post("/select-access", data={"access_id": forged}, follow_redirects=False)
check("Forged access_id returns 403", r.status_code == 403)


# ===========================================================================
# 2. Permission boundaries
# ===========================================================================
section("2. Permission boundaries — every role × forbidden routes")

forbidden = {
    "Anubhav (admin)": {
        "email": "anubhav@syncmedia.io",
        "expect_200": ["/dashboard", "/entries", "/admin/targets", "/admin/team",
                       "/admin/audit", "/nestle", "/nestle/entries"],
        "expect_403": [],
    },
    "Divyam (RH+manager)": {
        "email": "divyam@syncmedia.io",
        "label": "Total North",
        "expect_200": ["/dashboard", "/entries"],
        "expect_403": ["/admin/targets", "/admin/team", "/admin/audit", "/nestle", "/nestle/entries"],
    },
    "Varshesh (RH multi)": {
        "email": "varshesh@syncmedia.io",
        "label": "Total West",
        "expect_200": ["/dashboard", "/entries"],
        "expect_403": ["/admin/targets", "/admin/team", "/admin/audit", "/nestle"],
    },
    "Rahul (manager + Nestle owner)": {
        "email": "rahul@syncmedia.io",
        "expect_200": ["/dashboard", "/entries", "/nestle", "/nestle/entries", "/nestle/entries/new"],
        "expect_403": ["/admin/targets", "/admin/team", "/admin/audit"],
    },
    "Hatim (manager West 1)": {
        "email": "hatim@syncmedia.io",
        "expect_200": ["/dashboard", "/entries"],
        "expect_403": ["/admin/targets", "/admin/team", "/admin/audit", "/nestle", "/nestle/entries"],
    },
}

for role, spec in forbidden.items():
    cli = app.test_client()
    login(cli, spec["email"], spec.get("label"))
    for path in spec["expect_200"]:
        sc = cli.get(path).status_code
        check(f"{role} GET {path}", sc == 200, detail=f"got {sc}")
    for path in spec["expect_403"]:
        sc = cli.get(path).status_code
        check(f"{role} GET {path} → 403", sc == 403, detail=f"got {sc}")


# ===========================================================================
# 3. Dashboard role-stratification
# ===========================================================================
section("3. Dashboard role-stratification")

# Anubhav: region + team breakdowns
ca = app.test_client(); login(ca, "anubhav@syncmedia.io")
body = ca.get("/dashboard").get_data(as_text=True)
check("Admin sees Region table", "Region — target vs achieved" in body)
check("Admin sees Team table", "Team — target vs achieved" in body)
check("No 'Top performer' anywhere", "Top performer" not in body)
check("No 'Biggest deal' anywhere", "Biggest deal" not in body)
check("No 'Client mix'", "Client mix" not in body)
check("KPI strip has 'Plan'", '<span class="kpi__label">Plan</span>' in body)
check("KPI strip has 'Pending follow-ups'", 'Pending follow-ups' in body)
check("KPI strip dropped 'New revenue'", 'New revenue' not in body)
check("KPI strip dropped 'Existing revenue'", 'Existing revenue' not in body)

# Divyam Total North RH
cd = app.test_client(); login(cd, "divyam@syncmedia.io", "Total North")
body = cd.get("/dashboard").get_data(as_text=True)
check("RH has Team table", "Team — target vs achieved" in body)
check("RH does NOT have Region table", "Region — target vs achieved" not in body)

# Rahul manager (leaf)
cr = app.test_client(); login(cr, "rahul@syncmedia.io")
body = cr.get("/dashboard").get_data(as_text=True)
check("Manager has neither Region nor Team table",
      "Region — target vs achieved" not in body and "Team — target vs achieved" not in body)


# ===========================================================================
# 4. Targets matrix + carryover + audit
# ===========================================================================
section("4. Targets matrix + audit + carryover")

# Set targets for Q1
import re
ca = app.test_client(); login(ca, "anubhav@syncmedia.io")
body = ca.get("/admin/targets").get_data(as_text=True)
labels = re.findall(r'<strong>([^<]+)</strong>\s*<input type="hidden"', body)
target_q1 = {"North 1": 100, "North 2": 50, "West 1": 80, "West 2": 60, "South 1": 40}
form = {}
for i, label in enumerate(labels):
    form[f"label__{i}"] = label
    form[f"target__{i}__Q1"] = str(target_q1[label])
    for q in ("Q2", "Q3", "Q4"):
        form[f"target__{i}__{q}"] = "0"
ca.post("/admin/targets", data=form, follow_redirects=False)
audit_count = conn.execute("select count(*) from target_history").fetchone()[0]
check("target_history records 5 labels × 4 quarters = 20 baseline rows", audit_count == 20)

# Edit one target → adds an audit row
form["target__0__Q1"] = "150"  # bump first label
ca.post("/admin/targets", data=form, follow_redirects=False)
audit_after = conn.execute("select count(*) from target_history").fetchone()[0]
check("Editing one value adds exactly 1 audit row", audit_after == 21)

# /admin/audit?tab=targets shows changes
body = ca.get("/admin/audit?tab=targets").get_data(as_text=True)
check("Target audit tab renders", "Target changes" in body and labels[0] in body)

# Carryover: simulate post-Q1 today
from app.main import effective_target_for_scope, get_user
from flask import session as flask_session
admin_uid = uid_of("anubhav@syncmedia.io")
admin_aid = access_id_of("anubhav@syncmedia.io", "Pan India")
with app.test_request_context():
    flask_session["user_id"] = admin_uid
    flask_session["active_access_id"] = admin_aid
    user = get_user(admin_uid, access_id=admin_aid)
    bd_q2 = effective_target_for_scope(user, ["Q2"], 2026, today=date(2026, 8, 15))
    bd_q12 = effective_target_for_scope(user, ["Q1", "Q2"], 2026, today=date(2026, 8, 15))
    bd_q1 = effective_target_for_scope(user, ["Q2"], 2026, today=date(2026, 4, 30))
check("Q2 alone after Q1 end: carryover = sum(Q1 targets)", bd_q2["carryover"] == 380)
check("Q1+Q2 (Q1 in view): carryover = 0", bd_q12["carryover"] == 0)
check("Q2 alone but Q1 not yet ended: carryover = 0", bd_q1["carryover"] == 0)


# ===========================================================================
# 5. Team config flow
# ===========================================================================
section("5. Team config save + reconcile")

from pathlib import Path
cfg_text = Path("config/team_config.json").read_text()
cfg = json.loads(cfg_text)

# Valid edit: rename Hatim
hatim = next(u for u in cfg["users"] if u["email"] == "hatim@syncmedia.io")
hatim["name"] = "Hatim Renamed"
ca.post("/admin/team", data={"team_config_json": json.dumps(cfg)}, follow_redirects=False)
new_name = conn.execute("select name from users where email='hatim@syncmedia.io'").fetchone()["name"]
check("Valid team_config edit applied", new_name == "Hatim Renamed")
hatim["name"] = "Hatim Godhrawala"
ca.post("/admin/team", data={"team_config_json": json.dumps(cfg)}, follow_redirects=False)

# Invalid JSON
ca.post("/admin/team", data={"team_config_json": "not json"}, follow_redirects=False)
still_named = conn.execute("select name from users where email='hatim@syncmedia.io'").fetchone()["name"]
check("Invalid JSON rejected (DB unchanged)", still_named == "Hatim Godhrawala")

# Schema violation (bad role)
bad_cfg = json.loads(cfg_text)
bad_cfg["users"][0]["access"][0]["role"] = "wizard"
ca.post("/admin/team", data={"team_config_json": json.dumps(bad_cfg)}, follow_redirects=False)
admin_role = conn.execute("select role from users where email='anubhav@syncmedia.io'").fetchone()["role"]
check("Schema violation rejected", admin_role == "admin")


# ===========================================================================
# 6. Entries CRUD + RO gate + cross-book
# ===========================================================================
section("6. Entries CRUD + RO closure gate + cross-book guard")

# Add a temporary salesperson so we have someone to assign to
conn.execute(
    """insert into users (name, email, role, region, can_delete_own_entries, archived, auth_provider)
       values ('Test Sales', 'test.sales@syncmedia.io', 'salesperson', 'North', 0, 0, 'session')"""
)
conn.commit()
sp_id = conn.execute("select id from users where email='test.sales@syncmedia.io'").fetchone()["id"]

# Pipeline status — no RO required
ca2 = app.test_client(); login(ca2, "anubhav@syncmedia.io")
r = ca2.post("/entries/new", data={
    "client_name": "PipeCo", "client_type": "New", "assigned_user_id": str(sp_id),
    "quarter": "Q1", "entry_date": "2026-04-15",
    "plan_value": "1000", "confidence_pct": "50",
    "deal_status": "Pipeline",
}, follow_redirects=False)
check("Pipeline status saves with no RO data", r.status_code == 302)

# Closed Won missing RO — blocked
r = ca2.post("/entries/new", data={
    "client_name": "BadClose", "client_type": "New", "assigned_user_id": str(sp_id),
    "quarter": "Q1", "entry_date": "2026-04-15",
    "plan_value": "1000", "confidence_pct": "100",
    "deal_status": "Closed Won",
}, follow_redirects=False)
check("Closed Won missing RO is blocked (200 + errors)",
      r.status_code == 200 and b"RO number is required" in r.data)

# Full RO with file
fake = io.BytesIO(b"%PDF-1.4 e2e test"); fake.name = "ro.pdf"
r = ca2.post("/entries/new", data={
    "client_name": "GoodClose", "client_type": "Existing", "assigned_user_id": str(sp_id),
    "quarter": "Q1", "entry_date": "2026-04-15",
    "plan_value": "1000", "confidence_pct": "100",
    "deal_status": "Closed Won",
    "ro_number": "RO-E2E-001", "ro_date": "2026-04-20",
    "ro_amount_excl_gst": "5000", "gst_pct": "18",
    "ro_file": (fake, "ro.pdf"),
}, content_type="multipart/form-data", follow_redirects=False)
check("Full RO submission saves", r.status_code == 302)
saved = conn.execute("select * from revenue_entries where client_name='GoodClose'").fetchone()
check("ro_total auto-computed", saved["ro_total"] == 5900)
check("ro_file_path persisted as bare filename (post Fix #3)",
      saved["ro_file_path"]
      and "/" not in saved["ro_file_path"]
      and saved["ro_file_path"].endswith(".pdf"))

# Cross-book guard
nestle_cli = app.test_client(); login(nestle_cli, "rahul@syncmedia.io")
fake2 = io.BytesIO(b"%PDF-1.4 nestle"); fake2.name = "n.pdf"
nestle_cli.post("/nestle/entries/new", data={
    "quarter": "Q1", "entry_date": "2026-04-15",
    "deal_status": "Closed Won",
    "ro_number": "NESTLE-001", "ro_date": "2026-04-20",
    "ro_amount_excl_gst": "1000", "gst_pct": "18",
    "ro_file": (fake2, "n.pdf"),
}, content_type="multipart/form-data", follow_redirects=False)
nestle_id = conn.execute("select id from revenue_entries where book='nestle'").fetchone()["id"]

# Admin tries to cancel via /entries/<nestle_id>/cancel — should be 403
r = ca2.post(f"/entries/{nestle_id}/cancel")
check("Cross-book guard: /entries/<nestle_id>/cancel → 403", r.status_code == 403)

# Cancel via the proper Nestle path
r = ca2.post(f"/nestle/entries/{nestle_id}/cancel")
check("Admin can cancel a Nestle entry via /nestle/entries/<id>/cancel", r.status_code == 302)

# Nestle excluded from main rollup
body = ca2.get("/dashboard").get_data(as_text=True)
m = re.search(r'Achieved.*?target-hero__value">\s*([0-9.]+)', body, re.DOTALL)
hero_achieved = float(m.group(1)) if m else -1
# Active main entries: only "GoodClose" (RO 5900); PipeCo Pipeline status counts toward conversion but ro_total=0
check("Main dashboard hero excludes Nestle RO",
      hero_achieved == 5900,
      detail=f"got {hero_achieved}, expected 5900")


# ===========================================================================
# 7. CODEX FINDINGS — verify each fix now holds
# ===========================================================================
section("7. Codex adversarial findings — verify fixes")

import os, importlib

# 7a. Dev-login gate
# When SYNC_DEV_LOGIN=0, /login POST must be rejected.
os.environ["SYNC_DEV_LOGIN"] = "0"
import app.main as appmod
importlib.reload(appmod)
hardened_app = appmod.app
hardened_app.config["DEV_LOGIN_ENABLED"] = False  # belt + braces
ch = hardened_app.test_client()
r = ch.post("/login", data={"user_id": uid_of("anubhav@syncmedia.io")}, follow_redirects=False)
check("[CRITICAL fixed] /login POST returns 403 when SYNC_DEV_LOGIN=0", r.status_code == 403,
      detail=f"got {r.status_code}")
r = ch.get("/login")
check("[CRITICAL fixed] /login GET shows 'Sign in with Google' (no user picker)",
      b"Sign in with Google" in r.data and b"Select user (dev)" not in r.data)

# Reset to dev-login on for the rest of the harness
os.environ["SYNC_DEV_LOGIN"] = "1"
importlib.reload(appmod)
# Refresh the local handles to point at the reloaded module
from app.main import app as app_local
app.config["DEV_LOGIN_ENABLED"] = True

# 7b. Archived user bypass — should NO LONGER work
c_offboard = app.test_client(); login(c_offboard, "anubhav@syncmedia.io")
# Sanity: still admin pre-archive
pre = c_offboard.get("/admin/team").status_code
conn.execute("update users set archived = 1 where email = 'anubhav@syncmedia.io'")
conn.commit()
post = c_offboard.get("/admin/team", follow_redirects=False).status_code
conn.execute("update users set archived = 0 where email = 'anubhav@syncmedia.io'")
conn.commit()
check("[HIGH fixed] Pre-archive admin works, post-archive admin redirected/blocked",
      pre == 200 and post in (302, 403, 401),
      detail=f"pre={pre}, post={post}")

# 7c. RO files no longer reachable via /static
file_path_db = saved["ro_file_path"]  # bare filename now (post-fix)
c_anon = app.test_client()
# Old static URL — should 404
r = c_anon.get("/static/uploads/ro/" + (file_path_db or ""))
check("[HIGH fixed] Old /static/uploads/ro/ path returns 404 (file moved out)",
      r.status_code == 404, detail=f"got {r.status_code}")
# New /ro-doc/<id> — should 401/redirect when anonymous
r = c_anon.get(f"/ro-doc/{saved['id']}", follow_redirects=False)
check("[HIGH fixed] Anonymous /ro-doc/<id> rejected (401)", r.status_code == 401,
      detail=f"got {r.status_code}")
# Authorized admin can fetch
ca3 = app.test_client(); login(ca3, "anubhav@syncmedia.io")
r = ca3.get(f"/ro-doc/{saved['id']}")
check("[HIGH fixed] Authenticated admin /ro-doc/<id> serves the file", r.status_code == 200,
      detail=f"got {r.status_code} ({len(r.data)} bytes)")
# Non-authorized scope (Hatim — West only) cannot fetch a North entry's RO doc
ch2 = app.test_client(); login(ch2, "hatim@syncmedia.io")
r = ch2.get(f"/ro-doc/{saved['id']}", follow_redirects=False)
check("[HIGH fixed] Out-of-scope user /ro-doc/<id> returns 403", r.status_code == 403,
      detail=f"got {r.status_code}")

# 7d. Atomic mutation + audit — every mutation route now has exactly 1 commit
import inspect
for fn_name in ("cancel_entry", "uncancel_entry", "delete_entry", "undelete_entry",
                "nestle_cancel_entry", "nestle_uncancel_entry", "nestle_delete_entry"):
    src = inspect.getsource(getattr(appmod, fn_name))
    n = src.count("db.commit()")
    check(f"[MEDIUM fixed] {fn_name} has exactly 1 db.commit()", n == 1, detail=f"count={n}")


# ===========================================================================
# Summary
# ===========================================================================
section("Summary")
total = len(results)
passed = sum(1 for _, ok, _ in results if ok)
failed = total - passed
print(f"  Total: {total} · {PASS}: {passed} · {FAIL}: {failed}")
if failed:
    print()
    print("Failures:")
    for label, ok, detail in results:
        if not ok:
            print(f"  - {label}{(' — ' + detail) if detail else ''}")
sys.exit(0 if failed == 0 else 1)
