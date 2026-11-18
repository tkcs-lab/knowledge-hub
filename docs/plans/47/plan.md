# Plan 47: move_item, move knowledge from one group to another

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** In Claude, a person who is a member of two groups calls `kh_move_item` with an item id, the group it is in now (`from_group`) and the group it should go to (`to_group`). PrivacyFence shows both groups in its popup. A T3 record (`rec:`) moves at once and keeps its id. A T0 document (`doc:`) opens two pull requests: one adds the file to the target group's repo, one removes it from the source group's repo; each group's approvers merge their own, and the add must be merged first. The answer lists the pull requests, and any supersede links the move dropped; the moved document gets a new id once the target repo is indexed.

**Scope:** Adds the tool `move_item`, migration `0004_move_record.sql` (two definer functions, one tombstone table, row locks in `mark_record_outdated`), `ForgejoBot.delete_file`, a lock in the embed worker, a tombstone check in `ingest_record`, docs and the changelog. Does not purge anything: git history of the old repo, past usage queries and backups keep the old copy. Not available to n8n workflows. No approver check: holding both groups is the rule.

**Security:** A move changes who can read an item, so the call carries both groups and PrivacyFence hashes them into the popup (3.1). The database function checks both groups itself (3.2). The worker and the move share an advisory lock so no chunk keeps the old group (3.3). A tombstone stops a re-sent record from reappearing in the old group (3.4).

**Review focus:** the SQL of the move function and its lock order (3.2, 3.3); the tombstone and its effect on `ingest_record` (3.4); the two-pull-request order and retry rules for documents (3.6); the refusals that keep supersede links and `replaced_by` inside one group (3.5, 3.6); the rewritten usage-log rationale in p3.

## 1. Goal

A person who belongs to two groups can move one record or document from one to the other with a single tool call, for T3 and for T0, as in [issue #47](https://github.com/tkcs-lab/knowledge-hub/issues/47): "a tool (over claude, privacyfence) to move information from group A to group B. it assumes I have write access to both groups. effective both for t3 and t0". Owner decisions (board asks, 2026-11-18): a record moves in place; a document moves with two pull requests; one tool for both tiers, one item per call; any holder of both groups may move; the signature is `move_item(id, from_group, to_group)`.

## 2. Current state

- Records are never updated or deleted by the app. `kh_app` has SELECT and INSERT on `kh.records` (`src/knowledge_hub/migrations/0001_baseline.sql:230-233`); the only update path is the definer function `kh.mark_record_outdated` (`0003_supersede.sql:22-58`), which requires `replaced_by` to be in the same group.
- `ingest_record` dedupes on `(audience, source, source_ref)` (`src/knowledge_hub/tools/records.py:17,78`).
- Chunks carry their own copy of `audience` (`0001_baseline.sql:77-95`), set by the trigger `chunks_inherit_owner` on insert; search filters on the chunk row alone. `kh_app` has no UPDATE on chunks. The worker's `_embed_record` reads the record's audience, embeds, then deletes and inserts chunks in one transaction (`src/knowledge_hub/indexer/worker.py:61-98`).
- A record insert queues an `embed_record` job through `kh.records_enqueue_embed` (`0001_baseline.sql:351-362`).
- Documents come only from `main` of a group's Forgejo repo. `propose_doc` (`src/knowledge_hub/tools/propose.py:30-140`) is the only write path: bot branch `bot/propose/<uuid>`, one file, one pull request, an open-pull cap per repo. `ForgejoBot` (`src/knowledge_hub/forgejo/bot.py:23-100`) has `blob_sha`, `create_branch`, `put_file`, `open_pull`, `list_open_pulls` and no delete. The fake Forgejo answers 405 to DELETE (`tests/support/forgejo_fake.py:234`).
- A supersede link stays inside one repo (`docs/design.md`, Indexer). Fixed error texts live in `src/knowledge_hub/mcp/errors.py:32-48`; tools are registered in `src/knowledge_hub/mcp/server.py:32-41`; the PrivacyFence declaration is `deploy/privacyfence-upstream.example.yaml`, checked by `tests/unit/test_privacyfence_declaration.py`.

## 3. Design

### 3.1 The tool

`tools/move.py`, registered as `move_item`:

```python
async def move_item(call: ToolCall, id: str, from_group: str, to_group: str) -> dict[str, Any]
```

Checks, in this order, each with the named fixed text:

1. `call.caller.principal_type != "claude_client"` -> `NOT_FOR_WORKFLOWS`.
2. `from_group == to_group` -> `BAD_ARG` with `name="to_group"`.
3. `from_group` or `to_group` not in `call.caller.audiences`, or not `call.deps.config.is_active(...)` -> `AUDIENCE_NOT_HELD`.
4. `parse_id(id)` (from `tools/docs.py`) is `None` -> `BAD_ID`. Kind `rec` -> the record move (3.2); kind `doc` -> the document move (3.6).

Description (exact): `Move a record (rec:) or a document (doc:) from from_group to to_group, two groups you hold; from_group must be the group the item is in now. A record moves at once and keeps its id. A document moves with two pull requests: one adds it to to_group's repo, one removes it from from_group's repo; each group's approvers merge their own, add first. The document gets a new doc: id when the target repo is indexed. This is not a purge: the old repo's history, past usage queries and backups keep the old copy. Claude clients only.`

Result of a record: `{"tier": "T3", "id": "rec:<uuid>", "from_group": ..., "to_group": ..., "moved": true}`. Result of a document: `{"tier": "T0", "id": "doc:<uuid>", "from_group": ..., "to_group": ..., "add_pr_url": str, "add_pr_number": int, "remove_pr_url": str, "remove_pr_number": int, "dropped_supersedes": list[str], "revived": list[str], "note": str}` (`remove_pr_url` is `""` and `remove_pr_number` `0` when the second pull request could not be opened; `add_pr_url` is `""` and `add_pr_number` `0` when the add was already merged; `note` is `""` otherwise). A record keeps its id; a document does not, because the copy indexed from the target repo is a new row.

The log line `move_item sub=<sub> id=<id> from=<from> to=<to>` is written at INFO to `knowledge_hub.tools` after a successful move (a document: after the first pull request is open).

New fixed texts in `mcp/errors.py` (update the module docstring, which says later phases never add to them, to say the catalogue grows with each tool):

| Constant | Text |
|---|---|
| `NOT_FOR_WORKFLOWS` | `this tool is not available to workflows` |
| `MOVE_WRONG_GROUP` | `the item is not in from_group` |
| `MOVE_CONFLICT` | `the target group already has an item with this source and source_ref, path or id` |
| `MOVE_LINKED` | `another item names this one as its replacement or successor; clear that first` |
| `RECORD_MOVED` | `a record with this source and source_ref was moved out of this audience` |
| `MOVE_TEXT` | `this document cannot be moved by the tool; move it by hand` |
| `MOVE_PARTIAL` | `the removal pull request could not be opened; call move_item again to open it` |

`MOVE_PARTIAL` is the value of `note`, not an error.

### 3.2 Migration `0004_move_record.sql`

Exactly this file (it is applied as `kh_migrator`):

```sql
-- 0004: move a record between groups in place; tombstones of moved records; row locks in mark_record_outdated.

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
```

The grants of `mark_record_outdated` from 0003 stay (`CREATE OR REPLACE` keeps them). Python maps these database errors: SQLSTATE `P0K08` -> `AUDIENCE_NOT_HELD`; `P0K09` -> `MOVE_WRONG_GROUP`; `P0K0A` -> `MOVE_LINKED`; `UniqueViolation` (the target group already has this `(source, source_ref)`) -> `MOVE_CONFLICT`; `ForeignKeyViolation` (the group is not in `kh.groups`) -> `AUDIENCE_NOT_HELD`. The call runs in a savepoint (`async with call.conn.transaction()`, as `tools/outdated.py:37`). A `NULL` answer is `NOT_FOUND`. A moved record keeps its labels, blob, `written_by`, `outdated` mark and note; `replaced_by` is cleared because a successor in the old group would break the same-group rule.

### 3.3 The worker lock

`_embed_record` (`indexer/worker.py`) keeps reading the audience first. At the start of its write transaction it runs `SELECT pg_advisory_xact_lock(hashtext(%s))` with `rec:<id>`, then re-reads `audience`. When the record is gone it returns; when the audience differs from the one used for `may_embed` it raises `RuntimeError("record audience changed; the job is retried")` so the job retries with its normal backoff. The move queues its own `embed_record` job, which then embeds under the target group's policy. Effect on embeddings: a move into a group that may embed sends the body to the embedder for the first time; a move into a group that may not embed drops the vectors, so the record is found by full-text search only. Both are written into `docs/design.md`.

### 3.4 The tombstone

`kh.record_moves` holds `(audience, source, source_ref)` of every record moved out of a group. `ingest_record`, after its dedupe lookup finds nothing and before it uploads the blob, runs `SELECT kh.record_moved(%s, %s, %s)`; true raises `kh_error("record_moved", errors.RECORD_MOVED)`. Only callers who hold the group learn of a tombstone. Moving a record back into a group deletes that group's tombstone (3.2). Rejected: leaving it out (a workflow that re-sends by design would put the moved information back in the old group); copying the record instead of moving it (the old group keeps reading it).

### 3.5 Records that name a record or document

- A record named in another record's `replaced_by` cannot move (`MOVE_LINKED`).
- A document whose `superseded_by` is set, or that a record names in `replaced_by` (`doc:<uuid>`), cannot move (`MOVE_LINKED`).
- A document that itself `supersedes` others moves with its `supersedes` dropped (a link stays inside one repo): the result lists them in `dropped_supersedes`, and lists in `revived` the `doc_key` of every document of the source group whose `superseded_by` is this document (they become current again once the removal is merged).

### 3.6 The document move

Order of checks in `_move_document` (everything refused before anything is written):

1. `deps.forgejo_bot is None` -> `NOT_CONFIGURED`.
2. Load the row from `kh.documents` by id under RLS: `id, repo, path, doc_key, audience, title, body, labels, review_by, blob_sha, supersedes, superseded_by`. None -> `NOT_FOUND`. `audience != from_group` -> `MOVE_WRONG_GROUP`.
3. 3.5 refusals.
4. `to_repo = config.repo_for_group(to_group)`; `from_repo = row.repo`.
5. Text rules, all with `MOVE_TEXT` except the label rule: `validate_path(path)`, `check_text(title)`, `check_text(body)`, then render `text = render_file(doc_key, title, review_by.isoformat(), labels, body, [])` and parse it back with `parse_document(text, path)`; a `Rejected` result, or a `ParsedDoc` whose `(id, title, review_by.isoformat(), labels, body)` differs from `(doc_key, title, review_by.isoformat(), labels, body)` or whose `supersedes` is not `[]`, is `MOVE_TEXT`; size over `settings.propose_max_bytes` -> `TOO_LARGE`; any label in `config.archived_label_set` -> `BAD_LABEL`. The documents these rules refuse (a `<br>` or `List<String>` in the body) are moved by hand.
6. `bot.blob_sha(from_repo, path, "main") != row.blob_sha` -> `BLOB_CHANGED`.
7. Open pulls: `bot.list_open_pulls(repo, bot_login)` in both repos. The marker is the body line `Moved-from: doc:<uuid>`.
   - A marked pull in `to_repo` and a marked pull in `from_repo`: return both as the result, open nothing.
   - A marked pull only in `to_repo`: open only the removal pull request (step 10).
   - A marked pull only in `from_repo`: `MOVE_CONFLICT`.
   - None, and `bot.blob_sha(to_repo, path, "main")` equals `git_blob_sha(text)` (the git blob hash of the rendered text, a helper of `tools/move.py`: `hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()` over `text.encode()`): the add was already merged. Open only the removal pull request (step 10), whose body then starts `The add pull request is already merged.` instead of `Merge only after`; the result has `add_pr_url ""` and `add_pr_number 0`. Skip step 8.
   - None otherwise: continue with 8.
8. `bot.blob_sha(to_repo, path, "main")` is not `None`, or `kh.documents` holds a row with `repo = to_repo AND doc_key = <doc_key>` -> `MOVE_CONFLICT`.
9. Caps: a repo that needs a new pull request and already holds `settings.propose_max_open` or more open bot pull requests -> `TOO_MANY_OPEN`. Add pull request: branch `bot/move/<uuid4>` in `to_repo` from `head_sha(to_repo, "main")`, `put_file(to_repo, branch, path, text, "Move in: <title>\n\nRequested-by: <sub>", None)`, `open_pull(to_repo, branch, "main", "Move in: <title>"[:120], body)`; the body is `Moved from group `<from_group>`.` then a blank line, the three trailer lines of `propose_doc` (`Requested-by`, `Principal`, `Written-by`) and `Moved-from: doc:<uuid>`.
10. Removal pull request: branch `bot/move/<uuid4>` in `from_repo`, `delete_file(from_repo, branch, path, "Move out: <title>\n\nRequested-by: <sub>", row.blob_sha)`, `open_pull(from_repo, branch, "main", "Move out: <title>"[:120], body)`; the body starts `Merge only after <add_pr_url> is merged; until then the document is missing from both groups' index.` then the trailer lines and `Moved-from: doc:<uuid>`.
11. A `ForgejoError` in step 9 -> `PROPOSE_FAILED` (nothing is merged; a stray branch is harmless). A `ForgejoError` in step 10 -> log it, return the result with `remove_pr_url ""`, `remove_pr_number 0` and `note = MOVE_PARTIAL`.

`ForgejoBot.delete_file(repo, branch, path, message, sha) -> dict`: `_guard_branch(branch)`, `DELETE contents/<quoted path>` with body `{"branch", "message", "sha"}`, `ok=(200,)`, returns `{"sha": <commit sha>}` and raises `ForgejoError("commit response has no sha")` when it is missing.

Pull request order is not enforced: nothing stops removal before add. The removal's text says so and the runbook says merge add first. Rejected: a follow-up job that opens the removal after the add is indexed (new job kind, new state, the bot inside the worker; kept as the next step if out-of-order merges happen).

### 3.7 PrivacyFence and consent

`deploy/privacyfence-upstream.example.yaml` gets `move_item` with `gate: popup`, `verb: move`, `effect: Moves a record at once, or opens two pull requests that move a document, between two groups you hold; not a purge.` The upstream keeps `allowed_principal_types: [claude_client, n8n_workflow]` (other tools need the workflow), so the tool itself refuses workflows (3.1 step 1). `test_privacyfence_declaration.py` lists `move_item` in `WRITE_TOOLS`.

## 4. Manual steps

### Before implementation

None.

### After implementation

**ma1-deploy-and-tick** (about 20 minutes; do it from a checkout of the feature branch before the feature pull request is merged)

1. On a checkout of `feat/move-item`, back up the database as https://github.com/tkcs-lab/knowledge-hub/blob/feat/move-item/docs/runbook.md says, then run `kh migrate` against your database. You see `0004_move_record.sql` applied once; the migration only adds a table and functions.
2. Start `kh serve` from that checkout and restart PrivacyFence with `deploy/privacyfence-upstream.example.yaml` from the branch (the header of that file says which setting points PrivacyFence at it). You see `kh_move_item` in the tool list of your Claude connection.
3. In PrivacyFence's admin page for the `kh` upstream, allow `kh_move_item` for your Claude client and open the tool once: the popup shows `id`, `from_group` and `to_group`.
4. Report back: pass or fail, one line, in the pull request.

**ma2-live-probe** (about 20 minutes)

1. In Claude, ingest a throwaway record into group A (`kh_ingest_record`), then call `kh_move_item` with that id, `from_group` A and `to_group` B. You see `moved: true`; `kh_search` as a member of B finds it and as a member of A only does not.
2. Send the same `source_ref` into A again. You see `a record with this source and source_ref was moved out of this audience`.
3. Call `kh_move_item` for a throwaway document of A. You see two pull request links. Open the two pull request links from the answer (they are pages of your Forgejo, `<forgejo url>/<org>/<repo>/pulls/<number>`), merge the add pull request as an approver of B, then the removal pull request as an approver of A. After the webhook, `kh_search` finds the document in B only.
4. Report back: pass or fail for each of the three steps, in the pull request.

## 5. Risks and open questions

- **Forgejo's DELETE contents body.** The plan assumes `{branch, message, sha}` and a 200 answer with a `commit` object. A worker that sees another shape in the fake-independent unit test stops with `status=blocked` and a `Research:` line; `ma2-live-probe` is the live check.
- **Out-of-order merges.** If the removal is merged before the add, the document leaves the index until the add is merged. The removal's text and the runbook warn; a follow-up job is the upgrade (3.6).
- **Re-embedding policy.** A move changes what is embedded (3.3). With the `http` embedder a move into an allowed group sends the body out for the first time.
- **Two marks in opposite directions.** `mark_record_outdated` on two records naming each other as successor can deadlock on the new row locks; the database aborts one and the caller sees `internal error (ref ...)`. Rare; no fix planned.
- **Not a purge.** The old repo keeps history, `usage_events` keep query text, backups keep everything. The tool description and the docs say so.
- **A worker recognises a wrong brief** when a test it must extend does not exist where the brief names it, when the SQL of 3.2 fails to apply, or when a test outside the touched files fails because of the new table or tool: it stops with `status=blocked` and reports.

## Implementation manifest

```yaml
plan_slug: move-item
feature_branch: feat/move-item
tracking_issue: 47
max_parallel: 1
manual_before: []
manual_after:
  - id: ma1-deploy-and-tick
    title: Migrate, restart the hub and PrivacyFence, allow kh_move_item
    why: CI cannot reach the production database or PrivacyFence.
  - id: ma2-live-probe
    title: Move a throwaway record and a throwaway document for real
    why: Only the real Forgejo and PrivacyFence prove the DELETE call and the popup.
verify_after_merge:
  - .venv/bin/python -m pytest -q -k move
final_checks:
  - "`tld plan deviations check --file docs/plans/47/deviations.md --plan docs/plans/47/plan.md --all` exits 0"
  - "`grep -n \"move_item\" CHANGELOG.md` matches under `## [Unreleased]` and the changelog has no new version heading"
phases:
  - id: p1-move-record
    title: Migration, record move, tool, tombstone and worker lock
    depends_on: []
    complexity: M
    touches:
      - src/knowledge_hub/migrations/0004_move_record.sql
      - src/knowledge_hub/tools/move.py
      - src/knowledge_hub/tools/records.py
      - src/knowledge_hub/indexer/worker.py
      - src/knowledge_hub/mcp/errors.py
      - src/knowledge_hub/mcp/server.py
      - deploy/privacyfence-upstream.example.yaml
      - tests/integration/test_migration_0004.py
      - tests/integration/test_move_item_record.py
      - tests/integration/test_move_embed_race.py
      - tests/integration/test_migrate_runner.py
      - tests/integration/test_migration_0002.py
      - tests/integration/test_setup_postgres.py
      - tests/integration/test_rls_policies.py
      - tests/integration/test_schema_tables.py
      - tests/integration/test_ingest_record.py
      - tests/unit/test_privacyfence_declaration.py
    brief: |
      Read first: docs/design.md (Access model, Roles and RLS matrix, Tools), src/knowledge_hub/tools/outdated.py, src/knowledge_hub/tools/records.py, src/knowledge_hub/indexer/worker.py (lines 55-100), src/knowledge_hub/migrations/0003_supersede.sql, tests/integration/test_migration_0002.py, tests/integration/test_mark_record_outdated.py, tests/support/principals.py, tests/support/seed.py.
      Run `bash scripts/ensure_test_pg.sh` and set up `.venv` as README.md Quick start says before any integration test.
      1. Create `src/knowledge_hub/migrations/0004_move_record.sql` with exactly the SQL of plan section 3.2 (copy it; change nothing).
      2. `mcp/errors.py`: add the seven constants of the table in plan 3.1 with those exact texts, and change the module docstring so it no longer says later phases never add to the texts (say that each tool adds the texts it needs).
      3. Create `tools/move.py` with `move_item(call, id, from_group, to_group)` and `register(server, deps)` following `tools/outdated.py`: the four checks of plan 3.1 in that order, the description of 3.1 verbatim, `_move_record` per 3.2 (savepoint around `SELECT kh.move_record(%s, %s, %s)` with the key, `from_group`, `to_group`; the error mapping of 3.2; `NULL` is `NOT_FOUND`; the INFO log line of 3.1; the record result dict of 3.1). A `doc` id raises `kh_error("bad_arg", errors.BAD_ARG, name="id")` for now; phase p2 replaces that branch.
      4. `mcp/server.py`: import `move` in `register_tools` and call `move.register(server, deps)`.
      5. `tools/records.py`: after `found = await _existing(...)` is `None` (line 78) and before `deps.blobs.put`, run `SELECT kh.record_moved(%s, %s, %s)` on `call.conn` with `(audience, source, source_ref)`; a true value raises `kh_error("record_moved", errors.RECORD_MOVED)`.
      6. `indexer/worker.py` `_embed_record`: at the start of the final `async with self.pool.connection() as conn, conn.transaction():` block add the advisory lock and the audience re-check of plan 3.3 (exact exception text `record audience changed; the job is retried`).
      7. `deploy/privacyfence-upstream.example.yaml`: add the `move_item` entry of plan 3.7 after `propose_doc`, and one header comment line saying that a new tool needs PrivacyFence restarted and the tool allowed on each Claude connection. `tests/unit/test_privacyfence_declaration.py`: add `"move_item"` to `WRITE_TOOLS`.
      8. Existing tests that list migrations or tables: `tests/integration/test_migrate_runner.py` (four migrations: define `MOVE = "0004_move_record.sql"`, add it to the three assertions at lines 25, 38, 54 and rename `test_three_migrations_are_shipped` to `test_four_migrations_are_shipped`), `tests/integration/test_migration_0002.py` (define `MOVE = "0004_move_record.sql"` next to `SUPERSEDE`; line 98 becomes `migrate(dsn) == [SUPERSEDE, MOVE]`), `tests/integration/test_setup_postgres.py` (the `+ migration` lines near line 80 gain `+ migration 0004_move_record.sql`), `tests/integration/test_rls_policies.py` (the `TABLES` tuple at line 15 and the list at line 218 gain `record_moves`; `DEFINERS` at line 31 gains `"move_record"` and `"record_moved"`; the new table has no grant for `kh_app` or `kh_indexer`), `tests/integration/test_schema_tables.py` (add `"record_moves": ["audience", "source", "source_ref", "moved_at"]`). Run each file and fix only what the new migration changes.
      9. New `tests/integration/test_migration_0004.py` (pattern: `test_migration_0002.py`, same `role_conn` and `_record` helpers copied in): `test_move_function_moves_record_and_chunks` (audience of record and its chunks both change; `replaced_by` cleared, outdated mark kept), `test_move_function_answers_null_for_missing_hidden_or_anonymous`, `test_move_function_refuses_unheld_target` (SQLSTATE `P0K08`), `test_move_function_refuses_same_group_and_wrong_from` (`P0K09`), `test_move_function_refuses_linked_record` (`P0K0A`), `test_move_function_writes_tombstone_and_clears_it_on_move_back`, `test_move_function_queues_embed_job`, `test_record_moved_is_false_for_unheld_group`.
      10. New `tests/integration/test_move_item_record.py` (pattern: `tests/integration/test_mark_record_outdated.py`; callers `multi` (groups `all` and `mgmt`), `alice_all`, `carol_hr`, `wf_token`): `test_move_changes_group_and_keeps_id`, `test_search_finds_moved_record_only_in_new_group`, `test_move_needs_both_groups` (`alice_all` to `mgmt` is `audience is not held by this caller or not configured`), `test_wrong_from_group`, `test_same_group_is_bad_arg`, `test_hidden_record_is_not_found` (`multi` names a record of `hr`), `test_target_with_same_source_ref_is_conflict`, `test_linked_record_is_refused`, `test_workflow_principal_is_refused`, `test_doc_id_is_refused_until_p2` (marked with a comment that p2 removes it), `test_log_line` (caplog `move_item sub=multi`).
      11. In `tests/integration/test_ingest_record.py` add `test_ingest_refuses_a_moved_out_source_ref` (move a record out of `all`, then `ingest_record` with the same `source` and `source_ref` into `all` gives `a record with this source and source_ref was moved out of this audience`) and `test_move_back_clears_the_tombstone`.
      12. New `tests/integration/test_move_embed_race.py` (read `tests/integration/test_webhook_worker.py` for how an `embed_record` job is run with a `FakeEmbedder`): an embedder whose `embed` first commits an audience change of the record through an admin connection, then returns vectors; `await worker.run_once()` must fail the job (state `failed`, `last_error` contains `record audience changed`) and leave no chunk with the old audience.
      Stop with `status=blocked` if the SQL of 3.2 does not apply on PostgreSQL 16, or if a table or migration assertion outside the files named in step 8 fails.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_migration_0004.py tests/integration/test_move_item_record.py tests/integration/test_move_embed_race.py tests/integration/test_ingest_record.py tests/integration/test_migrate_runner.py tests/integration/test_migration_0002.py -q passes"
      - ".venv/bin/python -m pytest tests/unit/test_privacyfence_declaration.py tests/unit/test_tool_schemas.py -q passes"
      - ".venv/bin/python -m pytest tests/integration/test_rls_policies.py tests/integration/test_schema_tables.py tests/integration/test_setup_postgres.py -q passes"
      - "grep -n 'move_item' deploy/privacyfence-upstream.example.yaml matches"
      - ".venv/bin/python -m ruff check . passes"
  - id: p2-move-document
    title: Document move with two pull requests and ForgejoBot.delete_file
    depends_on: [p1-move-record]
    complexity: M
    touches:
      - src/knowledge_hub/forgejo/bot.py
      - src/knowledge_hub/tools/move.py
      - tests/support/forgejo_fake.py
      - tests/unit/test_forgejo_bot.py
      - tests/integration/test_move_item_doc.py
      - tests/integration/test_move_item_record.py
    brief: |
      Read first: docs/design.md (Indexer, Tools: propose_doc), src/knowledge_hub/tools/propose.py, src/knowledge_hub/forgejo/bot.py, src/knowledge_hub/propose_render.py, tests/support/forgejo_fake.py, tests/integration/test_propose_doc.py (lines 1-60), tests/unit/test_forgejo_bot.py.
      Run `bash scripts/ensure_test_pg.sh` first.
      1. `forgejo/bot.py`: add `ForgejoBot.delete_file(repo, branch, path, message, sha)` exactly as plan 3.6 describes (after `put_file`; uses `self._json("DELETE", ..., body={"branch": branch, "message": message, "sha": sha}, ok=(200,))`).
      2. `tests/support/forgejo_fake.py` `_contents`: handle `DELETE`: the branch must exist (404), the path must exist (404), `body["sha"]` must equal the blob sha of the file (409), then `self.commit(repo.name, {path: None}, branch=branch)` and answer 200 with `{"content": None, "commit": {"sha": sha, "parents": [...]}}` in the shape of the POST/PUT answer. The 405 for other methods stays.
      3. `tests/unit/test_forgejo_bot.py` (mark each new test `pytest.mark.unit` as the file does): `test_delete_file_removes_it_on_the_branch`, `test_delete_file_stale_sha_conflicts` (HTTP 409), `test_delete_file_refuses_a_non_bot_branch` (`ForgejoRefused`).
      4. `tools/move.py`: replace the temporary `doc` branch of `move_item` with `_move_document(call, key, from_group, to_group)` implementing plan 3.5 and 3.6 steps 1 to 11 in that order. Reuse `render_file`, `check_text`, `validate_path` from `propose_render` and `parse_document`, `ParsedDoc` from `indexer/frontmatter`; add the helper `git_blob_sha(text)` of plan 3.6 to `tools/move.py`. SQL for the row: `SELECT id, repo, path, doc_key, audience, title, body, labels, review_by, blob_sha, supersedes, superseded_by FROM kh.documents WHERE id = %s`; revived: `SELECT doc_key FROM kh.documents WHERE superseded_by = %s ORDER BY doc_key`; records naming the document: `SELECT 1 FROM kh.records WHERE replaced_by = %s` with `doc:<uuid>`. Catch `(ForgejoError, KeyError, TypeError)` as `propose_doc` does.
      5. In `tests/integration/test_move_item_record.py` delete `test_doc_id_is_refused_until_p2`.
      6. New `tests/integration/test_move_item_doc.py` (fixtures as `test_propose_doc.py`: `FakeForgejo` with repos `kb-all` and `kb-mgmt`, `deps.forgejo_bot`; this file's `seeded` documents come from `tests/support/seed.py` `add_doc` with `repo`, `path`, `doc_key`, `audience`, `blob_sha` equal to `blob_sha(<file text>)` and the same text committed to the fake repo; caller `multi`). Tests: `test_move_opens_add_and_remove_pulls` (the add pull in `kb-mgmt` holds the rendered file on a `bot/move/` branch, title `Move in: <title>`, body ends with `Moved-from: doc:<uuid>`; the removal pull in `kb-all` has a branch without the file, body starts `Merge only after`; `main` of both repos untouched), `test_result_lists_dropped_supersedes_and_revived`, `test_wrong_from_group`, `test_target_path_exists_is_conflict`, `test_target_doc_key_in_use_is_conflict`, `test_stale_blob_is_refused`, `test_superseded_document_is_linked`, `test_document_named_by_a_record_is_linked`, `test_html_in_body_is_move_text`, `test_archived_label_is_bad_label`, `test_second_call_returns_the_open_pulls` (no new pull opened), `test_only_the_add_pull_open_opens_the_removal`, `test_only_the_removal_pull_open_is_conflict`, `test_removal_failure_returns_partial` (monkeypatch `deps.forgejo_bot.delete_file` to raise `ForgejoError`: result has `remove_pr_url == ""` and the note `the removal pull request could not be opened; call move_item again to open it`), `test_add_failure_is_propose_failed` (monkeypatch `deps.forgejo_bot.put_file` to raise `ForgejoError`; the answer is `could not open the pull request`), `test_retry_after_the_add_is_merged_opens_only_the_removal` (commit the rendered file to `kb-mgmt` main and open no pull requests: the result has `add_pr_url == ""`, a removal pull request exists whose body starts `The add pull request is already merged.`), `test_open_pull_cap` (`KH_PROPOSE_MAX_OPEN` reached in the target repo gives `too many open proposals in this repo`), `test_not_configured_without_bot`, `test_log_line`.
      Stop with `status=blocked` if the real shape of Forgejo's DELETE answer is needed and cannot be settled from the fake: report `Research: <the one question>`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_forgejo_bot.py tests/integration/test_move_item_doc.py tests/integration/test_move_item_record.py -q passes"
      - ".venv/bin/python -m pytest tests/integration/test_propose_doc.py tests/unit/test_privacyfence_declaration.py -q passes"
      - "grep -n 'def delete_file' src/knowledge_hub/forgejo/bot.py matches"
      - ".venv/bin/python -m ruff check . passes"
  - id: p3-retire-docs
    title: Docs and changelog
    depends_on: [p2-move-document]
    complexity: S
    touches:
      - docs/design.md
      - docs/runbook.md
      - README.md
      - CHANGELOG.md
    brief: |
      Read first: docs/design.md (Access model, Roles and RLS matrix, Tools), docs/runbook.md (lines 60-130), README.md, CHANGELOG.md (Unreleased and 0.4.0), plan sections 3.1 to 3.7 of docs/plans/47/plan.md.
      1. `docs/design.md`, Tools: change `Eight tools` to `Nine tools`, add `move_item` to the writes list and add a bullet for it with the facts of plan 3.1, 3.2, 3.5 and 3.6 in the style of the `mark_record_outdated` bullet (arguments, who may call, what moves, the refusals, the two pull requests and their order, `dropped_supersedes` and `revived`).
      2. `docs/design.md`, Roles and RLS matrix: the `records` row gains "UPDATE only through the definer functions `kh.mark_record_outdated` and `kh.move_record`"; add a row for `record_moves` (no grant for `kh_app` or `kh_indexer`; `kh.record_moved` answers only for a group the caller holds); in the paragraph on `usage_events` replace "Records cannot be updated or deleted, and `kh setup apply --prune` refuses to remove a group that still has records, so this holds over time." with: "A key whose record or document left the reader's groups is dropped by the row-level join in `usage`, and a moved record keeps its id, so no reader learns of an item they cannot read."
      3. `docs/design.md`: add the section "Moving an item" (the tombstone and `ingest_record` refusing a moved-out `source_ref`, the worker lock and the re-embed policy of plan 3.3 including the two effects, "a move is not a purge") and add to Accepted residuals: "A move is not a purge: the old repo's history, past usage queries and backups keep the old copy. The two pull requests of a document move are not ordered; merge the add first.". Leave the Trust section unchanged. Put "Moving an item" after the Tools section and before "Deviation from the PrivacyFence contract", and say that a moved document gets a new `doc:` id while a record keeps its `rec:` id.
      4. `docs/runbook.md`: a section "Moving an item between groups" (how to call `kh_move_item`; the popup shows both groups; for a document merge the add pull request as an approver of the target group, then the removal as an approver of the source group; a retry returns the open pull requests; refusals and what to do: moved-out `source_ref`, linked item, text the tool cannot move), and in the n8n token section one sentence that `kh_move_item` is for Claude clients only and workflows are refused. A rollout paragraph: `kh migrate`, restart `kh serve` and PrivacyFence, allow the new tool; recreate the n8n tokens only if PrivacyFence asks for it.
      5. `README.md`: add the `move_item` row to the Tools table: `| \`move_item\` | Move a record at once, or a document with two pull requests, from one group you hold to another. |`.
      6. `CHANGELOG.md` under `## [Unreleased]`, a `### Added` list: the tool `move_item` (`kh_move_item`, gate `popup`, Claude clients only); migration `0004_move_record.sql` (run `kh migrate`; restart PrivacyFence and allow the tool); `ForgejoBot.delete_file`; `ingest_record` refuses a `source_ref` that was moved out of the group (`a record with this source and source_ref was moved out of this audience`). A `### Changed` list: the embed worker re-checks a record's group under a lock; `mark_record_outdated` takes row locks. No version heading.
      7. Run `.venv/bin/python -m pytest tests/unit/test_docs_links.py -q` and fix any link it reports.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_docs_links.py -q passes"
      - "grep -n 'Nine tools' docs/design.md matches"
      - "grep -n 'move_item' README.md CHANGELOG.md docs/runbook.md matches in all three files"
      - "grep -n '^## \\[' CHANGELOG.md shows no heading above `## [Unreleased]`"
      - ".venv/bin/python -m ruff check . passes"
```
