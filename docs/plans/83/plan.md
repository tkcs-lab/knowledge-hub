# Plan 83: removing T3 records that the source removed

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** An n8n feed that mirrors a folder (the Drive feed) can remove what the folder lost: with a hub key it calls the new tool `delete_records` with `audience`, `source` and `source_refs` (exact refs, or `prefix: true` for every part of a file), up to 500 per call. The records leave search at once and `get_doc` shows them as outdated with the note `removed from source`; if the file comes back, `ingest_record` with the same `source_ref` restores the record. After `KH_RECORD_PURGE_DAYS` (default 30) `kh daily` deletes the row, its chunks and its S3 body for good. An administrator undoes a wrong run with `kh records restore --audience G --source S --by EMAIL`.

**Scope:** Migration `0008_record_delete.sql` (a removal reason on records, four definer functions, `mark_record_outdated` and `move_record` redefined), the tool `delete_records`, a restore in `ingest_record`, the purge in `kh daily`, the setting, the `kh records restore` command, docs and changelog. Unchanged: dataset tools, T0 and T0a, search, the panel. Visible side effects: `mark_record_outdated` and `kh move` refuse a record being purged or removed from its source, a person's mark cancels a purge, `ingest_record` can answer `restored: true` or refuse a purging record, and the `kh daily` line gains `; records purged: N`. `docs/feeds.md` gets the Drive recipe; the n8n workflow changes by hand (follow-up r2). No sync tool for records.

**Security:** `delete_records` hides data, so it is for hub keys only, holds to the key's group, refuses `dataset:` sources, hides at most 500 records per call and, unless `force` is true, at most the larger of 50 and 20 percent of a source per 24 hours. The purge is the only hard delete and runs as `kh_indexer` through two definer functions, and the claim refuses a grace period under one day. Hidden content stays readable through `get_doc` until the purge, and backups are not purged.

**Review focus:** the purge claim state and its order (3.4); re-sent deletions that must not reset the 30-day clock, and the 24-hour guard (3.2); the restore inside `ingest_record` (3.3); the Drive rules for what to send (3.7).

## 1. Goal

Today nothing deletes a T3 record: `mark_record_outdated` only hides one, and the Drive feed's notes list deleted files and old versions of changed files as not handled. After this change a feed removes the records of what its source removed, and the hub frees the storage after a grace period, so a folder and the hub stay in step. The owner chose "hide at once, delete for good after 30 days". Tracking issue: [tkcs-lab/knowledge-hub#83](https://github.com/tkcs-lab/knowledge-hub/issues/83).

## 2. Current state

- `kh.records` (`migrations/0001_baseline.sql:58`, columns added in 0003 and 0007). `kh_app` can SELECT and INSERT only; `kh_indexer` can SELECT only; chunks cascade on delete (`0001:80`). `mark_record_outdated` and `move_record` are definer functions (`0007_datasets.sql:229` and `:271`).
- `ingest_record` (`src/knowledge_hub/tools/records.py:40`) dedupes on `(audience, source, source_ref)` and answers `existed: true` without looking at the outdated mark (`_existing`, line 25).
- Datasets already have `delete_rows`, `finish_sync` and a reason on their rows (`datasets/rows.py:262`). Search hides outdated records through the `hidden` CTE (`search/sql.py:111`).
- `kh daily` (`cli.py:110`, `jobs/daily.py`) purges identity rows, usage events and dataset history through `kh_indexer`. `Deps.blobs` is `None` when S3 is not configured (`deps.py:78`).
- Tool lists to extend: `KEY_TOOLS` (`auth/keys.py:19`, sorted, checked against the registered tools by `tests/unit/test_hub_keys.py:60`) and `KEY_ONLY_TOOLS` (`auth/bearer.py:14`, checked by `tests/unit/test_bearer_middleware.py:207`).
- The Drive feed (skill `n8n-drive-to-kh-t3-feed`, workflow `V2X standards Drive to knowledge hub T3`, id `t8s6CGCmeXqXaQ2Q`) ingests with source `gdrive` and `source_ref` `<fileId>@<modified_time>` plus `#pNofM` for parts. It keeps an append-only Grist manifest and skips files already in it, so it cannot resend every file on each run.
- The highest SQLSTATE in use is `P0K0D`.

## 3. Design

### 3.1 What was chosen, and what was rejected

**Chosen: delete by reference, hide first, purge later.** A hub-key tool names the `source_ref`s to remove (exact, or by prefix). The feed already knows what is gone: manifest entries whose file is no longer in the listing, and older versions of a changed file. The hub changes only records of one `(audience, source)` that the key's group holds.

Rejected:

- **A present-list sync (like `finish_sync` for rows).** The feed would send the key of every file present, the hub would hide the rest, and a percentage guard would catch a truncated listing. It needs a place for a file key inside `source_ref` (a Drive-specific split at `@`, or a new column) and a sync table, and the feed must page through the whole folder on every run. Kept as the follow-up if wrong diffs turn up in practice; the 24-hour guard (3.2) and the restore (3.6) cover the risk until then.
- **Delete by record id.** The feed would have to store the ids that `ingest_record` returns. Parts and versions make it worse. Rejected.
- **Hard delete at once, or hide forever.** The owner chose hide, then purge.
- **Reusing `mark_record_outdated` for the feed.** One id at a time, no group-wide match, and no way to tell a feed's removal from a person's mark, which the purge must not touch.

### 3.2 Migration `0008_record_delete.sql`

One new column `outdated_reason`: `''` (live, or hidden by a person), `'delete'` (hidden by `delete_records`, restorable, purged after the grace period) or `'purge'` (claimed by the purge; nothing may restore it). A person's mark never gets purged. Write the file with exactly these parts, in this order. Header line: `-- 0008: removing T3 records: outdated_reason, delete_records, restore_records and the purge.`

```sql
ALTER TABLE kh.records
    ADD COLUMN outdated_reason text NOT NULL DEFAULT '',
    ADD CONSTRAINT records_outdated_reason CHECK (outdated_reason IN ('', 'delete', 'purge')),
    ADD CONSTRAINT records_reason_needs_outdated CHECK (outdated_reason = '' OR outdated),
    ADD CONSTRAINT records_reason_not_row CHECK (dataset_row = '' OR outdated_reason = '');
CREATE INDEX records_removed_idx ON kh.records (outdated_at) WHERE outdated_reason <> '';
```

```sql
CREATE FUNCTION kh.delete_records(aud text, src text, refs text[], by_prefix boolean, force boolean)
    RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    who    text := kh.caller_sub();
    ids    uuid[];
    total  bigint;
    recent bigint;
    lim    bigint;
BEGIN
    IF who IS NULL OR NOT kh.can_read(aud) THEN
        RETURN NULL;                       -- hidden and anonymous answer the same
    END IF;
    IF left(src, 8) = 'dataset:' THEN
        RAISE EXCEPTION 'record mirrors a dataset row' USING ERRCODE = 'P0K0B';
    END IF;
    WITH hit AS (
        SELECT r.id FROM kh.records r
         WHERE r.audience = aud AND r.source = src AND r.dataset_row = '' AND r.outdated_reason = ''
           AND CASE WHEN by_prefix
                    THEN EXISTS (SELECT 1 FROM unnest(refs) p WHERE starts_with(r.source_ref, p))
                    ELSE r.source_ref = ANY (refs) END
         ORDER BY r.id
           FOR NO KEY UPDATE OF r)
    SELECT coalesce(array_agg(id), '{}') INTO ids FROM hit;
    IF cardinality(ids) > 500 THEN
        RAISE EXCEPTION 'more than 500 records' USING ERRCODE = 'P0K0E';
    END IF;
    IF cardinality(ids) = 0 THEN
        RETURN 0;
    END IF;
    IF NOT force THEN
        SELECT count(*) INTO total FROM kh.records
         WHERE audience = aud AND source = src AND dataset_row = '' AND NOT outdated;
        SELECT count(*) INTO recent FROM kh.records
         WHERE audience = aud AND source = src AND outdated_reason = 'delete'
           AND outdated_at > now() - interval '24 hours';
        lim := greatest(50, (total + recent) / 5);
        IF recent + cardinality(ids) > lim THEN
            RAISE EXCEPTION 'shrink % %', recent + cardinality(ids), lim USING ERRCODE = 'P0K10';
        END IF;
    END IF;
    UPDATE kh.records
       SET outdated = true, outdated_at = now(), outdated_by = who,
           outdated_note = 'removed from source', outdated_reason = 'delete'
     WHERE id = ANY (ids);
    RETURN cardinality(ids);
END $$;
```

Why it is shaped this way:

- Only rows with `outdated_reason = ''` are changed and counted. The Drive manifest is append-only, so the feed sends the same gone files on every run; rows already `'delete'` or `'purge'` are left alone, so a re-sent call neither resets the 30-day clock nor eats the 500 cap, and it answers `0`.
- The prefix match is `starts_with`, never `LIKE`: Drive ids contain `_`, which `LIKE` reads as a wildcard.
- The rows are locked in id order before the update, so two overlapping calls cannot deadlock.
- A manually hidden record (reason `''`, outdated true) is taken over: its note becomes `removed from source` and it will be purged. A person who wants to keep it marks it again (3.2, `mark_record_outdated`).
- The guard counts what `delete_records` hid in the last 24 hours plus this call, against the larger of 50 and a fifth of the source's live records plus those hidden in the last 24 hours (so the limit does not grow as records are hidden). Its message is `shrink <n> <limit>`; the tool reads the two numbers from it.

```sql
CREATE FUNCTION kh.restore_records(aud text, src text, refs text[], by_prefix boolean, since timestamptz)
    RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    ids uuid[];
BEGIN
    IF kh.caller_sub() IS NULL OR NOT kh.can_read(aud) THEN
        RETURN NULL;
    END IF;
    WITH hit AS (
        SELECT r.id FROM kh.records r
         WHERE r.audience = aud AND r.source = src AND r.dataset_row = '' AND r.outdated_reason = 'delete'
           AND (since IS NULL OR r.outdated_at >= since)
           AND (refs IS NULL OR CASE WHEN by_prefix
                    THEN EXISTS (SELECT 1 FROM unnest(refs) p WHERE starts_with(r.source_ref, p))
                    ELSE r.source_ref = ANY (refs) END)
         ORDER BY r.id
           FOR NO KEY UPDATE OF r)
    SELECT coalesce(array_agg(id), '{}') INTO ids FROM hit;
    UPDATE kh.records
       SET outdated = false, outdated_at = NULL, outdated_by = '', outdated_note = '',
           outdated_reason = '', replaced_by = ''
     WHERE id = ANY (ids);
    RETURN cardinality(ids);
END $$;
```

`refs` null means every `'delete'` record of the source (the administrator's bulk undo); only `'delete'` rows are restored, never `'purge'`.

```sql
CREATE FUNCTION kh.records_purge_claim(days integer, lim integer, skip uuid[])
    RETURNS TABLE (rec_id uuid, rec_blob_key text)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
BEGIN
    IF days < 1 OR lim < 1 OR lim > 1000 THEN
        RAISE EXCEPTION 'bad purge arguments' USING ERRCODE = 'P0K11';
    END IF;
    RETURN QUERY
    WITH due AS (
        SELECT r.id FROM kh.records r
         WHERE r.dataset_row = '' AND r.id <> ALL (skip)
           AND (r.outdated_reason = 'purge'
                OR (r.outdated_reason = 'delete' AND r.outdated_at < now() - make_interval(days => days)))
         ORDER BY r.outdated_at, r.id
         LIMIT lim
           FOR UPDATE SKIP LOCKED),
    upd AS (
        UPDATE kh.records r SET outdated_reason = 'purge' FROM due WHERE r.id = due.id
        RETURNING r.id, r.blob_key)
    SELECT upd.id, upd.blob_key FROM upd;
END $$;

CREATE FUNCTION kh.records_purge_delete(ids uuid[]) RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = kh, pg_temp
    AS $$
DECLARE
    rid  uuid;
    gone uuid[];
    n    integer;
BEGIN
    FOR rid IN SELECT x FROM unnest(ids) AS x ORDER BY x LOOP
        PERFORM pg_advisory_xact_lock(hashtext('rec:' || rid::text));
    END LOOP;
    gone := ARRAY(SELECT r.id FROM kh.records r
                   WHERE r.id = ANY (ids) AND r.outdated_reason = 'purge' AND r.dataset_row = ''
                   ORDER BY r.id);
    DELETE FROM kh.index_jobs
     WHERE kind = 'embed_record' AND state IN ('queued', 'failed') AND payload ->> 'id' = ANY (gone::text[]);
    UPDATE kh.records SET replaced_by = ''
     WHERE replaced_by = ANY (ARRAY(SELECT 'rec:' || g::text FROM unnest(gone) AS g));
    DELETE FROM kh.records WHERE id = ANY (gone);
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;
```

The claim and the delete are two calls so the S3 body can go between them. A claimed row sits in `'purge'`: a restore cannot reach it, so a record that was just restored can never lose its body, and a blob that fails to delete leaves the row in `'purge'` for the next run. Chunks go with the row (`ON DELETE CASCADE`).

Then redefine two functions with `CREATE OR REPLACE`, copied from `0007_datasets.sql` (`move_record` lines 229 to 270, `mark_record_outdated` lines 271 to 311), with only these edits:

- `mark_record_outdated`: after the dataset-row check add `IF EXISTS (SELECT 1 FROM kh.records WHERE id = rec AND outdated_reason = 'purge') THEN RAISE EXCEPTION 'record is being purged' USING ERRCODE = 'P0K0F'; END IF;`, and in the final `UPDATE` end the `SET` list with the lines `replaced_by   = CASE WHEN flag THEN successor ELSE '' END,` and `outdated_reason = ''` as the last two items of the `SET` list (the first gets a trailing comma, the second has none; a person's mark, on or off, takes the record out of the purge).
- `move_record`: after the dataset-row check add `IF EXISTS (SELECT 1 FROM kh.records WHERE id = rec AND outdated_reason <> '') THEN RAISE EXCEPTION 'record was removed from its source' USING ERRCODE = 'P0K12'; END IF;`.

Grants, at the end of the file:

```sql
REVOKE EXECUTE ON FUNCTION kh.delete_records(text, text, text[], boolean, boolean) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION kh.restore_records(text, text, text[], boolean, timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION kh.records_purge_claim(integer, integer, uuid[]) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION kh.records_purge_delete(uuid[]) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION kh.move_record(uuid, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION kh.mark_record_outdated(uuid, boolean, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kh.delete_records(text, text, text[], boolean, boolean) TO kh_app;
GRANT EXECUTE ON FUNCTION kh.restore_records(text, text, text[], boolean, timestamptz) TO kh_app;
GRANT EXECUTE ON FUNCTION kh.records_purge_claim(integer, integer, uuid[]) TO kh_indexer;
GRANT EXECUTE ON FUNCTION kh.records_purge_delete(uuid[]) TO kh_indexer;
GRANT EXECUTE ON FUNCTION kh.move_record(uuid, text, text) TO kh_app;
GRANT EXECUTE ON FUNCTION kh.mark_record_outdated(uuid, boolean, text, text) TO kh_app;
```

The new SQLSTATEs: `P0K0E` more than 500 records, `P0K0F` record is being purged, `P0K10` shrink guard, `P0K11` bad purge arguments, `P0K12` record was removed from its source. Fixed texts for the hub (new constants in `mcp/errors.py`):

- `RECORDS_CAP = "this call would hide more than 500 records; split it"`
- `RECORDS_SHRINK = "delete_records would hide {n} records of this source within 24 hours, more than the limit of {limit}; repeat with force=true if that is right"`
- `RECORD_PURGING = "this record is being deleted; send it again after the next kh daily"`
- `MOVE_REMOVED = "the record was removed from its source; restore it first (kh records restore)"`

### 3.3 The tool `delete_records` and the restore in `ingest_record`

`delete_records(call, audience: str, source: str, source_refs: list[str], prefix: bool = False, force: bool = False)` in the new module `tools/record_delete.py`, registered in `mcp/server.py`. Checks, in this order, with the error each gives:

1. the caller is not an `n8n_workflow`: `kh_error("only_workflows", errors.ONLY_WORKFLOWS)`;
2. the audience is not held or not active: `AUDIENCE_NOT_HELD`;
3. `source` empty, longer than 200 characters, holding NUL, or starting with `dataset:`: `BAD_ARG` with `name="source"`;
4. `source_refs` empty, an entry that is not a string or holds NUL, an exact entry outside 1 to 200 characters, or (with `prefix`) an entry outside 8 to 200 characters: `BAD_ARG` with `name="source_refs"`; more than 500 entries: `TOO_MANY_ROWS`.

Then `SELECT kh.delete_records(%s, %s, %s, %s, %s)` inside `call.conn.transaction()` (a savepoint, as `tools/outdated.py:38` does). SQLSTATE `P0K0E` gives `kh_error("records_cap", RECORDS_CAP)`, `P0K10` gives `kh_error("records_shrink", RECORDS_SHRINK, n=..., limit=...)` with the two numbers read from `exc.diag.message_primary.split()[1:3]`, `P0K0B` gives `BAD_ARG` `name="source"`, a null result gives `AUDIENCE_NOT_HELD`. The reply is `{"outdated": n}`. One line logs `log.info("delete_records sub=%s audience=%s source=%s refs=%d prefix=%s outdated=%d", call.caller.sub, audience, source, len(source_refs), prefix, n)` on the logger `knowledge_hub.tools` (as `tools/outdated.py` does). The description text: `Hide the records of a source that the source no longer has (hub keys only): by exact source_ref, or with prefix=true by the start of source_ref. Hidden records leave search and are deleted for good after the purge period. Sending a source_ref again with ingest_record restores it. At most 500 refs per call; more than a fifth of a source within 24 hours needs force=true.`

`ingest_record` (`tools/records.py`): `_existing` returns `(id, reason)` instead of the id, reading `outdated_reason` too. At the place where `found` is used (line 82):

- reason `''`: unchanged, `{"id": found, "existed": True}`;
- reason `'delete'`: run `SELECT kh.restore_records(%s, %s, ARRAY[%s], false, NULL)` (audience, source, source_ref) in a savepoint, read the record again with `_existing`; reason `''` now: return `{"id": ..., "existed": True, "restored": True}`; the record gone (a purge finished in between): go on to the insert path; reason `'purge'`: raise `kh_error("record_purging", RECORD_PURGING)`;
- reason `'purge'`: raise `kh_error("record_purging", RECORD_PURGING)`.

The description gains one sentence: `A record that delete_records hid is restored when it is sent again.` `tools/outdated.py` maps SQLSTATE `P0K0F` to `kh_error("record_purging", RECORD_PURGING)`. `delete_records` joins `KEY_TOOLS` (first entry: it sorts before `delete_rows`) and `KEY_ONLY_TOOLS`.

### 3.4 The purge

`jobs/daily.py`: `async def purge_records(indexer_pool, blobs, days: int) -> tuple[int, int]` returns `(purged, blob_failures)`. It keeps a set `failed` of record ids and runs up to 20 rounds. Each round: in its own `async with indexer_pool.connection() as conn, conn.transaction():` run `SELECT rec_id, rec_blob_key FROM kh.records_purge_claim(%s::int, 200, %s::uuid[])` with `days` and `list(failed)` (the claim commits when the block ends); stop when it returns no row. For each claimed row delete the S3 object with `await blobs.delete(key)` (skip an empty key; a missing object is not an error because S3 answers success); on any exception log a warning with the record id (never the key) and add the id to `failed`. Then, in a second, later transaction and only when some blob went, run `SELECT kh.records_purge_delete(%s::uuid[])` with the ids whose blob went (UUID objects) and add the result to `purged`. `blob_failures` is `len(failed)`, so a record is counted once however many rounds there are. Failed ids are passed to the next claim so they are not claimed again in the same run; they stay in `'purge'` and are claimed again by tomorrow's run.

`cli.py` `_daily`: after the dataset history purge, when `deps.blobs is None` the line gains `; records purged: skipped (no blob store)`; otherwise `; records purged: N`, plus `; record blobs failed: M` only when M is above 0. Setting `record_purge_days: int = Field(30, ge=1, le=3650)` (`KH_RECORD_PURGE_DAYS`) in `settings.py` next to `dataset_history_days`.

Order and why: the claim moves a row to `'purge'` in its own committed transaction, the blob goes, and only then the row. The other order (blob first, then row) could delete the body of a record that was restored in between. Rows `kh.records_purge_delete` removes drop their queued and failed embed jobs, and the embed worker already returns quietly for a record that is gone (`indexer/worker.py:91`).

### 3.5 Records left pointing at a purged record

`replaced_by` is plain text with no foreign key. A record that names a purged record as its replacement gets `replaced_by = ''` inside `kh.records_purge_delete` (3.2). Only outdated records can have a `replaced_by` (the check constraint of 0003), so the constraint still holds.

### 3.6 The administrator's undo: `kh records restore`

`admin/records_cmd.py`, registered in `admin/cli.py` (`COMMAND_MODULES`): `kh records restore --audience G --source S [--prefix P] [--since ISO] --by EMAIL` (plus the common options `add_common(parser, mutating=False)` as `move_cmd` uses). The module follows `admin/move_cmd.py`: the administrator must be in `config.admins` (exit 1 with `records: <email> is not an administrator`), the group must match `GROUP_RE` (exit 2), the work runs as `Caller(sub=f"admin:{email}", principal_type="admin", audiences=frozenset({G}), ...)` inside `caller_tx(deps.app_pool, caller)`. `do_restore(deps, email, audience, source, prefix, since) -> dict` calls `kh.restore_records(aud, src, refs, true, since)` with `refs = [prefix]` when `--prefix` is given, else `NULL` with `by_prefix` false, and returns `{"restored": n}`; the command prints it as sorted JSON. `--since` is an ISO 8601 time with a time zone. The module defines `_run(args) -> int` (async) and `_restore(args) -> int` (`asyncio.run(_run(args))`, the `set_defaults(func=_restore)` target). `_run` checks in this order: `--since` first (`datetime.fromisoformat`; unparsable gives `records: --since is not a valid time` and one without a time zone gives `records: --since needs a time zone`, both exit 2, before any config is read), then `args.config is None` (`records: --config or KH_CONFIG_FILE is required`, exit 2), then the administrator check (exit 1), then the group name (exit 2), then the work; a `KhError` prints `records: <text>` and exits 1. `kh records ...` must also be routed by `cli.py` (3.6 step in p3). `admin/move.py` maps SQLSTATE `P0K12` to `kh_error("move_removed", MOVE_REMOVED)`.

### 3.7 What the docs say

Literal text to place (the phase brief names each file):

- **`docs/feeds.md`**, new section `## Removing what the source removed (T3 records)` after the incremental and full runs of datasets are described, containing: the `delete_records` arguments and the reply; that a call only touches records of the key's group and `source`, hides what is still live, answers `0` for a record it already hid, and never restarts the 30-day clock; the rules for a folder feed (the Drive feed is the example): (1) work from the feed's own manifest, never from a partial picture: when any folder listing of the run failed, or the listing is empty, send no deletion; (2) for a file that is gone from the listing, send the prefix `<fileId>@<modified_time>` of each manifest version exactly as stored (it also matches the `#pNofM` parts), with `prefix: true`, then append a manifest row with status `deleted`; (3) for a changed file, after the new version went in, send the prefix of each older version the same way, never the bare `<fileId>@` which also matches the new one; (4) `deleted` is not a skip status: a key whose newest manifest row is `deleted` counts as new, so a file that returns is sent again and the hub restores it; (5) a call that the hub refuses with the shrink text is a stop sign, not something to retry with `force`; (6) `kh key create --tools` must list `delete_records`. Also the answers table gains rows for the three new texts, and the existing `kh key create` line lists `delete_records`.
- **`docs/design.md`**: the tools paragraph says fifteen tools and lists `delete_records` among the hub-key tools; the records rows of the schema section say that records are never updated or deleted by `kh_app`, that `kh.delete_records` and `kh.restore_records` are definer functions for it and that the purge functions are `kh_indexer`'s only; a new bullet `Removal of records` with the three states of `outdated_reason`, the 24-hour guard, the claim-then-blob-then-row purge order and the `ingest_record` restore; the accepted residuals gain: hidden content stays readable through `get_doc` and `include_outdated` until the purge; backups, usage history and S3 object versions (when versioning is on) keep copies, so the purge is not a backup purge; `delete_records` overwrites the note of a hidden record and a person's later `mark_record_outdated` cancels the purge of it; a record that named a purged record as replacement loses that pointer.
- **`docs/configuration.md`**: a row `KH_RECORD_PURGE_DAYS`, default `30`, 1 to 3650, days `kh daily` keeps records that `delete_records` hid before it deletes the row, the chunks and the S3 body; the `kh daily` row ends with `; records purged: N` (or `; records purged: skipped (no blob store)`, and `; record blobs failed: M` when above 0); a `kh records restore` row in the commands table.
- **`docs/runbook.md`**: the daily job line mentions the record purge; a routine task `Undo a wrong removal` with the restore command, and one `Find records pending deletion` (`search` with `include_outdated=true` shows them; a record that stays in the purge because its S3 body cannot be deleted shows as `record blobs failed: M` in the `kh daily` line, and `ingest_record` of it answers that the record is being deleted until the store works again; `get_doc` shows `note` `removed from source`).
- **`README.md`**: the tools table gains a `delete_records` row, the `kh` commands table gains `kh records`.
- **`CHANGELOG.md`**: entries under `## [Unreleased]` only.

## 4. Manual steps

### Before implementation

None.

### After implementation

None. Everything that only a person can do needs a published release, so it happens after the pull request merges and is listed here as the release follow-up (it is not a gate of the pull request; the final reply of this plan and the issue body carry it).

### Release follow-up (after a release containing this change)

**r1. Upgrade the hub and give the feed key the new tool** (about 20 minutes).

1. Pull the new image tag as the [deployment guide](https://github.com/tkcs-lab/knowledge-hub/blob/feat/record-removal/docs/deployment.md) describes for upgrades.
2. Run `kh migrate`. It prints `applied 0008_record_delete.sql`. Restart `kh serve`.
3. Run `kh daily`. Its line ends with `records purged: 0`.
4. Run `kh key list` and look at the `tools` column of the key that the Drive feed uses. If it lists `ingest_record` without `delete_records`, create a replacement: `kh key create --group <group of the feed> --tools <the old tools>,delete_records --name <old name>-2 --out feed.key`. Put the part after `key: ` into the n8n credential of the feed (**Credentials → kh-all** on [n8n](https://n8n.tkcs.name)), never into a chat, then delete `feed.key` and run `kh key revoke <old id>`.
5. Report in issue #83: `r1: pass (0008 applied, daily prints records purged, feed key allows delete_records)` or `r1: fail (<step and message>)`.

**r2. Teach the Drive feed to send deletions** (about 45 minutes). Open the workflow `V2X standards Drive to knowledge hub T3` ([n8n](https://n8n.tkcs.name/workflow/t8s6CGCmeXqXaQ2Q)). The rules are the ones in the new section of [docs/feeds.md](https://github.com/tkcs-lab/knowledge-hub/blob/feat/record-removal/docs/feeds.md).

1. After the folder walk, add a Code node `Find Removed` that reads the manifest keys (`<fileId>@<modified_time>`, status `ingested` or `no_text`, newest row per key) and the file ids of the current listing, and emits (a) for each manifest key whose file id is not in the listing, that key as a prefix, and (b) for each manifest key whose file id is in the listing and for which a newer manifest key of the same file with status `ingested` exists, that older key as a prefix. The node emits nothing when any folder listing failed or the listing is empty.
2. Add an MCP Client node `Delete Records` (endpoint `https://kh.tkcs.name/mcp`, credential `kh-all`) calling `delete_records` with `audience` as confirmed for the feed (sticky note beside Build Records), `source` `gdrive`, `source_refs` the emitted prefixes in chunks of 500 and `prefix` true.
3. After each successful call add one manifest row per prefix with status `deleted` (Grist `Manifest` table, same columns as the other rows).
4. In `Filter New Files`, treat a key whose newest manifest row is `deleted` as new, so a file that returns is ingested again.
5. Run once on a small test folder with a scratch file: delete it in Drive, run, and see the `Delete Records` output `{"outdated": N}` with N above 0.
6. Report in issue #83: `r2: pass (scratch file removed, outdated N)` or `r2: fail (<node and message>)`.

**r3. See the real S3 purge work** (about 10 minutes plus one day of waiting). CI uses a fake store, so this is the one check that needs the real one.

1. Set `KH_RECORD_PURGE_DAYS=1` for the `kh daily` timer and restart nothing else.
2. One day after the scratch file of r2 was removed, run `kh daily`. Its line shows `records purged: 1` or more and no `record blobs failed`.
3. `get_doc` of the scratch record answers `not found`, and its object is gone from the bucket (`records/<uuid>` in the S3 console). Set `KH_RECORD_PURGE_DAYS` back to `30`.
4. Report in issue #83: `r3: pass` or `r3: fail (<step and message>)`.

## 5. Risks and open questions

- **The follow-up steps need a release.** r1 to r3 cannot run before the pull request merges (they need a published image), and r3 needs a day of waiting, so they are not manual_after items. The issue body lists them under `## After the last merge`, and the owner reports r1 to r3 on issue #83.
- **A wrong diff from the feed** (an empty or partial Drive listing) hides data. The 24-hour guard, the 500 cap and the 30-day grace limit the damage, and `kh records restore` undoes it in one command. A feed with a legitimate large cleanup needs `force=true` once. Early sign: `delete_records` answers with the shrink text.
- **Old versions of already ingested files** (records ingested before this change) are found by the manifest like any other, so the first full pass of the feed removes them through the old-version rule. Nothing hub-side tells them apart.
- **Hidden does not mean secret.** Until the purge, `get_doc` and `search(include_outdated=true)` still return the content. A file removed because it was shared by mistake needs `kh` to purge sooner than the setting allows: lower `KH_RECORD_PURGE_DAYS` to `1` and run `kh daily` (recorded as a residual in 3.7).
- **S3 behaviour not verified from the planning environment.** The purge assumes `DeleteObject` on a missing key succeeds (true for AWS S3 and MinIO) and that the bucket keeps one object version. If the store answers an error for a missing key, that record's row stays in `'purge'` and is retried daily and the run prints `record blobs failed: M`; if versioning is on, old versions of the body remain (residual in 3.7). The default region name `garage` in `settings.py` suggests Garage, which has no versioning.
- **A brief that is wrong.** If `mark_record_outdated` or `move_record` in `0007_datasets.sql` no longer match the line ranges of 3.2, or a SQLSTATE `P0K0E` to `P0K12` is already used in `migrations/`, stop with `status=blocked`. A test that pins the number of migrations or the columns of `kh.records` that the briefs do not name is fixed in the same phase (tests are not product text).
- **Architect findings** (role `architect`, `AGENT-REPORT architect at=make_plan verdict=findings blocking=5 non_blocking=11`) are all taken in: the claim state and order (3.4), the clock that is not reset (3.2), literal prefix matching (3.2), the restore on `ingest_record` (3.3), the bulk restore command (3.6), the shrink guard (3.2), `replaced_by` cleanup (3.5), advisory locks and embed jobs (3.2), the no-blob-store case (3.4), `move_record` (3.2), definer argument checks (3.2), the tool and test lists (phases p1 to p3), the Drive rules (3.7) and the residuals (3.7). Its S3 question is the open item above.

## Implementation manifest

```yaml
plan_slug: record-removal
feature_branch: feat/record-removal
tracking_issue: 83
max_parallel: 2
manual_before: []
manual_after: []
verify_after_merge:
  - .venv/bin/python -m pytest -q -k removal
final_checks:
  - "`tld plan deviations check --file docs/plans/83/deviations.md --plan docs/plans/83/plan.md --all` exits 0"
  - the changelog has entries under "## [Unreleased]" and no new version heading
phases:
  - id: p1-removal-sql
    title: Migration 0008 with the removal reason, four definer functions, two redefined functions and the error texts
    depends_on: []
    complexity: M
    touches:
      - src/knowledge_hub/migrations/0008_record_delete.sql
      - src/knowledge_hub/mcp/errors.py
      - tests/integration/test_record_removal_sql.py
      - tests/integration/test_rls_policies.py
      - tests/integration/test_schema_tables.py
      - tests/integration/test_migrate_runner.py
      - tests/integration/test_migration_0002.py
    brief: |
      Run `bash scripts/ensure_test_pg.sh` first (a database for the integration tests). Patterns to copy: tests/integration/test_migration_0007.py (fixture `mig`, helper `_row_record`, how definer functions are called as a caller with `caller_tx`) and tests/integration/test_mark_record_outdated.py.
      1. Create src/knowledge_hub/migrations/0008_record_delete.sql. Put the SQL of plan section 3.2 in it, in the order the plan gives: header comment, ALTER TABLE and index, `kh.delete_records`, `kh.restore_records`, `kh.records_purge_claim`, `kh.records_purge_delete`, then `CREATE OR REPLACE` of `kh.move_record` and `kh.mark_record_outdated` (copy lines 229 to 270 and 271 to 311 of src/knowledge_hub/migrations/0007_datasets.sql and make only the two edits of 3.2), then the REVOKE and GRANT lines. Copy the SQL text exactly; do not change names, argument lists or SQLSTATEs.
      2. In src/knowledge_hub/mcp/errors.py add the four constants RECORDS_CAP, RECORDS_SHRINK, RECORD_PURGING and MOVE_REMOVED with the exact texts of plan 3.2, next to the other fixed texts.
      3. tests/integration/test_migrate_runner.py: add `RECORD_DELETE = "0008_record_delete.sql"`, append it to the list in `test_seven_migrations_are_shipped` (rename the test `test_eight_migrations_are_shipped`), to the tuple in `test_schema_migrations_lists_all_migrations` and to the expected list of `test_missing_schema_migrations_means_nothing_applied`. tests/integration/test_schema_tables.py: add "outdated_reason" at the end of the `records` list. tests/integration/test_migration_0002.py: append `"0008_record_delete.sql"` to the list that line 101 compares `migrate(dsn)` with. tests/integration/test_rls_policies.py: add "delete_records", "restore_records", "records_purge_claim" and "records_purge_delete" to `DEFINERS`.
      4. Create tests/integration/test_record_removal_sql.py (marker integration) with these tests, each calling the functions as the caller named: test_reason_column_and_constraints (a reason without outdated, a reason other than '', 'delete', 'purge', and a reason on a dataset row are each refused by the database); test_delete_records_exact_refs; test_delete_records_prefix_is_literal (four records with source_refs `a_b@1#p1of1`, `axb@1#p1of1`, `a%b@1#p1of1` and `aXXXb@1#p1of1`: the prefix `a_b@1` hides only the first and the prefix `a%b@1` hides only the third, so `_` and `%` match only themselves); test_delete_records_keeps_the_clock_of_removed_rows (a second call returns 0 and `outdated_at` is unchanged); test_delete_records_refuses_more_than_500 (501 matching rows give SQLSTATE P0K0E and change nothing); test_delete_records_shrink_guard_and_force (hiding 60 of 100 live records gives P0K10 with the message `shrink 60 50`, because the limit is greatest(50, 100 / 5) = 50; the same call with force true succeeds and answers 60); test_delete_records_refuses_dataset_source_and_foreign_group (P0K0B, and a caller who does not hold the group gets NULL); test_restore_records_filters (by exact ref, by prefix, by `since`, and null refs for the whole source; a 'purge' row is never restored); test_mark_record_outdated_takes_over_and_refuses_purging (marking a 'delete' record outdated again sets reason '' ; a 'purge' record gives P0K0F); test_move_record_refuses_removed_record (P0K12 for reason 'delete'); test_purge_claim_respects_the_grace_period (called as `kh.records_purge_claim(30, 200, '{}')`: a 'delete' row hidden 31 days ago is claimed and becomes 'purge'; one hidden 1 day ago is not; a 'purge' row is claimed again on the next call unless its id is in the third argument); test_purge_claim_rejects_bad_arguments (days 0, lim 0 and lim 1001, each with `'{}'` as the third argument, give P0K11); test_purge_delete_removes_rows_chunks_jobs_and_links (a 'purge' row goes with its chunk and its queued embed job; another record's `replaced_by` naming it becomes ''; a 'delete' row passed in the same list stays and keeps its queued embed job); test_purge_functions_are_for_the_indexer_only (kh_app and PUBLIC cannot execute the purge functions, kh_indexer can; kh_app can execute delete_records and restore_records, kh_indexer cannot). Use `add_record` from tests/support/seed.py (it accepts the new column as a field) and `wf_all` / `alice_all` from tests/support/principals.py. Name the tests exactly as written.
      5. Run `.venv/bin/python -m ruff check .` and `.venv/bin/python -m pytest -q tests/integration/test_record_removal_sql.py tests/integration/test_rls_policies.py tests/integration/test_schema_tables.py tests/integration/test_migrate_runner.py`, then the whole suite with `.venv/bin/python -m pytest -q`; fix every test that pins the number of migrations or the columns of kh.records (tests are not product text). Stop with status=blocked if a SQLSTATE of 3.2 is already used in src/knowledge_hub/migrations/ or the line ranges of step 1 do not hold the two functions.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_record_removal_sql.py -q passes (14 tests)"
      - ".venv/bin/python -m pytest tests/integration/test_migrate_runner.py tests/integration/test_migration_0002.py tests/integration/test_rls_policies.py tests/integration/test_schema_tables.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
      - ".venv/bin/python -m ruff check . passes"
      - "grep -c \"P0K0E\\|P0K0F\\|P0K10\\|P0K11\\|P0K12\" src/knowledge_hub/migrations/0008_record_delete.sql prints a number of 5 or more"
      - "grep -n \"RECORDS_CAP\\|RECORDS_SHRINK\\|RECORD_PURGING\\|MOVE_REMOVED\" src/knowledge_hub/mcp/errors.py matches 4 lines"
  - id: p2-removal-tools
    title: The delete_records tool, the restore inside ingest_record and the purging refusal
    depends_on: [p1-removal-sql]
    complexity: M
    touches:
      - src/knowledge_hub/tools/record_delete.py
      - src/knowledge_hub/tools/records.py
      - src/knowledge_hub/tools/outdated.py
      - src/knowledge_hub/mcp/server.py
      - src/knowledge_hub/auth/keys.py
      - src/knowledge_hub/auth/bearer.py
      - tests/integration/test_record_removal_tools.py
      - tests/unit/test_bearer_middleware.py
    brief: |
      Run `bash scripts/ensure_test_pg.sh` first. Read plan sections 3.2 and 3.3. Patterns to copy: src/knowledge_hub/tools/rows.py (a hub-key-only tool with a guard), src/knowledge_hub/tools/outdated.py (savepoint plus SQLSTATE mapping), tests/integration/test_ingest_record.py (fixtures `deps` and `blobs`, `mcp_client.call(wf_all, ...)`) and tests/integration/test_mark_record_outdated.py.
      1. Create src/knowledge_hub/tools/record_delete.py with `delete_records` and `register(server, deps)` exactly as plan 3.3 describes (check order, errors, the savepoint, the SQLSTATE mapping, the reply `{"outdated": n}`, the INFO log line, the description text). Register it in `register_tools` of src/knowledge_hub/mcp/server.py (the import line there is near the 110 character limit of ruff: split it into a parenthesised import with one module per line, and call `record_delete.register(server, deps)` after `records.register`).
      2. src/knowledge_hub/auth/keys.py: put "delete_records" first in `KEY_TOOLS` (the tuple is sorted). src/knowledge_hub/auth/bearer.py: add "delete_records" to `KEY_ONLY_TOOLS` (wrap the frozenset over several lines, one name per line, so the line stays under 110 characters).
      3. src/knowledge_hub/tools/records.py: change `_existing` to return `(id_text, reason)` or None, and apply the three cases of plan 3.3 where `found` is used; keep the unique-violation fallback answering `{"id": ..., "existed": True}` with the id only. Add the sentence to the tool description.
      4. src/knowledge_hub/tools/outdated.py: map SQLSTATE P0K0F to `kh_error("record_purging", errors.RECORD_PURGING)` next to the other mappings.
      5. tests/unit/test_bearer_middleware.py: in `test_oauth_write_scope_has_no_row_tools` add "delete_records" to the set.
      6. Create tests/integration/test_record_removal_tools.py (marker integration), named exactly: test_delete_records_hides_exact_and_prefix_matches; test_delete_records_counts_only_changed_rows (the second identical call answers `{"outdated": 0}`); test_delete_records_refusals (an OAuth user such as `alice_all`: the answer is the text `TOOL_NOT_ALLOWED` or `ONLY_WORKFLOWS`, whichever `tests/integration/test_ingest_record.py` expects for the same caller; an audience that is not held, a `dataset:x` source, an empty list, a prefix of 7 characters, an entry holding NUL, 501 refs giving TOO_MANY_ROWS); test_delete_records_cap_and_shrink_texts (RECORDS_CAP for one prefix such as `big@` that matches 501 records (the tool itself refuses more than 500 refs, so one prefix is how 501 records are reached), RECORDS_SHRINK with the numbers for the guard, success with `force: true`); test_removed_record_is_hidden_from_search_and_readable_in_get_doc (seed with `add_chunked_record` and copy the search calls of tests/integration/test_search_outdated.py; hidden from `search` unless `include_outdated`, `get_doc` shows outdated true and note `removed from source`); test_ingest_restores_a_removed_record (`existed` true and `restored` true, the record is live again); test_ingest_refuses_a_purging_record (RECORD_PURGING); test_ingest_leaves_a_manual_mark_alone (a hidden record with reason '' answers `existed` true and stays outdated); test_mark_record_outdated_refuses_a_purging_record (RECORD_PURGING). Put 'purge' state in by inserting the row with `add_record(..., outdated=True, outdated_at=datetime(2026, 1, 1, tzinfo=UTC), outdated_by="x", outdated_reason="purge")`.
      7. Run ruff and `.venv/bin/python -m pytest -q`. `tests/unit/test_hub_keys.py::test_key_tools_match_registered_tools`, `tests/unit/test_bearer_middleware.py::test_scope_sets_match_registered_tools` and `tests/unit/test_tool_schemas.py` must pass without further change.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_record_removal_tools.py -q passes (9 tests)"
      - ".venv/bin/python -m pytest tests/unit/test_hub_keys.py tests/unit/test_bearer_middleware.py tests/unit/test_tool_schemas.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
      - ".venv/bin/python -m ruff check . passes"
      - "grep -n \"delete_records\" src/knowledge_hub/auth/keys.py src/knowledge_hub/auth/bearer.py matches 2 lines"
  - id: p3-removal-purge-cli
    title: The purge in kh daily, KH_RECORD_PURGE_DAYS and the kh records restore command
    depends_on: [p1-removal-sql]
    complexity: M
    touches:
      - src/knowledge_hub/settings.py
      - src/knowledge_hub/jobs/daily.py
      - src/knowledge_hub/cli.py
      - src/knowledge_hub/admin/records_cmd.py
      - src/knowledge_hub/admin/cli.py
      - src/knowledge_hub/admin/move.py
      - tests/integration/test_record_removal_purge.py
      - tests/unit/test_record_removal_cli.py
      - tests/unit/test_settings.py
      - tests/unit/test_cli_rebuild.py
      - tests/integration/test_daily.py
    brief: |
      Run `bash scripts/ensure_test_pg.sh` first. Read plan sections 3.4 and 3.6. Patterns to copy: src/knowledge_hub/jobs/daily.py (`purge_dataset_history`), src/knowledge_hub/admin/move_cmd.py (administrator check, `do_move`, `register`), tests/integration/test_daily.py (fixtures `world`, `indexer_pool`), tests/integration/test_admin_move_record.py and tests/unit/test_cli_rebuild.py::test_daily_and_requeue.
      1. src/knowledge_hub/settings.py: add `record_purge_days: int = Field(30, ge=1, le=3650)` after `dataset_history_days`.
      2. src/knowledge_hub/jobs/daily.py: add `purge_records(indexer_pool, blobs, days)` exactly as plan 3.4 describes (up to 20 rounds of 200, a claim in its own transaction, the S3 body deleted before the row in a later transaction, a `failed` set passed to the next claim, a warning without the key on a failure) .
      3. src/knowledge_hub/cli.py `_daily`: import `purge_records`; call it with `deps.indexer_pool`, `deps.blobs`, `deps.settings.record_purge_days` unless `deps.blobs is None`; extend the printed text as plan 3.4 says, in the form `...; dataset history rows purged: H; records purged: N`, with `; records purged: skipped (no blob store)` when there is no blob store and `; record blobs failed: M` only when M is above 0.
      4. Create src/knowledge_hub/admin/records_cmd.py as plan 3.6 says: `do_restore(deps, email, audience, source, prefix, since)` and `register(sub)` creating the subcommand `records` with the action `restore` (copy the nested subparser style of `kh key` in src/knowledge_hub/auth/keys_cli.py `_parser`, but build it with `sub.add_parser("records")` and `.add_subparsers(dest="records_action", required=True)`), with the options `--audience`, `--source`, `--prefix`, `--since`, `--by` and `add_common(parser, mutating=False)` on the `restore` parser. Add "records_cmd" to `COMMAND_MODULES` in src/knowledge_hub/admin/cli.py, and add "records" to the tuple `("label", "group", "person", "repo", "move")` in src/knowledge_hub/cli.py (around line 162) that routes commands to `admin.cli.main`.
      5. src/knowledge_hub/admin/move.py: add `REMOVED_SQLSTATE = "P0K12"` and map it to `kh_error("move_removed", MOVE_REMOVED)` next to the other SQLSTATE mappings.
      6. Update the two tests that pin the daily line: tests/unit/test_cli_rebuild.py::test_daily_and_requeue (the stub deps there, a `SimpleNamespace`, gets `blobs=None` and `settings` with `record_purge_days=30`; assert the first output line ends with `; dataset history rows purged: 7; records purged: skipped (no blob store)`; then add a second case that sets `blobs` to `object()` and monkeypatches `daily_mod.purge_records` to an async function returning `(3, 0)` and asserts the line ends with `; records purged: 3`) and tests/integration/test_daily.py line 98 (its `deps` fixture has no blob store, so the line now ends `usage events purged: 1; dataset history rows purged: 0; records purged: skipped (no blob store)`). tests/unit/test_settings.py: add test_record_purge_days (default 30; `0` and `3651` refused).
      7. Create tests/integration/test_record_removal_purge.py (marker integration), named exactly: test_purge_records_deletes_row_chunks_and_blob (a record hidden 31 days ago with a blob in `MemoryBlobStore`; after the call the row, its chunk and the blob are gone, the result is `(1, 0)`); test_purge_records_keeps_recent_and_manual_marks (a record hidden 2 days ago and one hidden by a person 100 days ago stay); test_purge_records_keeps_the_row_when_the_blob_fails (a blob store whose `delete` raises: result `(0, 1)`, the row is in reason 'purge' and the next call with a working store purges it); test_purge_records_ignores_dataset_rows; test_restore_record_command_restores_by_prefix_and_since (call `do_restore` as an administrator); test_restore_refuses_a_non_administrator_and_a_bad_group (call `records_cmd._run` with an `argparse.Namespace` built by hand: exit 1 for an email not in `config.admins`, exit 2 for a group name that does not match `GROUP_RE`); test_move_refuses_a_removed_record (`do_move` of a record with reason 'delete' raises KhError with MOVE_REMOVED). Create tests/unit/test_record_removal_cli.py (marker unit): test_records_restore_is_routed_by_kh (`cli.main(["records", "restore", ...])` with `records_cmd.do_restore` monkeypatched to an async stub returning `{"restored": 2}` prints it as JSON and exits 0), test_records_restore_since_needs_a_zone (a `--since` without a zone exits 2 with `records: --since needs a time zone`, no config file needed) and test_records_restore_since_must_parse (exit 2 with `records: --since is not a valid time`). Also add test_purge_records_counts_a_failed_blob_once (one working and one failing blob: the result is `(1, 1)`) to the integration file.
      8. Run ruff and `.venv/bin/python -m pytest -q`.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_record_removal_purge.py tests/unit/test_record_removal_cli.py -q passes (11 tests)"
      - ".venv/bin/python -m pytest tests/unit/test_settings.py tests/unit/test_cli_rebuild.py tests/integration/test_daily.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
      - ".venv/bin/python -m ruff check . passes"
      - "grep -n \"record_purge_days\" src/knowledge_hub/settings.py src/knowledge_hub/cli.py matches 2 lines"
      - "grep -n \"records_cmd\" src/knowledge_hub/admin/cli.py matches 1 line"
  - id: p4-removal-docs
    title: Retirement phase, docs, README and changelog for record removal
    depends_on: [p2-removal-tools, p3-removal-purge-cli]
    complexity: M
    touches:
      - docs/feeds.md
      - docs/design.md
      - docs/configuration.md
      - docs/runbook.md
      - README.md
      - CHANGELOG.md
    brief: |
      Make the literal edits of plan section 3.7, file by file. Read each file around the place first and keep its style (tables stay tables).
      1. docs/feeds.md: add the section `## Removing what the source removed (T3 records)` after the section `## Full runs (weekly)` (before `## Reference recipe`), with the content listed for feeds.md in 3.7, rules (1) to (6) as a numbered list. In the answers table of `## When something goes wrong` add rows for `this call would hide more than 500 records; split it`, `delete_records would hide ... repeat with force=true if that is right` and `this record is being deleted; send it again after the next kh daily`, each with its meaning in one sentence. In the `kh key create` example make `--tools` read `list_datasets,upsert_rows,finish_sync,delete_rows` and add after it one sentence that a feed of T3 records adds `ingest_record,delete_records`.
      2. docs/design.md: in the tools paragraph (line 175) change `Fourteen tools` to `Fifteen tools` and add `delete_records` to the hub-key tools; in the schema/roles text about records (around line 97) say what 3.7 says; add the bullet `Removal of records` and the three residuals from 3.7 where the other residuals are listed; in the line about `mark_record_outdated` (around line 178) add that a person's mark takes a record out of the purge.
      3. docs/configuration.md: the `KH_RECORD_PURGE_DAYS` row next to `KH_DATASET_HISTORY_DAYS` (line 151); the `kh daily` row (line 189) with the new text and the output suffixes; a commands-table row `kh records restore --audience G --source S [--prefix P] [--since ISO] --by EMAIL` saying what it does.
      4. docs/runbook.md: the daily job line (line 56) mentions the purge of removed records after `KH_RECORD_PURGE_DAYS`; add the two routine tasks of 3.7 in the routine tasks table.
      5. README.md: add a `delete_records` row to the tools table (`Hide the records of a source that the source no longer has, by source_ref or prefix (hub keys only); they are deleted after KH_RECORD_PURGE_DAYS.`) and a `kh records` row to the `kh` commands table.
      6. CHANGELOG.md: under `## [Unreleased]` add `### Added` with: the tool `delete_records` and the restore of a hidden record by `ingest_record`; `kh records restore`; the setting `KH_RECORD_PURGE_DAYS` and the purge in `kh daily`. Add `### Changed` with: migration `0008_record_delete.sql` (a new column `outdated_reason`; `kh.mark_record_outdated` and `kh.move_record` redefined, and a person's mark cancels the purge; `kh move` refuses a record that was removed from its source); `kh daily` prints `; records purged: N`; upgrading needs `kh migrate`, and there is no rollback script for 0008. Open no version heading.
      7. Run `.venv/bin/python -m pytest -q tests/unit/test_docs_links.py` and `.venv/bin/python -m ruff check .`. Report `docs-changed:` lines for no doc (this repository has no docs index).
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_docs_links.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
      - "grep -n \"Fifteen tools\" docs/design.md matches 1 line"
      - "grep -n \"KH_RECORD_PURGE_DAYS\" docs/configuration.md matches 1 line or more"
      - "grep -n \"delete_records\" README.md docs/feeds.md docs/design.md CHANGELOG.md matches in all four files"
      - "grep -m2 -n \"^## \\[\" CHANGELOG.md prints ## [Unreleased] first and ## [0.7.1] second"
```
