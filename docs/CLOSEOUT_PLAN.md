# Project Closeout Plan — GRX10 Automated Market Research Tool (Medtech APAC demo)

**Objective:** finish the tool to a fully working, client-demo-ready state — every screen populated with the real Jabil Medtech APAC data, the two-click drill chain working everywhere, charts + sources view + NL-brief opener built, and every screen QA'd. No open placeholders.

**Definition of done:** a client can be walked through the demo end to end (per the storyboard in `docs/DEMO_SCRIPT.md`) with no broken screens, no console/render errors, every number drillable to a real source, and the NL brief interpreting a request into a confirmed plan + recommended sources.

---

## Current state (baseline)
- ✅ Backend (FastAPI) + Postgres + pipeline run; 10 routers register.
- ✅ Real Medtech APAC data loaded: 198 cells, $97.4B total 2026 TAM (matches report), 48 HIGH / 134 MED / 16 LOW, 476 triangulation rows, 10 OEMs, 5 catalysts.
- ✅ Cell Explorer (list + filters) and Cell Detail (TAM band + confidence + triangulation table + source panel → source URL) working with real data.
- ✅ Bugs fixed: 204 routers, credential store, pipeline `_load_credential`, authkit version, Comtrade HS-version, `/api` route prefix, NUMERIC-string formatters, missing `/triangulation` endpoint, nested→flat shape, Next fetch caching.
- ⚠️ Frontend runs from a temp copy (`%LOCALAPPDATA%/Temp/grx10-frontend`) with WorkOS stubbed; backend runs from OneDrive source. Fixes are synced source↔temp manually.

## Scope of closeout (workstreams)
- **W1 — Charts / dashboard:** confidence-distribution donut, market-size-by-family bar, market-size-by-geography, headline stats ($97.4B, 198 cells, HIGH/MED/LOW). On the dashboard landing + Cell Explorer summary.
- **W2 — Sources view:** a screen listing the registered sources with class (A/B/C), health, and a plain-English **"why this source matters / what it's used for"** rationale, plus **recommended sources per method**. (Your explicit priority.)
- **W3 — NL-brief opener:** `POST /brief/interpret` — interpret a natural-language brief ("medtech, SE Asia, 2024–2029, cardiovascular, exclude China") into a structured config (families, geographies, years, constraints) + recommended sources with per-source rationale; frontend brief-intake screen with an **editable confirmation card**. LLM via `ANTHROPIC_API_KEY` with a deterministic rule-based fallback so it demos without a key.
- **W4 — Verify + fix remaining screens** against medtech data: Players, Catalysts, Assumptions Ledger, Reports (3 PDFs), Excel export (5 flavors), Status, Settings. Fix any shape/endpoint/formatter bugs (same class as the cell fixes).
- **W5 — QA + integration:** sync source↔temp, restart servers, sweep every endpoint (200 + valid JSON) and every page (renders, no error markers), compile all Python, produce + clear a punch list.
- **W6 — Closeout docs:** `DEMO_SCRIPT.md` (10-min storyboard), refresh `RESUME.md`, update memory.

---

## CHECKLIST (every item must be TRUE at done)

### Data & backend
- [ ] All 33 subcategories × 3 geographies × 2 years cells present and non-null TAM/confidence.
- [ ] `GET /cells`, `/cells/{id}`, `/cells/{id}/triangulation`, `/cells/{id}/triangulation-summary`, `/cells/{id}/players` → 200 + correct shape.
- [ ] `GET /players/relationships`, `/status/*`, `/settings/*`, `/assumptions*`, `/connectors` (catalog) → 200.
- [ ] New: `GET /sources` (registry + rationale) and `POST /brief/interpret` → 200 + correct shape.
- [ ] `POST /reports/{executive-audit|gap-analysis|player-shares}` → returns a download_url; the GET streams a valid PDF.
- [ ] `POST /exports/excel/{flavor}` → download_url; the XLSX streams with a _README sheet + hyperlinked sources.
- [ ] Every Python module compiles (`compileall` exit 0); all routers register (10+).

### Frontend screens (each renders real data, no runtime error)
- [ ] Dashboard — headline stats + at least 2 charts (confidence dist, market by family/geo).
- [ ] Cell Explorer — 198 cells, filters work, TAM band + confidence chip per row.
- [ ] Cell Detail — TAM header + band + rationale; triangulation table with tier badges + source names; row expands to source panel with clickable source URL; confidence chip present.
- [ ] Players — Top-N shares per selected cell (chart), the 10 OEMs, source-anchored.
- [ ] Sources — list with class + health + "why it matters" + recommended-per-method.
- [ ] Assumptions Ledger — renders (empty-state acceptable if no assumptions), add form present, reverse-drill wired.
- [ ] Reports — 3 standard report buttons generate downloadable PDFs; custom builder present; Excel buttons work.
- [ ] Status — per-source freshness, connector health, cell coverage.
- [ ] Settings — validation-profile picker (Light/Standard/Conservative/Audit-grade), web-search toggle, audience switcher.
- [ ] NL-Brief — intake box with example prompt; interprets to an editable spec card + recommended sources; "confirm" routes to the populated model.

### Acceptance criteria (from spec §9)
- [ ] Two-click drill from any number to a source URL — verified on ≥3 cells.
- [ ] Every chart shows segment/geography label + a confidence indicator.
- [ ] Every TAM shows its band. Every list view is sub-second.
- [ ] PDF has a numbered Sources page with clickable URLs; Excel has a _README sheet.
- [ ] No unhandled runtime errors on any screen (browser console clean of app errors).

### Closeout
- [ ] All temp fixes synced to OneDrive source (source is canonical + runnable).
- [ ] `docs/DEMO_SCRIPT.md` written (10-min storyboard: open on finished asset → drill wow → brief → confirm plan → confidence → maintained-asset close).
- [ ] `docs/RESUME.md` + memory refreshed.
- [ ] Final QA report: every checklist item ticked or explicitly flagged with reason.

---

## Execution mechanism
1. **Workflow (parallel implementers)** — one agent per workstream (W1–W4), each editing the OneDrive source (backend + its frontend files), self-checking (compile + logic).
2. **QA/integration stage** — one agent syncs source→temp, restarts servers, sweeps all endpoints + pages, compiles, and returns a punch list.
3. **Supervisor (main session)** — applies punch-list fixes, does the browser visual QA loop across all 9+ screens, writes `DEMO_SCRIPT.md`, and produces the final completion report. Drives to done without user intervention.

## Known constraints (handled, not blockers)
- **Anthropic key:** NL-brief LLM path needs `ANTHROPIC_API_KEY`. Built with a deterministic rule-based fallback → demos fully without a key; richer with one.
- **OneDrive/node_modules:** dev runtime stays in temp; source remains canonical; a sync+restart step precedes QA.
- **WorkOS:** stubbed for local viewing (temp only); real AuthKit intact in source for deploy.
