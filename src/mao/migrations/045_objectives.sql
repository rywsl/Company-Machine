CREATE TABLE execution_origins (
  kind TEXT NOT NULL,
  execution_id TEXT NOT NULL,
  objective_id TEXT NOT NULL,
  cycle_id TEXT NOT NULL,
  task_id TEXT,
  parent_kind TEXT NOT NULL,
  parent_id TEXT NOT NULL,
  PRIMARY KEY (kind, execution_id)
);

CREATE TABLE objective_bindings (
  kind TEXT NOT NULL,
  registry_key TEXT NOT NULL,
  objective_id TEXT NOT NULL,
  activation_epoch TEXT NOT NULL,
  PRIMARY KEY (kind, registry_key)
);

CREATE TABLE objectives (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL UNIQUE,
  grant TEXT NOT NULL,
  standards TEXT NOT NULL,
  state TEXT NOT NULL,
  next_due_at TEXT NOT NULL,
  active_cycle TEXT,
  reason TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);

CREATE TABLE objective_cycles (
  id TEXT PRIMARY KEY,
  objective_id TEXT NOT NULL,
  number INTEGER NOT NULL,
  state TEXT NOT NULL,
  plan_asset TEXT,
  checks TEXT NOT NULL DEFAULT '[]',
  evidence TEXT NOT NULL DEFAULT '{}',
  watermark INTEGER NOT NULL,
  context TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE (objective_id, number)
);

CREATE TABLE objective_dispatches (
  cycle_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  message_id TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL,
  PRIMARY KEY (cycle_id, task_id)
);

ALTER TABLE agent_turns ADD COLUMN objective_id TEXT;
ALTER TABLE agent_turns ADD COLUMN cycle_id TEXT;
ALTER TABLE agent_turns ADD COLUMN task_id TEXT;
