CREATE TABLE schedule_queue (
  job_id     TEXT PRIMARY KEY,
  enqueued_at TEXT NOT NULL
);
CREATE TABLE dead_letters (
  job_id     TEXT PRIMARY KEY,
  station    TEXT NOT NULL,
  error_code TEXT NOT NULL,
  error      TEXT NOT NULL,
  at         TEXT NOT NULL
);
CREATE TABLE quarantines (
  bean_id TEXT PRIMARY KEY,
  reason  TEXT NOT NULL,
  by      TEXT NOT NULL,
  at      TEXT NOT NULL,
  source  TEXT NOT NULL CHECK (source IN ('manual', 'auto'))
);
CREATE TABLE cron_fires (
  line    TEXT PRIMARY KEY,
  last_at TEXT NOT NULL,
  run_id  TEXT NOT NULL
);
CREATE TABLE breaker_state (
  subject    TEXT PRIMARY KEY,
  state      TEXT NOT NULL CHECK (state IN ('closed', 'open', 'half_open')),
  changed_at TEXT NOT NULL,
  probes_out INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE rate_buckets (
  name       TEXT PRIMARY KEY,
  tokens     REAL NOT NULL,
  updated_at TEXT NOT NULL
);
