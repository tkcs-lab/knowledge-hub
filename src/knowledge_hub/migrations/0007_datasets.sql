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

CREATE FUNCTION kh.dataset_mirror_row(ds text, r jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    dtitle  text;
    tcol    text;
    auds    text[];
    rid     text;
    ttl     text;
    body    text;
    dead    boolean;
    rec_id  uuid;
    old_ttl text;
    old_exc text;
    old_occ timestamptz;
    old_out boolean;
    new_occ timestamptz;
BEGIN
    SELECT d.title, d.title_column, d.audiences INTO dtitle, tcol, auds
      FROM kh.datasets d WHERE d.name = ds;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown dataset %', ds;
    END IF;
    rid := 'row:' || ds || ':' || (r ->> '_id');
    IF (r ->> '_audience') <> ALL (auds) THEN
        DELETE FROM kh.records WHERE dataset_row = rid;
        RETURN;
    END IF;
    ttl := left(coalesce(nullif(r ->> tcol, ''), r ->> '_key'), 300);
    body := dtitle || ': ' || ttl;
    SELECT body || coalesce(string_agg(E'\n' || c.label || ': ' || (r ->> c.name), '' ORDER BY c.ordinal), '')
      INTO body
      FROM kh.columns c
     WHERE c.dataset = ds AND c.embed AND NOT c.sensitive
       AND coalesce(r ->> c.name, '') <> '';
    body := left(body, 4000);
    dead := coalesce((r ->> '_outdated')::boolean, false);
    new_occ := (r ->> '_source_updated_at')::timestamptz;

    SELECT x.id, x.title, x.excerpt, x.occurred_at, x.outdated
      INTO rec_id, old_ttl, old_exc, old_occ, old_out
      FROM kh.records x WHERE x.dataset_row = rid FOR UPDATE;
    IF NOT FOUND THEN
        INSERT INTO kh.records (id, audience, source, source_ref, source_url, title, blob_key, excerpt,
                                content_type, occurred_at, labels, written_by, created_by_sub,
                                created_by_type, dataset_row, outdated, outdated_at, outdated_by,
                                outdated_note, replaced_by)
        VALUES (gen_random_uuid(), r ->> '_audience', 'dataset:' || ds, r ->> '_id', '', ttl, '', body,
                'text/plain', new_occ, '{}', 'dataset', coalesce(kh.caller_sub(), 'dataset'),
                'dataset', rid, dead, CASE WHEN dead THEN now() END,
                CASE WHEN dead THEN 'dataset' ELSE '' END, '', '');
        RETURN;
    END IF;
    IF old_ttl IS DISTINCT FROM ttl OR old_exc IS DISTINCT FROM body
       OR old_occ IS DISTINCT FROM new_occ OR old_out IS DISTINCT FROM dead THEN
        UPDATE kh.records
           SET title = ttl, excerpt = body, occurred_at = new_occ, outdated = dead,
               outdated_at = CASE WHEN dead THEN coalesce(outdated_at, now()) END,
               outdated_by = CASE WHEN dead THEN 'dataset' ELSE '' END
         WHERE id = rec_id;
        IF old_exc IS DISTINCT FROM body
           AND NOT EXISTS (SELECT 1 FROM kh.index_jobs
                            WHERE kind = 'embed_record' AND state = 'queued'
                              AND payload ->> 'id' = rec_id::text) THEN
            INSERT INTO kh.index_jobs (kind, payload)
            VALUES ('embed_record', jsonb_build_object('id', rec_id::text));
        END IF;
    END IF;
END $$;

REVOKE EXECUTE ON FUNCTION kh.dataset_mirror_row(text, jsonb) FROM PUBLIC;

CREATE FUNCTION kh.dataset_refresh(ds text, after uuid, n int) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    r    jsonb;
    last uuid;
BEGIN
    IF ds !~ '^[a-z][a-z0-9_]{0,39}$' OR NOT EXISTS (SELECT 1 FROM kh.datasets WHERE name = ds) THEN
        RAISE EXCEPTION 'unknown dataset %', ds;
    END IF;
    FOR r IN EXECUTE format(
        'SELECT to_jsonb(t) FROM kh_data.%I t WHERE t._id > $1 ORDER BY t._id LIMIT $2', ds)
        USING coalesce(after, '00000000-0000-0000-0000-000000000000'::uuid), n
    LOOP
        PERFORM kh.dataset_mirror_row(ds, r);
        last := (r ->> '_id')::uuid;
    END LOOP;
    RETURN last;
END $$;

REVOKE EXECUTE ON FUNCTION kh.dataset_refresh(text, uuid, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.dataset_refresh(text, uuid, int) TO kh_indexer;

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

-- History (p9): versions of a row in kh_data.<n>__h, written by triggers that exist only while history is on.

CREATE FUNCTION kh.dataset_history_row() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    h    text := TG_TABLE_NAME || '__h';
    cols text;
BEGIN
    IF TG_TABLE_SCHEMA <> 'kh_data' THEN
        RAISE EXCEPTION 'dataset_history_row outside kh_data';
    END IF;
    SELECT string_agg(format('%I', c), ', ') INTO cols FROM (
        SELECT unnest(ARRAY['_row_id', '_key', '_audience', '_source_updated_at', '_outdated',
                            '_valid_from', '_valid_to']) AS c, 0 AS o
        UNION ALL
        SELECT name, ordinal FROM kh.columns WHERE dataset = TG_TABLE_NAME
        ORDER BY o
    ) s;
    EXECUTE format('UPDATE kh_data.%I SET _valid_to = now() WHERE _row_id = $1 AND _valid_to IS NULL', h)
        USING NEW._id;
    EXECUTE format(
        'INSERT INTO kh_data.%I (%s) SELECT %s FROM jsonb_populate_record(NULL::kh_data.%I, $1)',
        h, cols, cols, h)
        USING to_jsonb(NEW) || jsonb_build_object(
            '_row_id', NEW._id, '_valid_from', now(), '_valid_to', NULL);
    RETURN NULL;
END $$;

REVOKE EXECUTE ON FUNCTION kh.dataset_history_row() FROM PUBLIC;

CREATE FUNCTION kh.dataset_history_purge(days int) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    ds    text;
    n     bigint;
    total bigint := 0;
BEGIN
    FOR ds IN SELECT name FROM kh.datasets WHERE history ORDER BY name LOOP
        EXECUTE format(
            'DELETE FROM kh_data.%I WHERE _valid_to < now() - make_interval(days => $1)', ds || '__h')
            USING days;
        GET DIAGNOSTICS n = ROW_COUNT;
        total := total + n;
    END LOOP;
    RETURN total;
END $$;

REVOKE EXECUTE ON FUNCTION kh.dataset_history_purge(int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.dataset_history_purge(int) TO kh_indexer;
