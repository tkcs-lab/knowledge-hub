-- Usage events: what people search and open, for the promotion agent (plan 37, section 3.2).
-- Written by kh_app for the caller's own group set; read back under the subset rule; purged by kh_indexer.

CREATE TABLE kh.usage_events (
    id          uuid PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    kind        text NOT NULL CHECK (kind IN ('search', 'open')),
    who         text NOT NULL CHECK (who ~ '^[0-9a-f]{32}$'),
    audiences   text[] NOT NULL CHECK (cardinality(audiences) > 0),
    query       text CHECK (char_length(query) <= 500),
    query_norm  text GENERATED ALWAYS AS (lower(btrim(regexp_replace(query, '\s+', ' ', 'g')))) STORED,
    min_tier    text CHECK (min_tier IN ('T0', 'T3')),
    t0_fts_hits int NOT NULL DEFAULT 0 CHECK (t0_fts_hits >= 0),
    t3_fts_hits int NOT NULL DEFAULT 0 CHECK (t3_fts_hits >= 0),
    owner_keys  text[] NOT NULL DEFAULT '{}',
    CONSTRAINT usage_search_shape CHECK (kind <> 'search' OR (query IS NOT NULL AND min_tier IS NOT NULL)),
    CONSTRAINT usage_open_shape CHECK (kind <> 'open'
        OR (query IS NULL AND min_tier IS NULL AND cardinality(owner_keys) = 1))
);
CREATE INDEX usage_events_at_idx ON kh.usage_events (at);

ALTER TABLE kh.usage_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.usage_events FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.usage_events FOR ALL TO kh_migrator USING (true) WITH CHECK (true);

-- kh_app inserts only for exactly the caller's own group set and only for people; it reads an event only
-- when every group of the searcher is also held by the reader (the empty set is never readable).
GRANT SELECT, INSERT ON kh.usage_events TO kh_app;
CREATE POLICY kh_app_select ON kh.usage_events FOR SELECT TO kh_app
    USING (cardinality(audiences) > 0 AND audiences <@ kh.caller_audiences());
CREATE POLICY kh_app_insert ON kh.usage_events FOR INSERT TO kh_app
    WITH CHECK (cardinality(audiences) > 0 AND audiences = kh.caller_audiences()
                AND kh.caller_type() = 'claude_client');

GRANT SELECT, DELETE ON kh.usage_events TO kh_indexer;
CREATE POLICY kh_indexer_select ON kh.usage_events FOR SELECT TO kh_indexer USING (true);
CREATE POLICY kh_indexer_delete ON kh.usage_events FOR DELETE TO kh_indexer USING (true);
