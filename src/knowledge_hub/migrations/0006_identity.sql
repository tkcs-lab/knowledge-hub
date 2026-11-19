-- Migration 0006: identity (hub keys, OAuth server state, directory snapshot).
-- Drops the PrivacyFence replay objects of 0001 (deploy/rollback-0006.sql recreates them).

DROP FUNCTION kh.claim_seen(text, timestamptz);
DROP TABLE kh.claim_jti;

CREATE TABLE kh.api_keys (
    id text PRIMARY KEY CHECK (id ~ '^[a-z2-7]{10}$'),
    secret_sha256 bytea NOT NULL CHECK (octet_length(secret_sha256) = 32),
    name text NOT NULL CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9 ._-]{0,62}$'),
    audience text NOT NULL,
    tools text[] NOT NULL CHECK (cardinality(tools) > 0),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz, revoked_at timestamptz, last_used_at timestamptz);
CREATE TABLE kh.oauth_clients (client_id text PRIMARY KEY, info jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), last_used_at timestamptz NOT NULL DEFAULT now(),
    authorized_at timestamptz);
CREATE TABLE kh.oauth_pending (state_sha256 bytea PRIMARY KEY, kind text NOT NULL CHECK (kind IN ('upstream','consent')),
    client_id text NOT NULL, params jsonb NOT NULL,
    nonce text NOT NULL, verifier text NOT NULL, expires_at timestamptz NOT NULL);
CREATE TABLE kh.oauth_codes (code_sha256 bytea PRIMARY KEY, client_id text NOT NULL, email text NOT NULL,
    params jsonb NOT NULL, expires_at timestamptz NOT NULL);
CREATE TABLE kh.oauth_tokens (token_sha256 bytea PRIMARY KEY, kind text NOT NULL CHECK (kind IN ('access','refresh')),
    family_id uuid NOT NULL, family_started_at timestamptz NOT NULL, client_id text NOT NULL, email text NOT NULL,
    scopes text[] NOT NULL, resource text NOT NULL, expires_at timestamptz NOT NULL, used_at timestamptz, revoked_at timestamptz);
CREATE INDEX oauth_tokens_family ON kh.oauth_tokens (family_id);
CREATE TABLE kh.oauth_consents (email text NOT NULL, client_id text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (email, client_id));
CREATE TABLE kh.directory_snapshot (id int PRIMARY KEY CHECK (id = 1), data jsonb NOT NULL, fetched_at timestamptz NOT NULL);

ALTER TABLE kh.api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.api_keys FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.api_keys FOR ALL TO kh_migrator USING (true) WITH CHECK (true);
ALTER TABLE kh.oauth_clients ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.oauth_clients FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.oauth_clients FOR ALL TO kh_migrator USING (true) WITH CHECK (true);
ALTER TABLE kh.oauth_pending ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.oauth_pending FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.oauth_pending FOR ALL TO kh_migrator USING (true) WITH CHECK (true);
ALTER TABLE kh.oauth_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.oauth_codes FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.oauth_codes FOR ALL TO kh_migrator USING (true) WITH CHECK (true);
ALTER TABLE kh.oauth_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.oauth_tokens FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.oauth_tokens FOR ALL TO kh_migrator USING (true) WITH CHECK (true);
ALTER TABLE kh.oauth_consents ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.oauth_consents FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.oauth_consents FOR ALL TO kh_migrator USING (true) WITH CHECK (true);
ALTER TABLE kh.directory_snapshot ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.directory_snapshot FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.directory_snapshot FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

CREATE FUNCTION kh.api_key_get(p_id text) RETURNS TABLE(secret_sha256 bytea, audience text, tools text[])
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ SELECT k.secret_sha256, k.audience, k.tools FROM kh.api_keys k
          WHERE k.id = p_id AND k.revoked_at IS NULL AND (k.expires_at IS NULL OR k.expires_at > now())  $$;
REVOKE EXECUTE ON FUNCTION kh.api_key_get(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.api_key_get(text) TO kh_app;

CREATE FUNCTION kh.api_key_touch(p_id text) RETURNS void
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ UPDATE kh.api_keys SET last_used_at = now()
          WHERE id = p_id AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')  $$;
REVOKE EXECUTE ON FUNCTION kh.api_key_touch(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.api_key_touch(text) TO kh_app;

CREATE FUNCTION kh.oauth_client_get(p_id text) RETURNS jsonb
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ SELECT info FROM kh.oauth_clients WHERE client_id = p_id  $$;
REVOKE EXECUTE ON FUNCTION kh.oauth_client_get(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.oauth_client_get(text) TO kh_app;

CREATE FUNCTION kh.oauth_client_put(p_id text, p_info jsonb, p_max integer) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ DECLARE v_count integer;
          BEGIN
              DELETE FROM kh.oauth_clients WHERE authorized_at IS NULL AND created_at < now() - interval '1 hour';
              SELECT count(*) INTO v_count FROM kh.oauth_clients;
              IF v_count >= p_max THEN
                  RETURN false;
              END IF;
              INSERT INTO kh.oauth_clients (client_id, info) VALUES (p_id, p_info);
              RETURN true;
          END;  $$;
REVOKE EXECUTE ON FUNCTION kh.oauth_client_put(text, jsonb, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.oauth_client_put(text, jsonb, integer) TO kh_app;

CREATE FUNCTION kh.oauth_client_authorize(p_id text) RETURNS void
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ UPDATE kh.oauth_clients SET authorized_at = now() WHERE client_id = p_id AND authorized_at IS NULL  $$;
REVOKE EXECUTE ON FUNCTION kh.oauth_client_authorize(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.oauth_client_authorize(text) TO kh_app;

CREATE FUNCTION kh.oauth_client_touch(p_id text) RETURNS void
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ UPDATE kh.oauth_clients SET last_used_at = now() WHERE client_id = p_id  $$;
REVOKE EXECUTE ON FUNCTION kh.oauth_client_touch(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.oauth_client_touch(text) TO kh_app;

CREATE FUNCTION kh.oauth_pending_put(p_state bytea, p_kind text, p_client text, p_params jsonb, p_nonce text, p_verifier text, p_expires timestamptz) RETURNS void
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ BEGIN
              DELETE FROM kh.oauth_pending WHERE expires_at < now();
              INSERT INTO kh.oauth_pending (state_sha256, kind, client_id, params, nonce, verifier, expires_at)
              VALUES (p_state, p_kind, p_client, p_params, p_nonce, p_verifier, p_expires);
          END;  $$;
REVOKE EXECUTE ON FUNCTION kh.oauth_pending_put(bytea, text, text, jsonb, text, text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.oauth_pending_put(bytea, text, text, jsonb, text, text, timestamptz) TO kh_app;

CREATE FUNCTION kh.oauth_pending_take(p_state bytea, p_kind text) RETURNS TABLE(client_id text, params jsonb, nonce text, verifier text)
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ DELETE FROM kh.oauth_pending
          WHERE state_sha256 = p_state AND kind = p_kind AND expires_at > now()
          RETURNING client_id, params, nonce, verifier  $$;
REVOKE EXECUTE ON FUNCTION kh.oauth_pending_take(bytea, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.oauth_pending_take(bytea, text) TO kh_app;

CREATE FUNCTION kh.oauth_pending_peek(p_state bytea, p_kind text) RETURNS TABLE(client_id text, params jsonb, nonce text, verifier text)
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ SELECT p.client_id, p.params, p.nonce, p.verifier FROM kh.oauth_pending p
          WHERE p.state_sha256 = p_state AND p.kind = p_kind AND p.expires_at > now()  $$;
REVOKE EXECUTE ON FUNCTION kh.oauth_pending_peek(bytea, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.oauth_pending_peek(bytea, text) TO kh_app;

CREATE FUNCTION kh.oauth_code_put(p_code bytea, p_client text, p_email text, p_params jsonb, p_expires timestamptz) RETURNS void
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ INSERT INTO kh.oauth_codes (code_sha256, client_id, email, params, expires_at)
          VALUES (p_code, p_client, p_email, p_params, p_expires)  $$;
REVOKE EXECUTE ON FUNCTION kh.oauth_code_put(bytea, text, text, jsonb, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.oauth_code_put(bytea, text, text, jsonb, timestamptz) TO kh_app;

CREATE FUNCTION kh.oauth_code_take(p_code bytea) RETURNS TABLE(client_id text, email text, params jsonb, expires_at timestamptz)
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ DELETE FROM kh.oauth_codes WHERE code_sha256 = p_code AND expires_at > now()
          RETURNING client_id, email, params, expires_at  $$;
REVOKE EXECUTE ON FUNCTION kh.oauth_code_take(bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.oauth_code_take(bytea) TO kh_app;

CREATE FUNCTION kh.oauth_token_put(p_token bytea, p_kind text, p_family uuid, p_family_started timestamptz, p_client text, p_email text, p_scopes text[], p_resource text, p_expires timestamptz) RETURNS void
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ INSERT INTO kh.oauth_tokens (token_sha256, kind, family_id, family_started_at, client_id, email, scopes, resource, expires_at)
          VALUES (p_token, p_kind, p_family, p_family_started, p_client, p_email, p_scopes, p_resource, p_expires)  $$;
REVOKE EXECUTE ON FUNCTION kh.oauth_token_put(bytea, text, uuid, timestamptz, text, text, text[], text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.oauth_token_put(bytea, text, uuid, timestamptz, text, text, text[], text, timestamptz) TO kh_app;

CREATE FUNCTION kh.oauth_token_get(p_token bytea, p_kind text) RETURNS TABLE(family_id uuid, family_started_at timestamptz, client_id text, email text, scopes text[], resource text, expires_at timestamptz, used_at timestamptz)
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ SELECT t.family_id, t.family_started_at, t.client_id, t.email, t.scopes, t.resource, t.expires_at, t.used_at
          FROM kh.oauth_tokens t
          WHERE t.token_sha256 = p_token AND t.kind = p_kind AND t.revoked_at IS NULL AND t.expires_at > now()  $$;
REVOKE EXECUTE ON FUNCTION kh.oauth_token_get(bytea, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.oauth_token_get(bytea, text) TO kh_app;

CREATE FUNCTION kh.oauth_refresh_use(p_token bytea, p_grace_seconds integer) RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ DECLARE v_used timestamptz; v_family uuid;
          BEGIN
              SELECT t.used_at, t.family_id INTO v_used, v_family FROM kh.oauth_tokens t
              WHERE t.token_sha256 = p_token AND t.kind = 'refresh' AND t.revoked_at IS NULL AND t.expires_at > now()
              FOR UPDATE;
              IF NOT FOUND THEN
                  RETURN 'gone';
              END IF;
              IF v_used IS NULL THEN
                  UPDATE kh.oauth_tokens SET used_at = now() WHERE token_sha256 = p_token;
                  RETURN 'ok';
              END IF;
              IF v_used > now() - make_interval(secs => p_grace_seconds) THEN
                  RETURN 'reuse_grace';
              END IF;
              UPDATE kh.oauth_tokens SET revoked_at = now() WHERE family_id = v_family AND revoked_at IS NULL;
              RETURN 'reuse';
          END;  $$;
REVOKE EXECUTE ON FUNCTION kh.oauth_refresh_use(bytea, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.oauth_refresh_use(bytea, integer) TO kh_app;

CREATE FUNCTION kh.oauth_token_revoke(p_token bytea) RETURNS void
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ UPDATE kh.oauth_tokens SET revoked_at = now()
          WHERE revoked_at IS NULL
            AND family_id IN (SELECT t.family_id FROM kh.oauth_tokens t WHERE t.token_sha256 = p_token)  $$;
REVOKE EXECUTE ON FUNCTION kh.oauth_token_revoke(bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.oauth_token_revoke(bytea) TO kh_app;

CREATE FUNCTION kh.oauth_family_revoke(p_family uuid) RETURNS void
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ UPDATE kh.oauth_tokens SET revoked_at = now() WHERE family_id = p_family AND revoked_at IS NULL  $$;
REVOKE EXECUTE ON FUNCTION kh.oauth_family_revoke(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.oauth_family_revoke(uuid) TO kh_app;

CREATE FUNCTION kh.oauth_consent_has(p_email text, p_client text) RETURNS boolean
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ SELECT EXISTS (SELECT 1 FROM kh.oauth_consents WHERE email = p_email AND client_id = p_client)  $$;
REVOKE EXECUTE ON FUNCTION kh.oauth_consent_has(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.oauth_consent_has(text, text) TO kh_app;

CREATE FUNCTION kh.oauth_consent_put(p_email text, p_client text) RETURNS void
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ INSERT INTO kh.oauth_consents (email, client_id) VALUES (p_email, p_client) ON CONFLICT DO NOTHING  $$;
REVOKE EXECUTE ON FUNCTION kh.oauth_consent_put(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.oauth_consent_put(text, text) TO kh_app;

CREATE FUNCTION kh.directory_snapshot_get() RETURNS TABLE(data jsonb, fetched_at timestamptz)
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ SELECT s.data, s.fetched_at FROM kh.directory_snapshot s WHERE s.id = 1  $$;
REVOKE EXECUTE ON FUNCTION kh.directory_snapshot_get() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.directory_snapshot_get() TO kh_app;
GRANT EXECUTE ON FUNCTION kh.directory_snapshot_get() TO kh_indexer;
GRANT EXECUTE ON FUNCTION kh.directory_snapshot_get() TO kh_migrator;

CREATE FUNCTION kh.directory_snapshot_put(p_data jsonb) RETURNS void
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ INSERT INTO kh.directory_snapshot (id, data, fetched_at) VALUES (1, p_data, now())
          ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, fetched_at = EXCLUDED.fetched_at  $$;
REVOKE EXECUTE ON FUNCTION kh.directory_snapshot_put(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.directory_snapshot_put(jsonb) TO kh_indexer;

CREATE FUNCTION kh.oauth_purge() RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ DECLARE v_total integer := 0; v_n integer;
          BEGIN
              DELETE FROM kh.oauth_pending WHERE expires_at < now();
              GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;
              DELETE FROM kh.oauth_codes WHERE expires_at < now();
              GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;
              DELETE FROM kh.oauth_tokens WHERE expires_at < now() - interval '1 day';
              GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;
              DELETE FROM kh.oauth_clients
              WHERE (authorized_at IS NULL AND created_at < now() - interval '1 hour')
                 OR last_used_at < now() - interval '30 days';
              GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;
              RETURN v_total;
          END;  $$;
REVOKE EXECUTE ON FUNCTION kh.oauth_purge() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.oauth_purge() TO kh_indexer;
