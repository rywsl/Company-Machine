ALTER TABLE egress_plans ADD COLUMN token_sha256 TEXT;
ALTER TABLE egress_plans ADD COLUMN kind TEXT NOT NULL DEFAULT 'job';
ALTER TABLE egress_plans ADD COLUMN closed_at TEXT;
