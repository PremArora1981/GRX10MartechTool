# RESUME — pick up here

> **2026-07-01 UPDATE — the demo is CLOSED OUT and working end to end on the real Medtech APAC data.**
> The "open blockers" listed further down (route-prefix, Comtrade 0-rows) are FIXED. Current state, the
> full checklist, and the 10-minute walkthrough live in `docs/CLOSEOUT_PLAN.md` + `docs/DEMO_SCRIPT.md`.
> The DB now holds the **Medtech APAC** data (198 cells, not the old 42 industrial). Same restart
> sequence below still applies. Read CLOSEOUT_PLAN.md first.

---

Local smoke test of the built v1 is **most of the way working**. Backend + DB + pipeline run; the
frontend renders. Postgres data persists in the Docker volume `martechtool_grx10_pgdata`, so the
schema, seeded spine (42 cells, 17 sources, 15 methods), and the **encrypted Comtrade credential**
all survive a machine restart. **CRED_MASTER_KEY must stay `local-dev-master-key-2026`** or the
stored Comtrade key won't decrypt.

## Restart sequence (run from repo root)

Repo root: `C:\Users\PremArora\OneDrive - GRX10 Solutions Private Limited\Products\Martech Tool`
Python venv: `%LOCALAPPDATA%\Temp\grx10-venv` · Temp frontend (local-view copy): `%LOCALAPPDATA%\Temp\grx10-frontend`

```bash
# 0. Start Docker Desktop first (wait for whale icon steady).
PY="$LOCALAPPDATA/Temp/grx10-venv/Scripts/python.exe"
export DATABASE_URL="postgresql+psycopg://grx10:grx10@localhost:5433/grx10_market_research"
export CRED_MASTER_KEY="local-dev-master-key-2026"

# 1. DB (data persists; migrate is idempotent — only needed if schema changed)
cd "<repo root>" && docker compose up -d db

# 2. Backend API (http://localhost:8000)
export ENV=development
"$PY" -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000   # run in background

# 3. Frontend (http://localhost:3000) — runs from the TEMP copy (see "local-view stubs")
cd "$LOCALAPPDATA/Temp/grx10-frontend" && node node_modules/next/dist/bin/next dev -p 3000

# If the temp venv/frontend were wiped: recreate venv (python -m venv + pip install -r requirements.txt),
# and re-copy frontend to temp + npm install --legacy-peer-deps + re-apply the local-view stubs below.
```

## Bugs FOUND & FIXED in the real source (persisted, done)
1. **204 routers** — `routers/connectors.py` + `routers/commentary.py`: `@router.delete(status_code=204)` on `-> None` made FastAPI infer `response_model=NoneType` (truthy) → tripped the 204 assert → both routers silently skipped. Fixed with explicit `response_model=None`.
2. **Credential `store()` add-vs-rotate** — `services/credential_service.py`: decided rotate-vs-add from `sources.auth_secret_ref` (which the YAML seed pre-populates), so the first save no-op'd an UPDATE. Fixed to key off whether the `connector_credentials` row exists.
3. **Pipeline `_load_credential`** — `pipeline/run.py`: imported the wrong module (`credentials` not `credential_service`) and called it without a session. Rewritten to use `credential_service.retrieve(session, cred_ref=...)`. (Credential now resolves + decrypts in-pipeline — verified.)
4. **Frontend `authkit-nextjs` version** — `frontend/package.json` pinned `^0.14.1` (does not exist). Bumped to `^4.2.0`. NOTE: 4.x wants Next 15; we're on Next 14.2.5 → needs `--legacy-peer-deps` and it still fails to resolve `@workos-inc/node` at build (see open issue #1).
5. **docker-compose migrate** — added `--search-path=/liquibase/changelog` so Liquibase finds the changelog.

Also added earlier: `routers/reports.py` (new) and `services/web_search.py` (new).

## OPEN ISSUES — fix these tomorrow (in priority order)

### #1 — Frontend↔backend route-prefix mismatch (BLOCKER for data in UI)
Frontend `lib/api.ts` calls bare paths (`/cells`, `/reports/{type}`, `/exports/excel/{flavor}`); backend
mounts `/api/cells`, `/api/export`, and my `routers/reports.py` is at `/reports` (no `/api`). Inconsistent
across the codebase. Local workaround applied: set `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api`
in the temp `.env.local` — fixes cells/players/status but then `/reports` + `/exports/excel` 404
(reports router has no `/api`, export router is `/api/export/xlsx` not `/api/exports/excel/...`).
**Real fix:** pick ONE convention. Recommend: give every backend router the `/api` prefix (reports →
`/api/reports`, rename export route to `/api/exports/excel/{flavor}` or fix the frontend path), and set
base = `.../api`. Was mid-reload-with-`/api`-base when paused — re-screenshot Cell Explorer to confirm 42 cells show.

### #2 — Comtrade pipeline ingest lands 0 rows (root cause NOT found)
Credential resolves, `missing_credential()=False`, probe OK, and a **direct `conn.pull(...)` makes real
API calls** (ran 2+ min). But `stage_ingest` finishes in <1s with `raw_rows_inserted: 0`. Not the
taxonomy shape (matches the working direct test), not the credential, not the probe (recorded OK).
Suspects to check next: (a) does `stage_ingest` actually reach the pull loop, or skip at probe/raw_table
guard? add a log of how many sources it iterates + whether comtrade enters the `engine.begin()` block;
(b) the whole pull is wrapped in ONE `engine.begin()` that commits only at the end — confirm it isn't
silently rolling back; (c) `_lookback_years(since)` with `--since 2024-01-01` → [2024,2025,2026]; confirm
2024 calls actually fire (add the "pull starting — N calls" log to output). Reproduce with:
`"$PY" -m pipeline.run --stages ingest --anchor-year 2024 --since 2024-01-01` and DON'T filter the logs.
Note: only `un_comtrade` is currently `enabled=true` (others disabled for the focused run) — re-enable
with `UPDATE sources SET enabled=true;` when done.

### #3 — Connector design: can't target a single year
`comtrade._lookback_years` always queries start_year..current_year, wasting calls on empty lag years
(2025/2026). Consider passing the anchor year through to `pull()` so ingest year == cell year.

### #4 — 3 catalog-only methods unimplemented
`regulatory_count_unit_price`, `shipment_aggregation`, `standards_membership_proxy` — registry correctly
reports them unimplemented (not faked). Implement or mark out-of-scope.

## Local-view stubs (TEMP copy only — `%LOCALAPPDATA%\Temp\grx10-frontend`, NOT in real source)
To render the UI without a real WorkOS tenant, these temp files were replaced with mocks:
- `middleware.ts` → no-op pass-through
- `lib/auth.ts` → `withAuth()` returns a mock `owner` user (arvind@grx10.com); no WorkOS import
- `app/callback/route.ts`, `app/logout/route.ts` → simple redirects
- `.env.local` → dummy WORKOS_* + `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api`
These are intentionally NOT in the OneDrive source (which keeps real AuthKit). The real fix is to make
the source run locally without WorkOS env (auth.ts already try/catches — the blocker was the bad authkit
version + Next15 peer; revisit whether a Next14-compatible authkit version exists, or bump to Next 15).

## What's PROVEN working
Liquibase 24 changesets → 31 tables + view · app imports · 10/10 routers register · 18 connectors +
12 methods discovered · config_loader seeds spine · pipeline builds exactly 42 cells, sizing + confidence
view refresh run · 4 no-auth connectors probe OK live (world_bank, gdelt, usaspending, sec_edgar) ·
credential envelope-encryption store/retrieve round-trips · Comtrade live auth HTTP 200 · frontend renders
(Cell Explorer nav + filters + table, mock owner user).
