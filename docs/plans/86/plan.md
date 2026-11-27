# Graduated T0 overdue weight and a one-year review_by default in propose_doc

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** An operator sets `KH_REVIEW_OVERDUE_HALF_LIFE_DAYS=180` (and optionally `KH_REVIEW_OVERDUE_FLOOR`) and restarts the hub; from then on a T0 document that is a few days past its `review_by` ranks almost as before, and one that is a year or more past it sinks toward the floor, without waiting for `kh daily`. With the default `0` nothing changes. An agent calling `propose_doc` may now leave out `review_by`; the pull request's file then carries `review_by` one year after the proposal date (`KH_DEFAULT_REVIEW_DAYS`, default 365), also when it edits an existing document.

**Scope:** The search statement weights non-hidden T0 hits by days overdue, computed in the `scored` CTE from `kh.documents.review_by` and a reference date that the search cursor carries, so every page of one search uses one date. Superseded documents, outdated records, T0a and T3 keep today's weighting. `stale` keeps its meaning in `list_stale`, `get_doc` and the hit fields. `propose_doc`'s `review_by` becomes optional. No migration, no trigger change, no rewrite of stored dates; the indexer still requires `review_by` in hand-written frontmatter. Cursors issued before the upgrade are refused once.

**Performance:** The search statement gains one primary-key join to `kh.documents` per fused candidate (at most twice `KH_SEARCH_POOL` rows); the chunk triggers and `kh daily` do no extra work.

**Review focus:** The CASE in the `scored` CTE and its proof that half-life 0 is bit-identical to today (3.1); the cursor carrying the reference date and the one-time refusal of old cursors (3.2); the decision that an edit with no `review_by` resets the date (3.3, open question Q4); the recommended values 180 days and floor 0.5 (3.1, Q2).

## Open questions (confirm before implement)

Each has a recommendation; the plan is written for the recommendation. Answer on issue #86 or when implement asks.

- **Q1. Where the age is computed.** Recommended: in the `scored` CTE, from `review_by` through a join to `kh.documents`, against a reference date bound as a parameter. Rejected: a stored `overdue_days` column on documents and chunks, because it needs a migration, a change to both chunk triggers, a daily rewrite of every overdue document's chunks, and it would make ranking depend on when `kh daily` last ran, which the ask forbids. Section 3.1.
- **Q2. Shape and recommended values.** Recommended: `weight = floor + (1 - floor) * 0.5 ^ (days_overdue / half_life)`, an asymptote that never goes below the floor; document `KH_REVIEW_OVERDUE_HALF_LIFE_DAYS=180` and `KH_REVIEW_OVERDUE_FLOOR=0.5` as the values to start with. At those values 1 day overdue weighs 0.998, 90 days 0.854, 400 days 0.607, five years about 0.5. Floor 0.5 equals today's `KH_STALE_WEIGHT`, so no overdue document ranks lower than it does today, and a T0 document at the floor (0.5) still outranks an equal T3 match (0.4). Rejected: `max(floor, 0.5 ^ (days / half_life))`, which hits the floor at a kink and halves a document at one half-life. Section 3.1.
- **Q3. Paging stability.** Recommended: the reference date is fixed on the first page and carried, HMAC-bound, in the cursor, so a search paged across midnight keeps one order. Cost: the cursor grows from 40 to 44 bytes and cursors issued before the upgrade answer `CURSOR_FOREIGN` once. Rejected: no cursor change (a day roll between pages can swap neighbours), and putting the knobs in the cursor (they are server configuration, like `KH_SEARCH_MIN_SIMILARITY`). Section 3.2.
- **Q4. An edit with `review_by` omitted.** Recommended: reset to the proposal date plus `KH_DEFAULT_REVIEW_DAYS`, as you assumed. The edit is reviewed by an approver who sees the new date in the diff, and keeping the stored date would need a database read for a value the caller can pass explicitly (`get_doc` returns it). Section 3.3.
- **Q5. Dates in the past and far ahead.** Recommended: keep both accepted, as today. A past date is the way to propose a document that is due for review at once; `9999-12-31` is a valid date and the parse-back check accepts it. Only `""` means omitted; `" "` is still `review_by is invalid`. Section 3.3.
- **Q6. Which date is "today".** Recommended: the UTC date in Python (`datetime.now(UTC).date()`) for both the search reference date and the proposal date. `kh daily` keeps using Postgres `current_date` for `stale`; the two can differ for a few hours a day, which only matters for the `stale` field, not the weight. Sections 3.1, 3.3.

## 1. Goal

Issue: https://github.com/tkcs-lab/knowledge-hub/issues/86

Today a T0 document one day past `review_by` and one five years past it get the same 0.5 penalty, and only after `kh daily` has flipped `stale`. After this change the operator can turn on a graduated weight that grows with the time overdue and is independent of `kh daily`. Separately, agents proposing documents no longer have to invent a review date: `propose_doc` writes one year from the proposal date into the file when `review_by` is left out, so the merged file is self-describing and the indexer, unchanged, accepts it.

## 2. Current state

- `src/knowledge_hub/search/sql.py:118-127`, the `scored` CTE: `m.rrf * tier_weight * CASE WHEN c.stale OR h.owner_key IS NOT NULL THEN %(stale_weight)s ELSE 1 END`. `c.stale` is the chunk's copy of `kh.documents.stale`. `SearchFilters` (sql.py:17-42) carries `stale_weight` (default 0.5); `build_search_query` (sql.py:166-194) binds it.
- `kh.chunks.stale` is copied from the owner by `kh.chunks_inherit_owner` on insert and pushed by `kh.owner_propagate_to_chunks` on `UPDATE OF audience, labels, stale, tier` (`migrations/0004_tech_tier.sql:10-41`). `kh.chunks` has no `review_by`.
- `src/knowledge_hub/jobs/daily.py` `run_daily`: `UPDATE kh.documents SET stale = (review_by < current_date) WHERE stale IS DISTINCT FROM ...`. The indexer also sets `stale` on upsert (`indexer/sync.py:84`, `u.review_by < date.today()`).
- `src/knowledge_hub/tools/search.py:64-130` builds `SearchFilters` from settings (`settings.stale_weight`, sql.py tier weights) and pages with `decode_cursor`/`encode_cursor` (`search/cursor.py`): a 32-byte HMAC over `sub|audiences hash|scope hash|offset` plus an 8-byte offset, 40 bytes, base64url. The scope (`_cursor_scope`, search.py:29-32) is the query and the filters, not the server knobs.
- Ties are broken by chunk id at every ORDER BY (sql.py:86, 97, 129, 132, 162), so for fixed data, settings and date the order is deterministic.
- T0a documents get `review_by` 100 years ahead (`indexer/tech_docs.py:52`, `tech_review_by`), so they are never stale. T3 records have no `review_by`; their chunks carry `stale = false`.
- `src/knowledge_hub/tools/propose.py:30-90`: `review_by: str` is a required argument, checked by `_DATE_RE` (`\d{4}-\d{2}-\d{2}`) and `date.fromisoformat`, error `_bad("review_by")` (text `BAD_ARG` with name `review_by`); `render_file` (`propose_render.py:38-45`) writes `review_by: "<value>"`; the parse-back check compares `back.review_by.isoformat()` with it. The description (propose.py:144-150) says `review_by is YYYY-MM-DD.`
- The tool guard (`mcp/errors.py:128-147`) fills omitted keyword arguments from the declared defaults, so a parameter with a default is optional in the schema.
- The indexer requires `review_by` (`indexer/frontmatter.py:84-96`); `kh lint` reports `missing review_by`.
- Tests: `tests/integration/test_search_ranking.py` (tier weight, `test_stale_weight_demotes_a_better_match`, cursor), `tests/integration/test_search_outdated.py` (hidden items weighted like stale), `tests/unit/test_search_sql.py`, `tests/unit/test_cursor.py`, `tests/integration/test_propose_doc.py` (`ARGS` has `review_by`, `test_bad_args` rejects `2027-02-30` and `tomorrow`, `test_edit_with_base_blob`), `tests/unit/test_tool_schemas.py`, `tests/unit/test_settings.py`. Test seeds insert documents directly with `stale` and `review_by` set independently (`tests/support/seed.py:30-38`).
- Pattern commit for a search setting with SQL, settings and tests: `9dec0ff` (the similarity floor).

## 3. Design

### 3.1 The overdue weight (phase p1)

**Settings** (`src/knowledge_hub/settings.py`, next to `stale_weight`):

```python
review_overdue_half_life_days: int = Field(0, ge=0, le=36500)
review_overdue_floor: float = Field(0.5, ge=0.0, le=1.0)
```

Environment names `KH_REVIEW_OVERDUE_HALF_LIFE_DAYS` and `KH_REVIEW_OVERDUE_FLOOR`. `0` switches the graduated weight off.

**Python formula** (`src/knowledge_hub/search/sql.py`, module level, used by tests as the oracle and named in the docs):

```python
OVERDUE_MAX_HALF_LIVES = 64


def overdue_weight(days_overdue: int, half_life_days: int, floor: float) -> float:
    """The score multiplier of a T0 hit ``days_overdue`` days past review_by; the SQL computes the same."""
    if half_life_days <= 0 or days_overdue <= 0:
        return 1.0
    return floor + (1.0 - floor) * 0.5 ** min(days_overdue / half_life_days, OVERDUE_MAX_HALF_LIVES)
```

(`half_life_days <= 0` returns 1.0 here only as a guard; in SQL half-life 0 takes the old `stale` branch.)

**`SearchFilters`** gains three fields after `stale_weight`:

```python
overdue_half_life_days: int = 0
overdue_floor: float = 0.5
as_of: dt.date | None = None   # the reference date; None means today in UTC
```

`build_search_query` binds `"half_life": filters.overdue_half_life_days`, `"overdue_floor": filters.overdue_floor`, `"as_of": filters.as_of or dt.datetime.now(dt.UTC).date()`.

**The `scored` CTE** becomes exactly:

```sql
scored AS (
  SELECT c.id, c.owner_key, c.tier, c.stale, c.text, c.document_id, c.record_id,
         (h.owner_key IS NOT NULL) AS outdated,
         m.rrf * (%(tier_weights)s::jsonb ->> c.tier)::float8
               * CASE WHEN h.owner_key IS NOT NULL THEN %(stale_weight)s
                      WHEN %(half_life)s::int > 0 AND c.tier = 'T0' AND od.review_by IS NOT NULL
                      THEN %(overdue_floor)s::float8 + (1 - %(overdue_floor)s::float8)
                           * power(0.5::float8,
                                   least(greatest(%(as_of)s::date - od.review_by, 0)::float8
                                         / greatest(%(half_life)s::int, 1), 64))
                      WHEN c.stale THEN %(stale_weight)s
                      ELSE 1 END AS score,
         CASE WHEN c.embedding IS NOT NULL AND c.embedding_model = %(model)s
              THEN 1 - (c.embedding <=> %(qvec)s::vector) END AS similarity
    FROM main_fused m JOIN kh.chunks c ON c.id = m.id
    LEFT JOIN hidden h ON h.owner_key = c.owner_key
    LEFT JOIN kh.documents od ON od.id = c.document_id
),
```

Why this is right:

- **Half-life 0 is today's statement.** With `half_life = 0` the second branch is never taken: hidden gives `stale_weight`, stale gives `stale_weight`, else `1`, which is today's `c.stale OR hidden`. The result type is unchanged (float8 from the bound float and the integer 1), so scores are bit-identical. The new parameters are bound but unused.
- **Hidden wins.** A superseded T0 document shown with `include_outdated=true` keeps the flat `stale_weight` whatever its age; an outdated record likewise.
- **T0a and T3 never take the new branch** (`c.tier = 'T0'`): T0a keeps `stale` (always false), T3 chunks keep `stale = false`, so both keep weight 1.
- **Independent of `kh daily`.** The branch reads `review_by` and the bound date, never `stale`. The `stale` hit field still comes from the stored flag, so with the weight on a hit can say `stale: false` (daily not run yet) while being weighted as 1 day overdue; the docs say so.
- **Row-level security.** `kh_app` may read a document when it may read its audience; a chunk inherits the audience of its document, so every chunk that passed RLS finds its document. `od` is a separate alias because the final SELECT already uses `d`.
- **No underflow.** The exponent is capped at 64 half-lives (`0.5^64` is about 5e-20), so `power` never underflows to an error in Postgres, and `greatest(..., 1)` keeps the division defined even though the branch is guarded.
- **Cost.** One primary-key lookup in `kh.documents` per row of `main_fused` (at most `2 * KH_SEARCH_POOL`). No migration, no trigger change, no extra work in `kh daily`.

**Rejected: a stored `overdue_days` column.** It would need a migration adding the column to `kh.documents` and `kh.chunks`, a change to `kh.chunks_inherit_owner` and `kh.owner_propagate_to_chunks` (and its `UPDATE OF` list), and a daily rewrite of the chunks of every overdue document (each changes every day), and ranking would still depend on when `kh daily` last ran. Rejected: copying `review_by` to chunks (a migration and a trigger change for what one join gives).

**`tools/search.py`** passes `overdue_half_life_days=settings.review_overdue_half_life_days`, `overdue_floor=settings.review_overdue_floor` and `as_of=as_of` to `SearchFilters`, where `as_of = _today()` and

```python
def _today() -> dt.date:
    """The reference date of a new search (UTC); tests replace it."""
    return dt.datetime.now(dt.UTC).date()
```

(p2 replaces `as_of = _today()` with the cursor's date on later pages.) The cursor scope (`_cursor_scope`) is not changed: the knobs are server configuration.

### 3.2 The reference date in the cursor (phase p2)

`src/knowledge_hub/search/cursor.py`:

- `_mac(key, sub, audiences, query, offset, as_of: dt.date)`: the message becomes `f"{sub}|{aud}|{qh}|{offset}|{as_of.isoformat()}"`.
- `encode_cursor(key, sub, audiences, query, offset, as_of: dt.date) -> str`: token = MAC (32 bytes) + `offset.to_bytes(8, "big")` + `as_of.toordinal().to_bytes(4, "big")`, base64url without padding (44 bytes before encoding).
- `decode_cursor(...) -> tuple[int, dt.date]`: a raw length other than `_MAC_LEN + 12` is `CURSOR_FOREIGN` (so a 40-byte cursor from before the upgrade is refused); `dt.date.fromordinal` raising `ValueError` is `CURSOR_FOREIGN`; the MAC is checked over offset and date; `offset > max_offset` is `CURSOR_FOREIGN` as today. Add `_DATE_LEN = 4` next to `_MAC_LEN`.
- `tools/search.py`: `offset, as_of = 0, _today()`; with a cursor, `offset, as_of = decode_cursor(...)`; `encode_cursor(..., next_offset, as_of)`.

No other code calls the cursor functions (`list_stale` has its own position cursor in `tools/docs.py`). An old date in a replayed cursor only changes weights of that caller's own search, so the date needs no age limit.

### 3.3 `review_by` default in `propose_doc` (phase p3)

`src/knowledge_hub/settings.py`: `default_review_days: int = Field(365, ge=1, le=3650)` (env `KH_DEFAULT_REVIEW_DAYS`).

`src/knowledge_hub/tools/propose.py`:

- Signature: move `review_by` after `body` and give it a default: `..., title: str, body: str, review_by: str = "", labels: list[str] = [], ...`. Tool arguments are keywords, so callers are not affected.
- Add

  ```python
  def _today() -> dt.date:
      """The proposal date (UTC); tests replace it."""
      return dt.datetime.now(dt.UTC).date()
  ```

- Before the `_DATE_RE` check: `if review_by == "": review_by = (_today() + dt.timedelta(days=deps.settings.default_review_days)).isoformat()`. Then the existing check runs unchanged on the final value, and `render_file` and the parse-back check use it, so the rendered frontmatter holds the explicit date. `propose_render.py` needs no change.
- An edit (`base_blob` set) with `review_by` empty gets the same default: the stored date is not read (Q4).
- Past and far dates stay accepted (Q5); `" "` fails `_DATE_RE` and answers `review_by` as today.
- Description, exactly:

  `"Propose a new or changed document in the repository of a group you hold, as a pull request that its approvers review. To change an existing file pass its current blob_sha as base_blob; leave it empty to create a new file. review_by is YYYY-MM-DD and optional: left empty it is set to the proposal date plus the server's review period (365 days by default), also when you change an existing document; to keep a document's date pass the review_by that get_doc returned. To change an existing document keep its supersedes list: pass the supersedes that get_doc returned."`

The indexer, `kh lint`, `move.py` and `tech_docs.py` are not changed.

### 3.4 Docs (phase p4)

- `docs/design.md`, Search paragraph: replace "weighted by tier and by the stale flag" with "weighted by tier and by how long a T0 document is past its `review_by` (or by the stale flag while `KH_REVIEW_OVERDUE_HALF_LIFE_DAYS` is 0)", and add after the `include_outdated` sentence: "With `KH_REVIEW_OVERDUE_HALF_LIFE_DAYS` above 0 a T0 hit that is not hidden is weighted `floor + (1 - floor) * 0.5^(days overdue / half-life)`, days overdue being the days from `review_by` to the search's reference date (UTC, fixed on the first page and carried in the cursor), capped at 64 half-lives; the weight reads `review_by` directly, so it does not wait for `kh daily`, while the hit's `stale` field stays the stored flag. Superseded documents and outdated records keep the flat `KH_STALE_WEIGHT`; T0a (its `review_by` is a century ahead) and T3 (record recency is a separate change) are not weighted by age. At 0 (the default) a stale or hidden hit is weighted by `KH_STALE_WEIGHT` exactly as before. Like the similarity floor, the two settings are server configuration and outside the cursor scope; the reference date is in the cursor, so every page of one search uses one date." Change "Cursors are HMAC-bound to the caller, the query and the filters, with a bounded offset." to "... with a bounded offset and the reference date."
- `docs/design.md`, Indexer frontmatter line: after "`stale` is `review_by < today`, recomputed by `kh daily`." add "`propose_doc` fills an omitted `review_by` (see Tools); a hand-written file without it is still rejected."
- `docs/design.md`, Tools, the `propose_doc` bullet: add "`review_by` is optional: empty means the proposal date (UTC) plus `KH_DEFAULT_REVIEW_DAYS`, written into the file, also for an edit; an explicit date, in the past or far ahead, is kept."
- `docs/design.md`, Rejected alternatives, two rows: "A stored `overdue_days` column on documents and chunks | Needs a migration, two trigger changes and a daily rewrite of every overdue document's chunks, and ranking would depend on when `kh daily` ran; the search computes the age from `review_by`." and "Keeping the stored `review_by` when an edit omits it | An edit is a review; the approver sees the new date in the diff, and the caller can pass the old date from `get_doc`."
- `docs/configuration.md`, "Search, limits and tools" table, after `KH_STALE_WEIGHT` (whose text becomes "Multiplier for a stale hit (while `KH_REVIEW_OVERDUE_HALF_LIFE_DAYS` is 0) or an included outdated or superseded hit."):
  - `KH_REVIEW_OVERDUE_HALF_LIFE_DAYS` | `0` | 0 to 36500. Above 0, a T0 search hit past its `review_by` is weighted `floor + (1 - floor) * 0.5^(days overdue / half-life)` instead of `KH_STALE_WEIGHT`; 0 switches this off. T0a and T3 are not weighted by age; superseded and outdated hits keep `KH_STALE_WEIGHT`. Start with `180`.
  - `KH_REVIEW_OVERDUE_FLOOR` | `0.5` | 0 to 1: the weight a T0 hit approaches as it gets further overdue. `0.5` keeps every overdue document at or above today's stale weight and above an equal T3 match.
  - after `KH_PROPOSE_MAX_OPEN`: `KH_DEFAULT_REVIEW_DAYS` | `365` | 1 to 3650: days after the proposal date that `propose_doc` writes as `review_by` when the caller leaves it empty.
- `docs/runbook.md`, "Changing T0": add "`propose_doc` writes `review_by` one year (`KH_DEFAULT_REVIEW_DAYS`) after the proposal date when the caller leaves it empty, also for an edit; check the date in the diff before merging. A file written by hand still needs `review_by`." Add a subsection "### Enabling the graduated overdue weight" with: ship with `KH_REVIEW_OVERDUE_HALF_LIFE_DAYS=0`; per environment set it to `180` (and `KH_REVIEW_OVERDUE_FLOOR`, default `0.5`), restart `kh serve`; compare a few known queries before and after with `search`; set it back to `0` to undo. Say that search cursors issued before the upgrade answer `CURSOR_FOREIGN` once and the caller repeats the search.
- `README.md`: the tools table does not change (its `propose_doc` and `search` rows do not mention `review_by` or weights).
- `CHANGELOG.md` under `## [Unreleased]`: `### Added`: "The settings `KH_REVIEW_OVERDUE_HALF_LIFE_DAYS` (default 0, off) and `KH_REVIEW_OVERDUE_FLOOR` (default 0.5): `search` can weight a T0 document by how long it is past its `review_by`, decaying toward the floor, instead of the flat `KH_STALE_WEIGHT`; T0a, T3, superseded and outdated hits are unchanged. Once enabled, a document a few days overdue ranks almost as if it were current, where today it is halved at once." and "The setting `KH_DEFAULT_REVIEW_DAYS` (default 365)." `### Changed`: "`propose_doc`'s `review_by` is optional: left empty, the file gets the proposal date plus `KH_DEFAULT_REVIEW_DAYS`, also when it edits an existing document." and "Search cursors carry the search's reference date; cursors issued before the upgrade answer `CURSOR_FOREIGN` once."

## 4. Manual steps

### Before implementation

None.

### After implementation

1. **ma1-t0-feed-skill** (about 5 minutes). The `knowledge-hub-t0-feed` skill that agents use with `propose_doc` lives outside this repository.
   1. Open the skill's source where you maintain it (Claude settings, Skills, `knowledge-hub-t0-feed`).
   2. Search it for `review_by`. If it says the argument is required, change it to: "`review_by` is optional; left empty it becomes one year after the proposal date. To keep an existing document's date pass the `review_by` that `get_doc` returned."
   3. Report back in the pull request: `ma1: skill updated` or `ma1: skill did not mention review_by`.

The rollout of the weight itself (half-life 0 at release, then 180 per environment) happens after the release, as the runbook section of 3.4 says; it is not a step of this pull request.

## 5. Risks and open questions

- The open questions Q1 to Q6 above. If the owner picks another answer, the brief of the affected phase is wrong; implement must not start before they are confirmed.
- **The mcp test client must run the app in process** (`tests/support/mcp_client.py` says it does), so `monkeypatch.setattr("knowledge_hub.tools.search._today", ...)` reaches the tool. A worker who sees the patched date ignored stops with `status=blocked`.
- **Bit-identical half-life 0.** If an existing test in `test_search_ranking.py` or `test_search_outdated.py` that compares exact scores fails with the new CASE at default settings, the CASE is not equivalent: stop with `status=blocked` rather than loosening the assertion.
- **RRF ties in new ordering tests.** Weights near 1 (a document 1 day overdue at half-life 180 weighs 0.998) are smaller than the RRF gap between neighbouring ranks (about 1.6%), so the ordering test uses half-life 1 and the 180-day values are checked by score ratio, as step 6 of p1's brief says. A worker who finds an ordering test flaky stops rather than adding sleeps or retries.
- **`power` in Postgres.** The exponent cap at 64 avoids an underflow error; if the floor test with 100000 days overdue raises `value out of range`, the cap is missing.
- **Not taken from the architect review (adhoc run).** (1) Deriving the hit's `stale` field from the reference date when the weight is on: the owner asked to keep `stale` as it is, so the field stays the stored flag and the docs say it can lag the weight by up to a day. (2) Refusing cursors whose reference date is older than yesterday: the date only changes the weights of the caller's own search, so a time limit would add a failure without protecting anything. (3) A test that runs the old SQL text next to the new one: the unchanged existing ranking and outdated tests plus test g of p1 cover the half-life-0 equivalence. Also noted from that review: with the recommended values a T0 document one day overdue scores about 1.0 instead of 0.5, and a fresh T0a (0.7) outranks an overdue T0 only after about 238 days. This softening is intended and the changelog says so.
- **Deploy.** Agents holding a cursor across the upgrade get `CURSOR_FOREIGN` once (documented in the changelog and runbook).

## Implementation manifest

```yaml
plan_slug: t0-overdue-review-defaults
feature_branch: feat/t0-overdue-review-defaults
tracking_issue: 86
max_parallel: 2
manual_before: []
manual_after:
  - id: ma1-t0-feed-skill
    title: Check the knowledge-hub-t0-feed skill text for a required review_by
    why: The skill lives outside the repository, so no phase can change it; agents would keep inventing dates.
verify_after_merge:
  - .venv/bin/python -m pytest -q -k search
  - .venv/bin/python -m pytest -q -k cursor
  - .venv/bin/python -m pytest -q -k propose
  - .venv/bin/python -m pytest -q -k settings
final_checks:
  - "`tld plan deviations check --file docs/plans/86/deviations.md --plan docs/plans/86/plan.md --all` exits 0"
  - "CHANGELOG.md has the entries of plan section 3.4 under ## [Unreleased] and no new version heading"
phases:
  - id: p1-overdue-weight
    title: Graduated overdue weight for T0 hits in the search statement
    depends_on: []
    complexity: M
    touches:
      - src/knowledge_hub/settings.py
      - src/knowledge_hub/search/sql.py
      - src/knowledge_hub/tools/search.py
      - tests/unit/test_search_sql.py
      - tests/unit/test_settings.py
      - tests/integration/test_search_ranking.py
      - tests/integration/test_search_overdue.py
    brief: |
      Read first: docs/plans/86/plan.md sections 2, 3.1 and 5; README.md; docs/design.md section Search.
      Pattern commit: 9dec0ff (a search setting with SQL, settings and tests). Set up with
      `bash scripts/ensure_test_pg.sh` and `python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"`.
      1. src/knowledge_hub/settings.py: add `review_overdue_half_life_days` and `review_overdue_floor` exactly as
         plan 3.1 says, directly after `stale_weight`.
      2. src/knowledge_hub/search/sql.py: add `import datetime as dt`, `OVERDUE_MAX_HALF_LIVES` and
         `overdue_weight` exactly as plan 3.1; add the three `SearchFilters` fields after `stale_weight`; replace the
         `scored` CTE in `_BODY` with the text of plan 3.1 verbatim; bind `half_life`, `overdue_floor` and `as_of`
         in `build_search_query`.
      3. src/knowledge_hub/tools/search.py: add `import datetime as dt` and `_today()` exactly as plan 3.1; in
         `search`, set `as_of = _today()` before building the filters and pass `overdue_half_life_days`,
         `overdue_floor` and `as_of` to `SearchFilters`. Do not change `_cursor_scope` or the cursor calls.
      4. tests/unit/test_search_sql.py: add `test_overdue_weight_is_one_when_not_overdue_or_off`
         (`overdue_weight(0, 180, 0.5) == 1.0`, `overdue_weight(-5, 180, 0.5) == 1.0`, `overdue_weight(400, 0, 0.5) == 1.0`),
         `test_overdue_weight_values` (`overdue_weight(1, 180, 0.5) == approx(0.99808, abs=1e-5)`,
         `overdue_weight(180, 180, 0.5) == approx(0.75)`, `overdue_weight(400, 180, 0.5) == approx(0.60716, abs=1e-5)`),
         `test_overdue_weight_never_below_the_floor` (`overdue_weight(10**6, 1, 0.3) >= 0.3` and `== approx(0.3)`),
         and `test_build_search_query_binds_overdue_params` (default filters bind `half_life` 0, `overdue_floor` 0.5
         and a `dt.date` `as_of`; an explicit `as_of` is bound as given; the SQL contains
         `LEFT JOIN kh.documents od ON od.id = c.document_id` and
         `WHEN %(half_life)s::int > 0 AND c.tier = 'T0' AND od.review_by IS NOT NULL`).
      5. tests/unit/test_settings.py: extend `test_defaults` with `s.review_overdue_half_life_days == 0` and
         `s.review_overdue_floor == 0.5`; add `test_overdue_settings_are_bounded` (half-life -1 and 36501, floor -0.1
         and 1.1 raise `ValidationError`).
      6. New tests/integration/test_search_overdue.py, copying the fixtures of tests/integration/test_search_ranking.py
         (cursor_key, app_settings, deps, db, hits). A fixture `as_of` monkeypatches
         `knowledge_hub.tools.search._today` to return `date(2026, 6, 1)`; review dates are relative to it. Tests:
         a. `test_fresh_beats_one_day_and_400_days_overdue`: half-life 1, floor 0.2 (set on `app_settings` as
            test_search_ranking.py does for tier weights); docs "alpha beta" (review_by as_of - 400, best match),
            "alpha beta gamma" (as_of - 1), "alpha beta gamma delta epsilon" (review_by as_of + 30, worst match), all
            `stale=False`; order is fresh, 1 day, 400 days.
         b. `test_weight_follows_days_overdue_at_recommended_values`: one doc per offset 1 and 400 days overdue,
            `stale=False`; read each score at half-life 0, then at half-life 180 floor 0.5; each ratio equals
            `overdue_weight(days, 180, 0.5)` (approx 1e-9).
         c. `test_floor_is_respected_for_very_old_documents`: review_by `date(1990, 1, 1)`, half-life 1, floor 0.3;
            ratio to the half-life-0 score equals approx 0.3 and is >= 0.3; the call does not error.
         d. `test_not_yet_flagged_document_is_weighted_without_kh_daily`: a doc 400 days overdue with `stale=False`:
            at half-life 0 its ratio to a fresh baseline is 1 (today's behaviour depends on the flag); at half-life
            180 the ratio is `overdue_weight(400, 180, 0.5)`; the hit's `stale` field stays False.
         e. `test_t0a_and_t3_scores_are_unchanged`: a T0a doc (`tier="T0a"`, `repo` a tech repo of the test config or
            any repo; review_by as_of - 400, stale True) and a T3 record; their scores at half-life 180 floor 0.2 equal
            their scores at half-life 0 exactly.
         f. `test_hidden_hits_keep_the_flat_stale_weight`: a superseded T0 doc 400 days overdue with
            `include_outdated=true`: its score at half-life 180 equals its score at half-life 0.
         g. `test_half_life_zero_matches_the_stale_flag_exactly`: a stale doc (stale=True, 400 days overdue) and a
            fresh doc: at the default settings the stale doc's score is exactly `stale_weight` times its score with
            `stale_weight=1.0`.
      7. Run `.venv/bin/python -m pytest -q tests/integration/test_search_ranking.py tests/integration/test_search_outdated.py`
         unchanged: every existing test must pass without edits (the half-life-0 regression). If one fails, stop with
         status=blocked and quote it; never edit an existing assertion.
      8. Run `.venv/bin/python -m ruff check .` and `.venv/bin/python -m pytest -q`.
      No docs or changelog in this phase (p4 writes them).
    acceptance:
      - .venv/bin/python -m pytest tests/unit/test_search_sql.py -q passes
      - .venv/bin/python -m pytest tests/unit/test_settings.py -q passes
      - .venv/bin/python -m pytest tests/integration/test_search_overdue.py -q passes, with tests a to g present by name
      - .venv/bin/python -m pytest tests/integration/test_search_ranking.py tests/integration/test_search_outdated.py -q passes with no change to those files
      - grep -n "LEFT JOIN kh.documents od ON od.id = c.document_id" src/knowledge_hub/search/sql.py matches
      - .venv/bin/python -m ruff check . passes
  - id: p2-cursor-reference-date
    title: Carry the search reference date in the cursor
    depends_on: [p1-overdue-weight]
    complexity: S
    touches:
      - src/knowledge_hub/search/cursor.py
      - src/knowledge_hub/tools/search.py
      - tests/unit/test_cursor.py
      - tests/integration/test_search_overdue.py
    brief: |
      Read first: docs/plans/86/plan.md sections 3.1 and 3.2; docs/design.md section Search.
      Set up with `bash scripts/ensure_test_pg.sh` and the venv as in README.md Quick start.
      1. src/knowledge_hub/search/cursor.py: change `_mac`, `encode_cursor` and `decode_cursor` exactly as plan 3.2
         (add `import datetime as dt` and `_DATE_LEN = 4`; `decode_cursor` returns `tuple[int, dt.date]`).
      2. src/knowledge_hub/tools/search.py: `offset, as_of = 0, _today()`; with a cursor
         `offset, as_of = decode_cursor(...)`; pass `as_of` to `encode_cursor`. Nothing else changes.
      3. tests/unit/test_cursor.py: update `tok()` and every call to the new signatures (use `D = date(2026, 6, 1)`);
         `test_round_trip` asserts `== (20, D)`; add `test_date_is_bound` (a cursor decoded after replacing its last
         4 bytes with another ordinal is CURSOR_FOREIGN), `test_old_40_byte_cursor_is_foreign` (base64url of
         32 + 8 bytes is CURSOR_FOREIGN) and `test_bad_ordinal_is_foreign` (ordinal 0 with a valid MAC over it
         cannot be built, so assert a 44-byte token with ordinal bytes `0xFFFFFFFF` is CURSOR_FOREIGN). Keep every
         existing case.
      4. tests/integration/test_search_overdue.py: add `test_paging_is_stable_across_a_day_change`: half-life 1,
         floor 0.2, five docs "wombat <n>" with review_by as_of - n days for n in 0..4 (stale False); one call with
         limit 5 gives the reference order; then page with limit 2, and before pages 2 and 3 re-patch `_today` to
         as_of + 200 days; the concatenated pages equal the reference order with no duplicates. Add
         `test_paging_without_day_change_is_stable` with the same docs at half-life 180 and no re-patch.
      5. Run `.venv/bin/python -m pytest -q tests/integration/test_search_ranking.py`: its cursor test must pass
         unchanged. Run `.venv/bin/python -m ruff check .` and `.venv/bin/python -m pytest -q`.
      If `rg -n "encode_cursor|decode_cursor" src` shows a caller other than tools/search.py, stop with status=blocked.
    acceptance:
      - .venv/bin/python -m pytest tests/unit/test_cursor.py -q passes, with test_date_is_bound and test_old_40_byte_cursor_is_foreign present
      - .venv/bin/python -m pytest tests/integration/test_search_overdue.py -q passes, with test_paging_is_stable_across_a_day_change present
      - .venv/bin/python -m pytest tests/integration/test_search_ranking.py -q passes with no change to that file
      - .venv/bin/python -m ruff check . passes
  - id: p3-propose-review-default
    title: Optional review_by in propose_doc with KH_DEFAULT_REVIEW_DAYS
    depends_on: [p1-overdue-weight]
    complexity: S
    touches:
      - src/knowledge_hub/settings.py
      - src/knowledge_hub/tools/propose.py
      - tests/integration/test_propose_doc.py
      - tests/unit/test_tool_schemas.py
      - tests/unit/test_settings.py
    brief: |
      Read first: docs/plans/86/plan.md sections 2 and 3.3; docs/design.md section Tools (the propose_doc bullet).
      Set up with `bash scripts/ensure_test_pg.sh` and the venv as in README.md Quick start.
      1. src/knowledge_hub/settings.py: add `default_review_days: int = Field(365, ge=1, le=3650)` directly after
         `propose_max_open`.
      2. src/knowledge_hub/tools/propose.py: apply plan 3.3 exactly: move `review_by` after `body` with default
         `""`, add `_today()`, fill the default before the `_DATE_RE` check, and replace the description with the
         text of plan 3.3 verbatim. Do not change propose_render.py, the indexer or kh lint.
      3. tests/integration/test_propose_doc.py: add a fixture that monkeypatches
         `knowledge_hub.tools.propose._today` to `date(2026, 11, 27)`. Add:
         `test_omitted_review_by_is_one_year_after_the_proposal_date` (ARGS without review_by; the file on the branch
         holds `review_by: "2027-11-27"`), `test_empty_review_by_is_the_default` (`review_by=""`, same date),
         `test_default_review_days_follows_the_setting` (set `deps.settings.default_review_days = 30` the way the
         module's other tests change settings; date `2026-12-27`), `test_explicit_review_by_is_kept`
         (`2027-06-30` is written), `test_past_and_distant_review_by_are_accepted` (parametrize `2020-01-01` and
         `9999-12-31`; each is written as given), `test_edit_without_review_by_resets_to_the_default` (like
         `test_edit_with_base_blob`, old file has `review_by: 2099-01-01` via `md(...)`; the proposed file holds
         `review_by: "2027-11-27"`). Add `({"review_by": " "}, "review_by")` to the `test_bad_args` parameters;
         keep the existing ones.
      4. tests/unit/test_tool_schemas.py: add `test_propose_doc_review_by_is_optional`: the propose_doc schema has
         `review_by` with default `""`, `review_by` is not in `required`, and the description contains
         `review_by is YYYY-MM-DD and optional`. The existing flat-schema tests must still pass.
      5. tests/unit/test_settings.py: extend `test_defaults` with `s.default_review_days == 365`; add
         `test_default_review_days_is_bounded` (0 and 3651 raise `ValidationError`).
      6. `rg -n "review_by" src tests docs README.md` and confirm no text outside docs/ says propose_doc requires it
         (docs are p4's). Run `.venv/bin/python -m ruff check .` and `.venv/bin/python -m pytest -q`.
      If the tool guard refuses a call that omits review_by (`internal error`), stop with status=blocked: the default
      handling of mcp/errors.py differs from plan section 2.
    acceptance:
      - .venv/bin/python -m pytest tests/integration/test_propose_doc.py -q passes, with test_omitted_review_by_is_one_year_after_the_proposal_date and test_edit_without_review_by_resets_to_the_default present
      - .venv/bin/python -m pytest tests/unit/test_tool_schemas.py -q passes, with test_propose_doc_review_by_is_optional present
      - .venv/bin/python -m pytest tests/unit/test_settings.py -q passes
      - .venv/bin/python -m pytest tests/unit/test_frontmatter.py tests/unit/test_cli_lint.py -q passes with no change to those files
      - .venv/bin/python -m ruff check . passes
  - id: p4-docs-changelog
    title: Design, configuration, runbook and changelog
    depends_on: [p2-cursor-reference-date, p3-propose-review-default]
    complexity: S
    touches:
      - docs/design.md
      - docs/configuration.md
      - docs/runbook.md
      - CHANGELOG.md
    brief: |
      Read first: docs/plans/86/plan.md section 3.4 and docs/plans/86/deviations.md (if it exists).
      1. Apply every docs/design.md change of plan 3.4 (Search paragraph, Indexer frontmatter line, Tools propose_doc
         bullet, two Rejected alternatives rows), adjusting only where deviations.md records a different build.
      2. Apply the docs/configuration.md changes of plan 3.4 (the KH_STALE_WEIGHT text and three new rows).
      3. Apply the docs/runbook.md changes of plan 3.4 ("Changing T0" sentence and the new subsection
         "### Enabling the graduated overdue weight" right after "Changing T0").
      4. CHANGELOG.md: add the entries of plan 3.4 under `## [Unreleased]`, merging into the existing `### Added` and
         `### Changed` lists; open no version heading.
      5. README.md is not changed (plan 3.4 says why); do not edit it.
      6. Run `.venv/bin/python -m pytest -q tests/unit/test_docs_links.py` and `.venv/bin/python -m ruff check .`.
    acceptance:
      - grep -n "KH_REVIEW_OVERDUE_HALF_LIFE_DAYS" docs/configuration.md docs/design.md docs/runbook.md CHANGELOG.md matches in each file
      - grep -n "KH_DEFAULT_REVIEW_DAYS" docs/configuration.md docs/design.md CHANGELOG.md matches in each file
      - grep -n "Enabling the graduated overdue weight" docs/runbook.md matches
      - grep -n "overdue_days" docs/design.md matches (the rejected alternative row)
      - .venv/bin/python -m pytest tests/unit/test_docs_links.py -q passes
```
