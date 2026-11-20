CREATE TABLE human_requests (
  id           TEXT PRIMARY KEY,
  kind         TEXT NOT NULL,
  subject      TEXT NOT NULL,
  origin       TEXT NOT NULL,
  summary      TEXT NOT NULL,
  payload      TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('open', 'answered', 'cancelled', 'expired')),
  answer       TEXT,
  answered_by  TEXT,
  deadline     TEXT,
  created_at   TEXT NOT NULL,
  answered_at  TEXT
);
CREATE INDEX human_requests_open ON human_requests (kind, subject, status);
