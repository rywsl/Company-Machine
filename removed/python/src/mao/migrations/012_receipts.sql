CREATE TABLE receipts (
  id              TEXT PRIMARY KEY,
  station_name    TEXT NOT NULL,
  station         TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  job_id          TEXT NOT NULL,
  run_id          TEXT,
  inputs          TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('claimed','published','verified','failed','void')),
  target          TEXT,
  locator         TEXT NOT NULL DEFAULT '{}',
  items           TEXT NOT NULL DEFAULT '[]',
  note            TEXT,
  verify_method   TEXT NOT NULL,
  verify_detail   TEXT,
  verified_at     TEXT,
  voided_by       TEXT,
  void_reason     TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX receipts_idem ON receipts(station_name, idempotency_key) WHERE status != 'void';
CREATE INDEX receipts_job ON receipts(job_id);
ALTER TABLE jobs ADD COLUMN idempotency_key TEXT;
ALTER TABLE jobs ADD COLUMN run_value TEXT;
