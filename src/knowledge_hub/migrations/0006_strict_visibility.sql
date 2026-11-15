-- Strict visibility (plan section 3.8): kh.strict_visibility and the deferred constraint
-- triggers candidate_visibility_matches and conflict_visibility_matches. Runs as kh_migrator
-- (a member of kh_provenance).
--
-- Error convention follows 0005: SQLSTATE P0K04 for both "no sources" and "visibility
-- differs"; visibility is compared as sets.

-- ---------------------------------------------------------------------------
-- kh.strict_visibility(source_ids)
--
-- SECURITY INVOKER: documents, raw_records and derived_records are read through the
-- caller's own RLS only, so a source counts (is resolved) only when kh.visible(...) holds
-- for the caller. There is no provenance branch: a token cannot resolve a raw record it
-- ingested but cannot see. A malformed, missing or hidden id is simply not resolved.
--   n_resolved         number of elements of source_ids that resolved
--   required_audiences union of the resolved sources' audiences (sorted, '{}' when none)
--   allowed_emails     intersection of the resolved sources' non-null email lists
--                      (NULL when every resolved source has none, '{}' when it is empty)
-- ---------------------------------------------------------------------------

CREATE FUNCTION kh.strict_visibility(source_ids text[])
    RETURNS TABLE (n_resolved int, required_audiences text[], allowed_emails text[])
    LANGUAGE sql STABLE SECURITY INVOKER
    SET search_path = kh, pg_temp
    AS $$
WITH ids AS (
    SELECT t.ord,
           split_part(t.s, ':', 1) AS kind,
           CASE WHEN t.s ~ '^(doc|raw|derived):[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'
                THEN substr(t.s, strpos(t.s, ':') + 1)::uuid END AS uid
      FROM unnest($1) WITH ORDINALITY AS t(s, ord)
),
resolved AS (
    SELECT i.ord, d.required_audiences AS req, d.allowed_emails AS emails
      FROM ids i JOIN kh.documents d ON i.kind = 'doc' AND d.id = i.uid
     WHERE kh.visible(d.required_audiences, d.allowed_emails)
    UNION ALL
    SELECT i.ord, r.required_audiences, r.allowed_emails
      FROM ids i JOIN kh.raw_records r ON i.kind = 'raw' AND r.id = i.uid
     WHERE kh.visible(r.required_audiences, r.allowed_emails)
    UNION ALL
    SELECT i.ord, x.required_audiences, x.allowed_emails
      FROM ids i JOIN kh.derived_records x ON i.kind = 'derived' AND x.id = i.uid
     WHERE kh.visible(x.required_audiences, x.allowed_emails)
),
email_lists AS (
    SELECT ord, emails FROM resolved WHERE emails IS NOT NULL
)
SELECT
    (SELECT count(*)::int FROM resolved),
    (SELECT coalesce(array_agg(DISTINCT a ORDER BY a), '{}'::text[])
       FROM resolved, unnest(resolved.req) AS a),
    CASE WHEN NOT EXISTS (SELECT 1 FROM email_lists) THEN NULL::text[]
         ELSE (SELECT coalesce(array_agg(e ORDER BY e), '{}'::text[])
                 FROM (SELECT e
                         FROM email_lists l, unnest(l.emails) AS e
                        GROUP BY e
                       HAVING count(DISTINCT l.ord) = (SELECT count(*) FROM email_lists)) AS common)
    END
$$;

REVOKE EXECUTE ON FUNCTION kh.strict_visibility(text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.strict_visibility(text[]) TO kh_app;

-- ---------------------------------------------------------------------------
-- The trigger functions run as kh_provenance, which needs to read the parent and snapshot
-- rows (as for derived_sources in 0003): column grants on the parents, the whole snapshot
-- tables; the policies are USING (true).
-- ---------------------------------------------------------------------------

GRANT SELECT (id, required_audiences, allowed_emails) ON kh.candidates TO kh_provenance;
CREATE POLICY kh_provenance_select ON kh.candidates FOR SELECT TO kh_provenance USING (true);
GRANT SELECT ON kh.candidate_sources TO kh_provenance;
CREATE POLICY kh_provenance_select ON kh.candidate_sources FOR SELECT TO kh_provenance USING (true);
GRANT SELECT (id, required_audiences, allowed_emails) ON kh.conflicts TO kh_provenance;
CREATE POLICY kh_provenance_select ON kh.conflicts FOR SELECT TO kh_provenance USING (true);
GRANT SELECT ON kh.conflict_items TO kh_provenance;
CREATE POLICY kh_provenance_select ON kh.conflict_items FOR SELECT TO kh_provenance USING (true);

-- ---------------------------------------------------------------------------
-- candidate_visibility_matches / conflict_visibility_matches: at commit, recompute the
-- union and intersection over the snapshot columns of the row's source rows (independent
-- of the caller) and raise when there are no sources or the stored visibility differs.
-- AFTER INSERT only: an indexer UPDATE does not fire them. A row deleted in the same
-- transaction is skipped.
-- ---------------------------------------------------------------------------

CREATE FUNCTION kh.candidate_visibility_matches() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    stored_req    text[];
    stored_emails text[];
    n_sources     int;
    n_email_lists int;
    want_req      text[];
    want_emails   text[];
BEGIN
    SELECT r.required_audiences, r.allowed_emails INTO stored_req, stored_emails
      FROM kh.candidates r WHERE r.id = NEW.id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    SELECT count(*), count(*) FILTER (WHERE s.src_allowed_emails IS NOT NULL)
      INTO n_sources, n_email_lists
      FROM kh.candidate_sources s WHERE s.candidate_id = NEW.id;
    IF n_sources = 0 THEN
        RAISE EXCEPTION 'candidate has no sources' USING ERRCODE = 'P0K04';
    END IF;

    SELECT coalesce(array_agg(DISTINCT a ORDER BY a), '{}'::text[]) INTO want_req
      FROM kh.candidate_sources s, unnest(s.src_required_audiences) AS a
     WHERE s.candidate_id = NEW.id;

    IF n_email_lists = 0 THEN
        want_emails := NULL;
    ELSE
        SELECT coalesce(array_agg(e ORDER BY e), '{}'::text[]) INTO want_emails
          FROM (SELECT e
                  FROM kh.candidate_sources s, unnest(s.src_allowed_emails) AS e
                 WHERE s.candidate_id = NEW.id AND s.src_allowed_emails IS NOT NULL
                 GROUP BY e
                HAVING count(DISTINCT (s.source_kind, s.source_id)) = n_email_lists) AS common;
    END IF;

    IF NOT (stored_req @> want_req AND want_req @> stored_req)
       OR (stored_emails IS NULL) <> (want_emails IS NULL)
       OR (stored_emails IS NOT NULL
           AND NOT (stored_emails @> want_emails AND want_emails @> stored_emails)) THEN
        RAISE EXCEPTION 'candidate visibility does not match its sources'
            USING ERRCODE = 'P0K04';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER candidate_visibility_matches AFTER INSERT ON kh.candidates
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION kh.candidate_visibility_matches();

ALTER FUNCTION kh.candidate_visibility_matches() OWNER TO kh_provenance;
REVOKE EXECUTE ON FUNCTION kh.candidate_visibility_matches() FROM PUBLIC;

CREATE FUNCTION kh.conflict_visibility_matches() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    stored_req    text[];
    stored_emails text[];
    n_sources     int;
    n_email_lists int;
    want_req      text[];
    want_emails   text[];
BEGIN
    SELECT r.required_audiences, r.allowed_emails INTO stored_req, stored_emails
      FROM kh.conflicts r WHERE r.id = NEW.id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    SELECT count(*), count(*) FILTER (WHERE s.src_allowed_emails IS NOT NULL)
      INTO n_sources, n_email_lists
      FROM kh.conflict_items s WHERE s.conflict_id = NEW.id;
    IF n_sources = 0 THEN
        RAISE EXCEPTION 'conflict has no sources' USING ERRCODE = 'P0K04';
    END IF;

    SELECT coalesce(array_agg(DISTINCT a ORDER BY a), '{}'::text[]) INTO want_req
      FROM kh.conflict_items s, unnest(s.src_required_audiences) AS a
     WHERE s.conflict_id = NEW.id;

    IF n_email_lists = 0 THEN
        want_emails := NULL;
    ELSE
        SELECT coalesce(array_agg(e ORDER BY e), '{}'::text[]) INTO want_emails
          FROM (SELECT e
                  FROM kh.conflict_items s, unnest(s.src_allowed_emails) AS e
                 WHERE s.conflict_id = NEW.id AND s.src_allowed_emails IS NOT NULL
                 GROUP BY e
                HAVING count(DISTINCT (s.item_kind, s.item_id)) = n_email_lists) AS common;
    END IF;

    IF NOT (stored_req @> want_req AND want_req @> stored_req)
       OR (stored_emails IS NULL) <> (want_emails IS NULL)
       OR (stored_emails IS NOT NULL
           AND NOT (stored_emails @> want_emails AND want_emails @> stored_emails)) THEN
        RAISE EXCEPTION 'conflict visibility does not match its sources'
            USING ERRCODE = 'P0K04';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER conflict_visibility_matches AFTER INSERT ON kh.conflicts
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION kh.conflict_visibility_matches();

ALTER FUNCTION kh.conflict_visibility_matches() OWNER TO kh_provenance;
REVOKE EXECUTE ON FUNCTION kh.conflict_visibility_matches() FROM PUBLIC;
