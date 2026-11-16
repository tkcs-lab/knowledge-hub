# Plan: audience lists mean ANY-of, derived rows still need every source

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** An operator writes `audiences: [mgmt, finance]` for an entity kind, or `"series:weekly-sync": [mgmt, finance]` in `raw_audience_map`, and a caller holding mgmt or finance can read those rows. A caller holding neither gets `not found`, as for a missing row. A summary, candidate or conflict built from a mgmt source and a finance source stays readable only by someone who holds both. `propose_change` answers `ambiguous` when the sources fit two canonical repos and asks for a `target_id`. `kh.yaml` is refused if an `audiences` name has anything but lowercase letters, digits, `_` or `-`. `kh migrate` converts the stored rows; `kh recompute-visibility` gives the same result.

**Scope:** One migration `0007_any_of_audiences.sql`: a new `kh.visible`, a clause format inside `required_audiences`, the raw-visibility trigger, a CHECK per visibility column and a data migration. Python: clause helpers and routing in `visibility.py`, raw recompute, entity sync, the external-embedder filter, audience-name validation, a test-only `stop_after` in the migration runner. Policies, grants, chunk triggers, `kh check`, search and the derived and strict visibility functions keep their code. Domains, Forgejo teams and the PrivacyFence claim do not change.

**Security:** Every row whose configured list has two or more audiences becomes readable by each member; check the production `kh.yaml` first (ma1). Derived rows keep an AND over their sources. Enforcement stays in Postgres under forced RLS; no new definer function reads rows.

**Performance:** Rows of plain names keep today's inlined test; only rows with a multi-audience clause call a helper. On 200 000 rows: 81 ms today, 127 ms with 2% clause rows, 255 ms with 33%.

**Review focus:** the clause encoding inside the existing column (3.1, 3.2); the routing rule and the `ambiguous` error (3.5); the strict ingest check (3.4); the data migration (3.8); `kh.visible` staying inlinable and the `EXECUTE` grants (3.3, section 5).

## 1. Goal

Today a row lists audiences that a caller must hold all of. The owner wants a listed set of audiences to mean a wider audience: any one of them is enough to read the row, for entity kinds and for `raw_audience_map` entries. Rows derived from several sources (derived records, candidates, conflicts) must stay readable only by callers who can read every source. Tracking issue: [tkcs-lab/knowledge-hub#15](https://github.com/tkcs-lab/knowledge-hub/issues/15).

This document is the design note the owner asked for. **Nothing is implemented until the owner approves it**; the approval is starting `/tld:implement` on this plan.

## 2. Current state

- `src/knowledge_hub/migrations/0003_rls.sql:28-31`: `kh.visible(req, emails)` is `coalesce(req <@ kh.caller_audiences() AND (emails IS NULL OR kh.caller_email() = ANY(emails)), false)`, `LANGUAGE sql STABLE`, no `SET` clause (inlinable). Every `kh_app_select` policy on `documents`, `chunks`, `entities_cache`, `raw_records`, `derived_records`, `candidates`, `conflicts` calls it (`0003_rls.sql:186-295`); the three source tables and `document_versions` use `EXISTS` on their parent.
- `0002_tables.sql`: every visibility table has `required_audiences text[] NOT NULL`, `allowed_emails text[]` and `CONSTRAINT kh_no_open_rows CHECK (cardinality(required_audiences) > 0 OR allowed_emails IS NOT NULL)`. The source tables hold snapshots in `src_required_audiences`/`src_allowed_emails`.
- `0004_triggers.sql:9-62`: `chunks_inherit_owner` copies the owner's two visibility columns into every chunk; `owner_propagate_to_chunks` re-fires it after an owner UPDATE. `src/knowledge_hub/indexer/rebuild.py:19-37` (`DRIFT_SQL`) compares those columns.
- `0004_triggers.sql:97-125`: `raw_records_assign_visibility` sets `required_audiences` to the union of every matching `raw_audience_map` entry (series and folder) and raises `P0K02` unless the caller holds all of them; else participants become `allowed_emails`; else `P0K01`.
- `0005_derived_visibility.sql`, `0006_strict_visibility.sql`: `kh.derived_visibility` and `kh.strict_visibility` return the union of the resolved sources' `required_audiences` (`array_agg(DISTINCT a ORDER BY a)`) and the intersection of the non-null email lists; the deferred `*_visibility_matches` triggers recompute the same from the snapshots at commit and compare as sets (`P0K04`).
- `src/knowledge_hub/visibility.py:30-79`: `derive_visibility` (Python statement of the union rule) and `route_target_repo` (union `U`; `restricted_audiences_imply_all` drops `all`; `|U| >= 2` is `SPAN`; with an explicit repo `U <= {repo.audience}` or `WIDER`).
- `src/knowledge_hub/jobs/recompute.py:47-71` (`_recompute_raw`) repeats the trigger's union in SQL; `_refresh_and_apply` (`:84-125`) refreshes snapshots and sets parents to the union; derived rows go in citation order (`:130-149`). `jobs/daily.py:18-24,47-63` writes `spec.audiences` of each entity kind as the row's `required_audiences`.
- `src/knowledge_hub/embed/http.py:30-34` (`may_embed`): a chunk goes to the external embedder only if its audiences are a non-empty subset of `KH_EMBED_HTTP_ALLOWED_AUDIENCES` and it has no email list.
- `src/knowledge_hub/tools/derived.py:119-121` raises `derived_nobody` when the result has no audiences and no emails; `tools/promote.py:193-206` routes with the strict-visibility result; `tools/raw.py:96-97` maps `P0K02` to `INGEST_AUDIENCE`.
- `src/knowledge_hub/config.py:83-133`: `audiences` names are not validated; `db/caller_tx.py:22-24` refuses caller audiences that do not match `^[a-z0-9][a-z0-9_-]{0,62}$`.
- `src/knowledge_hub/db/migrate.py:31-53`: migrations are checksummed; an applied file must not change.
- Tests: `tests/support/seed.py:12-19` (`CASES`, written with triggers off), `tests/support/principals.py`, `tests/integration/test_leak_matrix.py:18-20` (an AND predicate).

## 3. Design

### 3.1 Options compared

| | (a) AND-of-OR groups in a new column (jsonb `[[..],[..]]`) | (b) two columns `visible_any`, `required_all` | **(c) clauses inside `required_audiences` (recommended)** |
|---|---|---|---|
| Representation | new jsonb column on 7 tables and 3 snapshot tables | 2 new `text[]` columns on 10 tables | each element of the existing `text[]` is one OR-clause, `finance|mgmt`; a plain name is a one-member clause |
| Exact? | yes | no: a derived row of a `[mgmt, finance]` source must flatten it into `required_all`, so a mgmt+hr caller loses a row it may read; a raw row hit by two map entries cannot be expressed | yes: a set of clauses is an AND of ORs |
| Migration size | drop and recreate 7 policies, column grants, chunk trigger, drift query, snapshot trigger, 5 visibility functions | same plus a two-column `kh_no_open_rows` | `CREATE OR REPLACE` of `kh.visible` and the raw trigger, one CHECK per table, data migration |
| Chunks, `DRIFT_SQL`, search | rewritten | rewritten | unchanged: the column is copied as before |
| `kh_no_open_rows` | rewritten | rewritten (either column) | unchanged: no clauses and no emails is still open |
| Deferred triggers, `derived_visibility`, `strict_visibility`, recompute's `_refresh_and_apply` | rewritten | rewritten | unchanged: the union of clause strings is exactly the AND over sources |
| `derived_nobody` | rework | rework | unchanged: a clause is never empty, so only an empty email intersection makes nobody |
| Unconverted code | breaks | breaks | fails closed: `finance|mgmt` is no audience name, so an old subset test never matches |
| Per-row cost of `kh.visible` | jsonb walk, not inlinable (the architect role measured about 6.5x) | two inlined tests | today's inlined test for plain rows; a helper sub-select only for rows with a `|` clause (3.3) |

(c) is (a) stored in the column that already exists. It is exact, keeps every policy, grant, trigger and the drift query, and the only SQL whose meaning changes is `kh.visible`. The architect role, run without this recommendation, reached the same choice. Rejected: (a) for its size with no gain over (c); (b) because it is lossy and cannot express a raw row matched by two map entries. Also rejected: adding a constant to `config_hash` to force a recompute on first start (the migration already converts the rows, and the loader would then rewrite every row at deploy time).

### 3.2 The clause format

- A **clause** is a non-empty set of audience names written as the names sorted by code point (C collation), de-duplicated, joined by `|`. `[mgmt, finance]` is `finance|mgmt`; `[all]` is `all`.
- `required_audiences` (and `src_required_audiences`) is a set of clauses. A row is visible to a caller when **every clause shares at least one audience with the caller's audiences**, and the email rule holds. `'{}'` (no clause) means no audience condition, as today, and `kh_no_open_rows` still requires emails then.
- Existing plain names are one-member clauses, so every document, derived, candidate and conflict row keeps its exact meaning without a rewrite; only rows written from a configured list (entities, mapped raw rows) need one clause per list.
- Audience names never contain `|`: `config.py` validates `audiences` entries against `^[a-z0-9][a-z0-9_-]{0,62}$` (new, error `config: audience {a!r} is not a valid audience name`), and `caller_tx` already refuses other caller audiences.
- New SQL helpers in 0007, owned by `kh_migrator`, `LANGUAGE sql IMMUTABLE PARALLEL SAFE`, `EXECUTE` revoked from PUBLIC and granted to `kh_app`, `kh_indexer`, `kh_provenance`:

```sql
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
```

  `kh.audience_clause('{}')` is NULL; callers filter empty lists first.
- New CHECK on each of the 7 visibility tables: `CONSTRAINT kh_audience_clauses CHECK (kh.audience_clauses_ok(required_audiences))`; on `derived_sources`, `candidate_sources`, `conflict_items`: `CONSTRAINT kh_src_audience_clauses CHECK (kh.audience_clauses_ok(src_required_audiences))`. Added after the data migration, so they validate the converted rows.
- Python (`visibility.py`): `CLAUSE_SEP = "|"`; `clause(audiences: Iterable[str]) -> str` (`"|".join(sorted(set(audiences)))`, `ValueError("a clause needs at least one audience")` when empty); `clause_members(value: str) -> frozenset[str]`; `can_read(vis: Visibility, audiences: Iterable[str], email: str | None) -> bool`, the Python statement of `kh.visible` used by tests only. `Visibility.required_audiences` is documented as a list of clauses.

### 3.3 The new `kh.visible`

Same signature, so every policy and grant keeps working (`CREATE OR REPLACE`, still `LANGUAGE sql STABLE`, no `SET`). A sub-select stops Postgres from inlining a SQL function, so the sub-select lives in a helper that only rows holding a `|` clause call; the outer body stays inlinable and rows of plain names keep today's test:

```sql
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
```

`kh.audience_clauses_held`: owner `kh_migrator`, `EXECUTE` revoked from PUBLIC, granted to `kh_app` and `kh_provenance` (the roles that evaluate `kh.visible`; without the grant a read fails with `permission denied for function audience_clauses_held`, checked on PostgreSQL 16 while planning).

Measured while planning (PostgreSQL 16, 200 000 rows, one `count(*)` through the function): today's body 81 ms; this body 127 ms with 2% clause rows and 255 ms with 33%; a body with the sub-select inline (not inlinable) 590 ms.

Truth table (caller audiences / row): `{mgmt}` sees `finance|mgmt`; `{finance}` sees it; `{all}` does not; `{}` does not; `{mgmt}` does not see `{finance, mgmt}` (two clauses); `{mgmt, finance}` sees both; `{all}` sees `all|mgmt`; email rows as today. The answer for a hidden row and a missing row is the same query, so `get_doc`, `get_entity` and search keep their responses and their timing class (cost proportional to scanned rows, whatever their visibility).

### 3.4 Writers

- **Entities** (`jobs/daily.py`): `audiences[kind] = [clause(spec.audiences)]`; the empty-list refusal stays.
- **Raw trigger** (`CREATE OR REPLACE kh.raw_records_assign_visibility`, still `SECURITY DEFINER`, `SET search_path = kh, pg_temp`, owner `kh_provenance`): one clause per matching non-empty map entry, then the unchanged strict ingest check over the members:

```sql
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
```

  The ingest check stays strict (decision): a token may only write rows into audiences it holds all of; ANY-of changes who reads, not who may write. A series mapped to `[mgmt]` and a folder mapped to `[finance]` gives `{finance, mgmt}`, two clauses: both are needed, as today.
- **Documents**: one audience per repo, unchanged.
- **Derived, candidates, conflicts**: `kh.derived_visibility`, `kh.strict_visibility`, the snapshot trigger, the three deferred triggers and recompute's `_refresh_and_apply` are not edited. Their `array_agg(DISTINCT a ...)` over the sources' elements is the union of clause strings, which is the AND over every source's clauses. Example: sources `{mgmt}` and `{finance}` give `{finance, mgmt}`, hidden from a mgmt-only caller; sources `{finance|mgmt}` and `{hr}` give `{finance|mgmt, hr}`, readable by mgmt+hr.
- **`derived_nobody`** (`tools/derived.py:119`): unchanged.

### 3.5 Routing (`route_target_repo`)

Inputs as today. New rule, in order:

1. Any source with `allowed_emails` is refused (`EMAIL_SOURCES`), unchanged.
2. `K` = the set of clauses of all sources, each as `clause_members(...)`.
3. With `restricted_audiences_imply_all`: every clause that contains `all` becomes `{all}` (every audience implies `all`); then, if `K` has another clause, `{all}` is removed.
4. `R` (the audiences whose holders can read every source) = the intersection of the clauses of `K`; `K` empty means no restriction (as the empty union today).
5. With `explicit_repo`: unknown is `NO_REPO` (unchanged); accepted when `K` is empty or `repo.audience in R`; else `WIDER` with `u` = the clauses of `K` rendered with `clause(...)`, sorted, joined by `", "`.
6. Without: `K` empty means audience `all` (unchanged). `R` empty is `SPAN` with `a`, `b` the first two rendered clauses. Else the T0 repos whose audience is in `R`: none is `NO_REPO` with `a` = `" or ".join(sorted(R))`; exactly one is that repo; two or more is the new error `ambiguous`.

New text in `src/knowledge_hub/mcp/errors.py`: `AMBIGUOUS = "cannot propose: the sources fit both {repo_a} and {repo_b}; pass target_id of a document in the repository to use"` (`repo_a`, `repo_b` the first two matching repo names, sorted). Rejected: picking one of several repos automatically (the choice of audience is a person's).

The behaviour for single-audience sources is identical to today (`tests/unit/test_routing.py` keeps passing unchanged except for message details named in p2).

### 3.6 External embedder (`embed/http.py`)

`may_embed` flattens the clauses: `members = {a for c in audiences for a in c.split("|")}`; embed only when `audiences` is non-empty, there is no email list and `members <= allowed_audiences`. Conservative: every audience that could read the chunk must be allowed.

### 3.7 What does not change, and why

`chunks_inherit_owner`, `owner_propagate_to_chunks` and `DRIFT_SQL` copy and compare the column, whatever it holds. Search filters through the `chunks` policy, so it follows `kh.visible`. `config_hash` (`rebuild.py:40`) is unchanged: the configuration does not change, the data migration converts the rows. `kh_no_open_rows`, the policies and the column grants are unchanged.

### 3.8 Migration `0007_any_of_audiences.sql`, recompute and their equality

Runs as `kh_migrator` (member of `kh_provenance`; `USING (true)` on every table) in the runner's one transaction. In order:

1. Create `kh.audience_clause`, `kh.audience_clauses_ok`, revoke/grant as in 3.2.
2. Create `kh.audience_clauses_held` and `CREATE OR REPLACE kh.visible` (3.3), with the grants of 3.3.
3. `CREATE OR REPLACE kh.raw_records_assign_visibility` (3.4); re-run `ALTER FUNCTION ... OWNER TO kh_provenance` and `REVOKE EXECUTE ... FROM PUBLIC`.
4. Entities: `UPDATE kh.entities_cache SET required_audiences = ARRAY[kh.audience_clause(required_audiences)] WHERE cardinality(required_audiences) > 1;` (daily always wrote the whole configured list.)
5. Raw rows: the statement of `_recompute_raw` below, verbatim.
6. Derived rows: a `DO` block that loops: refresh `derived_sources` snapshots from the live rows (the `_LIVE` union of `recompute.py:16-23`), then set every derived row to the union/intersection over its snapshots (the aggregate of `recompute.py:98-125` over all derived ids), until the UPDATE changes 0 rows; after 1000 rounds `RAISE EXCEPTION 'derived visibility did not converge'` (a guard only: the write path cites existing rows, so no cycle can be stored through it). On an acyclic graph this reaches the same values as recompute's citation-order pass.
7. Candidates, then conflicts: one refresh-and-apply each (they are never sources).
8. Add the CHECK constraints of 3.2.

Chunks follow through `owner_propagate_to_chunks` (steps 5 and 6 are UPDATEs). The deferred triggers fire on INSERT only and do not run. The migration applies the current `kh.raw_audience_map`, as recompute would.

`jobs/recompute.py` `_recompute_raw` becomes:

```sql
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
   AND (r.required_audiences, r.allowed_emails) IS DISTINCT FROM (t.req, t.emails)
```

`kh_indexer` runs it, hence the `EXECUTE` grant on `kh.audience_clause`. Everything else in `recompute.py` stays. A raw row with neither a mapping nor participants keeps its stored singletons (narrower, safe), in both the migration and recompute.

`db/migrate.py`: `migrate(dsn: str, *, stop_after: str | None = None)` applies only files whose name is `<= stop_after` (for the migration tests); `kh migrate` does not pass it.

### 3.9 Tests to change or add

| File | Change |
|---|---|
| `tests/unit/test_visibility.py` | `clause`, `clause_members`, `can_read` truth table of 3.3, union of clause strings in `derive_visibility` |
| `tests/unit/test_routing.py` | rules of 3.5: one clause `finance|mgmt` with mgmt-only T0 repo routes to mgmt; with both repos `ambiguous`; `{finance|mgmt}` + explicit mgmt repo accepted; `{mgmt}`,`{finance}` `SPAN`; `all|mgmt` with and without the flag |
| `tests/unit/test_embed_providers.py`, `tests/unit/test_config.py` | clause flattening; audience-name validation |
| `tests/integration/test_rls_policies.py` | `test_visible_truth_table` with clauses; **required (1)** `test_any_of_rows_are_visible_to_each_listed_audience` (raw and entity row `finance|mgmt`: mgmt-only, finance-only, both see it; all-only and nobody do not) |
| `tests/integration/test_triggers.py` | raw trigger writes one clause per map entry; two entries give two clauses; `P0K02` for a mgmt-only token on a `[finance, mgmt]` entry |
| `tests/integration/test_derived_visibility_sql.py`, `test_strict_visibility.py` | **required (2)** a derived row (and a candidate and a conflict) from a mgmt source and a finance source is hidden from a mgmt-only caller and visible to mgmt+finance; `{finance|mgmt}`+`{hr}` readable by mgmt+hr |
| `tests/integration/test_any_of_migration.py` (new) | migrate to 0006, seed old-form rows, apply 0007: converted values, chunks follow, `DRIFT_SQL` empty, constraints present |
| `tests/integration/test_recompute_matches_migration.py` (new) | **required (4)** |
| `tests/integration/test_recompute.py`, `test_daily.py`, `test_migrate_runner.py` | clause output; `stop_after` |
| `tests/support/principals.py`, `seed.py`, `test_leak_matrix.py`, `test_search_leaks.py` | new principals and cases; predicate `can_read`; **required (3)** |

## 4. Manual steps

### Before implementation

None.

### After implementation

**ma1. Check the production `kh.yaml` for lists that now widen** (about 10 minutes)

1. Open the `kh.yaml` the hub's compose project mounts (the path in `KH_CONFIG_FILE`, see [docs/configuration.md](https://github.com/tkcs-lab/knowledge-hub/blob/feat/any-of-audiences/docs/configuration.md)).
2. List every `entities.<kind>.audiences` and every `raw_audience_map` value with two or more audiences.
3. For each, confirm that every listed audience may read those rows on its own. Where a list meant "only people in all of these", shorten it to the narrowest audience before deploying.
4. Report in the pull request: `ma1: pass (<n> multi-audience lists, all intended)` or `ma1: fail (<list>)`.

## 5. Risks and open questions

- **Widening on deploy.** Any configured list of two or more audiences becomes readable by each member. ma1 covers it; the changelog says it.
- **`CHECK` function privileges.** If an insert as `kh_app` or `kh_indexer` fails with `permission denied for function audience_clauses_ok`, the grants of 3.2 are missing; the worker fixes the grant, never the check.
- **Collation.** Clause strings must be built with C collation in SQL and `sorted()` in Python. A `P0K04` at commit in a test, or a CHECK failure on a valid list, means one side sorts differently: stop with `status=blocked`.
- **Search cost.** Measured in 3.3. If the outer `kh.visible` stops being inlined (an `EXPLAIN` of a chunk query shows `Filter: kh.visible(...)` instead of the expanded `CASE`), the body was changed: stop with `status=blocked`.
- **`derived_nobody` gap (found by the architect role, existing behaviour).** `tools/derived.py:119` lets through a row with audiences and `allowed_emails = []`, which nobody can read. It is not widened by this change and stays out of scope; a follow-up issue can refuse it.
- **Embedder filter.** 3.6 requires every member of every clause to be allowed. The looser rule (every clause shares one allowed audience) was rejected: a `finance|mgmt` row is also finance content.
- **Stale raw rows.** The migration applies the current map, which may differ from a row stored before an un-recomputed map change. This is what `kh recompute-visibility` would do; the loader recomputes on every hash change, so it is unlikely.
- **Test database.** Integration tests need PostgreSQL 16 with pgvector: `bash scripts/ensure_test_pg.sh`. A worker that cannot start it stops with `status=blocked`.
- **Seed rows.** `tests/support/seed.py` writes with triggers off but CHECKs still apply: every seeded clause must be canonical.

## Implementation manifest

```yaml
plan_slug: any-of-audiences
feature_branch: feat/any-of-audiences
tracking_issue: 15
max_parallel: 2
manual_before: []
manual_after:
  - id: ma1-review-production-lists
    title: Check the production kh.yaml for audience lists that now widen
    why: CI cannot see the production configuration; every multi-audience list becomes readable by each member
verify_after_merge:
  - .venv/bin/python -m pytest -q -m integration
final_checks:
  - "`tld plan deviations check --file docs/plans/15/deviations.md --plan docs/plans/15/plan.md --all` exits 0"
  - the changelog has entries under "## [Unreleased]" and no version heading
phases:
  - id: p1-sql-clauses
    title: Migration 0007 with the clause format, the new kh.visible and the data migration
    depends_on: []
    complexity: M
    touches:
      - src/knowledge_hub/migrations/0007_any_of_audiences.sql
      - src/knowledge_hub/db/migrate.py
      - tests/integration/test_rls_policies.py
      - tests/integration/test_triggers.py
      - tests/integration/test_derived_visibility_sql.py
      - tests/integration/test_strict_visibility.py
      - tests/integration/test_schema_tables.py
      - tests/integration/test_migrate_runner.py
      - tests/integration/test_any_of_migration.py
    brief: |
      Read first: docs/plans/15/plan.md sections 3.2, 3.3, 3.4, 3.7, 3.8; docs/design.md "Roles and RLS matrix"; src/knowledge_hub/migrations/0002-0006.
      1. Run `bash scripts/ensure_test_pg.sh`; if Postgres 16 with pgvector cannot start, stop with status=blocked.
      2. Do not edit 0001-0006 (checksummed). Create src/knowledge_hub/migrations/0007_any_of_audiences.sql with steps 1-8 of plan 3.8, using the SQL of 3.2, 3.3, 3.4 verbatim. Step 5 is the `_recompute_raw` statement of 3.8. Step 6 is a DO block with the loop and the error text of 3.8; copy the aggregate of src/knowledge_hub/jobs/recompute.py:98-125 with `{pcol}`=derived_id, `{kcol}`=source_kind, `{icol}`=source_id, `{table}`=derived_sources, `{parent}`=derived_records and no id filter. Step 7 does the same for candidate_sources/candidates (source_kind, source_id) and conflict_items/conflicts (item_kind, item_id).
      3. src/knowledge_hub/db/migrate.py: add keyword-only `stop_after: str | None = None` to `migrate`; skip files whose name is greater than it. `migrate_main` unchanged.
      4. Tests, extending the named files: test_migrate_runner.py (`stop_after` applies 0001-0006 only); test_rls_policies.py (`test_visible_truth_table` with the cases of plan 3.3, and the required test `test_any_of_rows_are_visible_to_each_listed_audience` for a raw row and an entities_cache row `{finance|mgmt}`, callers mgmt-only, finance-only, mgmt+finance visible, all-only and no-audience hidden); test_triggers.py (rename `test_mapped_series_and_folder_give_the_union` to `test_mapped_series_and_folder_give_one_clause_each`, add `test_multi_audience_entry_gives_one_clause` and `test_token_missing_one_mapped_audience_raises_p0k02`); test_derived_visibility_sql.py and test_strict_visibility.py (`test_mgmt_and_finance_sources_are_hidden_from_mgmt_only` for derived rows, candidates and conflicts; `test_any_of_source_and_hr_source_need_one_of_each`); test_schema_tables.py (`test_audience_clauses_check` rejects `''`, `'mgmt|finance'` (unsorted), `'MGMT'`, and accepts `'finance|mgmt'`, naming constraint `kh_audience_clauses`); test_triggers.py also: `test_mapped_series_gives_mapped_audiences_and_null_emails` (line 204) now expects `["all|mgmt"]`.
      5. New tests/integration/test_any_of_migration.py: build a fresh database with `migrate(dsn, stop_after="0006_strict_visibility.sql")`. Insert as kh_migrator in one transaction that starts with `SET LOCAL session_replication_role = replica` (as tests/support/seed.py does, so no trigger fires): a raw_audience_map entry `series:s1 -> {finance, mgmt}`; a document stored `{hr}`; a raw row of series s1 stored `{finance, mgmt}` with one chunk stored `{finance, mgmt}`; an entities_cache row stored `{finance, mgmt}`; a derived row stored `{finance, hr, mgmt}` citing the raw row and the document, with its two derived_sources snapshots `{finance, mgmt}` and `{hr}`; one candidate and one conflict with the same sources and stored values. Then `migrate(dsn)` and assert: raw `{finance|mgmt}` and its chunk the same; entity `{finance|mgmt}`; derived, candidate and conflict `{finance|mgmt, hr}`; `DRIFT_SQL` (src/knowledge_hub/indexer/rebuild.py) returns no rows; constraints `kh_audience_clauses` exist on the 7 visibility tables and `kh_src_audience_clauses` on the 3 source tables.
      6. Add `test_visible_stays_inlined` to test_rls_policies.py: as kh_app with audiences set, `EXPLAIN (COSTS OFF) SELECT 1 FROM kh.chunks` contains `strpos` and not `kh.visible(`.
      7. `.venv/bin/python -m ruff check .` and `.venv/bin/python -m pytest -q` pass.
      Stop with status=blocked if: a CHECK insert fails with a function permission error after the grants of 3.2; any existing test outside the touched files fails because a seeded value is not a canonical clause; the derived loop does not converge on the test data.
    acceptance:
      - .venv/bin/python -m pytest tests/integration/test_any_of_migration.py -q passes
      - .venv/bin/python -m pytest tests/integration/test_rls_policies.py::test_any_of_rows_are_visible_to_each_listed_audience -q passes
      - .venv/bin/python -m pytest tests/integration/test_derived_visibility_sql.py::test_mgmt_and_finance_sources_are_hidden_from_mgmt_only tests/integration/test_strict_visibility.py::test_mgmt_and_finance_sources_are_hidden_from_mgmt_only -q passes
      - git diff --name-only origin/main -- src/knowledge_hub/migrations/ lists only 0007_any_of_audiences.sql
      - .venv/bin/python -m pytest -q passes
  - id: p2-python-rules
    title: Clause helpers, routing, recompute, entity sync, embedder filter and config validation
    depends_on: [p1-sql-clauses]
    complexity: M
    touches:
      - src/knowledge_hub/visibility.py
      - src/knowledge_hub/mcp/errors.py
      - src/knowledge_hub/jobs/recompute.py
      - src/knowledge_hub/jobs/daily.py
      - src/knowledge_hub/embed/http.py
      - src/knowledge_hub/config.py
      - tests/unit/test_visibility.py
      - tests/unit/test_routing.py
      - tests/unit/test_embed_providers.py
      - tests/unit/test_config.py
      - tests/integration/test_recompute.py
      - tests/integration/test_daily.py
      - tests/integration/test_propose_change.py
      - tests/integration/test_recompute_matches_migration.py
    brief: |
      Read first: docs/plans/15/plan.md sections 3.2, 3.4, 3.5, 3.6, 3.8; src/knowledge_hub/visibility.py; src/knowledge_hub/jobs/recompute.py.
      1. visibility.py: add `CLAUSE_SEP`, `clause`, `clause_members`, `can_read` exactly as plan 3.2; update the `Visibility` and `derive_visibility` docstrings (clauses; the union of clauses is an AND over sources); rewrite `route_target_repo` to the six rules of plan 3.5.
      2. mcp/errors.py: add `AMBIGUOUS` with the text of plan 3.5; raise it as `kh_error("ambiguous", errors.AMBIGUOUS, repo_a=..., repo_b=...)`.
      3. jobs/recompute.py: replace the SQL of `_recompute_raw` with the statement of plan 3.8; update the module docstring (clauses). Nothing else.
      4. jobs/daily.py: `audiences[kind] = [clause(spec.audiences)]`.
      5. embed/http.py: `may_embed` as plan 3.6.
      6. config.py: add `AUDIENCE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")` and, in `Config` before `_validate`, `@field_validator("audiences") @classmethod def _audience_names(cls, value: list[str]) -> list[str]` (the pattern of `GovernanceConfig._team_names`) that raises `ValueError(f"audience {a!r} is not a valid audience name")` for the first entry that does not full-match. It must be a field validator: `_validate` checks repo audiences first and would report a different error. `_wrap` already adds the `config: ` prefix and the `(audiences)` location.
      7. Tests, extending the named files: test_visibility.py (`clause` sorts and de-duplicates, empty raises; `can_read` for every row of the plan 3.3 truth table; `derive_visibility` of `["finance|mgmt"]` and `["hr"]` is `["finance|mgmt", "hr"]`); test_routing.py (the cases in plan 3.9; existing tests keep passing; `finance` is not a default audience, so the new cases build their Config with `Config(audiences=["all", "mgmt", "hr", "contracts", "finance"], repos={**_default_repos(), "canonical-finance": RepoConfig(tier="T0", audience="finance")})`, or `Config(audiences=[...])` with only the repos the case needs); test_embed_providers.py (`finance|mgmt` embedded only when both are allowed); test_config.py (`parse_config({"audiences": ["all", "mgmt", "hr", "contracts", "a|b"]})` raises `ConfigError` with the message `config: audience 'a|b' is not a valid audience name (audiences)`); test_daily.py (a kind with `audiences=["mgmt", "finance"]` stored as `["finance|mgmt"]`); test_recompute.py (map `[finance, mgmt]` gives one clause; series `[mgmt]` + folder `[finance]` give two); test_propose_change.py (`test_any_of_source_with_two_repos_is_ambiguous`). Every Config in these tests that names `finance` lists it in `audiences`; without it the config is refused with `unknown audience 'finance'`.
      8. Required (4), new tests/integration/test_recompute_matches_migration.py: database A: `migrate(dsn, stop_after="0006_strict_visibility.sql")`, seed the rows of p1's test_any_of_migration (fixed ids) under map M = `series:s1 -> [finance, mgmt]`, then `migrate(dsn)`; database B: `migrate(dsn)`, seed the same ids under map `series:s1 -> [mgmt]` (raw stored `{mgmt}`), then `recompute_in` with a Config whose raw_audience_map is M. Assert the sorted visibility columns of raw_records, derived_records, derived_sources, candidates, conflicts and chunks are equal in A and B, and that a second `recompute_in` with M on A changes no row of raw_records, derived_records, derived_sources, candidates, conflicts and chunks (compare `xmin` of those tables before and after, as tests/integration/test_recompute.py:105 does; `raw_audience_map` is rewritten on every run and is not compared). Seed both databases with `SET LOCAL session_replication_role = replica`, as p1 step 5 does.
      9. `.venv/bin/python -m ruff check .` and `.venv/bin/python -m pytest -q` pass.
      Stop with status=blocked if an existing test in test_routing.py needs a changed expectation other than one created for the new rules, or if databases A and B differ.
    acceptance:
      - .venv/bin/python -m pytest tests/unit/test_visibility.py tests/unit/test_routing.py tests/unit/test_embed_providers.py tests/unit/test_config.py -q passes
      - .venv/bin/python -m pytest tests/integration/test_recompute_matches_migration.py tests/integration/test_recompute.py tests/integration/test_daily.py -q passes
      - grep -n "AMBIGUOUS = " src/knowledge_hub/mcp/errors.py matches
      - .venv/bin/python -m pytest -q passes
  - id: p3-leak-matrix
    title: Leak matrix and search leaks with any-of principals and cases
    depends_on: [p2-python-rules]
    complexity: M
    touches:
      - tests/support/principals.py
      - tests/support/seed.py
      - tests/integration/test_leak_matrix.py
      - tests/integration/test_search_leaks.py
      - tests/integration/test_rls_tables.py
      - tests/integration/test_list_stale.py
      - tests/integration/test_tool_get_doc.py
      - tests/integration/test_write_derived.py
    brief: |
      Read first: docs/plans/15/plan.md sections 3.2, 3.3, 3.9; tests/integration/test_leak_matrix.py; tests/support/seed.py.
      1. principals.py: add `mgmt_only = _caller("mia", ("mgmt",), "mia@example.org")`, `finance_only = _caller("finn", ("finance",), "finn@example.org")`, `mgmt_finance = _caller("max", ("finance", "mgmt"), None)` and add them to `PRINCIPALS` under the names `mgmt_only`, `finance_only`, `mgmt_finance` (`eve_nobody` is already there). `mgmt_only` and `finance_only` hold no `all`.
      2. seed.py: add cases `"mgmt_or_finance": (["finance|mgmt"], None)` and `"mgmt_and_finance": (["finance", "mgmt"], None)` to `CASES`. tool_cases.py needs no change (it builds its cases from `CASES`).
      3. In test_leak_matrix.py, test_search_leaks.py, test_tool_get_doc.py and test_write_derived.py replace every local predicate of the form `set(audiences) <= caller.audiences and (emails is None or caller.email in emails)` with `knowledge_hub.visibility.can_read(Visibility(*CASES[case]), caller.audiences, caller.email)` (in test_tool_get_doc.py and test_write_derived.py the lines at `_visible`, `test_hidden_source_reads_like_a_missing_one` and `test_every_hidden_seed_row_reads_like_a_missing_one`).
      4. test_rls_tables.py `EXPECTED`: `bob_mgmt` gains `mgmt_or_finance`; add `"mgmt_only": {"mgmt", "mgmt_or_finance"}`, `"finance_only": {"mgmt_or_finance"}`, `"mgmt_finance": {"mgmt", "mgmt_or_finance", "mgmt_and_finance"}`. Check every other principal's set against `can_read` and add what it now sees.
      5. test_list_stale.py `want` (test_each_principal_sees_only_visible_documents): add `"mgmt_only": {m}`, `"finance_only": set()`, `"mgmt_finance": {m}`.
      6. Required (3), in test_leak_matrix.py: `test_any_of_hidden_row_matches_missing_row`: for `get_doc`, `get_entity` and `search`, the response of `alice_all` for the `mgmt_or_finance` rows has the same fingerprint as for a missing id, and `mgmt_only` and `finance_only` get the row.
      7. `.venv/bin/python -m ruff check .` and `.venv/bin/python -m pytest -q` pass.
      Stop with status=blocked if a test outside the eight touched files fails because of the new principals or cases.
    acceptance:
      - .venv/bin/python -m pytest tests/integration/test_leak_matrix.py tests/integration/test_search_leaks.py -q passes
      - grep -n "mgmt_finance" tests/support/principals.py matches
      - .venv/bin/python -m pytest -q passes
  - id: p4-docs-retire
    title: Design, configuration, README and changelog describe ANY-of
    depends_on: [p2-python-rules]
    complexity: S
    touches:
      - docs/design.md
      - docs/configuration.md
      - README.md
      - CHANGELOG.md
      - deploy/kh.example.yaml
    brief: |
      Read first: docs/plans/15/plan.md section 3; docs/design.md; docs/configuration.md.
      1. docs/design.md "Roles and RLS matrix": replace the kh.visible bullet with the clause rule of plan 3.3 (every clause must share an audience with the caller; plain names are one-member clauses); add a bullet for the `kh_audience_clauses` check. "Provenance exception": "union of audiences" becomes "union of the sources' clauses, so the caller must satisfy every source". Raw bullet: one clause per matching map entry; the token must hold every mapped audience. "Promotion routing": replace the routing bullet with the rules of plan 3.5, including `ambiguous`. Add a row to "Rejected alternatives": "Two visibility columns (any and all) | Lossy for derived rows; a set of OR-clauses in the existing column is exact".
      2. docs/configuration.md: the `audiences` row (line 125) adds "Names are 1-63 characters of lowercase letters, digits, `_` or `-`, starting with a letter or digit; a name containing anything else is refused."; the `KH_EMBED_HTTP_ALLOWED_AUDIENCES` row (line 60) says "embeds only chunks whose every audience, including each member of a multi-audience list, is in it, and that have no `allowed_emails`"; the `raw_audience_map` and `entities` rows say "a list means ANY of these audiences may read the rows (a wider audience, not a narrower one)"; the restricted_audiences_imply_all section describes rule 3 of plan 3.5; add the upgrade note: "From 0007, multi-audience lists mean ANY-of; check them before upgrading."
      3. README.md "Audience model": "A row is visible when its required_audiences are all held" becomes the clause rule, and the derived sentence says a derived record needs every source.
      4. deploy/kh.example.yaml: the raw_audience_map comment shows `"series:weekly-sync": [mgmt, finance]   # mgmt or finance may read`.
      5. CHANGELOG.md under "## [Unreleased]": "Changed: an audience list in `entities.<kind>.audiences` or `raw_audience_map` now means any one of the audiences may read the rows; derived records, candidates and conflicts still need every source. Check multi-audience lists before upgrading (migration 0007)."
      6. `.venv/bin/python -m pytest -q -m unit` passes (tests/unit/test_docs_links.py).
    acceptance:
      - grep -n "ANY-of" docs/configuration.md matches
      - grep -n "ambiguous" docs/design.md matches
      - .venv/bin/python -m pytest tests/unit/test_docs_links.py -q passes
```
