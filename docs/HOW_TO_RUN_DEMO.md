# How to Run the Demo — GRX10 Automated Market Research Tool (Medtech APAC)

Everything you need to bring the demo up and walk it. Companion docs:
**`DEMO_SCRIPT.md`** (the 10-minute storyboard/narration) and **`GRX10_MarketResearch_Demo_Prebrief.pptx`** (the pre-demo framing deck).

Run the commands below in **Git Bash** (that's the shell everything was set up in). Windows paths use forward slashes there.

---

## 0. One-time prerequisites (already done on this machine)
- **Docker Desktop** installed and running.
- Python virtualenv at `%LOCALAPPDATA%\Temp\grx10-venv` (backend deps installed).
- Frontend run-copy at `%LOCALAPPDATA%\Temp\grx10-frontend` (`npm install` done; WorkOS stubbed for local viewing).
- Postgres data persists in the Docker volume `martechtool_grx10_pgdata` — the medtech data (198 cells, sources, encrypted Comtrade key) is already loaded and survives restarts.

> If the temp venv or temp frontend ever gets wiped, recreate per `RESUME.md`.

---

## 1. Set the environment (paste once per Git Bash session)

```bash
cd "C:/Users/PremArora/OneDrive - GRX10 Solutions Private Limited/Products/Martech Tool"
export PY="$LOCALAPPDATA/Temp/grx10-venv/Scripts/python.exe"
export DATABASE_URL="postgresql+psycopg://grx10:grx10@localhost:5433/grx10_market_research"
export CRED_MASTER_KEY="local-dev-master-key-2026"     # MUST match — decrypts the stored Comtrade key
export ENV=development
export ANTHROPIC_API_KEY="sk-ant-...your-key..."       # enables live LLM brief (Sonnet 4.6); omit to use the rule-based fallback
```

- `CRED_MASTER_KEY` must be exactly `local-dev-master-key-2026` or the stored connector credential won't decrypt.
- `ANTHROPIC_API_KEY` is read from the environment only — it is never written to a file in the repo. Without it, the NL brief still works via the deterministic fallback.

---

## 2. Start the three services

**a) Database** (start Docker Desktop first, then):
```bash
docker compose up -d db
# first time only (or after a schema change) — apply migrations:
docker compose run --rm migrate
```

**b) Backend API** → `http://localhost:8000`
```bash
"$PY" -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --log-level warning &
```

**c) Frontend** → `http://localhost:3000`
```bash
cd "$LOCALAPPDATA/Temp/grx10-frontend"
node node_modules/next/dist/bin/next dev -p 3000 &
cd "C:/Users/PremArora/OneDrive - GRX10 Solutions Private Limited/Products/Martech Tool"
```

Wait ~10 seconds after each. The frontend runs from the temp copy; if you edited frontend source, sync first:
`cp -r frontend/app frontend/components frontend/lib "$LOCALAPPDATA/Temp/grx10-frontend/"`

---

## 3. Verify it's up (30 seconds)

```bash
curl -s http://localhost:8000/health -o /dev/null -w "backend %{http_code}\n"        # expect 200
curl -s "http://localhost:8000/cells?limit=1" -o /dev/null -w "cells %{http_code}\n"  # expect 200
```

Then open **`http://localhost:3000`** in Chrome. You should land on the **Dashboard** showing **$97.41B**, 99 cells (2026), and three charts. If the charts look zoomed/cramped, press **Ctrl+0** to reset browser zoom.

---

## 4. Walk the demo

Follow **`DEMO_SCRIPT.md`** (the storyboard). The five beats, in order:

| # | Screen | What to show |
|---|---|---|
| 1 | **Dashboard** | Headline stats + confidence donut + market-by-family / by-geography charts. |
| 2 | **Cell Explorer → Cell Detail** | Open Coronary Stents · China · 2026 → **click an estimate row → the source panel → the live source URL**. This is the wow. |
| 3 | **Sources** | Sources by class A/B/C, "why it matters", and "used by: [methods]". |
| 4 | **New Brief** | Type a market brief in plain English → **Interpret** → the editable, sourced plan. Live LLM (Sonnet 4.6). |
| 5 | **Status / Connectors** | Frame it as a maintained asset that re-pulls on a schedule. |

Reliable pre-loaded example brief: *"Make a medtech market report for Southeast Asia, 2024–2029, focused on cardiovascular devices, exclude China."*

---

## 5. Presenter notes & safety
- **Don't run a full live ingestion on stage.** The model is pre-pulled real data; demo against it. Mention the live pull as "it refreshes weekly," don't trigger it.
- **The brief is live LLM** when the key is set (Sonnet 4.6) and falls back to rule-based if not — so it can't hard-fail in front of the client.
- **Auth is stubbed locally** (you appear as "Owner / Admin"); real WorkOS SSO is wired for deployment, not the local demo.
- **Two screens gate on real auth** in source (Connectors credential entry, Assumptions editing) — the local stub shows them fine.

## 6. Shut down
```bash
# stop the backend + frontend (find and kill the port listeners):
netstat -ano | grep -E ":8000|:3000" | grep LISTENING   # note the PIDs, then: taskkill //PID <pid> //F
docker compose stop db     # data is preserved in the volume
```

---

## Troubleshooting
| Symptom | Fix |
|---|---|
| Charts blank on first load | Wait ~5s and reload; press **Ctrl+0** to reset zoom. |
| A page 404s / "cannot find" | Backend probably restarting — recheck `/health`, then reload. |
| Brief returns generic result | `ANTHROPIC_API_KEY` not set → it used the rule-based fallback. Export the key and restart the backend. |
| Backend won't start | Confirm Docker `db` is healthy (`docker inspect --format '{{.State.Health.Status}}' grx10-mr-db`) and `CRED_MASTER_KEY` is set. |
| Frontend "next not recognized" | Launch via the node path shown above, not `npm run dev`, and run it from the **temp** copy (OneDrive corrupts `node_modules`). |
| Numbers look wrong / empty | Re-seed: `"$PY" -m backend.app.services.config_loader load` then `"$PY" scripts/seed_medtech_cells.py`. |
