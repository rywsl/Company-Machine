CREATE TABLE agents (
  name            TEXT PRIMARY KEY,
  state           TEXT NOT NULL CHECK (state IN ('active', 'paused')),
  session         TEXT,
  conversation    INTEGER NOT NULL DEFAULT 1,
  turns_in_conv   INTEGER NOT NULL DEFAULT 0,
  last_tokens     INTEGER NOT NULL DEFAULT 0,
  rotate_pending  INTEGER NOT NULL DEFAULT 0,
  budget_exhausted INTEGER NOT NULL DEFAULT 0,
  sandbox         TEXT,
  updated_at      TEXT NOT NULL
);
CREATE TABLE agent_messages (
  id          TEXT PRIMARY KEY,
  agent       TEXT NOT NULL,
  kind        TEXT NOT NULL,
  source      TEXT NOT NULL,
  body        TEXT NOT NULL,
  state       TEXT NOT NULL CHECK (state IN ('pending', 'delivered', 'done', 'dropped')),
  turn        TEXT,
  attempts    INTEGER NOT NULL DEFAULT 0,
  not_before  TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX agent_messages_pending ON agent_messages(agent, state, id);
CREATE TABLE agent_turns (
  id           TEXT PRIMARY KEY,
  agent        TEXT NOT NULL,
  conversation INTEGER NOT NULL,
  number       INTEGER NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN ('work', 'handoff')),
  session      TEXT,
  state        TEXT NOT NULL CHECK (state IN ('running', 'ok', 'failed', 'timeout', 'interrupted')),
  messages     TEXT NOT NULL,
  cost_usd     REAL NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  exit_code    INTEGER,
  error        TEXT,
  started_at   TEXT NOT NULL,
  finished_at  TEXT
);
CREATE INDEX agent_turns_agent ON agent_turns(agent, started_at);
CREATE TABLE agent_schedule (
  agent    TEXT NOT NULL,
  idx      INTEGER NOT NULL,
  last_at  TEXT NOT NULL,
  PRIMARY KEY (agent, idx)
);
