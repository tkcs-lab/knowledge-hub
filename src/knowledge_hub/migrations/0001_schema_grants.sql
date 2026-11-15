-- Schema, privileges, schema_migrations and claim_jti (plan sections 3.3 and 3.4).
-- Roles are created outside the migrations; this file only grants.

CREATE SCHEMA IF NOT EXISTS kh AUTHORIZATION kh_migrator;

GRANT USAGE ON SCHEMA public TO kh_migrator, kh_app, kh_indexer, kh_provenance;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA kh FROM PUBLIC;
GRANT USAGE ON SCHEMA kh TO kh_app, kh_indexer, kh_provenance;
GRANT CREATE ON SCHEMA kh TO kh_provenance;

CREATE TABLE kh.schema_migrations (
    name       text PRIMARY KEY,
    sha256     text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE kh.schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.schema_migrations FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.schema_migrations
    FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

CREATE TABLE kh.claim_jti (
    jti        text PRIMARY KEY,
    expires_at timestamptz NOT NULL
);
ALTER TABLE kh.claim_jti ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.claim_jti FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.claim_jti
    FOR ALL TO kh_migrator USING (true) WITH CHECK (true);
