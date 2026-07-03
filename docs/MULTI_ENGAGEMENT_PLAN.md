# Multi-Engagement from a Brief — Build Plan

Turns the single-engagement tool (one Medtech APAC dataset) into a multi-engagement
platform: a user submits any brief, confirms the plan, and gets a real new engagement
(its own taxonomy, geographies, cells, sources) they can switch to — without disturbing
the Medtech demo. Design decisions locked via the grill-me interview (2026-07-03).

## Locked decisions

| # | Decision | Resolution |
|---|----------|------------|
| 1 | Isolation | `engagement_id` FK, **row-scoped**, one shared DB/schema |
| 2 | Sources | **Per-engagement source rows** (each engagement owns its catalog, health, creds, budgets) |
| 3 | Subcategories | **Brief LLM emits** `proposed_subcategories` (name + HS/regulatory codes) + candidate endpoint per proposed connector |
| 4 | Initial state | Empty structure + **web-search auto-seed** (LOW-capped, on consent) |
| 5 | Active engagement | **Cookie + `X-Engagement-Id` header**; FastAPI dep scopes every query; default = Medtech |
| 6 | Proposed connectors | **Generic-REST auto-map**, probe synchronously, degrade to placeholder on fail |
| 7 | Auto-seed job | **Render one-off Job** (Jobs API) on Render; local subprocess for parity |
| 8 | Switcher | **Nav-top dropdown**: create / switch / archive; Medtech protected |
| 9 | Cost ceiling | **Cap grid (≤ ~120 cells)** + confirm cost banner before launching the seed job |
| 10 | Migration/settings | Backfill existing → `Medtech APAC` (is_demo, protected, default). `active_profile` + `web_search_enabled` move onto the engagement row; `method_registry` + profile **definitions** stay global |

## Data model

New `engagements` table:
```
engagements(
  engagement_id      text PRIMARY KEY,        -- e.g. 'eng_medtech', 'eng_<slug>_<n>'
  name               text NOT NULL,
  is_demo            boolean NOT NULL DEFAULT false,   -- protected from archive/delete
  status             text NOT NULL DEFAULT 'active',   -- active | archived
  active_profile     text NOT NULL DEFAULT 'Standard', -- FK-ish to validation_profiles.name
  web_search_enabled boolean NOT NULL DEFAULT true,
  brief_text         text,
  created_at         timestamptz DEFAULT now()
)
```

`engagement_id` NOT NULL FK added to (all default to `eng_medtech` on backfill):
- Spine: `taxonomy_families`, `taxonomy_subcategories`, `geographies`, `sources`, `companies`
- Cells: `cells`, `cell_triangulation`
- Players: `player_shares`, `supplier_relationships`, `facilities`
- Decisions: `catalysts`, `recommendations`, `commentary`, `assumptions`, `cell_assumption_link`
- Raw: all 12 `raw_*` tables
- Credentials: `connector_credentials` (already keyed by source_id → inherit)

**Global (unscoped):** `method_registry`, `validation_profiles` (definitions), `credential_audit`,
`databasechangelog*`.

**Unique-constraint changes** (must include engagement_id):
- `cells (subcategory_id, geography_id, year)` → `(engagement_id, subcategory_id, geography_id, year)`
- `geographies (country, segment)` → `(engagement_id, country, segment)`
- `sources`: **namespaced `source_id` per engagement** — keep `source_id` as the single-column text PK,
  add an `engagement_id` column for filtering. Medtech keeps its current ids (`un_comtrade`, …, all
  `eng_medtech`); new engagements get namespaced ids (`eng_<slug>__un_comtrade`). Every `raw_*.source_id`
  and `connector_credentials.source_id` FK stays **single-column, unchanged** — no ripple. (Revised from
  an earlier composite-PK idea, which touched every raw FK for no added benefit.)

**Materialized view** `cell_triangulation_summary`: add `engagement_id`; the `active` profile CTE
becomes **per-engagement** (join `cells → engagements.active_profile → validation_profiles`) instead
of the single global `is_active` row. `REFRESH` stays global (covers all engagements at once).

## Phases (dependency-ordered)

### Phase 1 — Schema + migration (FOUNDATION, sequential, self)
- New Liquibase changesets `7001-*`: create `engagements`; seed `eng_medtech` (is_demo);
  add `engagement_id` columns (nullable) → backfill all rows to `eng_medtech` → set NOT NULL + FK;
  rebuild unique constraints; recompose `sources`/`raw_*` FKs to `(engagement_id, source_id)`;
  redefine the matview per-engagement.
- Apply to local DB; verify Medtech data intact (198 cells still queryable).
- **Gate:** nothing else starts until this is green.

### Phase 2 — Model + scoping primitive (sequential, self)
- `backend/app/models.py`: add `Engagement`; add `engagement_id` to every scoped model + composite
  source FK; update the matview model.
- `backend/app/deps.py`: `EngagementDep` — reads `X-Engagement-Id` header / `engagement_id` query /
  cookie, defaults to `eng_medtech`, 404s unknown ids.
- `backend/app/schemas.py`: `EngagementOut`, add `engagement_id` where surfaced.
- **Gate:** compiles; defines the exact pattern routers will copy.

### Phase 3 — Router scoping (PARALLEL across agents, independent files)
Each agent scopes one router group to the active engagement (add `EngagementDep`, filter every query,
stamp `engagement_id` on writes). Independent files → parallelizable:
- 3a: `cells.py` + `players.py` + `stats.py`
- 3b: `sources.py` + `connectors.py` (per-engagement source rows + generic-REST config)
- 3c: `status.py` + `reports.py` + `export.py`
- 3d: `assumptions.py` + `commentary.py` + `reference.py` + `settings.py` (per-engagement profile/toggle)

### Phase 4 — Engagement CRUD + materialize + brief extension (self + 1 agent)
- New `routers/engagements.py`: `GET /engagements`, `POST /engagements` (from confirmed brief plan),
  `POST /engagements/{id}/archive`, `POST /engagements/{id}/activate` (sets cookie).
- New `services/engagement_materialize.py`: create taxonomy + geographies + per-engagement sources
  (generic-REST config from brief endpoints) + probe + degrade + build capped cell grid (anchor years,
  ≤120). Returns cost estimate (N cells).
- `services/seed_job.py`: launch Render one-off Job (Jobs API) scoped to `ENGAGEMENT_ID`; local
  subprocess fallback. `pipeline/run.py` gains `--engagement <id>` + web-search-only sizing path.
- `routers/brief.py`: LLM emits `proposed_subcategories` + per-connector candidate endpoint.

### Phase 5 — Frontend (PARALLEL, mostly independent files)
- 5a: `lib/api.ts` (inject `X-Engagement-Id`) + `lib/types.ts` (Engagement, extend brief types) +
  `lib/swr.ts` cache keys include engagement.
- 5b: `components/EngagementSwitcher.tsx` + wire into `NavShell.tsx`.
- 5c: `app/brief/page.tsx` — subcategory review section, "Create engagement" CTA, cost banner,
  post-create switch + route to dashboard.
- 5d: engagement management (archive) surface.

### Phase 6 — Integrate, QA, deploy (self)
- Typecheck/compile gates; `next build`.
- Browser QA: Medtech unchanged; create a drone/LatAm engagement end-to-end; switch back and forth;
  drill chain + players + reports per engagement; archive.
- Migrate schema on Render (new changesets), push, redeploy, verify live.

## Risks / watch-items
- Source-id namespacing must be applied consistently at materialize time (`eng_<slug>__<base>`); the
  scoping dep filters `sources` by `engagement_id`, so raw FKs stay single-column and safe.
- Matview per-engagement active profile — test HIGH/MED/LOW still computes for Medtech.
- Parallel router agents must all copy the **exact** `EngagementDep` pattern from Phase 2 — pin it in the
  prompt to avoid drift.
- Render Job needs `RENDER_API_KEY` in the API service env + the pipeline service as the job source.
- Cost ceiling must be enforced server-side (not just UI) before any seed job launches.
- **SECURITY GATE (deferred to WorkOS):** `get_engagement_id` currently trusts the
  header/query/cookie with no membership check. This is safe ONLY under anonymous-owner mode
  (single principal, no user store). Before multi-user / production, add
  `engagement_members(engagement_id, user_id)` and enforce membership in `get_engagement_id`
  (take `CurrentUserDep`, 403 on non-membership, no silent fallback for unauthorized ids). Flagged
  by automated security review 2026-07-04; accepted as deferred because there is no user boundary yet.
