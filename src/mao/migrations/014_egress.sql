CREATE TABLE model_usage (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id     TEXT NOT NULL,
  at         TEXT NOT NULL,
  model      TEXT NOT NULL,
  cost_usd   REAL NOT NULL,
  tokens_in  INTEGER,
  tokens_out INTEGER
);
CREATE INDEX model_usage_job ON model_usage(job_id);
CREATE TABLE budget_stops (
  job_id TEXT PRIMARY KEY,
  code   TEXT NOT NULL,
  at     TEXT NOT NULL
);
