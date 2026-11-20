CREATE TABLE audit_findings (
  id         TEXT PRIMARY KEY,
  invariant  TEXT NOT NULL,
  subject    TEXT NOT NULL,
  state      TEXT NOT NULL,
  detail     TEXT NOT NULL,
  at         TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE station_holds (
  station    TEXT PRIMARY KEY,
  finding_id TEXT NOT NULL,
  at         TEXT NOT NULL
);
CREATE UNIQUE INDEX audit_open ON audit_findings(invariant, subject) WHERE state IN ('open', 'held', 'wake');
