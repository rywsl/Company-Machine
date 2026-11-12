CREATE TABLE images (
  id              TEXT PRIMARY KEY,
  path            TEXT NOT NULL,
  "commit"        TEXT NOT NULL,
  context_digest  TEXT NOT NULL,
  state           TEXT NOT NULL,
  image_id        TEXT,
  ref             TEXT,
  log             TEXT NOT NULL,
  error           TEXT,
  by              TEXT NOT NULL,
  at              TEXT NOT NULL
);
CREATE INDEX images_context ON images(context_digest, state);
