-- 0007: operator-defined datasets: catalog, data schema, row records, sync bookkeeping, BI mapping.

CREATE SCHEMA kh_data AUTHORIZATION kh_migrator;
REVOKE ALL ON SCHEMA kh_data FROM PUBLIC;
GRANT USAGE ON SCHEMA kh_data TO kh_app, kh_bi;
GRANT USAGE ON SCHEMA kh TO kh_bi;

CREATE TABLE kh.datasets (
    name               text PRIMARY KEY
                       CHECK (name ~ '^[a-z][a-z0-9_]{0,39}$' AND position('__' in name) = 0 AND right(name, 1) <> '_'),
    title              text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    description        text NOT NULL DEFAULT '',
    audiences          text[] NOT NULL CHECK (cardinality(audiences) >= 1),
    title_column       text NOT NULL DEFAULT '',
    history            boolean NOT NULL DEFAULT false,
    max_outdated_percent int NOT NULL DEFAULT 20 CHECK (max_outdated_percent BETWEEN 0 AND 100),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE kh.columns (
    dataset     text NOT NULL REFERENCES kh.datasets (name),
    name        text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]{0,62}$'),
    ordinal     int NOT NULL,
    type        text NOT NULL CHECK (type IN ('text','number','date','bool','choice','ref')),
    label       text NOT NULL,
    choices     text[] NOT NULL DEFAULT '{}',
    ref_dataset text NOT NULL DEFAULT '',
    source_name text NOT NULL DEFAULT '',
    filterable  boolean NOT NULL DEFAULT false,
    embed       boolean NOT NULL DEFAULT false,
    sensitive   boolean NOT NULL DEFAULT false,
    indexed     boolean NOT NULL DEFAULT false,
    PRIMARY KEY (dataset, name),
    CONSTRAINT columns_embed_sensitive CHECK (NOT (embed AND sensitive)),
    CONSTRAINT columns_choices CHECK ((type = 'choice') = (cardinality(choices) > 0)),
    CONSTRAINT columns_ref CHECK ((type = 'ref') = (ref_dataset <> ''))
);
CREATE UNIQUE INDEX columns_source_name_idx ON kh.columns (dataset, source_name) WHERE source_name <> '';

CREATE TABLE kh.saved_queries (
    audience        text NOT NULL REFERENCES kh.groups (name),
    name            text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    dataset         text NOT NULL REFERENCES kh.datasets (name),
    spec            jsonb NOT NULL,
    created_by_sub  text NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (audience, name)
);

CREATE TABLE kh.dataset_syncs (
    dataset     text NOT NULL REFERENCES kh.datasets (name),
    audience    text NOT NULL REFERENCES kh.groups (name),
    sync_id     text NOT NULL CHECK (sync_id ~ '^[A-Za-z0-9._:-]{1,64}$'),
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    rows_seen   bigint NOT NULL DEFAULT 0,
    outdated    bigint NOT NULL DEFAULT 0,
    watermark   timestamptz,
    PRIMARY KEY (dataset, audience, sync_id)
);

CREATE TABLE kh.bi_roles (
    role_name  text PRIMARY KEY CHECK (role_name ~ '^kh_bi_[a-z2-7]{10}$'),
    audience   text NOT NULL REFERENCES kh.groups (name),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE kh.records ADD COLUMN dataset_row text NOT NULL DEFAULT '',
    ADD CONSTRAINT records_dataset_row CHECK ((dataset_row = '') = (left(source, 8) <> 'dataset:'));
CREATE UNIQUE INDEX records_dataset_row_idx ON kh.records (dataset_row) WHERE dataset_row <> '';
ALTER POLICY kh_app_insert ON kh.records
    WITH CHECK (kh.can_read(audience) AND created_by_sub = kh.caller_sub() AND dataset_row = '');

ALTER TABLE kh.index_jobs DROP CONSTRAINT index_jobs_kind_check;
ALTER TABLE kh.index_jobs ADD CONSTRAINT index_jobs_kind_check
    CHECK (kind IN ('git_push', 'embed_record', 'dataset_refresh'));
CREATE INDEX index_jobs_queued_idx ON kh.index_jobs (kind, (payload ->> 'id')) WHERE state = 'queued';

-- ---------------------------------------------------------------------------
-- Functions
-- ---------------------------------------------------------------------------

CREATE FUNCTION kh.bi_group() RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ SELECT audience FROM kh.bi_roles WHERE role_name = session_user $$;

REVOKE EXECUTE ON FUNCTION kh.bi_group() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.bi_group() TO kh_bi;

CREATE FUNCTION kh.dataset_row_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW._created_at := now();
        NEW._updated_at := now();
        NEW._seen_at := now();
        NEW._updated_by := coalesce(kh.caller_sub(), current_user);
    ELSE
        IF NEW._id IS DISTINCT FROM OLD._id OR NEW._key IS DISTINCT FROM OLD._key
           OR NEW._audience IS DISTINCT FROM OLD._audience
           OR NEW._created_at IS DISTINCT FROM OLD._created_at THEN
            RAISE EXCEPTION 'dataset row identity cannot change' USING ERRCODE = 'P0K0D';
        END IF;
        NEW._updated_at := now();
        NEW._updated_by := coalesce(kh.caller_sub(), current_user);
    END IF;
    RETURN NEW;
END $$;

REVOKE EXECUTE ON FUNCTION kh.dataset_row_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.dataset_row_guard() TO kh_app;

CREATE FUNCTION kh.dataset_mirror_row(ds text, r jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
BEGIN NULL; END $$;

REVOKE EXECUTE ON FUNCTION kh.dataset_mirror_row(text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.dataset_mirror_row(text, jsonb) TO kh_app;

CREATE FUNCTION kh.dataset_row_sync() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
BEGIN
    IF TG_TABLE_SCHEMA <> 'kh_data' THEN
        RAISE EXCEPTION 'dataset_row_sync outside kh_data';
    END IF;
    PERFORM kh.dataset_mirror_row(TG_TABLE_NAME, to_jsonb(NEW));
    RETURN NULL;
END $$;

REVOKE EXECUTE ON FUNCTION kh.dataset_row_sync() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.dataset_row_sync() TO kh_app;

-- Redefined as in 0005, plus the refusal of row records.

CREATE OR REPLACE FUNCTION kh.move_record(rec uuid, from_aud text, to_aud text)
    RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    who     text := kh.caller_sub();
    cur_aud text;
    v_src   text;
    v_ref   text;
BEGIN
    IF who IS NULL THEN
        RETURN NULL;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('rec:' || rec::text));
    SELECT audience, source, source_ref INTO cur_aud, v_src, v_ref
      FROM kh.records WHERE id = rec FOR UPDATE;
    IF cur_aud IS NULL OR NOT kh.can_read(cur_aud) THEN
        RETURN NULL;                       -- missing, hidden and anonymous answer the same
    END IF;
    IF EXISTS (SELECT 1 FROM kh.records WHERE id = rec AND dataset_row <> '') THEN
        RAISE EXCEPTION 'record mirrors a dataset row' USING ERRCODE = 'P0K0C';
    END IF;
    IF NOT kh.can_read(to_aud) THEN
        RAISE EXCEPTION 'to_group is not held' USING ERRCODE = 'P0K08';
    END IF;
    IF from_aud = to_aud OR cur_aud <> from_aud THEN
        RAISE EXCEPTION 'record is not in from_group' USING ERRCODE = 'P0K09';
    END IF;
    IF EXISTS (SELECT 1 FROM kh.records WHERE replaced_by = 'rec:' || rec::text) THEN
        RAISE EXCEPTION 'another record names this one as replacement' USING ERRCODE = 'P0K0A';
    END IF;
    UPDATE kh.records SET audience = to_aud, replaced_by = '' WHERE id = rec;
    UPDATE kh.chunks SET audience = to_aud WHERE record_id = rec;
    DELETE FROM kh.record_moves WHERE audience = to_aud AND source = v_src AND source_ref = v_ref;
    INSERT INTO kh.record_moves (audience, source, source_ref) VALUES (cur_aud, v_src, v_ref)
        ON CONFLICT (audience, source, source_ref) DO UPDATE SET moved_at = now();
    INSERT INTO kh.index_jobs (kind, payload) VALUES ('embed_record', jsonb_build_object('id', rec::text));
    RETURN cur_aud;
END $$;


CREATE OR REPLACE FUNCTION kh.mark_record_outdated(rec uuid, flag boolean, successor text, note text)
    RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    rec_aud  text;
    succ_aud text;
    who      text := kh.caller_sub();
BEGIN
    SELECT audience INTO rec_aud FROM kh.records WHERE id = rec FOR NO KEY UPDATE;
    IF rec_aud IS NULL OR who IS NULL OR NOT kh.can_read(rec_aud) THEN
        RETURN NULL;                       -- missing, hidden and anonymous answer the same
    END IF;
    IF EXISTS (SELECT 1 FROM kh.records WHERE id = rec AND dataset_row <> '') THEN
        RAISE EXCEPTION 'record mirrors a dataset row' USING ERRCODE = 'P0K0B';
    END IF;
    IF flag AND successor <> '' THEN
        IF successor !~ '^(doc|rec):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR successor = 'rec:' || rec::text THEN
            RAISE EXCEPTION 'replaced_by is invalid' USING ERRCODE = 'P0K07';
        END IF;
        IF left(successor, 3) = 'doc' THEN
            SELECT audience INTO succ_aud FROM kh.documents WHERE id = substr(successor, 5)::uuid;
        ELSE
            SELECT audience INTO succ_aud FROM kh.records WHERE id = substr(successor, 5)::uuid FOR KEY SHARE;
        END IF;
        IF succ_aud IS DISTINCT FROM rec_aud THEN   -- missing and other-group answer the same
            RAISE EXCEPTION 'replaced_by is invalid' USING ERRCODE = 'P0K07';
        END IF;
    END IF;
    UPDATE kh.records
       SET outdated      = flag,
           outdated_at   = CASE WHEN flag THEN now() ELSE NULL END,
           outdated_by   = CASE WHEN flag THEN who ELSE '' END,
           outdated_note = CASE WHEN flag THEN note ELSE '' END,
           replaced_by   = CASE WHEN flag THEN successor ELSE '' END
     WHERE id = rec;
    RETURN true;
END $$;

REVOKE EXECUTE ON FUNCTION kh.move_record(uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.move_record(uuid, text, text) TO kh_app;
REVOKE EXECUTE ON FUNCTION kh.mark_record_outdated(uuid, boolean, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.mark_record_outdated(uuid, boolean, text, text) TO kh_app;

-- ---------------------------------------------------------------------------
-- Row-level security
-- ---------------------------------------------------------------------------

ALTER TABLE kh.datasets ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.datasets FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.datasets FOR ALL TO kh_migrator USING (true) WITH CHECK (true);
GRANT SELECT ON kh.datasets TO kh_app, kh_indexer, kh_bi;
CREATE POLICY kh_app_select ON kh.datasets FOR SELECT TO kh_app USING (audiences && kh.caller_audiences());
CREATE POLICY kh_indexer_select ON kh.datasets FOR SELECT TO kh_indexer USING (true);
CREATE POLICY kh_bi_select ON kh.datasets FOR SELECT TO kh_bi USING ((SELECT kh.bi_group()) = ANY (audiences));

ALTER TABLE kh.columns ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.columns FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.columns FOR ALL TO kh_migrator USING (true) WITH CHECK (true);
GRANT SELECT ON kh.columns TO kh_app, kh_indexer, kh_bi;
CREATE POLICY kh_app_select ON kh.columns FOR SELECT TO kh_app
    USING (EXISTS (SELECT 1 FROM kh.datasets d WHERE d.name = dataset));
CREATE POLICY kh_indexer_select ON kh.columns FOR SELECT TO kh_indexer USING (true);
CREATE POLICY kh_bi_select ON kh.columns FOR SELECT TO kh_bi
    USING (EXISTS (SELECT 1 FROM kh.datasets d WHERE d.name = dataset));

ALTER TABLE kh.saved_queries ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.saved_queries FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.saved_queries FOR ALL TO kh_migrator USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE ON kh.saved_queries TO kh_app;
CREATE POLICY kh_app_select ON kh.saved_queries FOR SELECT TO kh_app USING (kh.can_read(audience));
CREATE POLICY kh_app_insert ON kh.saved_queries FOR INSERT TO kh_app
    WITH CHECK (kh.can_read(audience) AND created_by_sub = kh.caller_sub());
CREATE POLICY kh_app_update ON kh.saved_queries FOR UPDATE TO kh_app
    USING (kh.can_read(audience)) WITH CHECK (kh.can_read(audience));

ALTER TABLE kh.dataset_syncs ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.dataset_syncs FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.dataset_syncs FOR ALL TO kh_migrator USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE ON kh.dataset_syncs TO kh_app;
CREATE POLICY kh_app_select ON kh.dataset_syncs FOR SELECT TO kh_app USING (kh.can_read(audience));
CREATE POLICY kh_app_insert ON kh.dataset_syncs FOR INSERT TO kh_app WITH CHECK (kh.can_read(audience));
CREATE POLICY kh_app_update ON kh.dataset_syncs FOR UPDATE TO kh_app
    USING (kh.can_read(audience)) WITH CHECK (kh.can_read(audience));

ALTER TABLE kh.bi_roles ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.bi_roles FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.bi_roles FOR ALL TO kh_migrator USING (true) WITH CHECK (true);
