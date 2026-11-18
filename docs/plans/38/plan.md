# Plan 38: supersede links for T0 documents and a tool to mark T3 records outdated

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** In a group's Forgejo repo a new document lists the ids it replaces (`supersedes: [old-id]` in its frontmatter); once that pull request is merged, `search` stops returning the old document. For raw records the new tool `mark_record_outdated` (id, optional `replaced_by` and `note`) hides a record the same way, and `outdated=false` brings it back. `search` takes `include_outdated=true` to show hidden items; hits carry `outdated` and `replaced_by`, and `get_doc` shows `supersedes` and `superseded_by` for a document and `outdated`, `replaced_by` and `note` for a record. `ingest_record` and `propose_doc` also return up to three similar items (`similar`) so an agent can notice that the topic already exists.

**Scope:** Adds migration 0002 (new columns, one definer function, a one-time `blob_sha` reset), the `supersedes` frontmatter key with a resolver that runs on every sync, an owner-row hide filter in search, the new tool and its PrivacyFence declaration, a `supersedes` argument for `propose_doc`, the similar-items lookup with two new settings, and the docs. Also: `list_stale` skips superseded documents, old search cursors fail once, three new index alerts exist, and `kh lint` reports `invalid supersedes`. It does not change tiers, weights, the stale flag or who can read what; nothing is ever deleted by supersede or outdated.

**Security:** The new definer function is a second write path to `kh.records` (kh_app still has no UPDATE grant). It answers the same for a missing and a hidden record, accepts a replacement only from the same group, and is not executable by the indexer role or PUBLIC. Similar items stay inside the target group and are skipped when the embedder may not see that group.

**Performance:** One extra anti-join per search list; outdated items are rare. Ingest and propose add one embedding call (5 second limit), skipped when the group may not be embedded; failures are isolated.

**Review focus:** 3.1 the definer function and the one-time `blob_sha` reset that forces a re-read of every file; 3.2 the resolver rules (cycle, two successors, unknown id, old single-string lines); 3.3 the hide filter running before the pool limit; 3.6 failure isolation of the similar lookup.

## 1. Goal

Issue: https://github.com/tkcs-lab/knowledge-hub/issues/38

Today a replaced standard or a newer note on the same topic stays searchable at full weight: T0 only has the date-based `stale` flag (`src/knowledge_hub/jobs/daily.py:8-20`) and T3 records have nothing at all (`src/knowledge_hub/migrations/0001_baseline.sql:58-75`). After this plan:

- a T0 document can declare which documents it replaces, and the replaced ones leave the default search;
- any caller holding a record's group can mark that record outdated (and undo it), optionally naming its replacement;
- an agent that adds a document or record is shown the closest existing items in the same group.

The owner answered four design questions on the status board (ask `a-1a5b29f3651a2fa5`): the new document names the old one (`supersedes`), outdated items are hidden unless `include_outdated=true`, any holder of the group may mark a record, and similar items are returned by `propose_doc` and `ingest_record`.

## 2. Current state

- Frontmatter: required `id`, `title`, `review_by`, optional `labels`; every other key is ignored (`src/knowledge_hub/indexer/frontmatter.py:62-98`). The `supersedes`/`superseded_by` keys and `KH_SUPERSEDED_WEIGHT` of the old design were removed in 0.3.0 (`docs/configuration.md:99`).
- Sync diffs `main` against `kh.documents` by `(path, blob_sha)`; a file with an unchanged blob is never parsed again (`src/knowledge_hub/indexer/sync_plan.py:55-58`, `:82-89`), and `kh rebuild` only deletes `repo_state` (`src/knowledge_hub/indexer/rebuild.py:28`), so it re-reads nothing by itself. The write runs in one transaction under `pg_advisory_xact_lock` (`src/knowledge_hub/indexer/sync.py:37-79`).
- Search is one statement over `kh.chunks`; the filter `_FILTER` is applied before the pool `LIMIT` (`src/knowledge_hub/search/sql.py:50-81`); a hit is weighted by tier and by `chunks.stale` (`:84-92`). `stale` is copied onto chunks by triggers (`0001_baseline.sql:312-345`).
- `kh_app` can SELECT and INSERT `kh.records` but never UPDATE (`0001_baseline.sql:229-235`; asserted by `tests/integration/test_rls_policies.py:218-223`). The only definer functions are `claim_seen`, `records_enqueue_embed` and `labels_in_use_guard` (`tests/integration/test_rls_policies.py:31`).
- `ingest_record` inserts inside a savepoint (`src/knowledge_hub/tools/records.py:85-101`); `propose_doc` renders the whole file from its arguments and round-trips it through `parse_document` (`src/knowledge_hub/tools/propose.py:71-80`).
- Tool errors may only use the constants in `src/knowledge_hub/mcp/errors.py:31-47` ("later phases import them and never add to them", `:3-4`), and no tool argument may be named `reason` (`tests/unit/test_tool_schemas.py:35`).

## 3. Design

### 3.1 Migration `0002_supersede.sql`

`0001_baseline.sql` is checksummed and is not touched. The new file is applied by `kh migrate` (`src/knowledge_hub/db/migrate.py:50-67`). Its content, exactly:

```sql
-- 0002: supersede links on documents, outdated marks on records, one definer write path.

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
```

Why this shape (rejected alternatives): a column `UPDATE` grant plus policy for `kh_app` would break the invariant that `kh_app` never updates records and could not check the successor's group in one place; a `chunks.outdated` flag copied by triggers (the `stale` pattern) loses a race with `embed_record` (the worker inserts chunks in its own transaction, `src/knowledge_hub/indexer/worker.py:69-98`, so a mark committed in between would leave the chunks visible), so search filters on the owner rows instead (3.3). `outdated_by` holds the caller's `sub` for audit; no tool returns it.

### 3.2 T0 supersede: frontmatter and resolution

- **Frontmatter key** `supersedes`: optional list of document ids; a single string is read as a list of one (the removed old design accepted `supersedes: old-id`, and such files must keep their documents). Valid when it is a list of at most 20 strings, each matching `ID_RE` (`src/knowledge_hub/indexer/frontmatter.py:24`), without duplicates and not containing the document's own `id`. Otherwise the file is rejected with `invalid supersedes` (same mechanism as `invalid labels`, `:93-96`). A missing key means `[]`. `kh lint` reports it for free (`src/knowledge_hub/cli.py:90-106`).
- **Same repo only.** Ids are unique per repo, and a repo is one group, so a link never crosses groups.
- **Storage.** `kh.documents.supersedes` holds the doc keys the file lists; `kh.documents.superseded_by` holds the uuid of the document that replaces this one (set by the resolver, never by the upsert; the `ON CONFLICT ... DO UPDATE SET` list in `src/knowledge_hub/indexer/sync.py:59-63` gains `supersedes` and must not name `superseded_by`).
- **Resolver**, new module `src/knowledge_hub/indexer/supersede.py`, pure:

  ```python
  @dataclass(frozen=True)
  class DocLink:
      id: str            # documents.id as text
      doc_key: str
      path: str
      supersedes: tuple[str, ...]
      current: str | None  # documents.superseded_by as text, or None

  @dataclass(frozen=True)
  class Resolution:
      links: dict[str, str | None]       # every DocLink.id -> successor id or None
      alerts: list[tuple[str, str]]      # (path, message)

  def resolve(rows: Sequence[DocLink]) -> Resolution: ...
  ```

  Rules, in this order:
  1. For each document `new` and each key in `new.supersedes`: the target is the row with that `doc_key`. No such row: alert `(new.path, f"supersedes unknown id {key}")` and nothing else. The target is `new` itself: skip.
  2. A target named by several documents takes the successor with the smallest `doc_key`; when there were several, alert `(target.path, f"superseded by {n} documents; using {chosen.doc_key}")`.
  3. Follow `target -> successor` links. A document that lies on a cycle (it reaches itself) gets no link, and each member alerts `(member.path, f"supersedes cycle: {', '.join(sorted(member keys))}")`.
  4. Every other target maps to its direct successor. A chain A to B to C keeps A -> B and B -> C; both A and B are superseded.
  5. Every input row appears in `links`, with `None` when nothing supersedes it.
- **When it runs.** In `sync._write`, after `apply_chunks` and before the plan's alerts, on every sync of the repo: `_apply_supersedes(conn, repo)` selects `id, doc_key, path, supersedes, superseded_by` of every document of the repo from the database (inside the locked transaction, never from the plan), calls `resolve`, updates only rows where `superseded_by IS DISTINCT FROM` the new value, and writes the alerts through the existing `_alert` (`sync.py:82-87`, deduplicated on repo, path and message). Removing the line from the successor, or deleting the successor (`ON DELETE SET NULL`), makes the old document current again on the next sync.

### 3.3 Search, `get_doc`, `list_stale`

- `SearchFilters.include_outdated: bool = False` (`src/knowledge_hub/search/sql.py:17-30`).
- A CTE `hidden` is the first one of `_BODY`:
  `hidden AS (SELECT 'doc:' || id::text AS owner_key FROM kh.documents WHERE superseded_by IS NOT NULL UNION ALL SELECT 'rec:' || id::text FROM kh.records WHERE outdated)`. It runs under the caller's row-level security.
- `_FILTER` gains `AND (%(include_outdated)s OR NOT EXISTS (SELECT 1 FROM hidden h WHERE h.owner_key = c.owner_key))`, so hidden chunks never enter the pool and recall is kept. The `low_*` lists use the same filter.
- `scored` joins `LEFT JOIN hidden h ON h.owner_key = c.owner_key`, selects `(h.owner_key IS NOT NULL) AS outdated`, and multiplies the score by `stale_weight` when `c.stale OR h.owner_key IS NOT NULL` (an included outdated hit is weighted like a stale one; no new setting).
- The final select adds `p.outdated`, `d.superseded_by AS doc_superseded_by`, `r.replaced_by AS rec_replaced_by`.
- `search` (`src/knowledge_hub/tools/search.py`) gets `include_outdated: bool = False`, the argument goes into `_cursor_scope` (old cursors answer `cursor does not belong to this query` once), and each hit gets `outdated: bool` and `replaced_by: str` (T0: `doc:<superseded_by>` or `""`; T3: the stored `replaced_by`). With `min_tier="T0"`, a query whose only T0 matches are superseded now answers the existing `only T3 records match...` text when T3 hits exist; no new error text.
- `get_doc` (`src/knowledge_hub/tools/docs.py`): a document adds `supersedes` (list of doc keys), `superseded_by` (`doc:<uuid>` or `""`) and `outdated` (bool); a record adds `outdated` (bool), `outdated_at` (ISO text or `""`), `replaced_by` (id text or `""`) and `note` (the stored `outdated_note`). `outdated_by` is never returned.
- `list_stale` adds `AND superseded_by IS NULL`: a replaced document needs no review.
- The `search` tool description gains: `Superseded documents and outdated records are hidden unless include_outdated is true.`

### 3.4 Tool `mark_record_outdated`

New module `src/knowledge_hub/tools/outdated.py`, registered in `register_tools` (`src/knowledge_hub/mcp/server.py:30-38`). Signature (after the `ToolCall`): `id: str, outdated: bool = True, replaced_by: str = "", note: str = ""`.

1. `parse_id(id)` (`src/knowledge_hub/tools/docs.py:37`) must give kind `rec`, else `BAD_ARG name="id"` (the tool takes record ids only, so `BAD_ID`, which names both prefixes, would mislead).
2. `note`: at most 500 characters and no NUL, else `BAD_ARG name="note"`. `replaced_by`: empty, or a `doc:`/`rec:` id accepted by `parse_id`, else `BAD_ARG name="replaced_by"`; with `outdated=false` a non-empty `replaced_by` or `note` is `BAD_ARG` for that name; a NUL in either is `BAD_ARG`.
3. Run `SELECT kh.mark_record_outdated(%s, %s, %s, %s)` inside `async with call.conn.transaction():` (a savepoint). `NULL` answers `NOT_FOUND`; SQLSTATE `P0K07` answers `BAD_ARG name="replaced_by"`; any other database error propagates to `tool_guard`.
4. Log `mark_record_outdated sub=<sub> id=<id> outdated=<bool>` at INFO (the table keeps only the last actor).
5. Return `{"id": "rec:<uuid>", "outdated": <bool>, "replaced_by": <str>}` (`replaced_by` is `""` when `outdated` is false).
6. Description: `Mark a raw record of a group you hold as outdated (hidden from search unless include_outdated is true), optionally naming the document or record that replaces it, or mark it current again with outdated=false.`

Declaration in `deploy/privacyfence-upstream.example.yaml`: `mark_record_outdated: {gate: popup, verb: update, effect: "Marks a raw record of your group as outdated, or as current again."}`; the unit test `WRITE_TOOLS` gains the name.

### 3.5 `propose_doc` gets `supersedes`

`propose_doc(..., supersedes: list[str] = [])` after `base_blob` (new arguments go last). Each entry must match `ID_RE`, there are at most 20, no duplicates, none equals `id`, and each must exist as `kh.documents.doc_key` of the target repo; any failure is `BAD_ARG name="supersedes"`. `render_file` (`src/knowledge_hub/propose_render.py:37`) gets a trailing `supersedes: Sequence[str] = ()` and writes `supersedes: ["a", "b"]` (each id JSON-quoted like the `id:` line, joined by `, `) after the `labels` line only when non-empty. The round-trip comparison (`propose.py:75-80`) includes the parsed `supersedes`. The pull request body gets a last line `Supersedes: \`a\`, \`b\`` (ids match `ID_RE`, so they cannot carry Markdown) when non-empty. The tool description says: `To change an existing document keep its supersedes list: pass the supersedes that get_doc returned.` Editing a file through `propose_doc` would otherwise drop the line and silently make the old document current.

### 3.6 Similar items

- Settings (`src/knowledge_hub/settings.py`): `similar_limit: int = 3` (0 to 10; 0 switches the lookup off) as `KH_SIMILAR_LIMIT`, and `similar_min_score: float = 0.5` (0 to 1) as `KH_SIMILAR_MIN_SCORE`.
- New module `src/knowledge_hub/search/similar.py` with the constants `_SIMILAR_SQL` and `EMBED_TIMEOUT_S = 5` and `async def similar_items(call: ToolCall, text: str, audience: str, *, exclude_repo: str = "", exclude_key: str = "") -> list[dict[str, Any]]`:
  1. Return `[]` when `similar_limit` is 0, `deps.embedder` is `None`, or `not embedder.may_embed(audience)`.
  2. `try`: `(vector,) = await asyncio.wait_for(embedder.embed([text]), EMBED_TIMEOUT_S)`; then `async with call.conn.transaction():` (a savepoint) run `_SIMILAR_SQL`; `except Exception`: log a warning and return `[]`. The savepoint keeps the caller's transaction usable, so a failed lookup never loses the record or the pull request.
  3. `_SIMILAR_SQL` selects, from `kh.chunks c LEFT JOIN kh.documents d ON d.id = c.document_id LEFT JOIN kh.records r ON r.id = c.record_id`, `c.owner_key, c.tier, 1 - (c.embedding <=> %(qvec)s::vector) AS similarity, coalesce(d.title, r.title) AS title` where `c.embedding IS NOT NULL AND c.embedding_model = %(model)s AND c.audience = %(audience)s AND (d.id IS NULL OR d.superseded_by IS NULL) AND (r.id IS NULL OR NOT r.outdated) AND NOT coalesce(d.repo = %(xrepo)s AND d.doc_key = %(xkey)s, false)`, ordered by distance, `LIMIT 50`.
  4. In Python keep the best row per `owner_key`, drop rows below `similar_min_score`, return the first `similar_limit` as `{"id": owner_key, "tier": tier, "title": title, "similarity": round(similarity, 3)}`. The field is `similarity`, not `score`, so it is not mistaken for the RRF score of `search`.
- `ingest_record`: after a successful new insert only (not on the `existed` paths) it adds `"similar": await similar_items(call, f"{title}\n\n{body[:EXCERPT_CHARS]}", audience)` to the result.
- `propose_doc`: after the pull request is open it adds `"similar"` computed from `f"{title}\n\n{body[:2000]}"` for audience `group`, with `exclude_repo=repo, exclude_key=id` (editing a document must not report itself). The similar items are not written into the pull request.
- Only items of the target group are returned, so nothing crosses groups. The default `0.5` is untested against the real model (see Risks).

### 3.7 Rollout

1. Deploy the new image, `kh migrate` (applies 0002 and blanks `blob_sha`), then `kh rebuild` so every file is read again (embedding is not repeated).
2. Restart PrivacyFence: it reads the upstream declaration once at startup (header of `deploy/privacyfence-upstream.example.yaml`). Create the n8n tokens again for workflows that should call `mark_record_outdated`.
3. Between `kh migrate` and `kh rebuild` hits show an empty `blob_sha`; `propose_doc` with `base_blob` waits for the rebuild. After the rebuild every document has a new `commit_sha`, `indexed_at` and `updated_at` (every file is upserted once), so `source_link` in `get_doc` changes once.

### 3.8 Documentation

README tools table (seven tools), `docs/design.md` (migration statement at the Roles section, RLS matrix row for `records`, Indexer frontmatter and resolution, Search, Tools), `docs/configuration.md` (`KH_SIMILAR_LIMIT`, `KH_SIMILAR_MIN_SCORE`, `KH_STALE_WEIGHT` also weights included outdated hits), `docs/runbook.md` (the three new alerts, the rollout above), `CHANGELOG.md` under `## [Unreleased]`. All of it is written by the last phase.

## 4. Manual steps

Everything runs on the operator's own server, so all of it is after the last merge.

### Before implementation

None.

### After implementation

These run after the last phase is merged and before the feature pull request merges, on a hub you can test on (staging, or a copy of production), using the image built from the branch `feat/supersede-and-outdated`.

**ma1-migrate-and-rebuild** (about 10 minutes)

1. Deploy the image built from `feat/supersede-and-outdated` to that hub the way the runbook describes (`https://github.com/tkcs-lab/knowledge-hub/blob/feat/supersede-and-outdated/docs/runbook.md`).
2. Run `kh migrate`. You see `applied 0002_supersede.sql`.
3. Run `kh rebuild`. You see one `rebuilt <repo> at <sha>` line per group repo.
4. Run `kh check`. It prints nothing and exits 0.
5. Read the alerts as the runbook's `index_alerts` query shows (`SELECT created_at, repo, path, message FROM kh.index_alerts ORDER BY created_at DESC LIMIT 50;`) and look for `invalid supersedes`.
6. Report back: pass or fail for each of steps 2 to 4, and the paths of any `invalid supersedes` alert.

**ma2-privacyfence-restart** (about 10 minutes)

1. Restart the PrivacyFence container so it reads the declaration with the new tool (`https://github.com/tkcs-lab/knowledge-hub/blob/feat/supersede-and-outdated/deploy/privacyfence-upstream.example.yaml`, header lines 1-8; PrivacyFence reads it once at startup).
2. In your PrivacyFence admin page for tokens, create the n8n tokens again for each workflow that should call `mark_record_outdated`, with that tool ticked. Whether existing tokens survive an added tool or a new optional argument is not known (the `research:` question of the architect); re-creating them is the safe choice.
3. In a Claude client call `kh_mark_record_outdated` on a test record of your group, see the approval popup, approve, and check that `kh_search` no longer returns it and `kh_search` with `include_outdated=true` does.
4. Report back: pass or fail of step 3.

**ma3-similar-threshold** (about 10 minutes)

1. `kh_ingest_record` a test record, then `kh_ingest_record` a second record with nearly the same text and a different `source_ref`: the reply's `similar` list should name the first.
2. Ingest a record on an unrelated topic: `similar` should be empty or name nothing close.
3. If the first is empty or the second is noisy, set `KH_SIMILAR_MIN_SCORE` (default `0.5`) lower or higher and restart `kh serve`.
4. Report back: the value you ended with, or `unchanged`.

## 5. Risks and open questions

- **The similarity default is unverified.** `0.5` was chosen without measuring bge-m3 on this data. A worker cannot settle it; `ma3-similar-threshold` does. A test that asserts a specific real-model score is a mistake: tests use the fake embedder, where only identical text scores 1.0.
- **A resolver exception blocks a whole repo sync** because it runs in the sync transaction. The pure function is unit-tested on cycles, duplicates, unknown ids and chains; a worker that sees `resolve` raise on valid input stops with `status=blocked`.
- **Old single-string lines.** The removed design accepted `supersedes: old-id`; 3.2 reads it as a list of one, so such files keep their documents. A file that is invalid in another way is rejected with `invalid supersedes` and its row is removed on the next sync; `ma1-migrate-and-rebuild` reads the alerts for that.
- **A typo in `supersedes` can hide a valid document** until the next pull request. Only an unknown id raises an alert (`supersedes unknown id <key>`).
- **Hidden looks like gone.** A hidden document still opens through `get_doc` and `search` with `include_outdated=true`; the runbook says so.
- **Any member of a group can hide any record of that group** (owner decision). `outdated_by` and the INFO log keep the last actor.
- **Edits through `propose_doc` can un-supersede** if the agent leaves `supersedes` out (3.5). `get_doc` returns the list so the agent can pass it back.
- **Similar items add latency to ingest.** Bounded by the 5 second embedding limit and the 5 second statement timeout; `KH_SIMILAR_LIMIT=0` removes it.
- **`blob_sha` reset side effect**: every document is upserted once, so every `commit_sha`, `indexed_at` and `updated_at` moves to the current head and `source_link` changes once; the changelog says so. If the migration's `UPDATE kh.documents SET blob_sha = ''` runs and `kh rebuild` is forgotten, nothing is re-read until a file changes. `ma1-migrate-and-rebuild` covers it.
- **A worker that finds `0001_baseline.sql` must change** to make a test pass stops with `status=blocked`: the baseline is checksummed.
- **Research the owner can run later (not blocking):** whether PrivacyFence needs more than a restart when a tool or an optional argument is added; the cosine range that separates related from unrelated passages for bge-m3.

## Implementation manifest

```yaml
plan_slug: supersede-and-outdated
feature_branch: feat/supersede-and-outdated
tracking_issue: 38
max_parallel: 2
manual_before: []
manual_after:
  - id: ma1-migrate-and-rebuild
    title: Run kh migrate, kh rebuild and kh check on the deployed hub
    why: Applies 0002 and re-reads every document so supersedes lines are indexed; CI cannot run the operator's deployment.
  - id: ma2-privacyfence-restart
    title: Restart PrivacyFence and re-create n8n tokens for the new tool
    why: PrivacyFence reads the upstream declaration only at startup; a real approval popup cannot be exercised in CI.
  - id: ma3-similar-threshold
    title: Check the similar-items threshold on real data
    why: The default KH_SIMILAR_MIN_SCORE was not measured against the real embedding model.
verify_after_merge:
  - .venv/bin/python -m pytest -q -m integration
final_checks:
  - "`tld plan deviations check --file docs/plans/38/deviations.md --plan docs/plans/38/plan.md --all` exits 0"
  - "the changelog has entries under `## [Unreleased]` and no version heading"
phases:
  - id: p1-migration-0002
    title: Migration 0002 with the new columns and the definer function
    depends_on: []
    complexity: M
    touches:
      - src/knowledge_hub/migrations/0002_supersede.sql
      - tests/integration/test_migration_0002.py
      - tests/integration/test_migrate_runner.py
      - tests/integration/test_schema_tables.py
      - tests/integration/test_rls_policies.py
      - tests/integration/test_setup_postgres.py
    brief: |
      Read first: plan section 3.1, src/knowledge_hub/migrations/0001_baseline.sql, tests/integration/test_migrate_runner.py, tests/integration/test_rls_policies.py.
      1. Create src/knowledge_hub/migrations/0002_supersede.sql containing exactly the SQL block of plan section 3.1 (the statements, comments included). Do not edit 0001_baseline.sql.
      2. tests/integration/test_migrate_runner.py: add `SECOND = "0002_supersede.sql"`; rename test_exactly_one_migration_is_shipped to test_two_migrations_are_shipped and assert the list equals [BASELINE, SECOND]; change every assertion that lists applied migration names (the `migrate(...) == [BASELINE]` checks, the schema_migrations rows in test_schema_migrations_lists_the_baseline) so they list both files with their sha256 of the shipped text.
      3. tests/integration/test_schema_tables.py: add "supersedes", "superseded_by" to EXPECTED["documents"] and "outdated", "outdated_at", "outdated_by", "outdated_note", "replaced_by" to EXPECTED["records"].
      4. tests/integration/test_rls_policies.py: add "mark_record_outdated" to DEFINERS (line 31), and add test_mark_record_outdated_is_not_executable_by_public_or_indexer: has_function_privilege('kh_app', ...) is true, has_function_privilege('kh_indexer', ...) is false, and `SELECT count(*) FROM pg_proc p, aclexplode(p.proacl) a WHERE p.proname = 'mark_record_outdated' AND a.grantee = 0` is 0.
      5. tests/integration/test_setup_postgres.py: in test_plan_apply_converge add "+ migration 0002_supersede.sql" to the expected planned lines next to the 0001 one; the check in test_without_admin_dsn stays as it is.
      6. Create tests/integration/test_migration_0002.py (define local copies of `role_conn`, `attempt` and `DB = "kh_fresh"` from tests/integration/test_rls_policies.py; do not import that test module) with these tests: test_upgrade_from_baseline_adds_columns_and_resets_blob_sha (create an empty database the way test_migrate_runner does, `migrate(dsn, stop_after=BASELINE)`, insert a group and a document with blob_sha 'b1', `migrate(dsn)` returns [SECOND], the new columns exist with their defaults and the document's blob_sha is ''); test_mark_function_sets_and_clears_outdated (as kh_app holding group all: set with a note and a replaced_by of a same-group record, read the columns back as admin, then flag false and see every field reset); test_mark_function_answers_null_for_missing_hidden_or_anonymous (random uuid, a record of a group the caller does not hold, and a call with no kh.sub setting all return NULL and change nothing); test_mark_function_refuses_foreign_or_self_successor (each of: successor of another group, successor id that does not exist, malformed successor, the record itself raises SQLSTATE P0K07); test_a_document_can_be_the_successor (replaced_by 'doc:<uuid>' of a same-group document is accepted); test_outdated_check_constraint (admin UPDATE setting replaced_by while outdated is false violates records_outdated_fields); test_kh_app_still_cannot_update_records (UPDATE kh.records SET outdated = true as kh_app raises InsufficientPrivilege).
      7. Run `.venv/bin/python -m ruff check .` and the five test files named in acceptance; fix what fails in the tests you own.
      Stop with status=blocked if a test can only pass by editing 0001_baseline.sql, or if the migration fails on the test cluster for a reason other than your SQL (report the error text).
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_migration_0002.py tests/integration/test_migrate_runner.py tests/integration/test_schema_tables.py tests/integration/test_rls_policies.py tests/integration/test_setup_postgres.py -q passes"
      - "git diff --name-only origin/main -- src/knowledge_hub/migrations/0001_baseline.sql prints nothing"
      - "grep -n \"REVOKE EXECUTE ON FUNCTION kh.mark_record_outdated\" src/knowledge_hub/migrations/0002_supersede.sql matches"
      - ".venv/bin/python -m ruff check . passes"
  - id: p2-t0-supersede-indexer
    title: Parse supersedes and resolve superseded_by on every sync
    depends_on: [p1-migration-0002]
    complexity: M
    touches:
      - src/knowledge_hub/indexer/frontmatter.py
      - src/knowledge_hub/indexer/sync_plan.py
      - src/knowledge_hub/indexer/supersede.py
      - src/knowledge_hub/indexer/sync.py
      - tests/support/forgejo_fake.py
      - tests/unit/test_frontmatter.py
      - tests/unit/test_supersede.py
      - tests/unit/test_sync_plan.py
      - tests/unit/test_cli_lint.py
      - tests/integration/test_sync.py
    brief: |
      Read first: plan section 3.2, src/knowledge_hub/indexer/frontmatter.py, sync_plan.py, sync.py, tests/integration/test_sync.py, tests/support/forgejo_fake.py (function md).
      1. frontmatter.py: add `supersedes: list[str] = field(default_factory=list)` as the last field of ParsedDoc. In parse_document, after the labels block, read `data.get("supersedes")`: None means []; a single string becomes a list of that one string; otherwise it must be a list of at most 20 strings, each ID_RE.fullmatch, no duplicates, none equal to doc_id; any violation returns Rejected("invalid supersedes"). Pass the list into ParsedDoc.
      2. sync_plan.py: add `supersedes: list[str] = field(default_factory=list)` as the last field of DocUpsert and pass `supersedes=parsed.supersedes` by keyword where DocUpsert is built (line 102-105).
      3. Create src/knowledge_hub/indexer/supersede.py with DocLink, Resolution and resolve exactly as in plan section 3.2 (rules 1 to 5, the three alert texts verbatim).
      4. sync.py: the INSERT column list, VALUES and the ON CONFLICT SET list gain `supersedes` (value `u.supersedes`); do not add superseded_by to the SET list. Add `async def _apply_supersedes(conn, repo)` that selects id::text, doc_key, path, supersedes, superseded_by::text of every kh.documents row of the repo, builds DocLink rows, calls resolve, runs `UPDATE kh.documents SET superseded_by = %s::uuid WHERE id = %s::uuid AND superseded_by IS DISTINCT FROM %s::uuid` only for rows whose link differs, and writes each alert with `_alert`. Call it in `_write` right after `await apply_chunks(conn, chunk_plan)`.
      5. tests/support/forgejo_fake.py: give `md` a keyword `supersedes: list[str] | None = None` that adds a line `supersedes: [a, b]` to the frontmatter when not None.
      6. Tests. tests/unit/test_frontmatter.py: test_supersedes_defaults_to_empty, test_supersedes_is_parsed, test_supersedes_accepts_a_single_string, test_supersedes_rejects_bad_values (parametrized: not a list, a number item, bad id, duplicate, own id, 21 items) each returning Rejected("invalid supersedes"). tests/unit/test_sync_plan.py: test_supersedes_reaches_the_upsert. tests/unit/test_cli_lint.py: a file with `supersedes: [a, a]` makes `kh lint` print `<name>: invalid supersedes` and return 1. tests/unit/test_supersede.py: one test per rule of 3.2 (unknown id alert; self skipped; two successors smallest doc_key plus alert; two-document cycle and a three-document cycle give None links plus an alert per member; a chain keeps direct links; every input row is in links) and test_resolve_is_deterministic_for_shuffled_input. tests/integration/test_sync.py (use md(..., supersedes=[...]) and the existing `world`, `fake`, `reader`, `indexer_pool`, `q` helpers): test_supersede_marks_the_old_document, test_removing_supersedes_restores_the_old_document, test_deleting_the_successor_restores_the_old_document, test_supersede_cycle_alerts_and_links_nothing, test_unknown_supersedes_target_alerts, test_two_successors_pick_the_smallest_doc_key, test_unchanged_files_are_not_touched_by_a_second_sync (updated_at of the old document does not change when nothing changed), test_editing_a_superseded_document_keeps_it_superseded (change the body of the old document, sync, its superseded_by is still the successor).
      7. Run ruff and the named tests.
      Stop with status=blocked if `resolve` raises on any valid input, or if making a test pass needs a column change in the migration (p1 owns it).
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_frontmatter.py tests/unit/test_sync_plan.py tests/unit/test_cli_lint.py tests/unit/test_supersede.py -q passes"
      - ".venv/bin/python -m pytest tests/integration/test_sync.py -q passes"
      - ".venv/bin/python -m pytest tests/integration/test_sync.py::test_editing_a_superseded_document_keeps_it_superseded -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p3-search-and-get-doc
    title: Hide outdated items in search, show the marks in hits and get_doc
    depends_on: [p1-migration-0002]
    complexity: M
    touches:
      - src/knowledge_hub/search/sql.py
      - src/knowledge_hub/tools/search.py
      - src/knowledge_hub/tools/docs.py
      - tests/integration/test_search_outdated.py
      - tests/integration/test_search_ranking.py
      - tests/integration/test_search_leaks.py
      - tests/integration/test_tool_get_doc.py
      - tests/integration/test_list_stale.py
      - tests/integration/test_http_e2e.py
      - tests/unit/test_tool_schemas.py
    brief: |
      Read first: plan section 3.3, src/knowledge_hub/search/sql.py, src/knowledge_hub/tools/search.py, src/knowledge_hub/tools/docs.py, tests/integration/test_search_ranking.py, tests/support/seed.py.
      1. search/sql.py: add `include_outdated: bool = False` to SearchFilters; add the `hidden` CTE as the first CTE of `_BODY` (the SQL text of plan section 3.3); append the include_outdated condition to `_FILTER`; join `hidden` in `scored` with `LEFT JOIN hidden h ON h.owner_key = c.owner_key`, select `(h.owner_key IS NOT NULL) AS outdated` there and change the weight CASE to `WHEN c.stale OR h.owner_key IS NOT NULL`; add `p.outdated`, `d.superseded_by AS doc_superseded_by`, `r.replaced_by AS rec_replaced_by` to the final select; add `"include_outdated": filters.include_outdated` to params.
      2. tools/search.py: add `include_outdated: bool = False` after `cursor`; pass it to SearchFilters; change the signature to `_cursor_scope(query, min_tier, labels, audience, include_outdated: bool)` with `str(include_outdated)` as the last entry of `parts` and update both call sites; extend `_hit` with `"outdated": bool(row["outdated"])` and `"replaced_by"` (T0: `f"doc:{row['doc_superseded_by']}"` when it is not None else `""`; T3: `row["rec_replaced_by"] or ""`); append the sentence `Superseded documents and outdated records are hidden unless include_outdated is true.` to the tool description.
      3. tools/docs.py: `_DOC_SQL` also selects supersedes, superseded_by; `_REC_SQL` also selects outdated, outdated_at, outdated_note, replaced_by. get_doc returns the added keys of plan section 3.3 (never outdated_by). `_STALE_SQL` gets `AND superseded_by IS NULL`.
      4. Tests, new file tests/integration/test_search_outdated.py (copy the `cursor_key`, `app_settings` and `deps` fixtures with `FakeEmbedder()` from tests/integration/test_search_ranking.py:20-40; use add_chunked_doc and add_chunked_record from tests/support/seed.py; a superseded document is `add_chunked_doc(db, "text", superseded_by=<id of another document>)`, an outdated record is `add_chunked_record(db, "text", outdated=True, outdated_at=WHEN, outdated_by="alice", replaced_by="")`): test_superseded_document_is_hidden_by_default, test_outdated_record_is_hidden_by_default, test_include_outdated_returns_them_with_marks (outdated True, replaced_by as stored), test_included_outdated_hit_is_weighted_like_a_stale_one, test_hidden_items_do_not_use_up_the_pool (set search_pool to 1, hide the best match, the second best is still returned), test_include_outdated_is_part_of_the_cursor_scope (a cursor from one value is refused with the CURSOR_FOREIGN text under the other), test_min_tier_t0_with_only_superseded_t0_matches_answers_the_normative_text, test_hidden_rows_of_another_group_do_not_change_results. Extend tests/integration/test_tool_get_doc.py (new keys for a document and a record; no outdated_by key), tests/integration/test_list_stale.py (a superseded stale document is not listed), tests/unit/test_tool_schemas.py (search has include_outdated with default False). Fix any exact-dict assertion in test_search_ranking.py, test_search_leaks.py or test_http_e2e.py that the two new hit keys break by adding the keys, never by loosening the comparison.
      5. Run ruff and the named tests.
      Stop with status=blocked if the search statement stops returning one row per hit or the aggregate row on an empty page (test_search_ranking.py covers both).
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_search_outdated.py tests/integration/test_search_ranking.py tests/integration/test_search_leaks.py tests/integration/test_tool_get_doc.py tests/integration/test_list_stale.py tests/integration/test_http_e2e.py tests/unit/test_tool_schemas.py -q passes"
      - "grep -n \"include_outdated\" src/knowledge_hub/search/sql.py src/knowledge_hub/tools/search.py matches in both files"
      - ".venv/bin/python -m ruff check . passes"
  - id: p4-mark-record-outdated-tool
    title: The mark_record_outdated tool and its PrivacyFence declaration
    depends_on: [p1-migration-0002, p3-search-and-get-doc]
    complexity: S
    touches:
      - src/knowledge_hub/tools/outdated.py
      - src/knowledge_hub/mcp/server.py
      - deploy/privacyfence-upstream.example.yaml
      - tests/integration/test_mark_record_outdated.py
      - tests/unit/test_privacyfence_declaration.py
    brief: |
      Read first: plan section 3.4, src/knowledge_hub/tools/records.py (pattern), src/knowledge_hub/mcp/errors.py, tests/integration/test_ingest_record.py (pattern for calling a tool with mcp_client), tests/unit/test_privacyfence_declaration.py.
      0. The search assertions of test_mark_and_unmark need a `deps` fixture with `embedder=FakeEmbedder()` and the `cursor_key` and `app_settings` fixtures: copy them from tests/integration/test_search_ranking.py:20-40 into the new test file.
      1. Create src/knowledge_hub/tools/outdated.py: `async def mark_record_outdated(call, id: str, outdated: bool = True, replaced_by: str = "", note: str = "")` with the six steps of plan section 3.4 and a `register(server, deps)` like records.py using `tool_guard("mark_record_outdated", deps)`. Import `parse_id` from knowledge_hub.tools.docs. Use only NOT_FOUND and BAD_ARG from mcp/errors.py (no new constants). Catch the database error with `except psycopg.errors.Error as exc` around the savepoint and compare `exc.sqlstate == "P0K07"`.
      2. mcp/server.py: `from knowledge_hub.tools import docs, labels, outdated, propose, records, search` and `outdated.register(server, deps)` after `records.register(server, deps)`.
      3. deploy/privacyfence-upstream.example.yaml: add the tool entry of plan section 3.4 under `tools:` after `ingest_record`.
      4. tests/unit/test_privacyfence_declaration.py: add "mark_record_outdated" to WRITE_TOOLS.
      5. Create tests/integration/test_mark_record_outdated.py: test_mark_and_unmark (as alice_all: seed the record with `add_record(conn, audience="all")` over `fresh_db.admin_dsn(FRESH_DB)` and give it a chunk with `add_chunked_record`, mark with a note, get_doc shows outdated true and the note, search hides it, mark with outdated false restores everything); test_replaced_by_a_same_group_record_and_a_document; test_replaced_by_other_group_missing_and_malformed_all_answer_bad_arg_replaced_by (the BAD_ARG text for name replaced_by, identical for all three); test_hidden_and_missing_record_answer_not_found_identically (bob_mgmt on an `all` record and a random uuid give the same NOT_FOUND text); test_doc_id_is_refused (BAD_ARG name id); test_note_limits (501 characters and a NUL give BAD_ARG name note); test_clearing_with_a_replaced_by_is_refused; test_a_second_holder_of_the_group_can_unmark (alice_all marks, `multi` from tests/support/principals.py, which holds `all`, unmarks); test_n8n_workflow_token_can_mark_a_record_of_its_audience (wf_token from tests/support/principals.py); test_log_line_names_the_caller (caplog contains `mark_record_outdated sub=`).
      6. Run ruff and the named tests.
      Stop with status=blocked if the tool schema fails tests/unit/test_tool_schemas.py (argument names or schema keys) — report the failing assertion.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_mark_record_outdated.py tests/unit/test_privacyfence_declaration.py tests/unit/test_tool_schemas.py -q passes"
      - "grep -n \"mark_record_outdated\" deploy/privacyfence-upstream.example.yaml matches"
      - "grep -n \"outdated.register\" src/knowledge_hub/mcp/server.py matches"
      - ".venv/bin/python -m ruff check . passes"
  - id: p5-propose-supersedes
    title: propose_doc can state which documents the new file supersedes
    depends_on: [p2-t0-supersede-indexer]
    complexity: S
    touches:
      - src/knowledge_hub/tools/propose.py
      - src/knowledge_hub/propose_render.py
      - tests/integration/test_propose_doc.py
      - tests/unit/test_propose_render.py
    brief: |
      Read first: plan section 3.5, src/knowledge_hub/tools/propose.py, src/knowledge_hub/propose_render.py, tests/unit/test_propose_render.py, tests/integration/test_propose_doc.py.
      1. propose_render.py: `render_file(doc_id, title, review_by, labels, body, supersedes=())` writes the literal line `supersedes: ["a", "b"]` (each id JSON-quoted as the `id:` line is, joined by `, `) on the line after `labels:` only when `supersedes` is not empty.
      2. propose.py: add `supersedes: list[str] = []` as the last parameter of propose_doc (keep the noqa B006 comment style of `labels`). After the label checks: refuse with `_bad("supersedes")` when it has more than 20 entries, a duplicate, an entry that fails ID_RE, or an entry equal to `id`. Pass it to render_file. Extend the round-trip tuple (the `back` comparison) with `back.supersedes` versus `supersedes`. Inside the existing `SELECT 1 FROM kh.documents` block add a query `SELECT doc_key FROM kh.documents WHERE repo = %s AND doc_key = ANY(%s)`; when the found set differs from the requested set raise `_bad("supersedes")`. When supersedes is not empty append `\n\nSupersedes: ` followed by the ids as code spans joined by `, ` to the pull request body, after the trailer. Extend the tool description with the sentence `To change an existing document keep its supersedes list: pass the supersedes that get_doc returned.`
      3. Tests: test_propose_render.py: test_render_writes_supersedes_line_only_when_given and test_rendered_supersedes_round_trips_through_parse_document. test_propose_doc.py (follow the existing fixtures and fake Forgejo bot): test_supersedes_is_written_to_the_file_and_the_pr_body, test_unknown_supersedes_id_is_bad_arg, test_supersedes_duplicate_own_id_and_too_many_are_bad_arg, test_supersedes_defaults_leave_existing_behaviour (no line in the file when empty).
      4. Run ruff and the named tests.
      Stop with status=blocked if the round-trip check rejects a valid supersedes list (that means p2's parse_document disagrees with render_file) and report both texts.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_propose_render.py tests/integration/test_propose_doc.py -q passes"
      - "grep -n \"supersedes\" src/knowledge_hub/propose_render.py src/knowledge_hub/tools/propose.py matches in both files"
      - ".venv/bin/python -m ruff check . passes"
  - id: p6-similar-items
    title: ingest_record and propose_doc return similar items
    depends_on: [p1-migration-0002, p5-propose-supersedes]
    complexity: M
    touches:
      - src/knowledge_hub/search/similar.py
      - src/knowledge_hub/tools/records.py
      - src/knowledge_hub/tools/propose.py
      - src/knowledge_hub/settings.py
      - tests/integration/test_similar_items.py
      - tests/integration/test_ingest_record.py
      - tests/integration/test_propose_doc.py
      - tests/unit/test_settings.py
    brief: |
      Read first: plan section 3.6, src/knowledge_hub/tools/records.py, src/knowledge_hub/tools/propose.py, src/knowledge_hub/settings.py, src/knowledge_hub/embed/base.py (FakeEmbedder), tests/support/seed.py (add_chunked_doc, add_chunked_record).
      1. settings.py: add `similar_limit: int = Field(3, ge=0, le=10)` and `similar_min_score: float = Field(0.5, ge=0.0, le=1.0)` to Settings (import Field from pydantic).
      2. Create src/knowledge_hub/search/similar.py with `_SIMILAR_SQL` and `similar_items` exactly as in plan section 3.6 (steps 1 to 4, the savepoint, the `asyncio.wait_for(..., EMBED_TIMEOUT_S)` around the embedding only, the broad except that logs a warning with `log.warning("similar items failed: %s", type(exc).__name__)` and returns []).
      3. records.py: import `similar_items`; on the new-insert success path only (the final `return {"id": f"rec:{key}", "existed": False}`) compute `similar = await similar_items(call, f"{title}\n\n{body[:EXCERPT_CHARS]}", audience)` and return it under `"similar"`. The two `existed: True` returns stay unchanged. Add the sentence `The reply of a new record lists up to three similar items of the same group in similar.` to the tool description.
      4. propose.py: after `pull = await bot.open_pull(...)` compute `similar = await similar_items(call, f"{title}\n\n{body[:2000]}", group, exclude_repo=repo, exclude_key=id)` and add `"similar": similar` to the returned dict. The similar items are not written into the pull request.
      5. Tests. tests/unit/test_settings.py: defaults (3 and 0.5), env override, and a ValidationError for KH_SIMILAR_LIMIT=11 and KH_SIMILAR_MIN_SCORE=1.5. New tests/integration/test_similar_items.py (define a `deps` fixture with `embedder=FakeEmbedder()`, `blobs=MemoryBlobStore()` and `forgejo_bot` built as in tests/integration/test_propose_doc.py:37-48; the fake embedder returns 1.0 for identical text, near 0 otherwise; seed items with `add_chunked_record(conn, body, vector_of=f"{title}\n\n{body}")` so the stored vector equals the embedding of the query text): test_identical_text_is_returned_with_similarity_one, test_unrelated_text_is_not_returned, test_other_group_items_are_never_returned (a caller holding all and mgmt ingesting for `all` sees no `mgmt` item even when identical), test_outdated_and_superseded_items_are_not_returned, test_one_row_per_owner_and_limit_applies (limit set to 1 through app_settings), test_limit_zero_switches_it_off, test_embedder_that_may_not_embed_the_audience_returns_empty (an embedder stub whose may_embed returns False), test_failing_query_keeps_the_record (monkeypatch `knowledge_hub.search.similar._SIMILAR_SQL` to invalid SQL: ingest_record still succeeds, the record exists in kh.records and similar is []), test_embedding_timeout_keeps_the_record (a stub embed that sleeps 1 second while the test monkeypatches `knowledge_hub.search.similar.EMBED_TIMEOUT_S` to 0.05), test_existing_record_returns_no_similar_key (the `existed: True` reply has no `similar` key), test_propose_doc_excludes_the_document_being_edited. Update the exact-dict assertions in tests/integration/test_ingest_record.py and tests/integration/test_propose_doc.py by adding the `similar` key.
      6. Run ruff and the named tests.
      Stop with status=blocked if a failing lookup aborts the caller's transaction (the record disappears): that means the savepoint is wrong; report the traceback.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_similar_items.py tests/integration/test_ingest_record.py tests/integration/test_propose_doc.py tests/unit/test_settings.py -q passes"
      - ".venv/bin/python -m pytest tests/integration/test_similar_items.py::test_failing_query_keeps_the_record -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p7-retire-docs
    title: Docs and changelog for supersede, outdated and similar items
    depends_on: [p2-t0-supersede-indexer, p3-search-and-get-doc, p4-mark-record-outdated-tool, p5-propose-supersedes, p6-similar-items]
    complexity: M
    touches:
      - README.md
      - docs/design.md
      - docs/configuration.md
      - docs/runbook.md
      - CHANGELOG.md
    brief: |
      Read first: plan sections 3.1 to 3.8, README.md, docs/design.md, docs/configuration.md, docs/runbook.md, CHANGELOG.md.
      1. README.md tools table: add the row ``| `mark_record_outdated` | Mark a raw record outdated (hidden from search) or current again. |``; change the `search` row to mention `include_outdated`; the `ingest_record` and `propose_doc` rows mention `similar`; `propose_doc` mentions `supersedes`.
      2. docs/design.md: Tools section: `Six tools` becomes `Seven tools` and `mark_record_outdated` joins the writes (`popup`); Indexer frontmatter bullet: add `supersedes` (optional list of ids, at most 20, same repo, rejected as `invalid supersedes`) and one bullet for the resolver (the five rules of plan 3.2 and the three alert texts); Search section: the `hidden` filter, `include_outdated`, the `KH_STALE_WEIGHT` weighting of included items, the `only T3 records match` behaviour; RLS matrix: the `records` row for `kh_app` reads `SELECT where can_read(audience); INSERT with check ...; UPDATE only through kh.mark_record_outdated (definer); no DELETE`; replace the sentence that says the one migration only grants (Roles and RLS matrix section) by one that names 0001 and 0002 and says 0002 also adds the definer function; add a Similar items bullet (group-local, savepoint, 5 second limit, `similarity`).
      3. docs/configuration.md: add rows for `KH_SIMILAR_LIMIT` (`3`, 0 to 10, 0 switches the lookup off) and `KH_SIMILAR_MIN_SCORE` (`0.5`, 0 to 1), and change the `KH_STALE_WEIGHT` row to `Multiplier for a stale hit or an included outdated hit.`
      4. docs/runbook.md: add the three alert texts (`supersedes unknown id <key>`, `superseded by <n> documents; using <doc_key>`, `supersedes cycle: <keys>`) with their cause and fix to the alert table; add a short rollout section with the order of plan section 3.7 (kh migrate, kh rebuild, kh check, restart PrivacyFence, create n8n tokens again for the new tool); add one sentence that a hidden document is still readable with get_doc or search include_outdated=true.
      5. CHANGELOG.md under `## [Unreleased]` (no version heading): `### Added` with one bullet each for `supersedes`, `mark_record_outdated`, `include_outdated` and the `outdated`/`replaced_by` fields, `similar` and the two settings, and migration `0002_supersede.sql` (run `kh migrate`, then `kh rebuild`; restart PrivacyFence; create n8n tokens again for the new tool; every document is re-read once, so `commit_sha`, `indexed_at` and `source_link` change once); `### Changed` with `list_stale` no longer lists superseded documents and cursors issued before this release answer `cursor does not belong to this query` once.
      6. Run the whole suite.
      Stop with status=blocked if tests/unit/test_docs_links.py fails for a link you did not add.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_docs_links.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
      - "grep -n \"Seven tools\" docs/design.md matches"
      - "grep -n \"mark_record_outdated\" README.md docs/design.md CHANGELOG.md matches in all three files"
      - "grep -n \"KH_SIMILAR_MIN_SCORE\" docs/configuration.md matches"
      - ".venv/bin/python -m ruff check . passes"
```
