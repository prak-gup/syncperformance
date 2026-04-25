# Build Phases

## Phase 1 (Completed)
- Create normalized schema for users, hierarchy, revenue entries, and targets.
- Add required `client_type` enum (`New` / `Existing`) to enforce client classification.
- Enforce hierarchy + permissions at database level using RLS, permission-aware helpers, and integrity triggers.

## Phase 2 (Completed in this commit)
- Build revenue entry CRUD screens (add/edit/delete/list) with backend permission checks.
- Add role-scoped dashboard roll-ups for salesperson, manager, regional head, and admin.
- Keep `client_type` required in both form input and persisted schema.

## Phase 3 (Next)
- Seed scripts and automated access-matrix tests against PostgreSQL RLS policies.
- Add API token/session auth and bind DB user context for policy-driven access.

## Phase 4
- Production UI polish, charts, and exportable reporting screens.
