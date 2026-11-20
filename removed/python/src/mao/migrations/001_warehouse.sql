CREATE TABLE beans (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    meta_json TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    sealed INTEGER NOT NULL DEFAULT 0 CHECK (sealed IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE payload_files (
    bean_id TEXT NOT NULL REFERENCES beans(id),
    path TEXT NOT NULL,
    digest TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0),
    media_type TEXT NOT NULL,
    PRIMARY KEY (bean_id, path)
);

CREATE TABLE seals (
    id INTEGER PRIMARY KEY,
    bean_id TEXT NOT NULL REFERENCES beans(id),
    job_id TEXT,
    report_digest TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    sealed_at TEXT NOT NULL
);

CREATE UNIQUE INDEX seals_dedupe ON seals(bean_id, report_digest, ifnull(job_id, ''));

CREATE TABLE lineage (
    job_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    port TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    bean_id TEXT NOT NULL REFERENCES beans(id),
    PRIMARY KEY (job_id, direction, port, ordinal)
);

CREATE INDEX beans_type ON beans(type);
CREATE INDEX seals_bean ON seals(bean_id);
CREATE INDEX lineage_bean ON lineage(bean_id);
