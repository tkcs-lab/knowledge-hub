-- 0003: supersede links on documents, outdated marks on records, one definer write path.

ALTER TABLE kh.documents
    ADD COLUMN supersedes    text[] NOT NULL DEFAULT '{}',
    ADD COLUMN superseded_by uuid REFERENCES kh.documents (id) ON DELETE SET NULL;
CREATE INDEX documents_superseded_idx ON kh.documents (superseded_by) WHERE superseded_by IS NOT NULL;

ALTER TABLE kh.records
    ADD COLUMN outdated      boolean NOT NULL DEFAULT false,
    ADD COLUMN outdated_at   timestamptz,
    ADD COLUMN outdated_by   text NOT NULL DEFAULT '',
    ADD COLUMN outdated_note text NOT NULL DEFAULT '',
    ADD COLUMN replaced_by   text NOT NULL DEFAULT '',
    ADD CONSTRAINT records_outdated_fields CHECK (
        outdated OR (outdated_at IS NULL AND outdated_by = '' AND outdated_note = '' AND replaced_by = ''));
CREATE INDEX records_outdated_idx ON kh.records (id) WHERE outdated;

-- Files already in the repos may carry a supersedes line that 0001 ignored. An empty blob_sha makes
-- the next sync read every file once; chunks are matched by content_sha, so nothing is embedded again.
UPDATE kh.documents SET blob_sha = '';

CREATE FUNCTION kh.mark_record_outdated(rec uuid, flag boolean, successor text, note text)
    RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    rec_aud  text;
    succ_aud text;
    who      text := kh.caller_sub();
BEGIN
    SELECT audience INTO rec_aud FROM kh.records WHERE id = rec;
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
            SELECT audience INTO succ_aud FROM kh.records WHERE id = substr(successor, 5)::uuid;
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

REVOKE EXECUTE ON FUNCTION kh.mark_record_outdated(uuid, boolean, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.mark_record_outdated(uuid, boolean, text, text) TO kh_app;
