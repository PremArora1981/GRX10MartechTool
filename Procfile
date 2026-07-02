# Process definitions (Heroku/Render-style; render.yaml is the primary blueprint).
# `web` serves the FastAPI API; `release` applies the Liquibase changelog before
# a new release goes live; `pipeline` is the ordered ingestion runner (Cron Job).
web: uvicorn backend.app.main:app --host 0.0.0.0 --port $PORT
release: bash scripts/apply_changelog.sh
pipeline: python -m pipeline.run
