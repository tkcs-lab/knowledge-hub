-- Baseline schema of the two-tier hub (plan sections 3.3 and 3.5).
-- Roles kh_migrator, kh_app and kh_indexer are created outside the migrations; this file only
-- grants. Every table has forced row-level security and a kh_migrator_all policy, so each grant
-- below needs a policy of its own. Everything is owned by kh_migrator.

CREATE SCHEMA IF NOT EXISTS kh AUTHORIZATION kh_migrator;

GRANT USAGE ON SCHEMA public TO kh_migrator, kh_app, kh_indexer;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA kh FROM PUBLIC;
GRANT USAGE ON SCHEMA kh TO kh_app, kh_indexer;

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

CREATE TABLE kh.schema_migrations (
    name       text PRIMARY KEY,
    sha256     text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE kh.claim_jti (
    jti        text PRIMARY KEY,
    expires_at timestamptz NOT NULL
);

CREATE TABLE kh.groups (
    name text PRIMARY KEY CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    repo text NOT NULL UNIQUE
);

CREATE TABLE kh.labels (
    name        text PRIMARY KEY CHECK (name ~ '^[a-z0-9][a-z0-9_.:-]{0,62}$'),
    description text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE kh.documents (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    repo       text NOT NULL,
    path       text NOT NULL,
    doc_key    text NOT NULL,
    audience   text NOT NULL REFERENCES kh.groups (name),
    title      text NOT NULL,
    body       text NOT NULL,
    labels     text[] NOT NULL DEFAULT '{}',
    review_by  date NOT NULL,
    stale      boolean NOT NULL DEFAULT false,
    commit_sha text NOT NULL,
    blob_sha   text NOT NULL,
    indexed_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (repo, doc_key),
    UNIQUE (repo, path)
);

CREATE TABLE kh.records (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    audience        text NOT NULL REFERENCES kh.groups (name),
    source          text NOT NULL,
    source_ref      text NOT NULL,
    source_url      text NOT NULL DEFAULT '',
    title           text NOT NULL,
    blob_key        text NOT NULL,
    excerpt         text NOT NULL,
    content_type    text NOT NULL,
    occurred_at     timestamptz NOT NULL,
    labels          text[] NOT NULL DEFAULT '{}',
    written_by      text NOT NULL,
    created_by_sub  text NOT NULL,
    created_by_type text NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (audience, source, source_ref)
);

CREATE TABLE kh.chunks (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id      uuid REFERENCES kh.documents (id) ON DELETE CASCADE,
    record_id        uuid REFERENCES kh.records (id) ON DELETE CASCADE,
    owner_key        text NOT NULL,
    owner_created_at timestamptz NOT NULL,
    ordinal          int NOT NULL,
    heading_path     text NOT NULL DEFAULT '',
    text             text NOT NULL,
    content_sha      text NOT NULL,
    embedding        vector(1024),
    embedding_model  text,
    tsv              tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(heading_path, '') || ' ' || text)) STORED,
    tier             text NOT NULL CHECK (tier IN ('T0', 'T3')),
    audience         text NOT NULL,
    labels           text[] NOT NULL DEFAULT '{}',
    stale            boolean NOT NULL DEFAULT false,
    CONSTRAINT chunks_one_owner CHECK (num_nonnulls(document_id, record_id) = 1)
);
CREATE INDEX chunks_owner_key_idx ON kh.chunks (owner_key);
CREATE INDEX chunks_tsv_idx ON kh.chunks USING gin (tsv);

CREATE TABLE kh.index_jobs (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind       text NOT NULL CHECK (kind IN ('git_push', 'embed_record')),
    repo       text,
    payload    jsonb NOT NULL DEFAULT '{}',
    state      text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'done', 'failed')),
    attempts   int NOT NULL DEFAULT 0,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    run_after  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE kh.repo_state (
    repo             text PRIMARY KEY,
    last_indexed_sha text NOT NULL,
    indexed_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE kh.index_alerts (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repo       text NOT NULL,
    path       text NOT NULL,
    message    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Caller helpers (SECURITY INVOKER, fully qualified, no SET clause: inlinable in policies)
-- ---------------------------------------------------------------------------

CREATE FUNCTION kh.caller_audiences() RETURNS text[]
    LANGUAGE sql STABLE
    AS $$ SELECT coalesce(string_to_array(nullif(current_setting('kh.audiences', true), ''), ','), '{}'::text[]) $$;

CREATE FUNCTION kh.caller_sub() RETURNS text
    LANGUAGE sql STABLE
    AS $$ SELECT nullif(current_setting('kh.sub', true), '') $$;

CREATE FUNCTION kh.caller_type() RETURNS text
    LANGUAGE sql STABLE
    AS $$ SELECT nullif(current_setting('kh.principal_type', true), '') $$;

-- coalesce: a NULL audience is not readable, never "not unreadable".
CREATE FUNCTION kh.can_read(a text) RETURNS boolean
    LANGUAGE sql STABLE
    AS $$ SELECT coalesce($1 = ANY (kh.caller_audiences()), false) $$;

REVOKE EXECUTE ON FUNCTION kh.caller_audiences(), kh.caller_sub(), kh.caller_type(),
    kh.can_read(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.caller_audiences(), kh.caller_sub(), kh.caller_type(),
    kh.can_read(text) TO kh_app;

-- ---------------------------------------------------------------------------
-- Replay cache: kh_app reaches claim_jti only through this definer function.
-- ---------------------------------------------------------------------------

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

-- ---------------------------------------------------------------------------
-- Row-level security: enabled and forced everywhere, plus the kh_migrator policy
-- ---------------------------------------------------------------------------

ALTER TABLE kh.schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.schema_migrations FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.schema_migrations FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.claim_jti ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.claim_jti FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.claim_jti FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.groups FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.groups FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.labels ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.labels FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.labels FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.documents FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.documents FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.records ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.records FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.records FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

ALTER TABLE kh.chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.chunks FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.chunks FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

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
-- Grants and policies
-- ---------------------------------------------------------------------------

-- groups, labels: every caller and the indexer read them all.
GRANT SELECT ON kh.groups, kh.labels TO kh_app, kh_indexer;
CREATE POLICY kh_app_select ON kh.groups FOR SELECT TO kh_app USING (true);
CREATE POLICY kh_indexer_select ON kh.groups FOR SELECT TO kh_indexer USING (true);
CREATE POLICY kh_app_select ON kh.labels FOR SELECT TO kh_app USING (true);
CREATE POLICY kh_indexer_select ON kh.labels FOR SELECT TO kh_indexer USING (true);

-- documents
GRANT SELECT ON kh.documents TO kh_app;
CREATE POLICY kh_app_select ON kh.documents FOR SELECT TO kh_app USING (kh.can_read(audience));
GRANT SELECT, INSERT, UPDATE, DELETE ON kh.documents TO kh_indexer;
CREATE POLICY kh_indexer_all ON kh.documents FOR ALL TO kh_indexer USING (true) WITH CHECK (true);

-- records: kh_app inserts for audiences it holds and never updates or deletes.
GRANT SELECT, INSERT ON kh.records TO kh_app;
CREATE POLICY kh_app_select ON kh.records FOR SELECT TO kh_app USING (kh.can_read(audience));
CREATE POLICY kh_app_insert ON kh.records FOR INSERT TO kh_app
    WITH CHECK (kh.can_read(audience) AND created_by_sub = kh.caller_sub());
GRANT SELECT ON kh.records TO kh_indexer;
CREATE POLICY kh_indexer_select ON kh.records FOR SELECT TO kh_indexer USING (true);

-- chunks
GRANT SELECT ON kh.chunks TO kh_app;
CREATE POLICY kh_app_select ON kh.chunks FOR SELECT TO kh_app USING (kh.can_read(audience));
GRANT SELECT, INSERT, UPDATE, DELETE ON kh.chunks TO kh_indexer;
CREATE POLICY kh_indexer_all ON kh.chunks FOR ALL TO kh_indexer USING (true) WITH CHECK (true);

-- indexer-only tables
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

-- ---------------------------------------------------------------------------
-- Labels: every label on a row must exist; a label in use cannot be deleted.
-- ---------------------------------------------------------------------------

-- SECURITY INVOKER: both writers (kh_app, kh_indexer) may read every label.
CREATE FUNCTION kh.assert_labels_exist(names text[]) RETURNS void
    LANGUAGE plpgsql STABLE
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    missing text;
BEGIN
    SELECT n INTO missing
      FROM unnest(coalesce(names, '{}'::text[])) AS n
     WHERE NOT EXISTS (SELECT 1 FROM kh.labels l WHERE l.name = n)
     LIMIT 1;
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'unknown label %', missing USING ERRCODE = 'P0K05';
    END IF;
END $$;

CREATE FUNCTION kh.labels_exist_check() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = kh, pg_temp
    AS $$
BEGIN
    PERFORM kh.assert_labels_exist(NEW.labels);
    RETURN NEW;
END $$;

CREATE TRIGGER labels_exist BEFORE INSERT OR UPDATE OF labels ON kh.documents
    FOR EACH ROW EXECUTE FUNCTION kh.labels_exist_check();
CREATE TRIGGER labels_exist BEFORE INSERT OR UPDATE OF labels ON kh.records
    FOR EACH ROW EXECUTE FUNCTION kh.labels_exist_check();

-- Definer: it must see every document and record whoever deletes the label.
CREATE FUNCTION kh.labels_in_use_guard() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM kh.documents d WHERE OLD.name = ANY (d.labels))
       OR EXISTS (SELECT 1 FROM kh.records r WHERE OLD.name = ANY (r.labels)) THEN
        RAISE EXCEPTION 'label % is in use', OLD.name USING ERRCODE = 'P0K06';
    END IF;
    RETURN OLD;
END $$;

CREATE TRIGGER labels_in_use BEFORE DELETE ON kh.labels
    FOR EACH ROW EXECUTE FUNCTION kh.labels_in_use_guard();

-- ---------------------------------------------------------------------------
-- Chunk consistency. SECURITY INVOKER: the writer is kh_indexer (or kh_migrator), which
-- can read every owner row.
-- ---------------------------------------------------------------------------

CREATE FUNCTION kh.chunks_inherit_owner() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = kh, pg_temp
    AS $$
BEGIN
    IF NEW.document_id IS NOT NULL THEN
        SELECT 'doc:' || d.id, 'T0', d.audience, d.labels, d.stale, d.indexed_at
          INTO NEW.owner_key, NEW.tier, NEW.audience, NEW.labels, NEW.stale, NEW.owner_created_at
          FROM kh.documents d WHERE d.id = NEW.document_id;
    ELSIF NEW.record_id IS NOT NULL THEN
        SELECT 'rec:' || r.id, 'T3', r.audience, r.labels, false, r.ingested_at
          INTO NEW.owner_key, NEW.tier, NEW.audience, NEW.labels, NEW.stale, NEW.owner_created_at
          FROM kh.records r WHERE r.id = NEW.record_id;
    END IF;
    -- No owner row: the supplied values stay and the FK or CHECK rejects the chunk.
    RETURN NEW;
END $$;

CREATE TRIGGER chunks_inherit_owner BEFORE INSERT ON kh.chunks
    FOR EACH ROW EXECUTE FUNCTION kh.chunks_inherit_owner();

CREATE FUNCTION kh.owner_propagate_to_chunks() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = kh, pg_temp
    AS $$
BEGIN
    UPDATE kh.chunks
       SET audience = NEW.audience, labels = NEW.labels, stale = NEW.stale
     WHERE document_id = NEW.id;
    RETURN NULL;
END $$;

CREATE TRIGGER owner_propagate_to_chunks AFTER UPDATE OF audience, labels, stale ON kh.documents
    FOR EACH ROW EXECUTE FUNCTION kh.owner_propagate_to_chunks();

-- ---------------------------------------------------------------------------
-- Embed queue. kh_app has no grant on index_jobs, so this runs as kh_migrator.
-- ---------------------------------------------------------------------------

CREATE FUNCTION kh.records_enqueue_embed() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
BEGIN
    INSERT INTO kh.index_jobs (kind, payload)
    VALUES ('embed_record', jsonb_build_object('id', NEW.id::text));
    RETURN NULL;
END $$;

CREATE TRIGGER records_enqueue_embed AFTER INSERT ON kh.records
    FOR EACH ROW EXECUTE FUNCTION kh.records_enqueue_embed();

REVOKE EXECUTE ON FUNCTION kh.assert_labels_exist(text[]), kh.labels_exist_check(),
    kh.labels_in_use_guard(), kh.chunks_inherit_owner(), kh.owner_propagate_to_chunks(),
    kh.records_enqueue_embed() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.assert_labels_exist(text[]) TO kh_app, kh_indexer;
