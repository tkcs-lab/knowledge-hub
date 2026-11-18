# Knowledge hub design

The hub gives agents one MCP interface over two tiers: canonical documents in git (T0) and raw records in Postgres (T3).
Every result carries its tier and is filtered by the caller's groups inside Postgres, by row-level security, not in Python.
This document records the decisions as built, the alternatives that were rejected and one deviation from the PrivacyFence contract.

## Tiers and groups

| Tier | Source | Written by | Trust |
|---|---|---|---|
| T0 | `kh.documents`, from `*.md` files on `main` of a group's repo | a pull request merged by one of the group's approvers | normative |
| T3 | `kh.records`, body in S3 | `ingest_record` | raw material |

- A **group** is a name matching `^[a-z0-9][a-z0-9_-]{0,62}$`. It is the audience stored on rows, the audience name an n8n token carries and the access group of one T0 repo. Every group has exactly one repo.
- A **label** matches `^[a-z0-9][a-z0-9_.:-]{0,62}$` (`product:ember` is legal), is defined in `kh.yaml` and stored in `kh.labels`. Label names are visible to every caller; do not put secrets in them.
- An **archived group** (`archived: true` on the group in `kh.yaml`) acts as removed without deleting data: nobody holds it, so its documents and records are hidden, `ingest_record` and `propose_doc` refuse it and nothing is indexed for it. The database is unchanged; the state lives in `kh.yaml` and the hub reads it at start.
- An **archived label** (a name listed under `archived_labels` in `kh.yaml`) vanishes from `list_labels`, from result labels (`search`, `get_doc`, `list_stale`, `usage`) and from search filters, and writes using it fail with `unknown label`. The database is unchanged: the label stays in `kh.labels`, so documents that name it are still indexed, and the state lives in `kh.yaml`.
- Tools return the ids `doc:<uuid>` and `rec:<uuid>`. `search` defaults to `min_tier=T3`, which searches T0 and T3; `min_tier=T0` searches documents only; tier weights are `T0:1.0,T3:0.4`.

## Trust: Forgejo is the control

- The indexer reads the tree of `main` as Forgejo reports it. There is no trust walk, no anchor, no review verdict, no `approved_by`, no `disputed` status and no version table.
- The approval is the merge. Branch protection forbids direct push, the merge whitelist is the group's `<group>-approve` team, and required approvals are 0, so an approver may merge their own pull request. Forgejo's merge API takes `head_commit_id` and answers 409 when the head moved, so a merge through the UI is bound to the head the approver saw.
- One cheap check remains: the indexer raises the alert `main of <repo> was rewritten; run kh rebuild after review` when the new head does not descend from `repo_state.last_indexed_sha`.
- The webhook checks the HMAC signature and only queues a job; everything is re-fetched with the indexer token. Only `refs/heads/main` of a repo listed under a group queues a `git_push` job.

### What `kh setup plan` verifies

T0 trust rests entirely on Forgejo, so `plan` checks it and reports any difference as drift:

- per group, the teams `<group>` (write on the repo) and `<group>-approve` (read, in the merge whitelist) with exactly the configured members and approvers;
- an archived group is checked only for its Forgejo repo being archived;
- the repo is private with `main`, the bot has write and the indexer has read;
- branch protection on `main`: no direct push, the merge whitelist is exactly the approve team, `required_approvals: 0`, `apply_to_admins: true`, read back after every write (Forgejo silently drops whitelist teams that cannot read the repo);
- the identity of the bot and indexer tokens (`GET /user` equals the configured login, the bot has no repo admin, the indexer no push, neither is an org owner or site admin);
- the org webhook exists. Forgejo never returns its secret, so `plan` can only say `i webhook secret not verifiable`;
- a report-only line lists the members of the Owners team as people who can bypass protection.

`plan` exits 1 on drift, so running it on a schedule catches a protection change made in the Forgejo UI.

## Access model

- **Resolution.** `ClaimMiddleware` resolves the caller's groups from the verified claim. A `claude_client` holds the groups of `kh.yaml` in which a member's `people` email equals the claim's verified email, compared in lower case; a claim without an email gives no group, and the claim's `audiences` are ignored. An `n8n_workflow` holds the claim's `audiences` that are configured, non-archived groups, because a workflow has no `people` entry. A `service` calls no tool. The config is read when `kh serve` starts, so a change to `people` or to a group needs a restart.
- **The rule.** `kh.can_read(a)` is `a = ANY(kh.caller_audiences())`. A document, record or chunk is visible when its audience is held; inserting a record needs the audience too.
- **One list for people.** `kh.yaml` decides both a person's Forgejo teams and what the hub lets them read, so a joiner or leaver changes one file. PrivacyFence keeps authentication, the verified email and the write popups; its group-to-audience mapping is no longer used for people. A person whose PrivacyFence email differs from their `people` email reads nothing.
- **n8n tokens still need PrivacyFence's group map.** A person's groups come from `people` in `kh.yaml` through the verified email, so PrivacyFence needs no map entry for people. A scoped token's audiences are its creator's PrivacyFence audiences at each call, intersected with those ticked at creation (PrivacyFence ADR 0298), so `upstream_mcp.kh.groups` in PrivacyFence's `admin_config.json` keeps an entry for each audience an n8n workflow should hold, and each administrator who creates an n8n token is a member of those groups through a membership source (a `static` file, or the default `snapshot` sync, where a restricted audience is approved per person). Without the map every n8n token holds no audience, and `ingest_record` and `propose_doc` answer `audience is not held by this caller or not configured`. The hub's rule is unchanged: it ignores a `claude_client` claim's audiences and keeps an `n8n_workflow` claim's audiences that are configured groups.

### Accepted residuals

- A caller holding two groups can `ingest_record` or `propose_doc` into the wider one. The PrivacyFence popup shows `audience` and `group` and is the only gate.
- A leaked bot token can create branches and pull requests, push to open pull-request branches in every repo and read every T0 repo. It cannot merge.
- With required approvals 0 the approver must review the final diff before merging.
- The verified email is the identity: a Workspace email that is reassigned gives its new holder the old person's groups until `people` is edited. Remove a leaver from `people` first.
- Exact vector scans take time in proportion to the number of hidden rows; this timing is the accepted residual of the no-existence-leak rule.
- Stored query text can hold sensitive words. Readers are limited by the subset rule, retention is `KH_USAGE_RETENTION_DAYS`, and `KH_USAGE_LOG=false` switches logging off.

## Roles and RLS matrix

- Three roles, none superuser, none BYPASSRLS: `kh_migrator` (owns schema `kh`, migrations and backups), `kh_app` (request path) and `kh_indexer` (worker, rebuild, daily, requeue).
- Roles are created outside the migrations (tests: `tests/support/pg.py`; production: `kh setup apply` with an admin DSN, or the operator). The migrations `0001_baseline.sql` and `0002_usage_events.sql` only grant and create tables; `0003_supersede.sql` also adds columns and the definer function `kh.mark_record_outdated`. `kh migrate` refuses a database whose `kh.schema_migrations` holds a name the package does not ship: a fresh install is required.
- Every table has `ENABLE` and `FORCE ROW LEVEL SECURITY` and an explicit `kh_migrator_all` policy (`USING (true)`), because FORCE also binds the owner.
- Per request `caller_tx` opens a transaction on the `kh_app` pool and sets the audiences, principal type and `sub` with `set_config(..., true)` plus `statement_timeout = 5s`.

| Table | kh_app | kh_indexer |
|---|---|---|
| `schema_migrations` | none | none |
| `claim_jti` | through `kh.claim_seen` only | SELECT and DELETE of expired rows |
| `groups`, `labels` | SELECT | SELECT |
| `documents` | SELECT where `can_read(audience)` | ALL |
| `records` | SELECT where `can_read(audience)`; INSERT with check `can_read(audience) AND created_by_sub = kh.caller_sub()`; UPDATE only through `kh.mark_record_outdated` (definer); no DELETE | SELECT |
| `chunks` | SELECT where `can_read(audience)` | ALL |
| `index_jobs`, `repo_state`, `index_alerts` | none | ALL |
| `usage_events` | INSERT for the caller's own group set (people only); SELECT where every group of the searcher is held | SELECT, DELETE |

- `usage_events` is read under the subset rule: an event is visible when `audiences <@ kh.caller_audiences()`, that is, when the reader holds every group of the searcher. This leaks nothing, because each key in an event's `owner_keys` is a hit the searcher could read, so its audience is one of the searcher's groups, and the searcher's groups are a subset of the reader's. Records cannot be updated or deleted, and `kh setup apply --prune` refuses to remove a group that still has records, so this holds over time. The searcher's hash (`who`) and `audiences` are never returned by any tool.

- Triggers: `chunks_inherit_owner` copies tier, audience, labels, stale, owner key and created time from the owner row; `owner_propagate_to_chunks` pushes later changes of a document down; `records_enqueue_embed` is a definer function (`kh_app` has no grant on `index_jobs`) that queues an `embed_record` job; `kh.assert_labels_exist` raises SQLSTATE `P0K05` for an unknown label; a BEFORE DELETE trigger on `labels` raises `P0K06` while a document or record uses the label.
- Dedupe: re-ingesting the same `(audience, source, source_ref)` returns the existing row's id through a SELECT under RLS. This is safe because inserting already requires the audience, so it never reveals a record of another audience. A concurrent duplicate is caught as a unique violation and answered by the same SELECT.
- Caller-path inserts use application-generated UUIDs.
- No existence leaks: `get_doc` runs one RLS query and answers `not found` for a missing and a hidden row alike.

## Claim middleware and call binding

- `ClaimMiddleware` is pure ASGI around `/mcp` only. It buffers the body (413 past `KH_MAX_BODY_BYTES`), reads the `X-PrivacyFence-Claim` header and verifies it with PrivacyFence's vendored verifier (`auth/pf_claim_verify.py`, source commit and sha256 in `auth/VENDORED.md`). Any failure is 401 with an empty body and only the error code is logged.
- The replay cache is `kh.claim_seen(jti, expires_at)`, so a claim id works once across processes. The daily job purges expired ids.
- `human` is always refused, `service` is refused on `tools/call`. `email` is lower-cased and kept only for `claude_client`. Audiences must match the group regex; a claim with a bad name is refused for every principal type. The email then selects the person's groups through `people`, and the claim's `audiences` are used only for n8n workflows.
- The verifier checks the `req` field, a hash of the tool name and arguments. The middleware stores the parsed name and arguments as `kh_call`. `tool_guard` refuses a dispatch whose tool name differs from `kh_call`, or whose keyword arguments are not exactly the stored arguments over the declared defaults, so the call that runs is the call that was hashed. Any refusal answers `internal error (ref ...)`.
- `GET` and `DELETE` on `/mcp` are verified and answered 405: the server is stateless.
- Errors are fixed texts (`mcp/errors.py`). Any other exception becomes `internal error (ref <uuid>)` with the detail logged server-side only.
- Tool schemas are flat (`str`, bounded `int`, `bool`, `list[str]`, `Literal`), no argument is called `reason`, and a unit test applies PrivacyFence's skip rules to every registered schema.
- `KH_DEV_IDENTITY` is refused when `KH_ENV=prod`, and is used only for requests with no claim header. It follows the rules above: its `audiences` are ignored for a `claude_client`, whose groups come from its `email`.
- `/metrics` exists only when `KH_METRICS_TOKEN_FILE` is set. The rate limiter (`KH_RATE_LIMIT_PER_MIN` per caller) is per process, so a second replica doubles the allowance.

## Indexer

- `repo` is the bare repository name everywhere. A webhook's `full_name` is split at `/` and its owner must equal `org`.
- `sync_repo` takes `pg_advisory_xact_lock(hashtext(repo))`, lists the tree of `main`, and diffs it against `kh.documents` by `(path, blob_sha)`. Files rejected earlier are not stored, so they are retried on every push.
- Included files: `*.md`, no dot-prefixed path segment, not the root `README.md`, at most 1 MiB. Changed blobs are parsed, chunked and embedded outside the transaction; then one transaction deletes vanished rows, upserts documents (`audience` is the repo's group, always) and chunks, and records alerts.
- Frontmatter: required `id`, `title`, `review_by` (`YYYY-MM-DD`); optional `labels`, each of which must exist in `kh.labels` (otherwise the file is rejected `unknown label <x>`); optional `supersedes`, a list of document ids (a single string counts as a list of one; at most 20, no duplicates, not the document's own id; otherwise the file is rejected `invalid supersedes`); other keys are ignored. A supersede link stays inside one repo, so inside one group. A duplicate `id` in one repo rejects every file that holds it. `stale` is `review_by < today`, recomputed by `kh daily`.
- Supersede resolver (`indexer/supersede.py`, run in every sync of a repo over all its stored documents; it sets `kh.documents.superseded_by`, which the upsert never writes). Rules: (1) a key that names no document alerts `supersedes unknown id <key>`; a document naming itself is skipped; (2) a document named by several others takes the successor with the smallest `doc_key` and alerts `superseded by <n> documents; using <doc_key>`; (3) a document on a cycle gets no successor and each member alerts `supersedes cycle: <keys>`; (4) every other target maps to its direct successor, so in a chain A to B to C both A and B are superseded; (5) every document gets a value, none when nothing replaces it. Removing the line or deleting the successor makes the old document current again on the next sync.
- Embedding providers are `ollama`, `http` and `fake`; the `http` provider embeds only audiences in `KH_EMBED_HTTP_ALLOWED_AUDIENCES`. T3 bodies are read from S3 by `embed_record` jobs. Failed jobs retry five times with a growing delay. An `embed_record` job removes NUL characters from a stored body before chunking and logs a warning with the record id; the blob in S3 is not changed.

## Search

One statement inside `caller_tx`: an exact vector order (no ANN index) fused by RRF with a full-text list, each limited to `KH_SEARCH_POOL`, weighted by tier and by the stale flag, one best chunk per owner. A `hidden` CTE lists superseded documents and outdated records (under the caller's row-level security); the filter drops their chunks before the pool is cut, so recall is kept. `include_outdated=true` keeps them, marks each hit `outdated` with `replaced_by`, and weights them by `KH_STALE_WEIGHT` like a stale hit. With `min_tier=T0`, a query whose only T0 matches are hidden answers the `only T3 records match` text when T3 hits exist. By default (`min_tier=T3`) T0 and T3 are searched together, records weighted lower. With `min_tier=T0` only T0 is searched; when no T0 hit exists but T3 hits do, the call fails with `only T3 records match; repeat with min_tier T3 to see them`. Cursors are HMAC-bound to the caller, the query and the filters, with a bounded offset.

## Usage log

`kh.usage_events` records what people search and open, so an n8n promotion agent can find T3 records worth promoting to T0 and queries no T0 document answers.

- **What is stored.** One row per event: the time, the kind (`search` or `open`), the searcher as a keyed hash, the searcher's group set, and the ids (`doc:` or `rec:`) of the hits returned or the one document opened. A search row also holds the query text (at most 500 characters, NUL removed; a normalised form is generated from it), the `min_tier` asked for and two counts of documents whose chunks match the full-text query, one for T0 and one for T3. An open row holds no query and no tier.
- **Who is logged.** Only `claude_client` callers, so n8n workflows and the promotion agent's own calls stay out of the signal and out of the table. Only searches at offset 0 are logged, because paging would inflate counts, and `get_doc` is logged only when it finds the row. A `min_tier=T0` search that fails with `only T3 records match` is logged too. Nothing is logged when `KH_USAGE_LOG` is `false` or `KH_CURSOR_KEY_FILE` is unset.
- **Searcher hash.** HMAC-SHA256 of the `sub`, keyed with a sub-key derived from the content of `KH_CURSOR_KEY_FILE`; no name, email or `sub` is stored. Rotating that file resets the distinct-people counts.
- **Write after the transaction.** `tool_guard` writes the events after the tool's own transaction has closed, on success and on a refused call, never on another exception or on cancellation. The write takes one pooled connection and waits at most 0.25 s for it. A failure of any kind, a timeout included, drops the events, logs a warning and adds to `kh_usage_events_dropped_total`; it never fails the tool. A second connection is not opened inside the tool's transaction, which could exhaust the pool of 10.
- **Two signals.** A record is worth promoting when many distinct people find or open it (`usage` with `view=records`, ranked by distinct people because searches and opens inflate when Claude searches on its own), and `outranked_t0` counts the searches in which it ranked above every T0 hit. A query is a gap when no T0 document matches its full-text query (`view=queries`); `t3_matches` above 0 means records exist, 0 means nothing exists. Both views list only what at least two distinct people did, so what one person alone did never produces an item.
- **Why not pool counts.** The search statement's `t0_hits` and `t3_hits` count owners in the fused pool, whose vector list always returns the nearest chunks however unrelated, so they are above 0 whenever the caller sees any embedded document. The log stores counts of documents that match the full-text query (all terms), whatever labels or group filter the search carried, and compares the rank of a record with the first T0 key.
- **Retention.** `kh daily` deletes events older than `KH_USAGE_RETENTION_DAYS` (default 180), and `usage` never reads past that window.

## Tools

Eight tools, all with flat arguments: `search`, `get_doc`, `list_stale`, `list_labels`, `usage` (reads, `auto`) and `ingest_record`, `mark_record_outdated`, `propose_doc` (writes, `popup`). The declaration in `deploy/privacyfence-upstream.example.yaml` allows `claude_client` and `n8n_workflow`.

- `ingest_record` refuses unless the caller holds `audience` and it is a configured group, when the body exceeds `KH_MAX_RECORD_BYTES`, or when a label is unknown, or when a text argument (`source`, `source_ref`, `title`, `body`, `source_url`, `content_type` or a label) contains a NUL character, which Postgres text cannot store (`argument <name> is invalid`); other control characters are kept. It stores the body at `records/<uuid>`, keeps the first 2000 characters as excerpt and stamps the config's `bot_login` as `written_by`.
- `mark_record_outdated(id, outdated=true, replaced_by="", note="")` takes record ids only and calls the definer function `kh.mark_record_outdated`, which checks the caller can read the record and that `replaced_by` (a `doc:` or `rec:` id) belongs to the same group; a missing or unreadable record answers `NOT_FOUND`. `outdated=false` clears the mark, and then `replaced_by` and `note` must be empty. The table keeps only the last actor (`outdated_by`, never returned); the call is logged at INFO.
- Similar items (`search/similar.py`): after a new record is stored and after a pull request is opened, `ingest_record` and `propose_doc` return `similar`, up to `KH_SIMILAR_LIMIT` items of the same group (never another group) whose best chunk has `similarity` of at least `KH_SIMILAR_MIN_SCORE`, with `id`, `tier`, `title` and `similarity` (not the RRF score). Superseded and outdated items are left out, and `propose_doc` leaves out the document being edited. The lookup embeds the text with a 5 second limit and runs in a savepoint; any failure gives `[]` and never loses the record or the pull request. `KH_SIMILAR_LIMIT=0` switches it off.
- `propose_doc` requires the caller to hold `group`. The path matches `^[a-z0-9][a-z0-9/_-]*\.md$` with no `..`, `//` or dot segment and is not `README.md`; `base_blob` empty means the path must not exist, otherwise Forgejo enforces the race through `sha`. Text with control characters other than tab and newline, bidirectional controls, HTML comments or tags is refused, as is a body over `KH_PROPOSE_MAX_BYTES`. The hub renders the file itself, parses it back with the indexer's parser, creates the branch `bot/propose/<uuid4>` from the head of `main`, puts the one file and opens a pull request to `main`. The bot is refused any branch outside `bot/` and any pull request whose base is not `main`; it stops at `KH_PROPOSE_MAX_OPEN` open pull requests. The pull-request body lists `Requested-by`, `Principal` and `Written-by` as information, not enforced. Its optional `supersedes` list (existing ids of the repo, rules as in the frontmatter) is written into the file and named in a last body line `Supersedes:`; to change a document keep the `supersedes` that `get_doc` returned, or the old one becomes current again.

## Deviation from the PrivacyFence contract

`docs/identity-claim.md` in PrivacyFence says only `sub`, `principal_type`, `audiences` and `req` may change what a query returns, and calls `email` audit-only. The hub uses `email` for access: it selects a person's groups through `people` in `kh.yaml`. The owner decided this on 2026-11-15 for attendee-only records and extended it on 2026-11-17 (`group-source: khyaml-email`): the email is now the only thing that gives a person any group, and the claim's `audiences` are used only for n8n workflows.

PrivacyFence 8.0.0a3 ships the opt-in key `email_for_visibility` (PrivacyFence ADR 0310). The declaration sets it, so PrivacyFence puts the verified email in the claim for this upstream. Without it no person's claim carries an email, so no person holds any group.

What the hub relies on: `email` is lower-cased; it is ASCII only; it is present only for `claude_client` (the hub also drops it for every other principal); a change to any of these is a breaking contract change in PrivacyFence.

## Rejected alternatives

| Alternative | Why not |
|---|---|
| SQLAlchemy and Alembic | Raw policies, triggers and the fusion query dominate; `psycopg` 3 with a small migration runner is lighter. |
| HNSW index from day one | Older pgvector filters after the index scan, so narrow audiences lose recall. Exact scans are correct; speed is measured after the first deploy. |
| Filtering in Python | The requirement is enforcement in the database. |
| Indexing in the webhook request | Embedding is slow and Forgejo times out; the webhook queues a job. |
| The SDK's `auth=` hook | The claim is in its own header and verification needs the raw body. |
| Filesystem blob store | Owner declined; S3 from day one (`blobs/s3.py`, path-style, one object version, history from backups). |
| A second auth path | Owner declined; one claim path, n8n tested with signed claims. |
| A formal-review trust walk | Closest to the old design, but much more code than letting Forgejo branch protection decide. |
| A trust walk by merger | Still needs a walk and verdict storage. |
| A `sync_teams` job copying Forgejo team members into Postgres | A second store and a freshness rule for a list the hub already loads. |
| Read-only `readers:` per group | Owner decision: every member reads and writes, approvers merge. |
| A `repos:` map with a separate approve group | Lets an approve group become an audience with no repo. |
| Label join tables | Arrays plus the two triggers give the same guard with far less SQL. |
| A global unique `(source, source_ref)` | It leaks the existence of records of other audiences. |
| Building the new modules beside the old ones | Two schemas and double test-support work; the old world was deleted instead. |
| `get_doc` fetching from Forgejo | Slow and unfiltered; `get_doc` is an indexed lookup under RLS. |
| Rank-by-pool hit counts (`t0_hits`) as the "T0 answers it" signal | Always above 0; replaced by full-text match counts and the rank of the record against the first T0 key. |
| Writing the usage event inside the tool transaction | The `normative` error rolls it back, and a failed insert would fail the tool. |
| Writing the usage event in a `finally` | Runs on cancellation; awaiting the pool there is unsafe. |
| A second connection opened inside the tool's transaction | Can exhaust the pool of 10 under load. |
| Daily rollup tables for usage | Need one rollup per audience set to keep the subset rule; more code; lose the open-to-query link. |
| Metrics or log export of usage | No row-level security; the design rejects filtering outside the database. |
| `outcome`, `page_offset`, `labels`, `audience_filter` and a stored `query_norm` column in the usage log | Derivable or unused by either view (`query_norm` is a generated column). |
| A separate usage key file | One more secret; the sub-key derived from the cursor key is enough. |
| Logging n8n workflows | They are not people, and the agent's own searches would feed back into its signal. |
