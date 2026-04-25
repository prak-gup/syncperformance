# Sync Performance - Phase 2 CRUD Screens

This phase adds backend-enforced revenue CRUD screens with role-aware visibility:

- Salesperson: can add/edit own entries and delete own entries only when permission is enabled.
- Manager: sees team entries and team roll-up.
- Regional Head: sees region entries and region roll-up.
- Admin: sees all entries and India-level roll-up.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app/main.py
```

Open `http://127.0.0.1:5000/login` and log in as a seeded demo user.

## Notes

- `client_type` is mandatory and enforced in schema + forms.
- Authorization is enforced in backend route handlers before all read/write operations.
- The previous SQL RLS file remains under `db/phase1_schema_rls.sql`.
