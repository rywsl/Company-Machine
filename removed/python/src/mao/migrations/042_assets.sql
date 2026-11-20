CREATE TABLE asset_records (
  id TEXT PRIMARY KEY, document TEXT NOT NULL, by_actor TEXT NOT NULL,
  created_at TEXT NOT NULL, released_at TEXT, release_reason TEXT
);
CREATE TABLE asset_edges (
  asset_id TEXT NOT NULL REFERENCES asset_records(id), kind TEXT NOT NULL,
  ref_id TEXT NOT NULL, PRIMARY KEY(asset_id, kind, ref_id)
);
CREATE INDEX asset_edges_ref ON asset_edges(kind, ref_id);
CREATE TABLE asset_heads (
  slot TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES asset_records(id),
  generation INTEGER NOT NULL, proof TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE asset_authorizations (
  asset_id TEXT PRIMARY KEY REFERENCES asset_records(id),
  kind TEXT NOT NULL, standards TEXT NOT NULL, by_actor TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE asset_trials (
  id TEXT PRIMARY KEY, target TEXT NOT NULL REFERENCES asset_records(id),
  contract TEXT NOT NULL REFERENCES asset_records(id),
  purpose TEXT NOT NULL, subject TEXT NOT NULL, context TEXT NOT NULL,
  context_digest TEXT NOT NULL, watermark INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('open','passed','failed','cancelled')),
  result TEXT, by_actor TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT
);
CREATE UNIQUE INDEX asset_trials_open ON asset_trials(subject) WHERE state = 'open';
CREATE TABLE asset_uses (
  kind TEXT NOT NULL, execution_id TEXT NOT NULL, subject TEXT NOT NULL,
  epoch TEXT NOT NULL, context TEXT NOT NULL, assets TEXT NOT NULL,
  fresh INTEGER NOT NULL, handoff TEXT, trial_id TEXT, actions TEXT NOT NULL,
  PRIMARY KEY(kind, execution_id)
);
CREATE TABLE agent_workspaces (
  agent TEXT PRIMARY KEY, current_path TEXT NOT NULL, previous_path TEXT,
  trial_id TEXT, phase TEXT NOT NULL CHECK(phase IN ('stable','pending','testing','failed')),
  updated_at TEXT NOT NULL
);
