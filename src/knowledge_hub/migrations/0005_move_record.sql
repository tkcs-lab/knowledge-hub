-- 0005: move a record between groups in place; tombstones of moved records; row locks in mark_record_outdated.

CREATE TABLE kh.record_moves (
    audience   text NOT NULL,
    source     text NOT NULL,
    source_ref text NOT NULL,
    moved_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (audience, source, source_ref)
);
ALTER TABLE kh.record_moves ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.record_moves FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.record_moves FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

CREATE FUNCTION kh.record_moved(aud text, src text, ref text) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ SELECT kh.can_read(aud)
                 AND EXISTS (SELECT 1 FROM kh.record_moves WHERE audience = aud AND source = src AND source_ref = ref) $$;

REVOKE EXECUTE ON FUNCTION kh.record_moved(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.record_moved(text, text, text) TO kh_app;

CREATE FUNCTION kh.move_record(rec uuid, from_aud text, to_aud text)
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

REVOKE EXECUTE ON FUNCTION kh.move_record(uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.move_record(uuid, text, text) TO kh_app;

-- mark_record_outdated as in 0003, now taking row locks so a concurrent move cannot leave a successor in another group.
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
