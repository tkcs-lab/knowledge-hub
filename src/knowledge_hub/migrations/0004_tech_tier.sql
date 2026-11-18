-- 0004: tier T0a, technical documents indexed without review.
ALTER TABLE kh.documents
    ADD COLUMN tier text NOT NULL DEFAULT 'T0' CHECK (tier IN ('T0', 'T0a'));
ALTER TABLE kh.chunks DROP CONSTRAINT chunks_tier_check;
ALTER TABLE kh.chunks ADD CONSTRAINT chunks_tier_check CHECK (tier IN ('T0', 'T0a', 'T3'));
ALTER TABLE kh.usage_events DROP CONSTRAINT usage_events_min_tier_check;
ALTER TABLE kh.usage_events ADD CONSTRAINT usage_events_min_tier_check
    CHECK (min_tier IN ('T0', 'T0a', 'T3'));

CREATE OR REPLACE FUNCTION kh.chunks_inherit_owner() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = kh, pg_temp
    AS $$
BEGIN
    IF NEW.document_id IS NOT NULL THEN
        SELECT 'doc:' || d.id, d.tier, d.audience, d.labels, d.stale, d.indexed_at
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

CREATE OR REPLACE FUNCTION kh.owner_propagate_to_chunks() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = kh, pg_temp
    AS $$
BEGIN
    UPDATE kh.chunks
       SET audience = NEW.audience, labels = NEW.labels, stale = NEW.stale, tier = NEW.tier
     WHERE document_id = NEW.id;
    RETURN NULL;
END $$;

DROP TRIGGER owner_propagate_to_chunks ON kh.documents;
CREATE TRIGGER owner_propagate_to_chunks AFTER UPDATE OF audience, labels, stale, tier ON kh.documents
    FOR EACH ROW EXECUTE FUNCTION kh.owner_propagate_to_chunks();
