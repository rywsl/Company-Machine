CREATE TABLE service_operations (
  job_id TEXT NOT NULL,
  operation_key TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  provider_digest TEXT NOT NULL,
  state TEXT NOT NULL,
  idempotency_key TEXT,
  task_id TEXT,
  outputs TEXT,
  error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (job_id, operation_key)
);

CREATE TABLE budget_entries (
  id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('reserve', 'settle', 'release', 'unknown')),
  amount_usd REAL NOT NULL,
  at TEXT NOT NULL,
  UNIQUE (operation_id, scope, kind)
);

CREATE TABLE run_requests (
  agent TEXT NOT NULL,
  request_key TEXT NOT NULL,
  args_digest TEXT NOT NULL,
  run_id TEXT NOT NULL UNIQUE,
  cycle_id TEXT,
  task_id TEXT,
  PRIMARY KEY (agent, request_key)
);
CREATE UNIQUE INDEX run_requests_cycle_task ON run_requests(cycle_id, task_id)
  WHERE cycle_id IS NOT NULL AND task_id IS NOT NULL;
