-- Any-of audiences (plan 15 section 3): each element of required_audiences is a clause,
-- the audiences joined by '|' in C order; a row is visible when every clause shares an
-- audience with the caller. Plain names are one-member clauses. Runs as kh_migrator.

-- 1. Clause helpers.
CREATE FUNCTION kh.audience_clause(audiences text[]) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$ SELECT pg_catalog.string_agg(DISTINCT a COLLATE "C", '|' ORDER BY a COLLATE "C")
            FROM pg_catalog.unnest($1) AS t(a) $$;

CREATE FUNCTION kh.audience_clauses_ok(clauses text[]) RETURNS boolean
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$ SELECT NOT EXISTS (
              SELECT 1 FROM pg_catalog.unnest($1) AS t(c)
               WHERE c IS NULL
                  OR c !~ '^[a-z0-9][a-z0-9_-]{0,62}(\|[a-z0-9][a-z0-9_-]{0,62})*$'
                  OR c <> kh.audience_clause(pg_catalog.string_to_array(c, '|'))) $$;

REVOKE EXECUTE ON FUNCTION kh.audience_clause(text[]), kh.audience_clauses_ok(text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.audience_clause(text[]), kh.audience_clauses_ok(text[])
    TO kh_app, kh_indexer, kh_provenance;

-- 2. kh.visible. The sub-select lives in a helper only rows with a '|' clause call, so the
-- outer body stays inlinable.
CREATE FUNCTION kh.audience_clauses_held(req text[]) RETURNS boolean
    LANGUAGE sql STABLE
    AS $$ SELECT NOT EXISTS (SELECT 1 FROM pg_catalog.unnest($1) AS t(c)
                              WHERE NOT (pg_catalog.string_to_array(t.c, '|')
                                         OPERATOR(pg_catalog.&&) kh.caller_audiences())) $$;

CREATE OR REPLACE FUNCTION kh.visible(req text[], emails text[]) RETURNS boolean
    LANGUAGE sql STABLE
    AS $$ SELECT coalesce(
              CASE WHEN pg_catalog.strpos(pg_catalog.array_to_string($1, ','), '|') = 0
                   THEN $1 OPERATOR(pg_catalog.<@) kh.caller_audiences()
                   ELSE kh.audience_clauses_held($1) END
              AND ($2 IS NULL OR kh.caller_email() = ANY ($2)), false) $$;

REVOKE EXECUTE ON FUNCTION kh.audience_clauses_held(text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.audience_clauses_held(text[]) TO kh_app, kh_provenance;

-- 3. Raw visibility trigger: one clause per matching map entry.
CREATE OR REPLACE FUNCTION kh.raw_records_assign_visibility() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    mapped  text[];
    members text[];
BEGIN
    SELECT array_agg(c ORDER BY c COLLATE "C") INTO mapped
      FROM (SELECT DISTINCT kh.audience_clause(m.audiences) AS c
              FROM kh.raw_audience_map m
             WHERE cardinality(m.audiences) > 0
               AND ((m.kind = 'series' AND m.key = NEW.series)
                 OR (m.kind = 'folder' AND m.key = NEW.folder))) s;
    IF mapped IS NOT NULL THEN
        SELECT array_agg(DISTINCT a) INTO members
          FROM kh.raw_audience_map m, unnest(m.audiences) AS a
         WHERE (m.kind = 'series' AND m.key = NEW.series)
            OR (m.kind = 'folder' AND m.key = NEW.folder);
        IF NOT (members OPERATOR(pg_catalog.<@) kh.caller_audiences()) THEN
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

ALTER FUNCTION kh.raw_records_assign_visibility() OWNER TO kh_provenance;
REVOKE EXECUTE ON FUNCTION kh.raw_records_assign_visibility() FROM PUBLIC;

-- 4. Entities: the whole configured list was written as the row's audiences.
UPDATE kh.entities_cache
   SET required_audiences = ARRAY[kh.audience_clause(required_audiences)]
 WHERE cardinality(required_audiences) > 1;

-- 5. Raw rows (the statement of jobs/recompute.py _recompute_raw; chunks follow by trigger).
WITH new AS (
    SELECT r.id,
           (SELECT array_agg(c ORDER BY c COLLATE "C")
              FROM (SELECT DISTINCT kh.audience_clause(m.audiences) AS c
                      FROM kh.raw_audience_map m
                     WHERE cardinality(m.audiences) > 0
                       AND ((m.kind = 'series' AND m.key = r.series)
                         OR (m.kind = 'folder' AND m.key = r.folder))) s) AS mapped,
           (SELECT array_agg(DISTINCT lower(p) ORDER BY lower(p))
              FROM unnest(r.participants) AS p) AS people
      FROM kh.raw_records r
), target AS (
    SELECT id,
           CASE WHEN mapped IS NOT NULL THEN mapped ELSE '{}'::text[] END AS req,
           CASE WHEN mapped IS NOT NULL THEN NULL::text[] ELSE people END AS emails
      FROM new WHERE mapped IS NOT NULL OR people IS NOT NULL
)
UPDATE kh.raw_records r SET required_audiences = t.req, allowed_emails = t.emails
  FROM target t
 WHERE r.id = t.id
   AND (r.required_audiences, r.allowed_emails) IS DISTINCT FROM (t.req, t.emails);

-- 6. Derived rows: refresh the snapshots from live rows, then set each row to the union over
-- its snapshots, until nothing changes. The round limit is only a guard.
DO $mig$
DECLARE
    changed bigint;
    round   int := 0;
BEGIN
    LOOP
        round := round + 1;
        IF round > 1000 THEN
            RAISE EXCEPTION 'derived visibility did not converge';
        END IF;
        UPDATE kh.derived_sources s SET src_required_audiences = l.req, src_allowed_emails = l.emails
      FROM (SELECT 'doc' AS kind, id::text AS id, required_audiences AS req, allowed_emails AS emails
              FROM kh.documents
            UNION ALL
            SELECT 'raw', id::text, required_audiences, allowed_emails FROM kh.raw_records
            UNION ALL
            SELECT 'derived', id::text, required_audiences, allowed_emails FROM kh.derived_records) l
     WHERE l.kind = s.source_kind AND l.id = s.source_id
       AND (s.src_required_audiences, s.src_allowed_emails) IS DISTINCT FROM (l.req, l.emails);
        WITH src AS (
            SELECT derived_id AS pid, src_required_audiences AS req, src_allowed_emails AS emails,
                   row_number() OVER (PARTITION BY derived_id ORDER BY source_kind, source_id) AS rn
              FROM kh.derived_sources
        ), lists AS (
            SELECT pid, rn, emails FROM src WHERE emails IS NOT NULL
        ), common AS (
            SELECT l.pid, e.email
              FROM lists l, LATERAL (SELECT DISTINCT x AS email FROM unnest(l.emails) AS x) e
             GROUP BY l.pid, e.email
            HAVING count(*) = (SELECT count(*) FROM lists n WHERE n.pid = l.pid)
        ), agg AS (
            SELECT p.pid,
                   (SELECT coalesce(array_agg(DISTINCT a ORDER BY a), '{}'::text[])
                      FROM src s, unnest(s.req) AS a WHERE s.pid = p.pid) AS req,
                   CASE WHEN NOT EXISTS (SELECT 1 FROM lists l WHERE l.pid = p.pid)
                        THEN NULL::text[]
                        ELSE (SELECT coalesce(array_agg(c.email ORDER BY c.email), '{}'::text[])
                                FROM common c WHERE c.pid = p.pid)
                   END AS emails
              FROM (SELECT DISTINCT pid FROM src) p
        )
        UPDATE kh.derived_records t SET required_audiences = a.req, allowed_emails = a.emails
          FROM agg a
         WHERE t.id = a.pid AND (t.required_audiences, t.allowed_emails) IS DISTINCT FROM (a.req, a.emails);
        GET DIAGNOSTICS changed = ROW_COUNT;
        EXIT WHEN changed = 0;
    END LOOP;
END $mig$;

-- 7. Candidates, then conflicts: one refresh-and-apply each (they are never sources).
UPDATE kh.candidate_sources s SET src_required_audiences = l.req, src_allowed_emails = l.emails
      FROM (SELECT 'doc' AS kind, id::text AS id, required_audiences AS req, allowed_emails AS emails
              FROM kh.documents
            UNION ALL
            SELECT 'raw', id::text, required_audiences, allowed_emails FROM kh.raw_records
            UNION ALL
            SELECT 'derived', id::text, required_audiences, allowed_emails FROM kh.derived_records) l
     WHERE l.kind = s.source_kind AND l.id = s.source_id
       AND (s.src_required_audiences, s.src_allowed_emails) IS DISTINCT FROM (l.req, l.emails);
WITH src AS (
            SELECT candidate_id AS pid, src_required_audiences AS req, src_allowed_emails AS emails,
                   row_number() OVER (PARTITION BY candidate_id ORDER BY source_kind, source_id) AS rn
              FROM kh.candidate_sources
        ), lists AS (
            SELECT pid, rn, emails FROM src WHERE emails IS NOT NULL
        ), common AS (
            SELECT l.pid, e.email
              FROM lists l, LATERAL (SELECT DISTINCT x AS email FROM unnest(l.emails) AS x) e
             GROUP BY l.pid, e.email
            HAVING count(*) = (SELECT count(*) FROM lists n WHERE n.pid = l.pid)
        ), agg AS (
            SELECT p.pid,
                   (SELECT coalesce(array_agg(DISTINCT a ORDER BY a), '{}'::text[])
                      FROM src s, unnest(s.req) AS a WHERE s.pid = p.pid) AS req,
                   CASE WHEN NOT EXISTS (SELECT 1 FROM lists l WHERE l.pid = p.pid)
                        THEN NULL::text[]
                        ELSE (SELECT coalesce(array_agg(c.email ORDER BY c.email), '{}'::text[])
                                FROM common c WHERE c.pid = p.pid)
                   END AS emails
              FROM (SELECT DISTINCT pid FROM src) p
        )
        UPDATE kh.candidates t SET required_audiences = a.req, allowed_emails = a.emails
          FROM agg a
         WHERE t.id = a.pid AND (t.required_audiences, t.allowed_emails) IS DISTINCT FROM (a.req, a.emails);

UPDATE kh.conflict_items s SET src_required_audiences = l.req, src_allowed_emails = l.emails
      FROM (SELECT 'doc' AS kind, id::text AS id, required_audiences AS req, allowed_emails AS emails
              FROM kh.documents
            UNION ALL
            SELECT 'raw', id::text, required_audiences, allowed_emails FROM kh.raw_records
            UNION ALL
            SELECT 'derived', id::text, required_audiences, allowed_emails FROM kh.derived_records) l
     WHERE l.kind = s.item_kind AND l.id = s.item_id
       AND (s.src_required_audiences, s.src_allowed_emails) IS DISTINCT FROM (l.req, l.emails);
WITH src AS (
            SELECT conflict_id AS pid, src_required_audiences AS req, src_allowed_emails AS emails,
                   row_number() OVER (PARTITION BY conflict_id ORDER BY item_kind, item_id) AS rn
              FROM kh.conflict_items
        ), lists AS (
            SELECT pid, rn, emails FROM src WHERE emails IS NOT NULL
        ), common AS (
            SELECT l.pid, e.email
              FROM lists l, LATERAL (SELECT DISTINCT x AS email FROM unnest(l.emails) AS x) e
             GROUP BY l.pid, e.email
            HAVING count(*) = (SELECT count(*) FROM lists n WHERE n.pid = l.pid)
        ), agg AS (
            SELECT p.pid,
                   (SELECT coalesce(array_agg(DISTINCT a ORDER BY a), '{}'::text[])
                      FROM src s, unnest(s.req) AS a WHERE s.pid = p.pid) AS req,
                   CASE WHEN NOT EXISTS (SELECT 1 FROM lists l WHERE l.pid = p.pid)
                        THEN NULL::text[]
                        ELSE (SELECT coalesce(array_agg(c.email ORDER BY c.email), '{}'::text[])
                                FROM common c WHERE c.pid = p.pid)
                   END AS emails
              FROM (SELECT DISTINCT pid FROM src) p
        )
        UPDATE kh.conflicts t SET required_audiences = a.req, allowed_emails = a.emails
          FROM agg a
         WHERE t.id = a.pid AND (t.required_audiences, t.allowed_emails) IS DISTINCT FROM (a.req, a.emails);

-- 8. Clause checks, validated against the converted rows.
ALTER TABLE kh.documents ADD CONSTRAINT kh_audience_clauses CHECK (kh.audience_clauses_ok(required_audiences));
ALTER TABLE kh.chunks ADD CONSTRAINT kh_audience_clauses CHECK (kh.audience_clauses_ok(required_audiences));
ALTER TABLE kh.raw_records ADD CONSTRAINT kh_audience_clauses CHECK (kh.audience_clauses_ok(required_audiences));
ALTER TABLE kh.derived_records ADD CONSTRAINT kh_audience_clauses CHECK (kh.audience_clauses_ok(required_audiences));
ALTER TABLE kh.candidates ADD CONSTRAINT kh_audience_clauses CHECK (kh.audience_clauses_ok(required_audiences));
ALTER TABLE kh.conflicts ADD CONSTRAINT kh_audience_clauses CHECK (kh.audience_clauses_ok(required_audiences));
ALTER TABLE kh.entities_cache ADD CONSTRAINT kh_audience_clauses CHECK (kh.audience_clauses_ok(required_audiences));
ALTER TABLE kh.derived_sources ADD CONSTRAINT kh_src_audience_clauses CHECK (kh.audience_clauses_ok(src_required_audiences));
ALTER TABLE kh.candidate_sources ADD CONSTRAINT kh_src_audience_clauses CHECK (kh.audience_clauses_ok(src_required_audiences));
ALTER TABLE kh.conflict_items ADD CONSTRAINT kh_src_audience_clauses CHECK (kh.audience_clauses_ok(src_required_audiences));
