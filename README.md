# GRX10 Automated Market Research Tool

A generic, config-driven **market-sizing platform** that produces defensible,
source-traceable TAM estimates. Every number is drillable from a market *cell*
→ the *estimates* that triangulate it → the *source* → the *raw payload*. The
industrial power-components configuration in `config/` is a real reference
engagement, not a fixture — nothing industry-specific is hardcoded.

See [`docs/v1-definition.md`](docs/v1-definition.md) for the 13 locked decisions
and the full architecture; this README is the operational guide.

---

## What's in the box

| Path | What it is |
|---|---|
| `backend/`    | FastAPI service (Python 3.12, SQLAlchemy 2.0 / psycopg3, Pydantic v2). Maps to the existing Liquibase-owned schema — never generates migrations. |
| `frontend/`   | Next.js 14 App Router UI (RSC, Tailwind, Recharts/visx, WorkOS AuthKit). |
| `connectors/` | Pluggable source connectors (`probe()` / `pull()` / `normalize()` contract) + a family pattern. |
| `methods/`    | Pluggable estimation methods (`estimate()` contract → `cell_triangulation` rows). |
| `pipeline/`   | The ordered, idempotent run orchestrator (the Cron Job entrypoint). |
| `config/`     | Seed YAML + `players.csv` (the reference engagement). The **DB is authoritative** (Q11); YAML is seed + redacted backup only. |
| `db/changelog/` | Authoritative Liquibase schema (5 layers + raw-class extensions). |
| `render.yaml` | Render Blueprint: Postgres + API + frontend + pipeline Cron. |

### The data model in one breath
5 layers — **Spine** (taxonomy / geographies / methods / sources / profiles /
encrypted credentials) → **Raw** (`raw_*` tables, verbatim JSONB + typed cols)
→ **Cells** (`cells`, `cell_triangulation`, and the `cell_triangulation_summary`
materialised view that computes confidence) → **Players** → **Decisions**.

**Invariants:** no fact row without a non-null `source_id`; confidence is computed
*only* by the summary view (never a write-time human override); re-runs are
idempotent; `web_search_extraction` is hard-capped at LOW.

---

## Local development

### Prerequisites
- Python 3.12, Node 20+, PostgreSQL 16, and the [Liquibase CLI](https://docs.liquibase.com/start/install/home.html) (needs a JRE).

### 1. Database + schema
```bash
# Start a local Postgres 16 and create the database, then apply the schema.
createdb grx10_market_research
export DATABASE_URL="postgres://localhost:5432/grx10_market_research"
bash scripts/apply_changelog.sh      # runs `liquibase update` against DATABASE_URL
```
Liquibase tracks applied changesets in `DATABASECHANGELOG`, so this is safe to
re-run — only new changesets apply.

### 2. Seed the spine tables from config
```bash
cp .env.example .env            # then edit values
pip install -r requirements.txt
python -m backend.app.services.config_loader load        # config/ -> DB (idempotent)
```
- Taxonomy/subcategories are **versioned, not overwritten** — editing a name in
  `config/taxonomy.yaml` and re-loading bumps that row's `version`.
- Geographies, methods, companies and sources upsert idempotently.
- Export the DB back to YAML/CSV (secrets redacted) any time:
  ```bash
  python -m backend.app.services.config_loader export        # -> config/_export/
  ```

### 3. Run the API
```bash
uvicorn backend.app.main:app --reload --port 8000
```

### 4. Run the frontend
```bash
cd frontend && npm install && npm run dev        # http://localhost:3000
```

### 5. Run the pipeline manually
```bash
python -m pipeline.run                      # all stages, in order
python -m pipeline.run --stages ingest,normalize
python -m pipeline.run --since 2025-01-01   # ingestion lookback hint
```
Stages always execute in dependency order:
`ingest → normalize → size_cells → score_confidence → probe_health`. It prints a
JSON run report and exits non-zero if any stage failed. Every stage is
idempotent, so re-running is always safe.

---

## Deploy to Render

1. Push this repo to GitHub.
2. In Render: **New → Blueprint** and select `render.yaml`.
3. Fill the `grx10-shared` env-var group (marked `sync: false`): `WORKOS_API_KEY`,
   `WORKOS_CLIENT_ID`, `WORKOS_COOKIE_PASSWORD`, `CRED_MASTER_KEY`,
   `ANTHROPIC_API_KEY`, `SLACK_WEBHOOK_URL`; and `WORKOS_REDIRECT_URI` on the
   frontend service (`https://<frontend-url>/callback`).
4. Apply. Render provisions:
   - **grx10-mr-db** — managed Postgres 16 (`DATABASE_URL` wired automatically).
   - **grx10-mr-api** — FastAPI. Its `preDeployCommand` runs
     `scripts/apply_changelog.sh` so the schema migrates *before* new code goes
     live. The build step installs the Liquibase CLI.
   - **grx10-mr-web** — Next.js.
   - **grx10-mr-pipeline** — Cron Job running `python -m pipeline.run` weekly
     (`0 6 * * 1`); tighten the schedule to match your refresh cadence.
5. After the first deploy, seed config once from a shell on the API service:
   `python -m backend.app.services.config_loader load`.

> **`CRED_MASTER_KEY` is load-bearing:** it wraps every stored connector
> credential. Rotating it invalidates all stored secrets — rotate deliberately,
> with a credential re-entry plan.

---

## Where configuration lives

The **app/database is the single source of truth** (Q11). `config/*.yaml` and
`players.csv` are a *seed* on first boot and a *redacted backup* on export — not
a live mirror.

| Config | Authority | Notes |
|---|---|---|
| `taxonomy.yaml`     | YAML-seeded, **versioned** | Families/subcategories; HS + regulatory codes. |
| `geographies.yaml`  | YAML-seeded | `(country, segment)` — trade direction is first-class. |
| `methods.yaml`      | YAML-seeded | Method registry, tiers, source-class independence. |
| `sources.yaml`      | App-managed (seed only) | Re-seeding never clobbers health/budget/credential fields. |
| `players.csv`       | YAML-seeded | Seeded companies. |
| credentials, profiles, assumptions | App-managed | Never in YAML; credentials are encrypted at rest. |

Validation thresholds come from the **active** row in `validation_profiles`
(default **Standard**); the summary view reads it to compute confidence.

---

## How to add a connector

1. **Implement the contract** in `connectors/` (one module per source, or extend
   a family module). Subclass `connectors.base.Connector`:
   - `source_id`, `raw_table` class attributes.
   - `probe()` → a `ProbeResult` classified into the 7-state taxonomy
     (`OK`, `AUTH_FAILED`, `QUOTA_EXHAUSTED`, `RATE_LIMITED`, `UNREACHABLE`,
     `SCHEMA_MISMATCH`, `EMPTY`). Map HTTP errors: `402`/credit → `QUOTA_EXHAUSTED`,
     `429` → `RATE_LIMITED`, `401`/`403` → `AUTH_FAILED`, etc.
   - `pull(*, taxonomy, geographies, since)` → yields **verbatim** payloads.
   - `normalize(raw)` → a dict of typed columns for `self.raw_table`.
   - **Never fabricate data:** if the key is missing, `probe()` returns
     `AUTH_FAILED` and `pull()` yields nothing.
2. **Register it** in `connectors/registry.py` under its module name.
3. **Add a source row**: either via the Connectors admin UI (preferred — supports
   write-only credential entry) or by adding it to `config/sources.yaml` with
   `connector:` set to the module name and re-running the config loader. The
   `raw_table` must be one of the known `raw_*` tables.
4. **Enter credentials** (if any) write-only through the admin UI — stored
   encrypted in `connector_credentials`, referenced by `sources.auth_secret_ref`.
5. The next pipeline run picks it up automatically: `probe → pull → normalize`,
   then methods that declare that `raw_table` in `required_raw_tables` consume it.

See [`docs/connector-catalog.md`](docs/connector-catalog.md) for the full source
catalog, feasibility ratings, and the recommended build waves.
