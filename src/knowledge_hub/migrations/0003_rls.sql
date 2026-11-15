-- Helper and definer functions, row-level security and the policy matrix (plan section 3.3).
-- Runs as kh_migrator: every table and every function created here is owned by kh_migrator.
-- Policies are named <role>_<command>; each policy names exactly one role.

-- ---------------------------------------------------------------------------
-- Caller helpers (SECURITY INVOKER, LANGUAGE sql STABLE). Fully qualified, no SET clause,
-- so they stay inlinable inside policies.
-- ---------------------------------------------------------------------------

CREATE FUNCTION kh.caller_audiences() RETURNS text[]
    LANGUAGE sql STABLE
    AS $$ SELECT coalesce(string_to_array(nullif(current_setting('kh.audiences', true), ''), ','), '{}'::text[]) $$;

CREATE FUNCTION kh.caller_email() RETURNS text
    LANGUAGE sql STABLE
    AS $$ SELECT nullif(current_setting('kh.email', true), '') $$;

CREATE FUNCTION kh.caller_sub() RETURNS text
    LANGUAGE sql STABLE
    AS $$ SELECT nullif(current_setting('kh.sub', true), '') $$;

CREATE FUNCTION kh.caller_type() RETURNS text
    LANGUAGE sql STABLE
    AS $$ SELECT nullif(current_setting('kh.principal_type', true), '') $$;

-- coalesce(..., false): a caller without an email gets false rather than NULL for an
-- email-scoped row, so NOT kh.visible(...) can never be used to widen access.
CREATE FUNCTION kh.visible(req text[], emails text[]) RETURNS boolean
    LANGUAGE sql STABLE
    AS $$ SELECT coalesce($1 OPERATOR(pg_catalog.<@) kh.caller_audiences()
                          AND ($2 IS NULL OR kh.caller_email() = ANY ($2)), false) $$;

REVOKE EXECUTE ON FUNCTION kh.caller_audiences(), kh.caller_email(), kh.caller_sub(),
    kh.caller_type(), kh.visible(text[], text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.caller_audiences(), kh.caller_email(), kh.caller_sub(),
    kh.caller_type(), kh.visible(text[], text[]) TO kh_app, kh_provenance;

-- ---------------------------------------------------------------------------
-- Definer functions owned by kh_migrator. They read past RLS through the kh_migrator
-- policies, so each one returns only a boolean or the caller's own id.
-- ---------------------------------------------------------------------------

CREATE FUNCTION kh.owns_derived(id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ SELECT EXISTS (SELECT 1 FROM kh.derived_records r
                         WHERE r.id = $1 AND r.created_by_sub = kh.caller_sub()) $$;

CREATE FUNCTION kh.owns_candidate(id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ SELECT EXISTS (SELECT 1 FROM kh.candidates c
                         WHERE c.id = $1 AND c.created_by_sub = kh.caller_sub()) $$;

CREATE FUNCTION kh.owns_conflict(id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ SELECT EXISTS (SELECT 1 FROM kh.conflicts k
                         WHERE k.id = $1 AND k.created_by_sub = kh.caller_sub()) $$;

CREATE FUNCTION kh.my_raw_id(source_system text, source_ref text) RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ SELECT r.id FROM kh.raw_records r
          WHERE kh.caller_type() = 'n8n_workflow'
            AND r.ingested_by_sub = kh.caller_sub()
            AND r.source_system = $1
            AND r.source_ref = $2 $$;

CREATE FUNCTION kh.claim_seen(jti text, expires_at timestamptz) RETURNS boolean
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ WITH ins AS (
              INSERT INTO kh.claim_jti (jti, expires_at) VALUES ($1, $2)
              ON CONFLICT DO NOTHING
              RETURNING 1
          )
          SELECT EXISTS (SELECT 1 FROM ins) $$;

-- Moves a candidate created by the caller along proposed -> pr_open|failed and
-- failed -> pr_open|failed. A null branch, pr_number or pr_url keeps the stored value;
-- error is always replaced. Returns whether a row changed; never returns row data.
CREATE FUNCTION kh.candidate_mark(id uuid, new_state text, branch text, pr_number int,
                                  pr_url text, error text) RETURNS boolean
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ WITH upd AS (
              UPDATE kh.candidates c
                 SET state = $2,
                     branch = coalesce($3, c.branch),
                     pr_number = coalesce($4, c.pr_number),
                     pr_url = coalesce($5, c.pr_url),
                     error = $6
               WHERE c.id = $1
                 AND c.created_by_sub = kh.caller_sub()
                 AND c.state IN ('proposed', 'failed')
                 AND $2 IN ('pr_open', 'failed')
              RETURNING 1
          )
          SELECT EXISTS (SELECT 1 FROM upd) $$;

REVOKE EXECUTE ON FUNCTION kh.owns_derived(uuid), kh.owns_candidate(uuid), kh.owns_conflict(uuid),
    kh.my_raw_id(text, text), kh.claim_seen(text, timestamptz),
    kh.candidate_mark(uuid, text, text, int, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.owns_derived(uuid), kh.owns_candidate(uuid), kh.owns_conflict(uuid),
    kh.my_raw_id(text, text), kh.claim_seen(text, timestamptz),
    kh.candidate_mark(uuid, text, text, int, text, text) TO kh_app;

-- ---------------------------------------------------------------------------
-- ENABLE and FORCE row-level security on every table, and the kh_migrator policy.
-- schema_migrations and claim_jti got both in 0001; ENABLE/FORCE are repeated (no-ops).
-- ---------------------------------------------------------------------------

ALTER TABLE kh.schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.schema_migrations FORCE ROW LEVEL SECURITY;
ALTER TABLE kh.claim_jti ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.claim_jti FORCE ROW LEVEL SECURITY;

ALTER TABLE kh.documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.documents FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.documents FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.document_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.document_versions FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.document_versions FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.chunks FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.chunks FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.entities_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.entities_cache FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.entities_cache FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.raw_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.raw_records FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.raw_records FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.raw_audience_map ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.raw_audience_map FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.raw_audience_map FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.derived_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.derived_records FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.derived_records FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.derived_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.derived_sources FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.derived_sources FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.candidates FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.candidates FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.candidate_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.candidate_sources FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.candidate_sources FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.conflicts ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.conflicts FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.conflicts FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.conflict_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.conflict_items FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.conflict_items FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.index_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.index_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.index_jobs FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.repo_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.repo_state FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.repo_state FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.index_alerts ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.index_alerts FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.index_alerts FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

-- ---------------------------------------------------------------------------
-- The matrix. "ALL" means SELECT, INSERT, UPDATE and DELETE (never TRUNCATE, which
-- bypasses row-level security, nor REFERENCES or TRIGGER).
-- ---------------------------------------------------------------------------

-- documents
GRANT SELECT ON kh.documents TO kh_app;
CREATE POLICY kh_app_select ON kh.documents FOR SELECT TO kh_app
    USING (kh.visible(required_audiences, allowed_emails));
GRANT SELECT, INSERT, UPDATE, DELETE ON kh.documents TO kh_indexer;
CREATE POLICY kh_indexer_all ON kh.documents FOR ALL TO kh_indexer USING (true) WITH CHECK (true);
GRANT SELECT (id, required_audiences, allowed_emails, commit_sha) ON kh.documents TO kh_provenance;
CREATE POLICY kh_provenance_select ON kh.documents FOR SELECT TO kh_provenance USING (true);

-- document_versions
GRANT SELECT ON kh.document_versions TO kh_app;
CREATE POLICY kh_app_select ON kh.document_versions FOR SELECT TO kh_app
    USING (EXISTS (SELECT 1 FROM kh.documents d WHERE d.id = document_versions.document_id));
GRANT SELECT, INSERT, UPDATE, DELETE ON kh.document_versions TO kh_indexer;
CREATE POLICY kh_indexer_all ON kh.document_versions FOR ALL TO kh_indexer USING (true) WITH CHECK (true);

-- chunks
GRANT SELECT ON kh.chunks TO kh_app;
CREATE POLICY kh_app_select ON kh.chunks FOR SELECT TO kh_app
    USING (kh.visible(required_audiences, allowed_emails));
GRANT SELECT, INSERT, UPDATE, DELETE ON kh.chunks TO kh_indexer;
CREATE POLICY kh_indexer_all ON kh.chunks FOR ALL TO kh_indexer USING (true) WITH CHECK (true);

-- entities_cache
GRANT SELECT ON kh.entities_cache TO kh_app;
CREATE POLICY kh_app_select ON kh.entities_cache FOR SELECT TO kh_app
    USING (kh.visible(required_audiences, allowed_emails));
GRANT SELECT, INSERT, UPDATE, DELETE ON kh.entities_cache TO kh_indexer;
CREATE POLICY kh_indexer_all ON kh.entities_cache FOR ALL TO kh_indexer USING (true) WITH CHECK (true);

-- raw_records
GRANT SELECT, INSERT ON kh.raw_records TO kh_app;
CREATE POLICY kh_app_select ON kh.raw_records FOR SELECT TO kh_app
    USING (kh.visible(required_audiences, allowed_emails));
CREATE POLICY kh_app_insert ON kh.raw_records FOR INSERT TO kh_app
    WITH CHECK (kh.caller_type() = 'n8n_workflow' AND ingested_by_sub = kh.caller_sub());
GRANT SELECT ON kh.raw_records TO kh_indexer;
GRANT UPDATE (required_audiences, allowed_emails) ON kh.raw_records TO kh_indexer;
CREATE POLICY kh_indexer_select ON kh.raw_records FOR SELECT TO kh_indexer USING (true);
CREATE POLICY kh_indexer_update ON kh.raw_records FOR UPDATE TO kh_indexer USING (true) WITH CHECK (true);
GRANT SELECT (id, required_audiences, allowed_emails, ingested_by_sub) ON kh.raw_records TO kh_provenance;
CREATE POLICY kh_provenance_select ON kh.raw_records FOR SELECT TO kh_provenance USING (true);

-- raw_audience_map
GRANT SELECT, INSERT, UPDATE, DELETE ON kh.raw_audience_map TO kh_indexer;
CREATE POLICY kh_indexer_all ON kh.raw_audience_map FOR ALL TO kh_indexer USING (true) WITH CHECK (true);
GRANT SELECT ON kh.raw_audience_map TO kh_provenance;
CREATE POLICY kh_provenance_select ON kh.raw_audience_map FOR SELECT TO kh_provenance USING (true);

-- derived_records
GRANT SELECT, INSERT ON kh.derived_records TO kh_app;
CREATE POLICY kh_app_select ON kh.derived_records FOR SELECT TO kh_app
    USING (kh.visible(required_audiences, allowed_emails));
CREATE POLICY kh_app_insert ON kh.derived_records FOR INSERT TO kh_app
    WITH CHECK (created_by_sub = kh.caller_sub());
GRANT SELECT ON kh.derived_records TO kh_indexer;
GRANT UPDATE (required_audiences, allowed_emails) ON kh.derived_records TO kh_indexer;
CREATE POLICY kh_indexer_select ON kh.derived_records FOR SELECT TO kh_indexer USING (true);
CREATE POLICY kh_indexer_update ON kh.derived_records FOR UPDATE TO kh_indexer USING (true) WITH CHECK (true);
GRANT SELECT (id, required_audiences, allowed_emails, created_by_sub) ON kh.derived_records TO kh_provenance;
CREATE POLICY kh_provenance_select ON kh.derived_records FOR SELECT TO kh_provenance USING (true);

-- derived_sources (kh_indexer UPDATE of the snapshot columns: 3.3, recompute)
GRANT SELECT, INSERT ON kh.derived_sources TO kh_app;
CREATE POLICY kh_app_select ON kh.derived_sources FOR SELECT TO kh_app
    USING (EXISTS (SELECT 1 FROM kh.derived_records r WHERE r.id = derived_sources.derived_id));
CREATE POLICY kh_app_insert ON kh.derived_sources FOR INSERT TO kh_app
    WITH CHECK (kh.owns_derived(derived_id));
GRANT SELECT ON kh.derived_sources TO kh_indexer;
GRANT UPDATE (src_required_audiences, src_allowed_emails) ON kh.derived_sources TO kh_indexer;
CREATE POLICY kh_indexer_select ON kh.derived_sources FOR SELECT TO kh_indexer USING (true);
CREATE POLICY kh_indexer_update ON kh.derived_sources FOR UPDATE TO kh_indexer USING (true) WITH CHECK (true);
GRANT SELECT ON kh.derived_sources TO kh_provenance;
CREATE POLICY kh_provenance_select ON kh.derived_sources FOR SELECT TO kh_provenance USING (true);

-- candidates
GRANT SELECT, INSERT ON kh.candidates TO kh_app;
CREATE POLICY kh_app_select ON kh.candidates FOR SELECT TO kh_app
    USING (kh.visible(required_audiences, allowed_emails));
CREATE POLICY kh_app_insert ON kh.candidates FOR INSERT TO kh_app
    WITH CHECK (created_by_sub = kh.caller_sub());
GRANT SELECT ON kh.candidates TO kh_indexer;
GRANT UPDATE (state, decided_at, required_audiences, allowed_emails) ON kh.candidates TO kh_indexer;
CREATE POLICY kh_indexer_select ON kh.candidates FOR SELECT TO kh_indexer USING (true);
CREATE POLICY kh_indexer_update ON kh.candidates FOR UPDATE TO kh_indexer USING (true) WITH CHECK (true);

-- candidate_sources
GRANT SELECT, INSERT ON kh.candidate_sources TO kh_app;
CREATE POLICY kh_app_select ON kh.candidate_sources FOR SELECT TO kh_app
    USING (EXISTS (SELECT 1 FROM kh.candidates c WHERE c.id = candidate_sources.candidate_id));
CREATE POLICY kh_app_insert ON kh.candidate_sources FOR INSERT TO kh_app
    WITH CHECK (kh.owns_candidate(candidate_id));
GRANT SELECT ON kh.candidate_sources TO kh_indexer;
GRANT UPDATE (src_required_audiences, src_allowed_emails) ON kh.candidate_sources TO kh_indexer;
CREATE POLICY kh_indexer_select ON kh.candidate_sources FOR SELECT TO kh_indexer USING (true);
CREATE POLICY kh_indexer_update ON kh.candidate_sources FOR UPDATE TO kh_indexer USING (true) WITH CHECK (true);

-- conflicts
GRANT SELECT, INSERT ON kh.conflicts TO kh_app;
CREATE POLICY kh_app_select ON kh.conflicts FOR SELECT TO kh_app
    USING (kh.visible(required_audiences, allowed_emails));
CREATE POLICY kh_app_insert ON kh.conflicts FOR INSERT TO kh_app
    WITH CHECK (created_by_sub = kh.caller_sub());
GRANT SELECT ON kh.conflicts TO kh_indexer;
GRANT UPDATE (state, closed_at, closed_by_pr, required_audiences, allowed_emails) ON kh.conflicts TO kh_indexer;
CREATE POLICY kh_indexer_select ON kh.conflicts FOR SELECT TO kh_indexer USING (true);
CREATE POLICY kh_indexer_update ON kh.conflicts FOR UPDATE TO kh_indexer USING (true) WITH CHECK (true);

-- conflict_items
GRANT SELECT, INSERT ON kh.conflict_items TO kh_app;
CREATE POLICY kh_app_select ON kh.conflict_items FOR SELECT TO kh_app
    USING (EXISTS (SELECT 1 FROM kh.conflicts k WHERE k.id = conflict_items.conflict_id));
CREATE POLICY kh_app_insert ON kh.conflict_items FOR INSERT TO kh_app
    WITH CHECK (kh.owns_conflict(conflict_id));
GRANT SELECT ON kh.conflict_items TO kh_indexer;
GRANT UPDATE (src_required_audiences, src_allowed_emails) ON kh.conflict_items TO kh_indexer;
CREATE POLICY kh_indexer_select ON kh.conflict_items FOR SELECT TO kh_indexer USING (true);
CREATE POLICY kh_indexer_update ON kh.conflict_items FOR UPDATE TO kh_indexer USING (true) WITH CHECK (true);

-- index_jobs, repo_state, index_alerts
GRANT SELECT, INSERT, UPDATE, DELETE ON kh.index_jobs TO kh_indexer;
CREATE POLICY kh_indexer_all ON kh.index_jobs FOR ALL TO kh_indexer USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE, DELETE ON kh.repo_state TO kh_indexer;
CREATE POLICY kh_indexer_all ON kh.repo_state FOR ALL TO kh_indexer USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE, DELETE ON kh.index_alerts TO kh_indexer;
CREATE POLICY kh_indexer_all ON kh.index_alerts FOR ALL TO kh_indexer USING (true) WITH CHECK (true);

-- claim_jti: kh_app reaches it only through kh.claim_seen; kh_indexer purges expired rows.
GRANT SELECT, DELETE ON kh.claim_jti TO kh_indexer;
CREATE POLICY kh_indexer_select ON kh.claim_jti FOR SELECT TO kh_indexer USING (expires_at < now());
CREATE POLICY kh_indexer_delete ON kh.claim_jti FOR DELETE TO kh_indexer USING (expires_at < now());

-- schema_migrations: kh_migrator only (policy from 0001).
