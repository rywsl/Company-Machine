CREATE TABLE epochs (
  "commit"  TEXT PRIMARY KEY,
  tree      TEXT NOT NULL,
  built_at  TEXT NOT NULL
);
CREATE TABLE epoch_pointers (
  name      TEXT PRIMARY KEY,
  "commit"  TEXT NOT NULL
);
CREATE TABLE changesets (
  id                TEXT PRIMARY KEY,
  base_epoch        TEXT NOT NULL,
  source_commit     TEXT NOT NULL,
  "commit"          TEXT NOT NULL UNIQUE,
  state             TEXT NOT NULL,
  risk_tier         TEXT,
  reviews_required  INTEGER NOT NULL DEFAULT 0,
  proposer          TEXT NOT NULL,
  proposer_runtime  TEXT,
  document          TEXT NOT NULL,
  report            TEXT NOT NULL DEFAULT '{}',
  stage_started_at  TEXT,
  activated_at      TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX changesets_state ON changesets(state);
CREATE TABLE reviews (
  id             TEXT PRIMARY KEY,
  changeset_id   TEXT NOT NULL,
  runtime        TEXT NOT NULL,
  runtime_digest TEXT NOT NULL,
  verdict        TEXT NOT NULL,
  note           TEXT NOT NULL DEFAULT '',
  by             TEXT NOT NULL,
  at             TEXT NOT NULL
);
CREATE UNIQUE INDEX reviews_runtime ON reviews(changeset_id, runtime);
CREATE TABLE governance_state (
  id      INTEGER PRIMARY KEY CHECK (id = 1),
  safe    INTEGER NOT NULL DEFAULT 0,
  since   TEXT,
  until   TEXT,
  level   INTEGER NOT NULL DEFAULT 0
);
