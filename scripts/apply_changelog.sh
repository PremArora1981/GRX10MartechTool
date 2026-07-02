#!/usr/bin/env bash
# Apply the Liquibase changelog master to the database pointed at by DATABASE_URL.
#
# Used as the API service's Render preDeployCommand so the schema is brought up to
# date before new application code goes live. Idempotent: Liquibase tracks applied
# changesets in DATABASECHANGELOG, so re-running only applies what is new.
#
# Requires the `liquibase` CLI (+ a JRE) and the PostgreSQL JDBC driver on PATH.
# See README "Deploy to Render" for how the build step provisions these.

set -euo pipefail

CHANGELOG="${CHANGELOG_FILE:-db/changelog/changelog-master.sql}"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "ERROR: DATABASE_URL is not set" >&2
  exit 1
fi

# Parse the standard postgres://user:pass@host:port/dbname URL into the pieces
# Liquibase needs (a JDBC URL + separate username/password). Python is always
# present in the API service's runtime.
read -r JDBC_URL DB_USER DB_PASS < <(python - "$DATABASE_URL" <<'PY'
import sys, urllib.parse as u
p = u.urlparse(sys.argv[1])
host = p.hostname or "localhost"
port = p.port or 5432
db = (p.path or "/").lstrip("/")
# sslmode=require is the Render default for external connections.
query = "?sslmode=require" if "sslmode" not in (p.query or "") else f"?{p.query}"
jdbc = f"jdbc:postgresql://{host}:{port}/{db}{query}"
print(jdbc, u.unquote(p.username or ""), u.unquote(p.password or ""))
PY
)

echo "Applying Liquibase changelog '$CHANGELOG' to $JDBC_URL"

if command -v liquibase >/dev/null 2>&1; then
  liquibase \
    --changelog-file="$CHANGELOG" \
    --url="$JDBC_URL" \
    --username="$DB_USER" \
    --password="$DB_PASS" \
    update
else
  echo "ERROR: 'liquibase' CLI not found on PATH." >&2
  echo "Install it in the build step (see README) or run migrations manually." >&2
  exit 127
fi

echo "Liquibase changelog applied."
