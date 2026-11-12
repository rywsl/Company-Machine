ALTER TABLE wakes ADD COLUMN not_before TEXT;
ALTER TABLE wakes ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
CREATE TABLE foreman_sessions (
  id          TEXT PRIMARY KEY,
  wake_id     TEXT NOT NULL,
  role        TEXT NOT NULL,
  runtime     TEXT NOT NULL,
  actor       TEXT NOT NULL,
  model       TEXT NOT NULL,
  epoch       TEXT NOT NULL,
  state       TEXT NOT NULL,
  exit_code   INTEGER,
  cost_usd    REAL NOT NULL DEFAULT 0,
  turns       INTEGER,
  outcome     TEXT NOT NULL DEFAULT '{"proposed":[],"reviewed":[]}',
  error       TEXT,
  started_at  TEXT NOT NULL,
  ended_at    TEXT
);
CREATE INDEX foreman_sessions_started ON foreman_sessions(started_at);
