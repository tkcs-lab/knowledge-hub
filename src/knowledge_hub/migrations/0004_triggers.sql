-- Triggers (plan sections 3.3 and 3.7): chunk inheritance, embed queue, raw visibility and
-- source snapshots. Runs as kh_migrator (a member of kh_provenance, which has CREATE on kh).

-- ---------------------------------------------------------------------------
-- Chunk consistency. SECURITY INVOKER: the writer is kh_indexer (or kh_migrator), which can
-- read every owner row.
-- ---------------------------------------------------------------------------

CREATE FUNCTION kh.chunks_inherit_owner() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = kh, pg_temp
    AS $$
BEGIN
    IF NEW.document_id IS NOT NULL THEN
        SELECT 'doc:' || d.id, d.tier, d.domain, d.status, d.stale, d.status = 'superseded',
               d.entities, d.indexed_at, d.required_audiences, d.allowed_emails
          INTO NEW.owner_key, NEW.tier, NEW.domain, NEW.status, NEW.stale, NEW.superseded,
               NEW.entities, NEW.owner_created_at, NEW.required_audiences, NEW.allowed_emails
          FROM kh.documents d WHERE d.id = NEW.document_id;
    ELSIF NEW.raw_id IS NOT NULL THEN
        SELECT 'raw:' || r.id, 'T3', r.domain, 'current', false, false,
               r.entities, r.ingested_at, r.required_audiences, r.allowed_emails
          INTO NEW.owner_key, NEW.tier, NEW.domain, NEW.status, NEW.stale, NEW.superseded,
               NEW.entities, NEW.owner_created_at, NEW.required_audiences, NEW.allowed_emails
          FROM kh.raw_records r WHERE r.id = NEW.raw_id;
    ELSIF NEW.derived_id IS NOT NULL THEN
        SELECT 'derived:' || r.id, 'T2', r.domain, 'current', false, false,
               r.entities, r.created_at, r.required_audiences, r.allowed_emails
          INTO NEW.owner_key, NEW.tier, NEW.domain, NEW.status, NEW.stale, NEW.superseded,
               NEW.entities, NEW.owner_created_at, NEW.required_audiences, NEW.allowed_emails
          FROM kh.derived_records r WHERE r.id = NEW.derived_id;
    END IF;
    -- No owner row (or no owner column): leave the supplied values; FK and CHECK reject it.
    RETURN NEW;
END $$;

CREATE TRIGGER chunks_inherit_owner BEFORE INSERT OR UPDATE ON kh.chunks
    FOR EACH ROW EXECUTE FUNCTION kh.chunks_inherit_owner();

-- Pushes owner changes down: a no-op UPDATE of the chunks fires chunks_inherit_owner,
-- which re-reads the (already updated) owner row.
CREATE FUNCTION kh.owner_propagate_to_chunks() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = kh, pg_temp
    AS $$
BEGIN
    IF TG_TABLE_NAME = 'documents' THEN
        UPDATE kh.chunks SET tier = tier WHERE document_id = NEW.id;
    ELSIF TG_TABLE_NAME = 'raw_records' THEN
        UPDATE kh.chunks SET tier = tier WHERE raw_id = NEW.id;
    ELSE
        UPDATE kh.chunks SET tier = tier WHERE derived_id = NEW.id;
    END IF;
    RETURN NULL;
END $$;

CREATE TRIGGER owner_propagate_to_chunks AFTER UPDATE ON kh.documents
    FOR EACH ROW EXECUTE FUNCTION kh.owner_propagate_to_chunks();
CREATE TRIGGER owner_propagate_to_chunks AFTER UPDATE ON kh.raw_records
    FOR EACH ROW EXECUTE FUNCTION kh.owner_propagate_to_chunks();
CREATE TRIGGER owner_propagate_to_chunks AFTER UPDATE ON kh.derived_records
    FOR EACH ROW EXECUTE FUNCTION kh.owner_propagate_to_chunks();

-- ---------------------------------------------------------------------------
-- Embed queue. kh_app has no grant on index_jobs, so these run as kh_migrator.
-- ---------------------------------------------------------------------------

CREATE FUNCTION kh.raw_records_enqueue_embed() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
BEGIN
    INSERT INTO kh.index_jobs (kind, payload)
    VALUES ('embed_raw', jsonb_build_object('id', NEW.id::text));
    RETURN NULL;
END $$;

CREATE FUNCTION kh.derived_records_enqueue_embed() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
BEGIN
    INSERT INTO kh.index_jobs (kind, payload)
    VALUES ('embed_derived', jsonb_build_object('id', NEW.id::text));
    RETURN NULL;
END $$;

CREATE TRIGGER raw_records_enqueue_embed AFTER INSERT ON kh.raw_records
    FOR EACH ROW EXECUTE FUNCTION kh.raw_records_enqueue_embed();
CREATE TRIGGER derived_records_enqueue_embed AFTER INSERT ON kh.derived_records
    FOR EACH ROW EXECUTE FUNCTION kh.derived_records_enqueue_embed();

-- ---------------------------------------------------------------------------
-- Raw visibility (3.7), owned by kh_provenance.
-- ---------------------------------------------------------------------------

CREATE FUNCTION kh.raw_records_assign_visibility() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    mapped text[];
BEGIN
    SELECT array_agg(DISTINCT a ORDER BY a) INTO mapped
      FROM kh.raw_audience_map m, unnest(m.audiences) AS a
     WHERE (m.kind = 'series' AND m.key = NEW.series)
        OR (m.kind = 'folder' AND m.key = NEW.folder);
    IF mapped IS NOT NULL THEN
        IF NOT (mapped OPERATOR(pg_catalog.<@) kh.caller_audiences()) THEN
            RAISE EXCEPTION 'ingest audience not held by caller' USING ERRCODE = 'P0K02';
        END IF;
        NEW.required_audiences := mapped;
        NEW.allowed_emails := NULL;
    ELSIF coalesce(cardinality(NEW.participants), 0) > 0 THEN
        NEW.required_audiences := '{}';
        NEW.allowed_emails := (SELECT array_agg(DISTINCT lower(p) ORDER BY lower(p))
                                 FROM unnest(NEW.participants) AS p);
    ELSE
        RAISE EXCEPTION 'no audience mapping and no participants' USING ERRCODE = 'P0K01';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER raw_records_assign_visibility BEFORE INSERT ON kh.raw_records
    FOR EACH ROW EXECUTE FUNCTION kh.raw_records_assign_visibility();

-- ---------------------------------------------------------------------------
-- Source snapshots (3.3), owned by kh_provenance. One function for the three source
-- tables: the kind column is source_kind (derived_sources, candidate_sources) or
-- item_kind (conflict_items), read through to_jsonb(NEW).
-- ---------------------------------------------------------------------------

CREATE FUNCTION kh.sources_snapshot_visibility() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    j jsonb := to_jsonb(NEW);
    k text := coalesce(j ->> 'source_kind', j ->> 'item_kind');
    i text := coalesce(j ->> 'source_id', j ->> 'item_id');
    found boolean := false;
BEGIN
    BEGIN
        IF k = 'doc' THEN
            SELECT true, d.required_audiences, d.allowed_emails
              INTO found, NEW.src_required_audiences, NEW.src_allowed_emails
              FROM kh.documents d WHERE d.id = i::uuid;
        ELSIF k = 'raw' THEN
            SELECT true, r.required_audiences, r.allowed_emails
              INTO found, NEW.src_required_audiences, NEW.src_allowed_emails
              FROM kh.raw_records r WHERE r.id = i::uuid;
        ELSIF k = 'derived' THEN
            SELECT true, r.required_audiences, r.allowed_emails
              INTO found, NEW.src_required_audiences, NEW.src_allowed_emails
              FROM kh.derived_records r WHERE r.id = i::uuid;
        END IF;
    EXCEPTION WHEN invalid_text_representation THEN
        found := false;
    END;
    IF NOT coalesce(found, false) THEN
        RAISE EXCEPTION 'source not found' USING ERRCODE = 'P0K03';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER sources_snapshot_visibility BEFORE INSERT ON kh.derived_sources
    FOR EACH ROW EXECUTE FUNCTION kh.sources_snapshot_visibility();
CREATE TRIGGER sources_snapshot_visibility BEFORE INSERT ON kh.candidate_sources
    FOR EACH ROW EXECUTE FUNCTION kh.sources_snapshot_visibility();
CREATE TRIGGER sources_snapshot_visibility BEFORE INSERT ON kh.conflict_items
    FOR EACH ROW EXECUTE FUNCTION kh.sources_snapshot_visibility();

ALTER FUNCTION kh.raw_records_assign_visibility() OWNER TO kh_provenance;
ALTER FUNCTION kh.sources_snapshot_visibility() OWNER TO kh_provenance;

REVOKE EXECUTE ON FUNCTION kh.chunks_inherit_owner(), kh.owner_propagate_to_chunks(),
    kh.raw_records_enqueue_embed(), kh.derived_records_enqueue_embed(),
    kh.raw_records_assign_visibility(), kh.sources_snapshot_visibility() FROM PUBLIC;
