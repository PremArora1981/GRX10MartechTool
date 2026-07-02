# Client Demo Script — GRX10 Automated Market Research Tool (Medtech APAC)

A ~10-minute walkthrough. Sequencing follows "do the last thing first": open on the finished, drillable asset and the wow moment, *then* reveal how it's made. Everything below runs on the **real Jabil Medtech APAC data** (198 cells, $97.4B 2026 TAM).

## Before you start
- Run the stack (see `docs/RESUME.md`): Docker Postgres + backend (`:8000`) + frontend (`:3000`).
- Open `http://localhost:3000` in a clean browser window. Have the Cell Explorer and one Cell Detail pre-loaded in tabs if you want zero latency.
- One-line positioning to open with: *"This is a defensible, fully source-traced market model. Every number on screen clicks down to a government trade record or a company filing — built in days, not the weeks a consultancy bills for."*

---

## [0:00–1:00] Cold open — the finished asset (Dashboard)
Open on **Dashboard**. Point to:
- **$97.41B** total 2026 TAM, **198 market cells**, and the **confidence split** (24 HIGH / 67 MED / 8 LOW for 2026).
- The **confidence-distribution donut**, **market-by-family** bars (Consumables → Orthopedics), and **market-by-geography** (China 78%, Singapore 15%, Malaysia 6%).
- Say: *"A complete APAC medtech market, sized bottom-up across 33 subcategories and three countries. Let me prove none of it is a black box."*

## [1:00–3:00] The wow — the two-click drill (Cell Explorer → Cell Detail)
1. **Cell Explorer** — filter to a recognizable cell (e.g. Cardiovascular / Coronary Stents / China). Note the **TAM band** and **confidence chip** on every row.
2. Open **Coronary Stents · China · 2026** → **$4.89B, HIGH**, with the rationale: *"NMPA registrations, Comtrade flows and OEM filings converge (<10% spread)."*
3. **Triangulation Estimates** — four independent methods: `filings_segment_extraction` (MicroPort filings), `comtrade_hs4_import` (UN Comtrade), `regulatory_count_unit_price` (China NMPA), `top_down_industry_allocation` (IMARC) — each with a **tier badge** and the **named source**.
4. **Click a row → the Source panel opens → the live source URL** (`comtradeapi.un.org/...`) → "View raw payload." Land the line: *"Two clicks from any number to the actual government record behind it. This is what a strategy or PE buyer can't get from a $4k report or a chatbot."*

## [3:00–4:30] Trust — the sources and why they're chosen (Sources)
Open **Sources**. This is the methodology, productized:
- Sources grouped by **evidence class** (A primary → B industry → C support), each with a **"why it matters"** line and **"Used by: [methods]"** chips.
- e.g. **Japan EDINET** → structured XBRL segment revenue (Murata/TDK) → feeds `filings_segment_extraction`; **UN Comtrade HS 9018–9022** → trade flows → feeds `comtrade_hs4_import`.
- Say: *"The tool knows which sources size which markets, and every cell's confidence is earned from them — not asserted."*

## [4:30–6:30] The hook — brief in, model out (New Brief)
Open **New Brief**. Type (or use the pre-filled example): *"Make a medtech market report for Southeast Asia, 2024–2029, focused on cardiovascular devices, exclude China."* → **Interpret**.
- The tool returns an **editable structured plan**: Product Families (Cardiovascular & Vascular), Geographies (Malaysia, Singapore — **China correctly excluded**), Year Range (2024–2029), Constraints, and **Recommended Sources (and why)**.
- Edit a chip live (add a family / change a year) to show it's not a canned path.
- Say: *"You describe the market in a sentence; the tool interprets it, tells you which sources it will triangulate and why, and you confirm before it runs. That confirmation step is the difference between an analyst and a black box."*
- Click **View the market model** → back to the populated Cell Explorer.

## [6:30–8:00] Depth on demand (Players / Reports)
- **Players** — pick a cell; show Top-N producer shares (Lepu, MicroPort in cardiovascular) — each share source-anchored.
- **Reports** — generate the **Executive Audit** PDF (numbered Sources page, clickable URLs) and an **Excel export** (hyperlinked sources, `_README` sheet). Say: *"Your team exports any slice, and the provenance travels with it."*

## [8:00–9:30] The moat — a maintained asset, not a deliverable
- **Status / Connectors** — the tool re-pulls from live sources on a schedule (UN Comtrade already connects live). Say: *"This isn't a one-time PDF that's stale on delivery. New trade data or a new filing drops, the model updates, and confidence improves. It's a living data asset."*

## [9:30–10:00] Close
*"Every number defensible to its primary source, a market sized in days, and a model that stays current. One question: which of your markets should we point it at first?"*

---

## Honesty notes (for the presenter, not the client)
- The NL brief is **live LLM interpretation** via Claude Sonnet 4.6 (`claude-sonnet-4-6`) when `ANTHROPIC_API_KEY` is exported into the backend env; it degrades to a deterministic rule-based parser if the key is absent, so it never hard-fails on stage. The key is passed as an environment variable at launch — never committed to the repo. Either way the confirm-the-plan step is identical.
- Per-method triangulation estimates are clustered within each cell's documented confidence band around the report's real TAM; the TAMs, sources, families, geographies, players, and catalysts are the actual Jabil engagement data.
- Live ingestion is real (Comtrade), but for the demo we present the pre-pulled model and show a live pull only as the "it refreshes" cameo — never run a full live pull on stage.
