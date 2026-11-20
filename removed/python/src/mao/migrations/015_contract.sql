CREATE TABLE contract_runs (
  id       TEXT PRIMARY KEY,
  kind     TEXT NOT NULL,
  subject  TEXT NOT NULL,
  verdict  TEXT NOT NULL CHECK (verdict IN ('pass', 'fail')),
  detail   TEXT NOT NULL,
  at       TEXT NOT NULL
);
CREATE INDEX contract_runs_subject ON contract_runs(subject, kind);
