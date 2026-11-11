CREATE TABLE runs (
  id             TEXT PRIMARY KEY,
  line           TEXT NOT NULL,
  epoch          TEXT NOT NULL,
  state          TEXT NOT NULL,
  plan           TEXT NOT NULL,
  plan_digest    TEXT NOT NULL,
  inputs         TEXT NOT NULL,
  variants       TEXT NOT NULL DEFAULT '{}',
  budget_cap_usd REAL NOT NULL,
  spent_usd      REAL NOT NULL DEFAULT 0,
  reserved_usd   REAL NOT NULL DEFAULT 0,
  outputs        TEXT NOT NULL DEFAULT '{}',
  error          TEXT,
  trigger        TEXT NOT NULL,
  rebuilt_from   TEXT REFERENCES runs(id),
  use_cache      INTEGER NOT NULL DEFAULT 1,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE TABLE run_nodes (
  run_id       TEXT NOT NULL REFERENCES runs(id),
  node_id      TEXT NOT NULL,
  ord          INTEGER NOT NULL,
  state        TEXT NOT NULL,
  job_id       TEXT,
  cache_hit    INTEGER NOT NULL DEFAULT 0,
  outputs      TEXT NOT NULL DEFAULT '{}',
  params       TEXT,
  reserved_usd REAL NOT NULL DEFAULT 0,
  error        TEXT,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (run_id, node_id)
);
CREATE TABLE run_node_jobs (
  run_id TEXT NOT NULL, node_id TEXT NOT NULL, job_id TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, node_id, job_id)
);
CREATE TABLE bean_labels (
  bean_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, set_by TEXT NOT NULL, at TEXT NOT NULL,
  PRIMARY KEY (bean_id, key)
);
CREATE TABLE trigger_fires (
  line TEXT NOT NULL, bean_id TEXT NOT NULL, run_id TEXT NOT NULL, at TEXT NOT NULL,
  PRIMARY KEY (line, bean_id)
);
CREATE TABLE cursors (name TEXT PRIMARY KEY, seq INTEGER NOT NULL);
ALTER TABLE events ADD COLUMN run_id TEXT;
CREATE INDEX events_run ON events(run_id, seq);
