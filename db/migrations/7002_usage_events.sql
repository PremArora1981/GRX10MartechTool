-- Usage logging for the alpha client-activity digest.
-- One row per real browser request (filtered in app: no health/bots/SSR).
-- Powers the daily usage email (counts + distinct IPs + geolocated city).
-- Idempotent.

CREATE TABLE IF NOT EXISTS usage_events (
    event_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    ip          text,
    path        text,
    user_agent  text
);

CREATE INDEX IF NOT EXISTS idx_usage_events_ts ON usage_events (ts);
