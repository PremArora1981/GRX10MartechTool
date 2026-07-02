# Automated Market Research Tool — v1 Definition

The build contract. Derived from `GRX10_Automated_MR_Tool_Generic_Spec.pdf` (v1.0) plus the decisions locked in the design interview. Where this document and the PDF differ, **this document wins** (it records conscious deviations).

Owner: GRX10 Solutions Private Limited · Reference engagement: Industrial power components · Target platform: **Render**

---

## 1. Locked decisions (Q1–Q13)

| # | Decision | Resolution |
|---|---|---|
| 1 | **Purpose** | A generic, reusable product built with real-engagement discipline. The industrial power-components config is a *real reference engagement* that must produce defensible, source-traceable numbers — not throwaway fixtures. Nothing industrial is hardcoded. |
| 2 | **Architecture** | Render PaaS. Pragmatic collapse of the spec's distributed AWS design into a small service set + one ordered pipeline. Connectors and methods stay pluggable (config-registered modules), not separate deployed services. |
| 3 | **Milestone** | Define *all* of v1 now (this doc). Build in dependency order; first runnable slice = "real cell → 2 real estimates → drill to source." |
| 4 | **Sources** | Keep SEC EDGAR as a universal connector + build the broad catalog in `docs/connector-catalog.md`. For the industrial config, the two first methods are `comtrade_hs4_import` (UN Comtrade) + `filings_segment_extraction` (**EDINET**, which gives structured XBRL segments for Murata/TDK). |
| 5 | **Confidence** | Configurable **validation profiles** (Light → Standard → Conservative → Audit-grade), with a clone-and-tweak escape hatch. Counting is `COUNT(DISTINCT method_code)` (fixes the spec's `COUNT(*)` bug). Independence is at the **method × source-class** level. Default profile = **Standard** (matches the spec's proven rule). |
| 6 | **Connectors** | Self-service connector layer, **in v1**. Rung (B): catalog connectors (in code) + a declarative generic-REST connector with **AI-assisted field mapping** + flagged scraping + **web-search fallback**. YAML becomes seed/export, not the only entry path. |
| 7 | **Validation** | 7-state connector-health taxonomy (`OK`, `AUTH_FAILED`, `QUOTA_EXHAUSTED`, `RATE_LIMITED`, `UNREACHABLE`, `SCHEMA_MISMATCH`, `EMPTY`) + a cost/budget pre-warning (🟠 at ~80% of `monthly_budget`/`quota_ceiling`) so "out of money" is caught before it blocks. |
| 8 | **Web-search fallback** | Writes a real estimate but **always Class C, hard-capped at LOW**, `method_code = web_search_extraction`, discovered URL auto-registered as a `source`, extraction snippet stored verbatim as raw payload. Can fill blanks + seed triangulation, never manufacture HIGH/MEDIUM. **On by default**, per-engagement toggle to disable. |
| 9 | **Credential security** | Envelope encryption in Postgres (`pgcrypto`, ciphertext-only at rest), write-only-from-UI (never returned to browser), admin-gated, rotation audit trail. Master key in a Render secret for v1 (KMS later if required). |
| 10 | **Auth** | **WorkOS** managed auth (SAML + OIDC to any customer IdP; email/Google login for pre-IdP engagements). Roles: `owner/admin` · `analyst` · `business` · `external`, mapped from IdP claims. `owner/admin` gates credential entry (Q9) and feeds Phase-3 audience switcher. |
| 11 | **Config authority** | The **app/database is the single source of truth**. YAML files are for initial setup (seed) + backups (export, secrets redacted) — not a live mirror. Taxonomy/geographies/methods are YAML-seeded + versioned; sources/credentials/assumptions/profiles are app-managed. |
| 12 | **Repo** | Single repo. Connectors are **plug-ins** (`pull()`/`normalize()`/`probe()` contract). Connector **families** (OCDS, ATS boards, newswire RSS, SDMX agencies) share one module driven by an endpoint list. |
| 13 | **Scope/sequencing** | **All of v1 — nothing cut.** Build order is dictated only by data dependencies. Two parallel tracks (below). |

---

## 2. Architecture on Render

| Plane | Spec (AWS) | v1 (Render) |
|---|---|---|
| Backend API | Fargate / FastAPI | Render **Web Service** (FastAPI, Python 3.12) |
| Frontend | Amplify / Next.js | Render **Web Service** (Next.js, RSC) |
| Database | RDS Postgres 16 | Render **Postgres** (16) |
| Raw payload archive | S3 | **JSONB in Postgres** (`raw_*.raw_json`) — no external object store in v1 |
| Scheduler | EventBridge | Render **Cron Job** (the ordered pipeline runner) |
| Ingestion/transform | Lambda / Batch / Step Functions | One pipeline process: `pull → normalize → size → score → refresh`, idempotent composite-key upserts |
| Secrets | Secrets Manager / Cognito | Render **secrets** (master key) + WorkOS (auth) |
| Failure handling | SQS DLQ + Slack | Pipeline ret/backoff + connector-health table + Slack webhook |
| Observability | CloudWatch / X-Ray | Render logs/metrics + a status page (pipeline freshness, connector health, cell coverage) |

**Invariants preserved from the spec (non-negotiable):** every fact row carries `source_id`, `method_code`, confidence, `_low`/`_high`; no fact row without a non-null source; cells sized by ≥2 independent methods before entering the model; confidence computed programmatically (no write-time human override); two-click drill from any number to the raw payload; trade direction / segmentation is a first-class dimension.

---

## 3. Data architecture (5 layers + raw-class extensions)

Same as the spec's five layers, with the raw layer extended for source classes the spec didn't enumerate:

- **Layer 0 — Raw:** `raw_trade_flows`, `raw_regulatory`, `raw_filings`, `raw_transcripts`, `raw_shipments`, `raw_external_metrics`, `raw_industry_reports`, `raw_patents`, **`raw_procurement`**, **`raw_standards`**, **`raw_news`**, **`raw_signals`** (new).
- **Layer 1 — Spine:** `taxonomy_families`, `taxonomy_subcategories`, `geographies`, `companies`, `sources`, `method_registry`, `assumptions`, **`validation_profiles`** (new), **`connector_credentials`** (new, encrypted).
- **Layer 2 — Cells:** `cells`, `cell_triangulation`, `cell_triangulation_summary` (the fixed `COUNT(DISTINCT)` view reading the active validation profile).
- **Layer 3 — Players:** `player_shares` (with `player_role`), `supplier_relationships`, `facilities`.
- **Layer 4 — Decisions:** `catalysts`, `recommendations`, `commentary` (new), `cell_assumption_link`.

See `db/changelog/` for the authoritative DDL.

---

## 4. Build plan — dependency-ordered, two tracks

**Foundation (everything depends on this):**
1. Repo scaffold + `render.yaml`.
2. DB schema (Liquibase changelogs, all layers).
3. Config loader (idempotent versioned upsert; YAML seed + export).
4. WorkOS auth + role mapping.

**Track A — data to value** (each step consumes the previous; ordered):
1. Connector framework (plug-in contract + family pattern) + Wave-1 connectors.
2. Ingestion pipeline (pull → normalize into `raw_*`).
3. Cell sizing (methods run against raw data → `cell_triangulation`).
4. Confidence engine (`cell_triangulation_summary` reading active profile).
5. Player shares.
6. PDF reports (Executive Audit, Gap Analysis, Player Shares) + Excel exports (hyperlinked sources).

**Track B — connector tooling** (independent of cells; parallel with A):
1. Connector admin UI (catalog, select, credential entry, 7-state health).
2. Declarative generic-REST connector + AI-assisted field mapping.
3. Flagged scraping support.
4. Web-search fallback (LOW-capped, auto source registration).
5. Assumptions ledger (reverse drill), commentary, audience switcher.

**Only sequencing rule:** anything that reads cell data comes after cells are real. Everything else is free to parallelize.

---

## 5. App screens (v1, all built)

1. **Login** (WorkOS).
2. **Cell Explorer** — filterable cells (subcategory × geography × year), TAM + band, confidence chip.
3. **Cell Detail** — estimates table (one row per method) → estimate → source (URL, publisher, accessed_at) → raw payload. Two-click audit chain.
4. **Connectors** — catalog list, select/add (incl. custom REST + AI mapping), credential entry, 7-state health + budget warnings.
5. **Players** — Top-N shares per cell, supplier relationships.
6. **Assumptions Ledger** — versioned assumptions, reverse drill to influenced cells.
7. **Reports** — three standard PDFs + custom builder; Excel export.
8. **Status** — pipeline freshness per source, connector health, cell coverage.
9. **Settings** — validation profile picker, web-search toggle, audience switcher.

---

## 6. Acceptance criteria (from spec §9, carried forward)

- Sub-second list views · two-click drill to source URL · every chart shows segment + confidence chip · every TAM shows its band · PDF has numbered clickable Sources page · Excel has `_README` sheet (scope, timestamp, methodology) · status page green within refresh window · pipeline idempotent (run-twice/diff) · all assumption changes via `superseded_by` (never overwritten) · WorkOS SSO works against the customer IdP.
