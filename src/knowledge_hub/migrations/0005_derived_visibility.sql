-- Derived visibility (plan section 3.7): kh.derived_visibility and the deferred constraint
-- trigger derived_visibility_matches. Runs as kh_migrator (a member of kh_provenance).
--
-- Both functions are SECURITY DEFINER and owned by kh_provenance, which reads only the
-- columns granted to it in 0003 (documents: id, required_audiences, allowed_emails,
-- commit_sha; raw_records: id, required_audiences, allowed_emails, ingested_by_sub;
-- derived_records: id, required_audiences, allowed_emails, created_by_sub; derived_sources)
-- through policies that are USING (true). Visibility is applied by the function body alone.

-- ---------------------------------------------------------------------------
-- kh.derived_visibility(source_ids)
--
-- source_ids are the prefixed forms doc:<uuid>, raw:<uuid> and derived:<uuid>. A source
-- counts (is resolved) when kh.visible(...) holds for the caller, or, for raw_records only,
-- when the caller is the n8n workflow token that ingested it (the provenance exception).
-- A malformed, missing or hidden id is simply not resolved, so missing and hidden ids give
-- identical results. Returns one row:
--   n_resolved         number of elements of source_ids that resolved
--   required_audiences union of the resolved sources' audiences (sorted, '{}' when none)
--   allowed_emails     intersection of the resolved sources' non-null email lists
--                      (NULL when every resolved source has none, '{}' when it is empty)
--   versions           versions[i] belongs to source_ids[i]: commit_sha for a resolved
--                      document, NULL for anything else
-- It returns no content.
-- ---------------------------------------------------------------------------

CREATE FUNCTION kh.derived_visibility(source_ids text[])
    RETURNS TABLE (n_resolved int, required_audiences text[], allowed_emails text[],
                   versions text[])
    LANGUAGE sql STABLE SECURITY DEFINER
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
    SELECT i.ord, d.required_audiences AS req, d.allowed_emails AS emails, d.commit_sha AS version
      FROM ids i JOIN kh.documents d ON i.kind = 'doc' AND d.id = i.uid
     WHERE kh.visible(d.required_audiences, d.allowed_emails)
    UNION ALL
    SELECT i.ord, r.required_audiences, r.allowed_emails, NULL::text
      FROM ids i JOIN kh.raw_records r ON i.kind = 'raw' AND r.id = i.uid
     WHERE kh.visible(r.required_audiences, r.allowed_emails)
        OR coalesce(kh.caller_type() = 'n8n_workflow'
                    AND r.ingested_by_sub = kh.caller_sub(), false)
    UNION ALL
    SELECT i.ord, x.required_audiences, x.allowed_emails, NULL::text
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
    END,
    (SELECT coalesce(array_agg(r.version ORDER BY i.ord), '{}'::text[])
       FROM ids i LEFT JOIN resolved r ON r.ord = i.ord)
$$;

ALTER FUNCTION kh.derived_visibility(text[]) OWNER TO kh_provenance;
REVOKE EXECUTE ON FUNCTION kh.derived_visibility(text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.derived_visibility(text[]) TO kh_app;

-- ---------------------------------------------------------------------------
-- derived_visibility_matches: at commit, recompute the union of audiences and the
-- intersection of the non-null email lists over the snapshot columns of the row's
-- derived_sources (independent of the caller) and raise when the row has no sources or
-- its stored visibility differs (compared as sets). AFTER INSERT only: the indexer's
-- recompute is an UPDATE and does not fire it. The stored visibility is re-read at commit;
-- a row deleted in the same transaction is skipped.
-- ---------------------------------------------------------------------------

CREATE FUNCTION kh.derived_visibility_matches() RETURNS trigger
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
      FROM kh.derived_records r WHERE r.id = NEW.id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    SELECT count(*), count(*) FILTER (WHERE s.src_allowed_emails IS NOT NULL)
      INTO n_sources, n_email_lists
      FROM kh.derived_sources s WHERE s.derived_id = NEW.id;
    IF n_sources = 0 THEN
        RAISE EXCEPTION 'derived record has no sources' USING ERRCODE = 'P0K04';
    END IF;

    SELECT coalesce(array_agg(DISTINCT a ORDER BY a), '{}'::text[]) INTO want_req
      FROM kh.derived_sources s, unnest(s.src_required_audiences) AS a
     WHERE s.derived_id = NEW.id;

    IF n_email_lists = 0 THEN
        want_emails := NULL;
    ELSE
        SELECT coalesce(array_agg(e ORDER BY e), '{}'::text[]) INTO want_emails
          FROM (SELECT e
                  FROM kh.derived_sources s, unnest(s.src_allowed_emails) AS e
                 WHERE s.derived_id = NEW.id AND s.src_allowed_emails IS NOT NULL
                 GROUP BY e
                HAVING count(DISTINCT (s.source_kind, s.source_id)) = n_email_lists) AS common;
    END IF;

    IF NOT (stored_req @> want_req AND want_req @> stored_req)
       OR (stored_emails IS NULL) <> (want_emails IS NULL)
       OR (stored_emails IS NOT NULL
           AND NOT (stored_emails @> want_emails AND want_emails @> stored_emails)) THEN
        RAISE EXCEPTION 'derived record visibility does not match its sources'
            USING ERRCODE = 'P0K04';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER derived_visibility_matches AFTER INSERT ON kh.derived_records
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION kh.derived_visibility_matches();

ALTER FUNCTION kh.derived_visibility_matches() OWNER TO kh_provenance;
REVOKE EXECUTE ON FUNCTION kh.derived_visibility_matches() FROM PUBLIC;
