ALTER TABLE beans ADD COLUMN expired_at TEXT;
CREATE INDEX beans_expired ON beans(expired_at);
