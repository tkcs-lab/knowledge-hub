-- Adds kh.merge_verdicts (one stored trust verdict per judged merge commit, readable by kh_indexer only)
-- and a stamp column on kh.documents and kh.document_versions (how the content was approved).
CREATE TABLE kh.merge_verdicts (
    repo text NOT NULL, merge_sha text NOT NULL, pr_number int,
    status text NOT NULL CHECK (status IN ('trusted', 'disputed')),
    stamp text CHECK (stamp IN ('reviewed', 'owner_merge')),
    reason text, approved_by text[] NOT NULL DEFAULT '{}', approved_at timestamptz, pr_url text,
    judged_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, merge_sha),
    CONSTRAINT merge_verdicts_stamp CHECK ((status = 'trusted') = (stamp IS NOT NULL)));
ALTER TABLE kh.documents ADD COLUMN stamp text CHECK (stamp IN ('reviewed', 'owner_merge', 'anchor'));
ALTER TABLE kh.document_versions ADD COLUMN stamp text CHECK (stamp IN ('reviewed', 'owner_merge', 'anchor'));

-- backfill: before this change only approvals existed
UPDATE kh.documents SET stamp = 'anchor' WHERE approved_by = ARRAY['trust-anchor'];
UPDATE kh.documents SET stamp = 'reviewed' WHERE cardinality(approved_by) > 0 AND approved_by <> ARRAY['trust-anchor'];
UPDATE kh.document_versions SET stamp = 'anchor' WHERE approved_by = ARRAY['trust-anchor'];
UPDATE kh.document_versions SET stamp = 'reviewed' WHERE cardinality(approved_by) > 0 AND approved_by <> ARRAY['trust-anchor'];

ALTER TABLE kh.merge_verdicts ENABLE ROW LEVEL SECURITY;
ALTER TABLE kh.merge_verdicts FORCE ROW LEVEL SECURITY;
CREATE POLICY kh_migrator_all ON kh.merge_verdicts FOR ALL TO kh_migrator USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE, DELETE ON kh.merge_verdicts TO kh_indexer;
CREATE POLICY kh_indexer_all ON kh.merge_verdicts FOR ALL TO kh_indexer USING (true) WITH CHECK (true);
