-- Rollback of migration 0006: run as kh_migrator from a checkout of the released tag.
BEGIN;

DROP FUNCTION IF EXISTS kh.api_key_get(text);
DROP FUNCTION IF EXISTS kh.api_key_touch(text);
DROP FUNCTION IF EXISTS kh.oauth_client_get(text);
DROP FUNCTION IF EXISTS kh.oauth_client_put(text, jsonb, integer);
DROP FUNCTION IF EXISTS kh.oauth_client_authorize(text);
DROP FUNCTION IF EXISTS kh.oauth_client_touch(text);
DROP FUNCTION IF EXISTS kh.oauth_pending_put(bytea, text, text, jsonb, text, text, timestamptz);
DROP FUNCTION IF EXISTS kh.oauth_pending_take(bytea, text);
DROP FUNCTION IF EXISTS kh.oauth_pending_peek(bytea, text);
DROP FUNCTION IF EXISTS kh.oauth_code_put(bytea, text, text, jsonb, timestamptz);
DROP FUNCTION IF EXISTS kh.oauth_code_take(bytea);
DROP FUNCTION IF EXISTS kh.oauth_token_put(bytea, text, uuid, timestamptz, text, text, text[], text, timestamptz);
DROP FUNCTION IF EXISTS kh.oauth_token_get(bytea, text);
DROP FUNCTION IF EXISTS kh.oauth_refresh_use(bytea, integer);
DROP FUNCTION IF EXISTS kh.oauth_token_revoke(bytea);
DROP FUNCTION IF EXISTS kh.oauth_family_revoke(uuid);
DROP FUNCTION IF EXISTS kh.oauth_consent_has(text, text);
DROP FUNCTION IF EXISTS kh.oauth_consent_put(text, text);
DROP FUNCTION IF EXISTS kh.directory_snapshot_get();
DROP FUNCTION IF EXISTS kh.directory_snapshot_put(jsonb);
DROP FUNCTION IF EXISTS kh.oauth_purge();

DROP TABLE IF EXISTS kh.api_keys, kh.oauth_clients, kh.oauth_pending, kh.oauth_codes, kh.oauth_tokens, kh.oauth_consents, kh.directory_snapshot CASCADE;

-- Recreate the PrivacyFence replay objects, copied from 0001_baseline.sql.
CREATE TABLE kh.claim_jti (
    jti        text PRIMARY KEY,
    expires_at timestamptz NOT NULL
);

CREATE FUNCTION kh.claim_seen(jti text, expires_at timestamptz) RETURNS boolean
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ WITH ins AS (
              INSERT INTO kh.claim_jti (jti, expires_at) VALUES ($1, $2)
              ON CONFLICT DO NOTHING
              RETURNING 1
          )
          SELECT EXISTS (SELECT 1 FROM ins) $$;

REVOKE EXECUTE ON FUNCTION kh.claim_seen(text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.claim_seen(text, timestamptz) TO kh_app;

ALTER TABLE kh.claim_jti ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.claim_jti FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.claim_jti FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

-- claim_jti: kh_app reaches it only through kh.claim_seen; kh_indexer purges expired rows.
GRANT SELECT, DELETE ON kh.claim_jti TO kh_indexer;
CREATE POLICY kh_indexer_select ON kh.claim_jti FOR SELECT TO kh_indexer USING (expires_at < now());
CREATE POLICY kh_indexer_delete ON kh.claim_jti FOR DELETE TO kh_indexer USING (expires_at < now());

DELETE FROM kh.schema_migrations WHERE name = '0006_identity.sql';

COMMIT;
