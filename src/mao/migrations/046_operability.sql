CREATE TABLE transport_attempts (
  request_id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL,
  principal_kind TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  origin_id TEXT,
  phase TEXT NOT NULL,
  outcome TEXT NOT NULL,
  bytes_sent INTEGER NOT NULL,
  deadline_at TEXT NOT NULL,
  error_code TEXT,
  usage_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE qualifications (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('runtime', 'machine')),
  subject_key TEXT NOT NULL,
  subject_digest TEXT NOT NULL,
  profile TEXT NOT NULL,
  platform_digest TEXT NOT NULL,
  environment_digest TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'blocked')),
  receipt_digest TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE deployment_operations (
  plan_digest TEXT PRIMARY KEY,
  manifest_digest TEXT NOT NULL,
  initial_state_digest TEXT NOT NULL,
  owner TEXT,
  lease_generation INTEGER,
  stage TEXT NOT NULL CHECK (stage IN ('planned', 'staged', 'applying', 'committed', 'failed')),
  journal TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE operation_credentials (
  id TEXT PRIMARY KEY,
  token_digest TEXT NOT NULL UNIQUE,
  root_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  scope TEXT NOT NULL CHECK (scope = 'operate'),
  secret_ref TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE operation_requests (
  credential_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  request_key TEXT,
  args_digest TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending', 'completed', 'failed')),
  result TEXT,
  error TEXT,
  message_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (credential_id, request_id)
);

CREATE TABLE operation_request_keys (
  credential_id TEXT NOT NULL,
  request_key TEXT NOT NULL,
  args_digest TEXT NOT NULL,
  message_id TEXT NOT NULL UNIQUE,
  result TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (credential_id, request_key)
);
