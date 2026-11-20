CREATE TABLE job_keys (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id     TEXT NOT NULL UNIQUE,
  job_key    TEXT NOT NULL,
  variant    TEXT NOT NULL DEFAULT '',
  material   TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX job_keys_key ON job_keys(job_key);
CREATE TABLE cache_invalidations (
  id      TEXT PRIMARY KEY,
  scope   TEXT NOT NULL CHECK (scope IN ('key', 'job', 'station', 'bean')),
  value   TEXT NOT NULL,
  max_seq INTEGER NOT NULL,
  reason  TEXT NOT NULL,
  by      TEXT NOT NULL,
  at      TEXT NOT NULL
);
