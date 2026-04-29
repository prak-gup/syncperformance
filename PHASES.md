# Build Phases

## Phase 1 (Completed)
- Create normalized schema for users, hierarchy, revenue entries, and targets.
- Add required `client_type` enum (`New` / `Existing`) to enforce client classification.
- Enforce hierarchy + permissions at database level using RLS, permission-aware helpers, and integrity triggers.

## Phase 2 (Completed in this commit)
- Build revenue entry CRUD screens (add/edit/delete/list) with backend permission checks.
- Add role-scoped dashboard roll-ups for salesperson, manager, regional head, and admin.
- Keep `client_type` required in both form input and persisted schema.

## Phase 3 (Completed in this commit) — Sales-tool maturity
- **Targets**: per-salesperson per-quarter targets in the new `targets` table; manager / regional / national auto-rollup as `SUM(descendants)`. National admin sets them at `/admin/targets`.
- **Top hero on dashboard**: Target / Achieved / % Achieved with progress bar.
- **Confidence formula**: Pipeline now derives from `Plan × Confidence%` server-side; the form shows pipeline as a read-only output that updates live as the user types.
- **Negotiation**: renamed from `negotiation_stage` → `negotiation`, constrained to Yes/No/(blank).
- **Soft-cancel + soft-delete**: Cancel = anyone with edit access (excludes from funnel, stays visible). Delete = admin only, soft (hidden from non-admin). Data is **never destroyed** — every mutation writes a versioned snapshot to `revenue_entry_history`.
- **Audit log**: `/admin/audit` shows the full history with per-record snapshot diffs and filters by action / actor / entry.
- **Multi-quarter filter**: dashboard quarter chip is now a multi-select toggle group (Q1+Q2+...).
- **Status filter**: Active (default) / Cancelled only / All. Charts and rollups exclude cancelled when Active.
- **"NEW" highlight**: entries created in last 7 days get an Apple System Green left border + pill on the entries page.
- **Team config**: `config/team_config.json` is the source of truth for users + hierarchy + regions + fiscal calendar. National admin edits it via `/admin/team`; on save the users table is reconciled additively (no destructive sync).
- **Google Auth provision**: `/auth/google/start` and `/auth/google/callback` stubs return 501 with help text and document the env vars + the `resolve_user_by_email` helper a developer should call.
- **Form alignment**: inputs / selects / textareas all use `--r-input: 12px`, `padding: 12px 16px`, hairline border, and shared focus state.

## Phase 4 (Next)
- Pipeline coverage ratio, sales velocity, stale-deal alerts, CSV/PDF export.
- Postgres + RLS migration referencing the existing SQL in `db/phase1_schema_rls.sql`.
- Replace dev session-by-id login with the wired Google OAuth callback.
