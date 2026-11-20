CREATE TABLE talk_messages (
  id           TEXT PRIMARY KEY,
  role         TEXT NOT NULL CHECK (role IN ('human', 'foreman', 'system')),
  kind         TEXT NOT NULL CHECK (kind IN ('text', 'card')),
  by_name      TEXT NOT NULL,
  body         TEXT NOT NULL,
  reply_state  TEXT NOT NULL CHECK (reply_state IN ('open', 'answered', 'unanswered', 'na')),
  in_reply_to  TEXT,
  card_type    TEXT,
  ref_kind     TEXT,
  ref_id       TEXT,
  card         TEXT NOT NULL DEFAULT '{}',
  mailbox_id   TEXT,
  created_at   TEXT NOT NULL
);
