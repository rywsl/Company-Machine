CREATE TABLE jobs (
  id          TEXT PRIMARY KEY,
  station     TEXT NOT NULL,
  epoch       TEXT NOT NULL,
  state       TEXT NOT NULL,
  attempt     INTEGER NOT NULL,
  inputs      TEXT NOT NULL,
  params      TEXT NOT NULL,
  outputs     TEXT NOT NULL DEFAULT '{}',
  cost_usd    REAL NOT NULL DEFAULT 0,
  error       TEXT,
  feedback    TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE TABLE events (
  seq     INTEGER PRIMARY KEY AUTOINCREMENT,
  id      TEXT NOT NULL UNIQUE,
  at      TEXT NOT NULL,
  kind    TEXT NOT NULL,
  job_id  TEXT,
  data    TEXT NOT NULL
);
CREATE INDEX events_job ON events(job_id, seq);
CREATE TABLE approvals (
  id          TEXT PRIMARY KEY,
  job_id      TEXT NOT NULL,
  attempt     INTEGER NOT NULL,
  request_id  TEXT NOT NULL,
  phase       TEXT NOT NULL,
  kind        TEXT NOT NULL,
  summary     TEXT NOT NULL,
  cost_usd    REAL NOT NULL,
  status      TEXT NOT NULL,
  by          TEXT,
  note        TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  decided_at  TEXT
);
CREATE INDEX approvals_job ON approvals(job_id);
CREATE UNIQUE INDEX approvals_request ON approvals(request_id);
