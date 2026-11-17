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
- Tools return the ids `doc:<uuid>` and `rec:<uuid>`. `search` defaults to `min_tier=T0`; tier weights are `T0:1.0,T3:0.4`.

## Trust: Forgejo is the control

- The indexer reads the tree of `main` as Forgejo reports it. There is no trust walk, no anchor, no review verdict, no `approved_by`, no `disputed` status and no version table.
- The approval is the merge. Branch protection forbids direct push, the merge whitelist is the group's `<group>-approve` team, and required approvals are 0, so an approver may merge their own pull request. Forgejo's merge API takes `head_commit_id` and answers 409 when the head moved, so a merge through the UI is bound to the head the approver saw.
- One cheap check remains: the indexer raises the alert `main of <repo> was rewritten; run kh rebuild after review` when the new head does not descend from `repo_state.last_indexed_sha`.
- The webhook checks the HMAC signature and only queues a job; everything is re-fetched with the indexer token. Only `refs/heads/main` of a repo listed under a group queues a `git_push` job.

### What `kh setup plan` verifies

T0 trust rests entirely on Forgejo, so `plan` checks it and reports any difference as drift:

- per group, the teams `<group>` (write on the repo) and `<group>-approve` (read, in the merge whitelist) with exactly the configured members and approvers;
- the repo is private with `main`, the bot has write and the indexer has read;
- branch protection on `main`: no direct push, the merge whitelist is exactly the approve team, `required_approvals: 0`, `apply_to_admins: true`, read back after every write (Forgejo silently drops whitelist teams that cannot read the repo);
- the identity of the bot and indexer tokens (`GET /user` equals the configured login, the bot has no repo admin, the indexer no push, neither is an org owner or site admin);
- the org webhook exists. Forgejo never returns its secret, so `plan` can only say `i webhook secret not verifiable`;
- a report-only line lists the members of the Owners team as people who can bypass protection.

`plan` exits 1 on drift, so running it on a schedule catches a protection change made in the Forgejo UI.

## Access model

- **Resolution.** `ClaimMiddleware` resolves the caller's groups from the verified claim. A `claude_client` holds the groups of `kh.yaml` in which a member's `people` email equals the claim's verified email, compared in lower case; a claim without an email gives no group, and the claim's `audiences` are ignored. An `n8n_workflow` holds the claim's `audiences` that are configured groups, because a workflow has no `people` entry. A `service` calls no tool. The config is read when `kh serve` starts, so a change to `people` or to a group needs a restart.
- **The rule.** `kh.can_read(a)` is `a = ANY(kh.caller_audiences())`. A document, record or chunk is visible when its audience is held; inserting a record needs the audience too.
- **One list for people.** `kh.yaml` decides both a person's Forgejo teams and what the hub lets them read, so a joiner or leaver changes one file. PrivacyFence keeps authentication, the verified email and the write popups; its group-to-audience mapping is no longer used for people. A person whose PrivacyFence email differs from their `people` email reads nothing.

### Accepted residuals

- A caller holding two groups can `ingest_record` or `propose_doc` into the wider one. The PrivacyFence popup shows `audience` and `group` and is the only gate.
- A leaked bot token can create branches and pull requests, push to open pull-request branches in every repo and read every T0 repo. It cannot merge.
- With required approvals 0 the approver must review the final diff before merging.
- The verified email is the identity: a Workspace email that is reassigned gives its new holder the old person's groups until `people` is edited. Remove a leaver from `people` first.
- Exact vector scans take time in proportion to the number of hidden rows; this timing is the accepted residual of the no-existence-leak rule.

## Roles and RLS matrix

- Three roles, none superuser, none BYPASSRLS: `kh_migrator` (owns schema `kh`, migrations and backups), `kh_app` (request path) and `kh_indexer` (worker, rebuild, daily, requeue).
- Roles are created outside the migrations (tests: `tests/support/pg.py`; production: `kh setup apply` with an admin DSN, or the operator). The one migration, `0001_baseline.sql`, only grants. `kh migrate` refuses a database whose `kh.schema_migrations` holds a name the package does not ship: a fresh install is required.
- Every table has `ENABLE` and `FORCE ROW LEVEL SECURITY` and an explicit `kh_migrator_all` policy (`USING (true)`), because FORCE also binds the owner.
- Per request `caller_tx` opens a transaction on the `kh_app` pool and sets the audiences, principal type and `sub` with `set_config(..., true)` plus `statement_timeout = 5s`.

| Table | kh_app | kh_indexer |
|---|---|---|
| `schema_migrations` | none | none |
| `claim_jti` | through `kh.claim_seen` only | SELECT and DELETE of expired rows |
| `groups`, `labels` | SELECT | SELECT |
| `documents` | SELECT where `can_read(audience)` | ALL |
| `records` | SELECT where `can_read(audience)`; INSERT with check `can_read(audience) AND created_by_sub = kh.caller_sub()`; no UPDATE or DELETE | SELECT |
| `chunks` | SELECT where `can_read(audience)` | ALL |
| `index_jobs`, `repo_state`, `index_alerts` | none | ALL |

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
- Frontmatter: required `id`, `title`, `review_by` (`YYYY-MM-DD`); optional `labels`, each of which must exist in `kh.labels` (otherwise the file is rejected `unknown label <x>`); other keys are ignored. A duplicate `id` in one repo rejects every file that holds it. `stale` is `review_by < today`, recomputed by `kh daily`.
- Embedding providers are `ollama`, `http` and `fake`; the `http` provider embeds only audiences in `KH_EMBED_HTTP_ALLOWED_AUDIENCES`. T3 bodies are read from S3 by `embed_record` jobs. Failed jobs retry five times with a growing delay.

## Search

One statement inside `caller_tx`: an exact vector order (no ANN index) fused by RRF with a full-text list, each limited to `KH_SEARCH_POOL`, weighted by tier and by the stale flag, one best chunk per owner. With `min_tier=T0` only T0 is searched; when no T0 hit exists but T3 hits do, the call fails with `only T3 records match; repeat with min_tier T3 to see them`. Cursors are HMAC-bound to the caller, the query and the filters, with a bounded offset.

## Tools

Six tools, all with flat arguments: `search`, `get_doc`, `list_stale`, `list_labels` (reads, `auto`) and `ingest_record`, `propose_doc` (writes, `popup`). The declaration in `deploy/privacyfence-upstream.example.yaml` allows `claude_client` and `n8n_workflow`.

- `ingest_record` refuses unless the caller holds `audience` and it is a configured group, when the body exceeds `KH_MAX_RECORD_BYTES`, or when a label is unknown. It stores the body at `records/<uuid>`, keeps the first 2000 characters as excerpt and stamps the config's `bot_login` as `written_by`.
- `propose_doc` requires the caller to hold `group`. The path matches `^[a-z0-9][a-z0-9/_-]*\.md$` with no `..`, `//` or dot segment and is not `README.md`; `base_blob` empty means the path must not exist, otherwise Forgejo enforces the race through `sha`. Text with control characters other than tab and newline, bidirectional controls, HTML comments or tags is refused, as is a body over `KH_PROPOSE_MAX_BYTES`. The hub renders the file itself, parses it back with the indexer's parser, creates the branch `bot/propose/<uuid4>` from the head of `main`, puts the one file and opens a pull request to `main`. The bot is refused any branch outside `bot/` and any pull request whose base is not `main`; it stops at `KH_PROPOSE_MAX_OPEN` open pull requests. The pull-request body lists `Requested-by`, `Principal` and `Written-by` as information, not enforced.

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
