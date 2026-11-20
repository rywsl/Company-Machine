CREATE TABLE instances (
  id              TEXT PRIMARY KEY,
  station         TEXT NOT NULL,
  mode            TEXT NOT NULL CHECK (mode IN ('pooled', 'resident')),
  digest          TEXT NOT NULL,
  state           TEXT NOT NULL CHECK (state IN ('starting','ready','busy','failed','crashed','stopped')),
  sandbox         TEXT NOT NULL,
  job             TEXT,
  jobs_served     INTEGER NOT NULL DEFAULT 0,
  failures        INTEGER NOT NULL DEFAULT 0,
  next_restart_at TEXT,
  idle_since      TEXT,
  started_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  error           TEXT
);
CREATE INDEX instances_station ON instances(station, state);
CREATE TABLE emits (
  id       TEXT PRIMARY KEY,
  instance TEXT,
  station  TEXT NOT NULL,
  port     TEXT NOT NULL,
  bean     TEXT NOT NULL,
  trigger  TEXT NOT NULL,
  at       TEXT NOT NULL
);
CREATE INDEX emits_station_at ON emits(station, at);
