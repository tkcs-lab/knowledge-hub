-- 0008: removing T3 records: outdated_reason, delete_records, restore_records and the purge.

ALTER TABLE kh.records
    ADD COLUMN outdated_reason text NOT NULL DEFAULT '',
    ADD CONSTRAINT records_outdated_reason CHECK (outdated_reason IN ('', 'delete', 'purge')),
    ADD CONSTRAINT records_reason_needs_outdated CHECK (outdated_reason = '' OR outdated),
    ADD CONSTRAINT records_reason_not_row CHECK (dataset_row = '' OR outdated_reason = '');
CREATE INDEX records_removed_idx ON kh.records (outdated_at) WHERE outdated_reason <> '';

CREATE FUNCTION kh.delete_records(aud text, src text, refs text[], by_prefix boolean, force boolean)
    RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    who    text := kh.caller_sub();
    ids    uuid[];
    total  bigint;
    recent bigint;
    lim    bigint;
BEGIN
    IF who IS NULL OR NOT kh.can_read(aud) THEN
        RETURN NULL;                       -- hidden and anonymous answer the same
    END IF;
    IF left(src, 8) = 'dataset:' THEN
        RAISE EXCEPTION 'record mirrors a dataset row' USING ERRCODE = 'P0K0B';
    END IF;
    WITH hit AS (
        SELECT r.id FROM kh.records r
         WHERE r.audience = aud AND r.source = src AND r.dataset_row = '' AND r.outdated_reason = ''
           AND CASE WHEN by_prefix
                    THEN EXISTS (SELECT 1 FROM unnest(refs) p WHERE starts_with(r.source_ref, p))
                    ELSE r.source_ref = ANY (refs) END
         ORDER BY r.id
           FOR NO KEY UPDATE OF r)
    SELECT coalesce(array_agg(id), '{}') INTO ids FROM hit;
    IF cardinality(ids) > 500 THEN
        RAISE EXCEPTION 'more than 500 records' USING ERRCODE = 'P0K0E';
    END IF;
    IF cardinality(ids) = 0 THEN
        RETURN 0;
    END IF;
    IF NOT force THEN
        SELECT count(*) INTO total FROM kh.records
         WHERE audience = aud AND source = src AND dataset_row = '' AND NOT outdated;
        SELECT count(*) INTO recent FROM kh.records
         WHERE audience = aud AND source = src AND outdated_reason = 'delete'
           AND outdated_at > now() - interval '24 hours';
        lim := greatest(50, (total + recent) / 5);
        IF recent + cardinality(ids) > lim THEN
            RAISE EXCEPTION 'shrink % %', recent + cardinality(ids), lim USING ERRCODE = 'P0K10';
        END IF;
    END IF;
    UPDATE kh.records
       SET outdated = true, outdated_at = now(), outdated_by = who,
           outdated_note = 'removed from source', outdated_reason = 'delete'
     WHERE id = ANY (ids);
    RETURN cardinality(ids);
END $$;

CREATE FUNCTION kh.restore_records(aud text, src text, refs text[], by_prefix boolean, since timestamptz)
    RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    ids uuid[];
BEGIN
    IF kh.caller_sub() IS NULL OR NOT kh.can_read(aud) THEN
        RETURN NULL;
    END IF;
    WITH hit AS (
        SELECT r.id FROM kh.records r
         WHERE r.audience = aud AND r.source = src AND r.dataset_row = '' AND r.outdated_reason = 'delete'
           AND (since IS NULL OR r.outdated_at >= since)
           AND (refs IS NULL OR CASE WHEN by_prefix
                    THEN EXISTS (SELECT 1 FROM unnest(refs) p WHERE starts_with(r.source_ref, p))
                    ELSE r.source_ref = ANY (refs) END)
         ORDER BY r.id
           FOR NO KEY UPDATE OF r)
    SELECT coalesce(array_agg(id), '{}') INTO ids FROM hit;
    UPDATE kh.records
       SET outdated = false, outdated_at = NULL, outdated_by = '', outdated_note = '',
           outdated_reason = '', replaced_by = ''
     WHERE id = ANY (ids);
    RETURN cardinality(ids);
END $$;

CREATE FUNCTION kh.records_purge_claim(days integer, lim integer, skip uuid[])
    RETURNS TABLE (rec_id uuid, rec_blob_key text)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
BEGIN
    IF days < 1 OR lim < 1 OR lim > 1000 THEN
        RAISE EXCEPTION 'bad purge arguments' USING ERRCODE = 'P0K11';
    END IF;
    RETURN QUERY
    WITH due AS (
        SELECT r.id FROM kh.records r
         WHERE r.dataset_row = '' AND r.id <> ALL (skip)
           AND (r.outdated_reason = 'purge'
                OR (r.outdated_reason = 'delete' AND r.outdated_at < now() - make_interval(days => days)))
         ORDER BY r.outdated_at, r.id
         LIMIT lim
           FOR UPDATE SKIP LOCKED),
    upd AS (
        UPDATE kh.records r SET outdated_reason = 'purge' FROM due WHERE r.id = due.id
        RETURNING r.id, r.blob_key)
    SELECT upd.id, upd.blob_key FROM upd;
END $$;

CREATE FUNCTION kh.records_purge_delete(ids uuid[]) RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    rid  uuid;
    gone uuid[];
    n    integer;
BEGIN
    FOR rid IN SELECT x FROM unnest(ids) AS x ORDER BY x LOOP
        PERFORM pg_advisory_xact_lock(hashtext('rec:' || rid::text));
    END LOOP;
    gone := ARRAY(SELECT r.id FROM kh.records r
                   WHERE r.id = ANY (ids) AND r.outdated_reason = 'purge' AND r.dataset_row = ''
                   ORDER BY r.id);
    DELETE FROM kh.index_jobs
     WHERE kind = 'embed_record' AND state IN ('queued', 'failed') AND payload ->> 'id' = ANY (gone::text[]);
    UPDATE kh.records SET replaced_by = ''
     WHERE replaced_by = ANY (ARRAY(SELECT 'rec:' || g::text FROM unnest(gone) AS g));
    DELETE FROM kh.records WHERE id = ANY (gone);
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;

-- Redefined as in 0007, plus the refusals of removed and purging records.

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
    IF EXISTS (SELECT 1 FROM kh.records WHERE id = rec AND outdated_reason <> '') THEN
        RAISE EXCEPTION 'record was removed from its source' USING ERRCODE = 'P0K12';
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
    IF EXISTS (SELECT 1 FROM kh.records WHERE id = rec AND outdated_reason = 'purge') THEN
        RAISE EXCEPTION 'record is being purged' USING ERRCODE = 'P0K0F';
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
           replaced_by   = CASE WHEN flag THEN successor ELSE '' END,
           outdated_reason = ''
     WHERE id = rec;
    RETURN true;
END $$;

REVOKE EXECUTE ON FUNCTION kh.delete_records(text, text, text[], boolean, boolean) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION kh.restore_records(text, text, text[], boolean, timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION kh.records_purge_claim(integer, integer, uuid[]) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION kh.records_purge_delete(uuid[]) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION kh.move_record(uuid, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION kh.mark_record_outdated(uuid, boolean, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.delete_records(text, text, text[], boolean, boolean) TO kh_app;
GRANT EXECUTE ON FUNCTION kh.restore_records(text, text, text[], boolean, timestamptz) TO kh_app;
GRANT EXECUTE ON FUNCTION kh.records_purge_claim(integer, integer, uuid[]) TO kh_indexer;
GRANT EXECUTE ON FUNCTION kh.records_purge_delete(uuid[]) TO kh_indexer;
GRANT EXECUTE ON FUNCTION kh.move_record(uuid, text, text) TO kh_app;
GRANT EXECUTE ON FUNCTION kh.mark_record_outdated(uuid, boolean, text, text) TO kh_app;
