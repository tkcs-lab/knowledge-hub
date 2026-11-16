-- Agent writes (plan 16 section 3.8): kh.proposals, its definer functions and the
-- agent_written marker on documents and chunks. Runs as kh_migrator.

-- 1. The table.
CREATE TABLE kh.proposals (
    id               uuid PRIMARY KEY,
    kind             text NOT NULL CHECK (kind IN ('note', 'doc', 'edit')),
    state            text NOT NULL CHECK (state IN ('pending', 'committed', 'pr_open', 'merged', 'closed', 'failed')),
    repo             text NOT NULL,
    path             text NOT NULL,
    doc_key          text NOT NULL,
    domain           text NOT NULL,
    title            text NOT NULL,
    content_sha      text NOT NULL,
    dedupe_key       text,
    branch           text,
    base_sha         text,
    commit_sha       text,
    pr_number        int,
    pr_url           text,
    requester_login  text NOT NULL,
    created_by_sub   text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    decided_at       timestamptz,
    error            text,
    required_audiences text[] NOT NULL,
    allowed_emails     text[],
    CONSTRAINT kh_no_open_rows CHECK (cardinality(required_audiences) > 0 OR allowed_emails IS NOT NULL),
    CONSTRAINT proposals_note_has_no_dedupe_key CHECK ((kind = 'note') = (dedupe_key IS NULL))
);
CREATE UNIQUE INDEX proposals_dedupe_key_open_idx ON kh.proposals (dedupe_key)
    WHERE dedupe_key IS NOT NULL AND state IN ('pending', 'pr_open');

-- 2. Row-level security, as for candidates; kh_app sees and inserts only its own rows.
ALTER TABLE kh.proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.proposals FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.proposals FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

GRANT SELECT, INSERT ON kh.proposals TO kh_app;
CREATE POLICY kh_app_select ON kh.proposals FOR SELECT TO kh_app
    USING (created_by_sub = kh.caller_sub() AND kh.visible(required_audiences, allowed_emails));
CREATE POLICY kh_app_insert ON kh.proposals FOR INSERT TO kh_app
    WITH CHECK (created_by_sub = kh.caller_sub() AND state = 'pending' AND pr_number IS NULL
                AND kh.visible(required_audiences, allowed_emails));

GRANT SELECT ON kh.proposals TO kh_indexer;
GRANT UPDATE (state, decided_at, pr_number, pr_url, required_audiences) ON kh.proposals TO kh_indexer;
CREATE POLICY kh_indexer_select ON kh.proposals FOR SELECT TO kh_indexer USING (true);
CREATE POLICY kh_indexer_update ON kh.proposals FOR UPDATE TO kh_indexer USING (true) WITH CHECK (true);

-- 3. Moves a proposal created by the caller along pending|failed -> committed|pr_open|failed.
-- A note goes to committed or failed, a doc or edit to pr_open or failed. A null branch,
-- commit_sha, pr_number or pr_url keeps the stored value; error is always replaced.
-- Returns whether a row changed; never returns row data.
CREATE FUNCTION kh.proposal_mark(id uuid, new_state text, branch text, commit_sha text,
                                 pr_number int, pr_url text, error text) RETURNS boolean
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ WITH upd AS (
              UPDATE kh.proposals p
                 SET state = $2,
                     branch = coalesce($3, p.branch),
                     commit_sha = coalesce($4, p.commit_sha),
                     pr_number = coalesce($5, p.pr_number),
                     pr_url = coalesce($6, p.pr_url),
                     error = $7
               WHERE p.id = $1
                 AND p.created_by_sub = kh.caller_sub()
                 AND p.state IN ('pending', 'failed')
                 AND ((p.kind = 'note' AND $2 IN ('committed', 'failed'))
                      OR (p.kind <> 'note' AND $2 IN ('pr_open', 'failed')))
              RETURNING 1
          )
          SELECT EXISTS (SELECT 1 FROM upd) $$;

-- 4. A number only: rows of the given kinds since a time, filtered by the non-null arguments,
-- with no visibility filter, so a token scoped to fewer audiences cannot multiply a limit.
CREATE FUNCTION kh.count_proposals_today(kinds text[], p_domain text, p_repo text, p_login text,
                                         since timestamptz) RETURNS int
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$ SELECT count(*)::int FROM kh.proposals p
           WHERE p.kind = ANY ($1)
             AND p.created_at >= $5
             AND ($2 IS NULL OR p.domain = $2)
             AND ($3 IS NULL OR p.repo = $3)
             AND ($4 IS NULL OR p.requester_login = $4) $$;

REVOKE EXECUTE ON FUNCTION
    kh.proposal_mark(uuid, text, text, text, int, text, text),
    kh.count_proposals_today(text[], text, text, text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    kh.proposal_mark(uuid, text, text, text, int, text, text),
    kh.count_proposals_today(text[], text, text, text, timestamptz) TO kh_app;

-- 5. agent_written on documents and chunks; chunks copy it from their document.
ALTER TABLE kh.documents ADD COLUMN agent_written boolean NOT NULL DEFAULT false;
ALTER TABLE kh.chunks ADD COLUMN agent_written boolean NOT NULL DEFAULT false;

CREATE OR REPLACE FUNCTION kh.chunks_inherit_owner() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = kh, pg_temp
    AS $$
BEGIN
    IF NEW.document_id IS NOT NULL THEN
        SELECT 'doc:' || d.id, d.tier, d.domain, d.status, d.stale, d.status = 'superseded',
               d.entities, d.indexed_at, d.required_audiences, d.allowed_emails, d.agent_written
          INTO NEW.owner_key, NEW.tier, NEW.domain, NEW.status, NEW.stale, NEW.superseded,
               NEW.entities, NEW.owner_created_at, NEW.required_audiences, NEW.allowed_emails,
               NEW.agent_written
          FROM kh.documents d WHERE d.id = NEW.document_id;
    ELSIF NEW.raw_id IS NOT NULL THEN
        SELECT 'raw:' || r.id, 'T3', r.domain, 'current', false, false,
               r.entities, r.ingested_at, r.required_audiences, r.allowed_emails, false
          INTO NEW.owner_key, NEW.tier, NEW.domain, NEW.status, NEW.stale, NEW.superseded,
               NEW.entities, NEW.owner_created_at, NEW.required_audiences, NEW.allowed_emails,
               NEW.agent_written
          FROM kh.raw_records r WHERE r.id = NEW.raw_id;
    ELSIF NEW.derived_id IS NOT NULL THEN
        SELECT 'derived:' || r.id, 'T2', r.domain, 'current', false, false,
               r.entities, r.created_at, r.required_audiences, r.allowed_emails, false
          INTO NEW.owner_key, NEW.tier, NEW.domain, NEW.status, NEW.stale, NEW.superseded,
               NEW.entities, NEW.owner_created_at, NEW.required_audiences, NEW.allowed_emails,
               NEW.agent_written
          FROM kh.derived_records r WHERE r.id = NEW.derived_id;
    END IF;
    -- No owner row (or no owner column): leave the supplied values; FK and CHECK reject it.
    RETURN NEW;
END $$;
