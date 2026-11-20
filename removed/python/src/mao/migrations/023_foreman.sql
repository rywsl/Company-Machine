CREATE TABLE wakes (
  id         TEXT PRIMARY KEY,
  signal     TEXT NOT NULL,
  subject    TEXT NOT NULL,
  payload    TEXT NOT NULL,
  status     TEXT NOT NULL,
  by         TEXT,
  at         TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE conversion_requests (
  id            TEXT PRIMARY KEY,
  from_type     TEXT NOT NULL,
  guarantees    TEXT NOT NULL,
  to_type       TEXT NOT NULL,
  requires      TEXT NOT NULL,
  found_at      TEXT NOT NULL,
  samples       TEXT NOT NULL,
  blocked_runs  TEXT NOT NULL,
  status        TEXT NOT NULL,
  changeset_id  TEXT,
  at            TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX conversion_key ON conversion_requests(from_type, guarantees, to_type, requires)
  WHERE status IN ('open', 'in_progress');
CREATE TABLE governance_mode (
  id      INTEGER PRIMARY KEY CHECK (id = 1),
  level   INTEGER NOT NULL,
  since   TEXT,
  until   TEXT,
  on_flag INTEGER NOT NULL
);
