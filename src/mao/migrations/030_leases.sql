CREATE TABLE leases (
  resource     TEXT PRIMARY KEY,
  owner        TEXT NOT NULL,
  acquired_at  TEXT NOT NULL,
  expires_at   TEXT NOT NULL
);
