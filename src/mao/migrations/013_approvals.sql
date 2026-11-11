ALTER TABLE approvals ADD COLUMN route TEXT NOT NULL DEFAULT 'foreman';
ALTER TABLE approvals ADD COLUMN payload TEXT NOT NULL DEFAULT '{}';
ALTER TABLE approvals ADD COLUMN deadline TEXT;
ALTER TABLE approvals ADD COLUMN decision TEXT;
ALTER TABLE approvals ADD COLUMN decided_by TEXT;
ALTER TABLE jobs ADD COLUMN phase TEXT NOT NULL DEFAULT 'execute';
CREATE TABLE approval_votes (
  approval_id TEXT NOT NULL,
  runtime    TEXT NOT NULL,
  by         TEXT NOT NULL,
  decision   TEXT NOT NULL CHECK (decision IN ('approve', 'deny')),
  note       TEXT NOT NULL DEFAULT '',
  at         TEXT NOT NULL,
  PRIMARY KEY (approval_id, runtime)
);
CREATE TABLE approval_presets (
  station_key TEXT NOT NULL,
  phase       TEXT NOT NULL,
  decision    TEXT NOT NULL,
  by          TEXT NOT NULL,
  note        TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (station_key, phase)
);
