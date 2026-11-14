CREATE TABLE changeset_signoffs (
  changeset_id TEXT PRIMARY KEY,
  decision     TEXT NOT NULL CHECK (decision IN ('approve', 'deny')),
  by           TEXT NOT NULL,
  note         TEXT NOT NULL DEFAULT '',
  at           TEXT NOT NULL
);
