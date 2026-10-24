CREATE TABLE IF NOT EXISTS environment_assessments (id TEXT PRIMARY KEY, region_id TEXT NOT NULL, change_id TEXT, status TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hazards (id TEXT PRIMARY KEY, kind TEXT NOT NULL, geometry TEXT NOT NULL, severity TEXT NOT NULL, source TEXT NOT NULL, reported_by TEXT NOT NULL, reported_at TEXT NOT NULL, expires_at TEXT, verified INTEGER NOT NULL DEFAULT 0, note TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nav_sessions (id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, consent INTEGER NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS industrial_sites (id TEXT PRIMARY KEY, name TEXT NOT NULL, geometry TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL, permit_ref TEXT, status TEXT NOT NULL, region_id TEXT, result TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_files (id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, filename TEXT NOT NULL, content_type TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, path TEXT NOT NULL, uploaded_by TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projections (id TEXT PRIMARY KEY, region_id TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_hazards_expires ON hazards(expires_at);
CREATE INDEX IF NOT EXISTS ix_evidence_incident ON evidence_files(incident_id);
