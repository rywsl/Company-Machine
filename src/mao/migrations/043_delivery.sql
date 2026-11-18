ALTER TABLE agent_messages ADD COLUMN enqueue_seq INTEGER;
UPDATE agent_messages
SET enqueue_seq = (
  SELECT COUNT(*) FROM agent_messages AS older
  WHERE older.created_at < agent_messages.created_at
     OR (older.created_at = agent_messages.created_at AND older.id <= agent_messages.id)
)
WHERE enqueue_seq IS NULL;
CREATE UNIQUE INDEX agent_messages_enqueue_seq ON agent_messages(enqueue_seq);

ALTER TABLE agent_turns ADD COLUMN owner TEXT;
ALTER TABLE agent_turns ADD COLUMN lease_generation INTEGER;

CREATE TABLE lease_generations (
  resource TEXT PRIMARY KEY,
  generation INTEGER NOT NULL
);

CREATE TABLE delivery_counters (
  name TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
INSERT INTO delivery_counters (name, value)
SELECT 'enqueue_seq', COALESCE(MAX(enqueue_seq), 0) FROM agent_messages;

CREATE TABLE delivery_keys (
  kind TEXT NOT NULL,
  source TEXT NOT NULL,
  target TEXT NOT NULL,
  message_id TEXT NOT NULL,
  PRIMARY KEY (kind, source, target)
);

CREATE TABLE effect_outbox (
  id TEXT PRIMARY KEY,
  effect_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  body TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending', 'claimed', 'done')),
  owner TEXT,
  lease_generation INTEGER,
  claim_until TEXT,
  revision INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_at TEXT NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE rollout_owner (
  slot INTEGER PRIMARY KEY CHECK (slot = 1),
  changeset_id TEXT UNIQUE
);
