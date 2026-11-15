# Plan: knowledge-hub, the trusted core of the company knowledge layer

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** Agents reach the hub through PrivacyFence as one MCP server (`kh_search`, `kh_get_doc`, `kh_propose_change` and the rest). Every hit shows its tier, domain, owner, approvals and freshness, and only what the caller's audiences allow is ever returned. Operators run `kh migrate`, `kh serve`, `kh rebuild` and `kh check`; Forgejo pushes to `/webhooks/forgejo` keep the T0/T1 index current.

**Scope:** A new Python service in this repo: schema and row-level security, claim verification with PrivacyFence's vendored verifier, the MCP tools, the Forgejo indexer with a trust walk, T3 ingest into S3-compatible storage, T2 derivation, promotion pull requests, conflicts, staleness, rate limits, metrics, backup scripts and a runbook. It does not change PrivacyFence, Forgejo or the compose repo; those are listed as owner steps.

**Security:** Audience is enforced only by Postgres RLS under four roles with FORCE RLS and per-transaction settings (section 3.3). Callers are verified per request, including the call hash. Tool errors are fixed texts; hidden and missing rows are indistinguishable. A token may cite raw records it ingested itself in `write_derived`, and the derived row still inherits the full restriction (section 3.7).

**Performance:** Search uses exact vector scans (no ANN index) because the corpus is small and ANN filtering under RLS loses recall on older pgvector. bge-m3 speed on the arm64 box is measured by the owner (manual step); the fallback is a 768-dimension model and a migration.

**Review focus:** the RLS predicate and role split (3.3); the claim middleware and call-hash check (3.2); the T2 visibility function with the provenance exception (3.7); the indexer trust walk and disputed handling (3.6); the promotion routing and refusal to widen (3.8).

## 1. Goal

Build `knowledge-hub`, the service that gives agents one MCP interface over canonical documents in git and machine-written records in Postgres, with every result carrying its trust tier and filtered by the caller's audience inside the database. Tracking issue: [tkcs-lab/knowledge-hub#1](https://github.com/tkcs-lab/knowledge-hub/issues/1). The owner's decisions, taken in conversation on 2026-11-15:

1. Callers are verified with PrivacyFence's new identity claim (merged to PrivacyFence `main` on 2026-11-15), using its vendored verifier.
2. `allowed_emails` matches the claim's `email`; a token may cite raw records it ingested itself in `write_derived` (provenance exception).
3. The n8n write tools are built and tested against signed test claims of type `n8n_workflow`; live n8n writes wait for a PrivacyFence change the owner is making. No second auth path.
4. Embeddings: `bge-m3` through Ollama, 1024 dimensions, English documents.
5. T3 blobs live in S3-compatible storage from day one.
6. `get_doc` on T0/T1 serves the indexed copy from Postgres.

## 2. Current state

- This repository holds only `LICENSE`, `README.md` (one line) and a placeholder `.tld/toolkit.yaml` (`project.name: REPO`, `stacks: []`, both verify commands are `echo`). There is no code.
- PrivacyFence (`https://github.com/privacyfence/privacyfence`, commit `04d2ecac53428a1210b4b777f8c466359ee730bf`, read 2026-11-15): the contract is `docs/identity-claim.md`; the single-file verifier is `contrib/verifier/pf_claim_verify.py` (sha256 starts `ab0d3ab73a8fdb2f`) with six `req` vectors in `contrib/verifier/req_test_vectors.json`. Header `X-PrivacyFence-Claim`; header `alg` `EdDSA`, `typ` `pf-identity-claim+jwt`; keys at `<issuer>/upstream-mcp/jwks`; payload `claim_version`, `iss`, `aud`, `sub`, `principal_type` (`claude_client`, `n8n_workflow`, `service`; `human` reserved), `email` (only for a verified person), `approved_by`, `token_id`, `act`, `audiences`, `tools` (information only), `req` (only on `tools/call`), `iat`, `exp`, `jti`. Every request that is not a `tools/call` carries `audiences: []`. PrivacyFence opens a fresh MCP session per call (`initialize`, `notifications/initialized`, `tools/call`, session delete) and skips any upstream tool whose input schema has `$ref`, `$defs` or `definitions`, a `reason` property, is over 32 KB or is not an object schema (`src/privacyfence/upstream_mcp_client.py:147-158`). It forwards upstream error text to the AI client (`docs/upstream-mcp.md`, section "Errors and size"). A person's audiences are the union of the audiences of the Workspace groups they belong to, set by the operator's `groups` map; `all` is only an ordinary audience that the operator maps to a group such as `everyone@<domain>` (`docs/upstream-mcp.md`, "Groups and audiences").
- Today a scoped token (what n8n uses) can only be given `auto`-gated upstream tools, and writes must be `popup` (`docs/upstream-mcp.md`, "Known limitation"). That is the follow-up the owner is doing in PrivacyFence.
- Compose repo (`andras-tkcs/tkcs-work-compose`, private, read 2026-11-15): Postgres 17 with pgvector on the internal `db` network, one login role per app from `roles/postgres/templates/ensure.sql.j2`; Forgejo org `kh` is planned (`docs/knowledge-hub.md`): repos `canonical-all`, `canonical-mgmt`, `canonical-hr`, `canonical-contracts`, `notes-all`, org-level webhook to `http://knowledge-hub:8080/webhooks/forgejo`, token files `kh_hub_bot_token`, `kh_indexer_token`, `kh_webhook_hmac`; restic backs up `/srv/data`. Finding 6 of that document is the indexer trust model implemented in section 3.6.
- Test environment: a cloud worker has Python 3.13, `uv`, and can `apt-get install postgresql-16 postgresql-16-pgvector` (pgvector 0.6.0 on Ubuntu 24.04). It has no Docker daemon.
- The MCP Python SDK `mcp==2.3.0` (the version PrivacyFence pins) offers `MCPServer(...).streamable_http_app(stateless_http=True, json_response=True, transport_security=...)`, `StreamableHTTPSessionManager(stateless=True)` and gives a tool handler the Starlette request through `ctx.request_context.request`; the default `host` turns on DNS-rebinding protection that only allows localhost hosts; and a tool exception's `str()` is returned to the caller. Phase `p5-mcp-get-doc` verifies these against the installed package.

## 3. Design

### 3.1 Package, settings and configuration

- Distribution `knowledge-hub`, import package `knowledge_hub` under `src/`, console script `kh`, Python `>=3.12`. Every module stays under 400 lines.
- Settings (`knowledge_hub/settings.py`, `pydantic-settings`, prefix `KH_`): `KH_ENV` (`dev`|`test`|`prod`, default `prod`), `KH_DEV_IDENTITY` (JSON), `KH_PF_ISSUER`, `KH_PF_AUDIENCE`, `KH_PF_JWKS_URL`, `KH_ALLOWED_HOSTS` (comma list), `KH_MAX_BODY_BYTES` (default `1048576`), `KH_DATABASE_URL_APP`, `KH_DATABASE_URL_INDEXER`, `KH_DATABASE_URL_MIGRATOR`, `KH_CONFIG_FILE`, `KH_FORGEJO_URL`, `KH_FORGEJO_ORG` (default `kh`), `KH_FORGEJO_BOT_TOKEN_FILE`, `KH_FORGEJO_INDEXER_TOKEN_FILE`, `KH_WEBHOOK_HMAC_FILE`, `KH_EMBED_PROVIDER` (`ollama`|`http`, default `ollama`), `KH_OLLAMA_URL`, `KH_EMBED_MODEL` (default `bge-m3`), `KH_EMBED_DIM` (default `1024`), `KH_EMBED_HTTP_URL`, `KH_EMBED_HTTP_TOKEN_FILE`, `KH_EMBED_HTTP_ALLOWED_AUDIENCES` (default `all`), `KH_S3_ENDPOINT`, `KH_S3_BUCKET`, `KH_S3_ACCESS_KEY_FILE`, `KH_S3_SECRET_KEY_FILE`, `KH_S3_REGION` (default `garage`), `KH_CURSOR_KEY_FILE`, `KH_CHUNKER_VERSION` (default `1`), `KH_PROMOTION_DAILY_LIMIT` (default `5`), `KH_RRF_K` (60), `KH_SEARCH_POOL` (50), `KH_TIER_WEIGHTS` (`T0:1.0,T1:0.85,T2:0.6,T3:0.4`), `KH_STALE_WEIGHT` (0.5), `KH_SUPERSEDED_WEIGHT` (0.3), `KH_DISPUTED_WEIGHT` (0.5), `KH_SEARCH_MAX_OFFSET` (100), `KH_MAX_RAW_BYTES` (1048576), `KH_RATE_LIMIT_PER_MIN` (120, used in `p23`). Secrets are read from files, never from variables.
- Dev override: `KH_DEV_IDENTITY` is a JSON object `{"sub", "principal_type", "audiences", "email"?}` (types `service` and `human` are refused). The settings validator raises `KH_DEV_IDENTITY is set while KH_ENV=prod; refusing to start`; `kh serve` exits 2 on any settings error and `ClaimMiddleware.__init__` re-checks. With the override active a request with no claim header uses the dev caller and logs a warning; a request that carries a header is still verified. Tests never use the override for claims.
- Config file (`kh.yaml`, model in `knowledge_hub/config.py`): `audiences` (names allowed in `required_audiences`; default `[all, mgmt, hr, contracts]`), `participant_label: attendees` (the name used in outputs for email-scoped T3 rows; `attendees` is not an audience), `domains: [portfolio, product, technical]`, `repos` (map of Forgejo repo name to `{tier: T0|T1, audience: <name>, trust_anchor: <sha>|null}`; default `canonical-all`/`all`, `canonical-mgmt`/`mgmt`, `canonical-hr`/`hr`, `canonical-contracts`/`contracts` as T0 and `notes-all`/`all` as T1), `raw_audience_map` (`series:<id>` or `folder:<id>` to a list of audiences), `ingest_principal_types` (default `[n8n_workflow, service]`), `restricted_audiences_imply_all` (default `false`), `entities` (per kind `prd|cmp|ver|cus` the `required_audiences` of its cache rows; default `cus: [contracts]`, others `[all]`), `forgejo_web_url`. Validation rejects an unknown audience or domain anywhere and a repo audience not in `audiences`.

### 3.2 Caller verification

- `knowledge_hub/auth/pf_claim_verify.py` is vendored byte for byte from PrivacyFence commit `04d2ecac53428a1210b4b777f8c466359ee730bf`; `auth/VENDORED.md` records the source URL, the commit and the file's sha256. The six vectors file is copied to `tests/data/pf_req_test_vectors.json`.
- `ClaimMiddleware` (pure ASGI, `auth/claims.py`) wraps only the `/mcp` route. For `POST`: read the body, refuse past `KH_MAX_BODY_BYTES` with 413; a missing `X-PrivacyFence-Claim` header is 401 with an empty body (unless the dev override applies); call `ClaimVerifier.verify(token, body=body)`; a `ClaimError` is 401 with an empty body and only `exc.code` logged. Always refuse `principal_type` `human`, and refuse `service` on a `tools/call`. Build `Caller(sub, principal_type, email, audiences, token_id)` with the email lower-cased or `None` and audiences checked against `^[a-z0-9][a-z0-9_-]{0,62}$` (else 401). Store `kh_caller` and `kh_call_hash` (`request_hash(tool, arguments)` for a `tools/call`, else `None`) in `scope["state"]` and replay the buffered body to the SDK.
- Replay cache: `auth/replay.py` `PgReplayCache.add_if_new(jti, expires_at)` runs `INSERT INTO kh.claim_jti ... ON CONFLICT DO NOTHING` through the migrator-owned table (granted to `kh_app`); a purge of expired rows runs in the daily job.
- `tool_guard` (`mcp/errors.py`) wraps every tool function: it refuses a call whose `kh_caller` is missing or whose `kh_call_hash` is `None`; recomputes the hash from the dispatched tool name and arguments and refuses a mismatch (the contract's "dispatch only what you hashed"); opens `caller_tx`; maps `KhError` to its fixed text; maps every other exception to `internal error (ref <uuid4>)` and logs the detail server-side only.
- HTTP app (`app.py`): root Starlette with routes `/mcp` (the middleware around the SDK's streamable handler), `/webhooks/forgejo` and `/healthz` (answers `ok` only). `StreamableHTTPSessionManager(stateless=True, json_response=True)` with `TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=KH_ALLOWED_HOSTS, allowed_origins=[])`. The root lifespan enters `mcp.session_manager.run()` (a mounted sub-app's lifespan would not run). If the SDK does not pass `scope["state"]` to the tool in stateless mode, fall back to a `contextvars.ContextVar` set by the middleware.
- Tool schemas are flat: `str`, bounded `int`, `bool`, `list[str]`, `Literal`. No pydantic model arguments and no argument named `reason`. A unit test applies PrivacyFence's skip rules to every registered tool schema.

### 3.3 Database roles and row-level security

- Roles: `kh_migrator` (LOGIN, owns schema `kh` and every table, runs migrations only); `kh_app` (LOGIN, NOBYPASSRLS, the request path); `kh_indexer` (LOGIN, NOBYPASSRLS, webhook worker, rebuild, daily job, recompute); `kh_provenance` (NOLOGIN, owns the definer functions of 3.7). No superuser, no BYPASSRLS. Every table has `ENABLE` and `FORCE ROW LEVEL SECURITY`. Migration `0001` creates schema `kh`, revokes `public`, and grants only what a table needs. Roles themselves are created outside the migrations (tests: `conftest.py` as the sandbox superuser; production: the Ansible template, a manual step); migrations only `GRANT`.
- Per request `db/caller_tx.py` opens a transaction on the `kh_app` pool and runs `SELECT set_config('kh.audiences', $1, true), set_config('kh.email', $2, true), set_config('kh.principal_type', $3, true), set_config('kh.sub', $4, true)` (audiences comma-joined after the regex check, bound as parameters) and `SET LOCAL statement_timeout = '5s'`. Nothing else uses the `kh_app` pool.
- Helper functions in schema `kh` (`LANGUAGE sql STABLE`): `caller_audiences()` returns `coalesce(string_to_array(nullif(current_setting('kh.audiences', true), ''), ','), '{}')`; `caller_email()` returns `nullif(current_setting('kh.email', true), '')`; `caller_sub()`; `caller_type()`; and `visible(req text[], emails text[])` returns `req <@ caller_audiences() AND (emails IS NULL OR caller_email() = ANY(emails))`. Every visibility-bearing table has `required_audiences text[] NOT NULL`, `allowed_emails text[] NULL` (stored lower-case) and `CHECK (cardinality(required_audiences) > 0 OR allowed_emails IS NOT NULL)` named `kh_no_open_rows`. `allowed_emails = '{}'` (empty, not null) means nobody. A caller with no audiences and no email therefore sees nothing, and a person in no group still sees rows that list their verified email.
- Policies: for `kh_app`, `SELECT` on `documents`, `chunks`, `raw_records`, `derived_records`, `candidates`, `conflicts`, `entities_cache` uses `USING (kh.visible(required_audiences, allowed_emails))`. Child tables (`document_versions`, `derived_sources`, `candidate_sources`, `conflict_items`) are visible only when `EXISTS (SELECT 1 FROM kh.<parent> p WHERE p.id = <fk>)` holds under the parent's own policy. Inserts for `kh_app`: `raw_records` `WITH CHECK (kh.caller_type() = 'n8n_workflow' AND ingested_by_sub = kh.caller_sub())`; `derived_records`, `candidates`, `conflicts` `WITH CHECK (created_by_sub = kh.caller_sub())`. There is no `UPDATE` or `DELETE` grant for `kh_app` and no grant on `index_jobs`, `repo_state`, `index_alerts`. `kh_indexer` has `FOR ALL USING (true) WITH CHECK (true)` on documents, chunks, versions, entities, jobs, repo state, alerts and the audience map, may `UPDATE` the state columns of `candidates` and `conflicts`, and may `UPDATE` the visibility columns of `raw_records`, `derived_records` and `chunks` (recompute).
- Caller-path inserts use application-generated UUIDs and never `RETURNING` or `ON CONFLICT DO UPDATE`, because the writer may not be able to see its own row.
- Chunk consistency: a `BEFORE INSERT OR UPDATE` trigger `chunks_inherit_owner` copies tier, domain, status, stale, superseded, owner key, owner created time and the visibility columns from the owning document, raw or derived row (anything the application supplies is overwritten); an `AFTER UPDATE` trigger `owner_propagate_to_chunks` on the three parent tables pushes changes down. `kh check` runs a drift query that must return zero rows.
- No existence leaks: `get_doc` and `get_entity` run one RLS query and answer `not found` for both missing and hidden rows through the same code path; counts are only ever taken under RLS; unique keys and dedupe keys are designed so a collision can only hit a row of identical visibility. The one accepted residual is timing proportional to hidden-row volume in exact vector scans.

### 3.4 Tables (migrations are numbered `.sql` files in `knowledge_hub/migrations/`, one transaction each, checksum in `kh.schema_migrations`; a changed applied file refuses the run)

- `0001_schema_roles_grants.sql`: schema, default privileges, `schema_migrations`, `claim_jti(jti text primary key, expires_at timestamptz)`.
- `0002_core_tables.sql`: `documents` (id uuid, repo, path, doc_key, tier `T0|T1`, domain, title, body, frontmatter jsonb, status `current|superseded|disputed`, stale bool, review_by date, owner text, entities text[], supersedes text[], superseded_by uuid, commit_sha, content_sha, approved_by text[], approved_at, merge_pr int, pr_url, indexed_at, updated_at, visibility columns; unique `(repo, doc_key)` and `(repo, path)`), `document_versions(document_id, commit_sha, path, tier, status, approved_by, approved_at, indexed_at)`, `chunks` (id, exactly one of `document_id|raw_id|derived_id`, ordinal, heading_path, text, content_sha, `embedding vector(1024)`, embedding_model, `tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(heading_path,'') || ' ' || text)) STORED`, owner_key, owner_created_at, tier, domain, status, stale, superseded, visibility columns), `entities_cache(id text primary key, kind, name, owner_team, status, attrs jsonb, synced_at, visibility columns)`.
- `0003_rls_functions_policies.sql`: helper functions, `kh_no_open_rows` checks, `ENABLE/FORCE ROW LEVEL SECURITY`, policies and grants of 3.3 for the tables that exist so far.
- `0004_chunk_triggers.sql`: the two triggers of 3.3.
- `0005_index_jobs_repo_state.sql`: `index_jobs(id, kind `git_push|embed_raw|embed_derived|pr_event`, repo, payload jsonb, state `queued|running|done|failed`, attempts, last_error, created_at, run_after)`, `repo_state(repo primary key, trust_anchor_sha, last_indexed_sha, trusted bool, untrusted_reason)`, `index_alerts(id, repo, path, message, created_at)`.
- `0006_raw_records.sql`: `raw_records(id, source_system, source_ref, source_url, series, folder, participants text[], title, blob_key, excerpt, content_type, occurred_at, ingested_by_sub, ingested_by_type, ingested_at, domain, entities text[], generated_by, visibility columns; unique (ingested_by_sub, source_system, source_ref))`, `raw_audience_map(kind, key, audiences text[], primary key (kind, key))`, trigger `raw_records_assign_visibility` (3.7).
- `0007_derived.sql`: `derived_records(id, kind `summary|extracted-fact|decision-candidate|changelog`, title, body, claims jsonb, domain, generated_by jsonb, created_by_sub, created_by_type, created_at, visibility columns)`, `derived_sources(derived_id, source_kind, source_id, version)`, function `kh.derived_visibility`, constraint trigger `derived_visibility_matches` (3.7).
- `0008_candidates_conflicts.sql`: `candidates(id, derived_id, target_doc_id, state `proposed|pr_open|promoted|rejected|failed|expired`, domain, target_repo, target_path, title, body_sha, dedupe_key, branch, pr_number, pr_url, created_by_sub, created_at, decided_at, error, visibility columns)` with a unique partial index on `dedupe_key` where state is `proposed` or `pr_open`, `candidate_sources`, `conflicts(id, state `open|closed`, domain, note, created_by_sub, created_at, closed_at, closed_by_pr, visibility columns)`, `conflict_items(conflict_id, item_kind, item_id)`.

### 3.5 Search

One statement inside `caller_tx`, exact vector order (no ANN index), RRF over a vector list and a full-text list, each limited to `KH_SEARCH_POOL` candidates and filtered by domain, tier and entities; `score = rrf * tier_weight * (stale ? KH_STALE_WEIGHT : 1) * (superseded ? KH_SUPERSEDED_WEIGHT : 1) * (disputed ? KH_DISPUTED_WEIGHT : 1)`; one best chunk per owner (`DISTINCT ON (owner_key)`); order by `score DESC, id`. Vectors are only compared with chunks whose `embedding_model` equals the configured model. `min_tier` includes that tier and every higher one (`T0` is highest); `lower_tier_hits` counts matching owner records in the tiers below `min_tier` (counts per tier `T2`, `T3` and `newest_at`), computed under RLS over the same fused pool. Hits carry `text`, `tier`, `domain`, `owner`, `approved_by`, `approved_at`, `review_by`, `status`, `stale`, `entities`, `source_link` and `citation_id` (`doc:<uuid>`, `raw:<uuid>`, `derived:<uuid>`). When the best returned hit is `T2` or `T3` only and the caller asked for `min_tier` `T0` or `T1`, the error is `cannot answer normatively from T2/T3 only: <n> lower-tier hits; call search with min_tier="T3" to see them`. Cursor: opaque `base64url(HMAC-SHA256(KH_CURSOR_KEY, sub|audience-hash|query-hash|offset) || offset)`, offset at most `KH_SEARCH_MAX_OFFSET`, re-executes the query under RLS; a foreign cursor is refused with `cursor does not belong to this query`. List tools use keyset cursors `(created_at, id)` over visible rows.

### 3.6 Indexer and trust model

- Webhook: verify `X-Forgejo-Signature` (hex HMAC-SHA256 of the raw body, `hmac.compare_digest`, key from `KH_WEBHOOK_HMAC_FILE`); a bad signature is 401 with an empty body. The payload is only a trigger: it inserts a `git_push` or `pr_event` job and answers 202; everything is re-fetched from Forgejo with the indexer token. A worker in the app process runs jobs, one repo at a time under `pg_advisory_xact_lock(hashtext(repo))`.
- Frontmatter: required `id`, `title`, `domain`, `review_by`; optional `owner`, `type`, `entities`, `supersedes`, `superseded_by`, `status`. Any of `tier`, `audience`, `audiences`, `required_audiences`, `allowed_emails`, `visibility`, `approved_by`, `approved_at` rejects the file: it is not indexed, an earlier row of it is removed, and an alert reads `frontmatter must not set <key>; the server decides tier and audience`. A duplicate `id` in one repo rejects the later file.
- Trust (canonical repos only), pure function in `indexer/trust.py` over commits and PR/review records: walk the first-parent chain of `main` from `repo_state.trust_anchor_sha` (config `trust_anchor`, `null` means the first commit) to HEAD. Every commit on that chain must be a merge commit whose pull request was merged into `main` with `merge_commit_sha` equal to the commit, and whose reviews hold at least one non-dismissed official `APPROVED` review by someone who is neither the PR author nor `kh-hub-bot`. A non-merge first-parent commit sets `repo_state.trusted = false` with an alert `non-merge commit <sha> on main of <repo>`, and every document of that repo is indexed `disputed` until an operator reviews and moves the anchor in config. A file changed by a merge without an approved PR is `disputed` with the alert `file <path> on <repo>/main has no approved merge PR`. `approved_by` and `approved_at` come from the reviews. `notes-*` (T1) repos need no approval but must be listed in config. Forgejo API facts relied on (verified by a probe in `p9`): `GET /repos/{o}/{r}/commits/{sha}/pull` (PR for a merge commit), `GET /repos/{o}/{r}/pulls/{n}/reviews` with `official`, `dismissed` and `state`, `GET /repos/{o}/{r}/git/trees`, `GET /repos/{o}/{r}/raw/{path}?ref=<sha>`. If the commit-to-PR lookup is missing, list closed PRs and match `merge_commit_sha`.
- Documents: upsert by `(repo, doc_key)`; rename and delete handled by diffing the tree at the pushed commit against the index; `superseded_by` is accepted only when the successor's repo audience is equal or wider, otherwise the file is `disputed` with `superseded_by points to a document with a narrower audience`; each indexed commit adds a `document_versions` row. Chunks split on markdown headings (`heading_path` is the heading chain joined with ` > `), pieces over 1200 characters are split at paragraph boundaries into pieces of at most 1200 characters with 150 characters of overlap, pieces under 200 characters merge into the previous piece of the same section; `KH_CHUNKER_VERSION` is part of the chunk hash. Embeddings are computed before the write transaction, and each repo is written in one transaction. A chunk is rewritten only when `content_sha`, chunker version or embedding model changes.
- `kh rebuild [--repo R]` re-walks trust, reads the tree at HEAD, upserts, deletes documents absent from the tree; running it twice changes nothing (same rows, same `indexed_at`). `kh check` compares HEAD and content shas with the index, runs the chunk drift query, checks the `raw_audience_map` hash against the recorded one, and exits 1 on any drift.
- `get_doc` serves the indexed row and returns `commit_sha` and `indexed_at`. `version=<sha>` is accepted only when `document_versions` holds that sha for the document (checked through RLS); then the file is fetched from Forgejo; otherwise `version <sha> is not an indexed version of this document`.

### 3.7 T3 and T2 visibility, and the provenance exception

- `ingest_raw` principals: `ingest_principal_types` from config (default `n8n_workflow`, `service`); PrivacyFence mints `service` only for its startup tool list, so in practice only `n8n_workflow` can call it. The call stores the body in S3 under `raw/<uuid>`, sets `blob_key`, stores a 2000-character excerpt, writes `ingested_by_sub` and `ingested_by_type` from the claim, and queues an `embed_raw` job; the worker chunks and embeds it. Visibility is assigned by the `raw_records_assign_visibility` trigger from `raw_audience_map` (loaded from config by the indexer at startup, hash recorded): a mapped `series:<id>` or `folder:<id>` gives `required_audiences = <mapped>`, `allowed_emails = NULL`; anything else gives `required_audiences = '{}'` and `allowed_emails = lower(participants)`; an unmapped record with no participants is rejected with `cannot ingest: no audience mapping for series or folder and no participants`. If both a series and a folder are mapped, the union of both audience lists is used.
- `kh.derived_visibility(source_ids text[])` is `SECURITY DEFINER`, owned by `kh_provenance`, `search_path` pinned to `kh, pg_temp`. `kh_provenance` has column-level `SELECT` on `(id, required_audiences, allowed_emails, ingested_by_sub)` of `documents`, `raw_records`, `derived_records` only. A source is visible to the function when `kh.visible(...)` holds, or, for `raw_records` only, when `kh.caller_type() = 'n8n_workflow' AND ingested_by_sub = kh.caller_sub()` (the provenance exception). It returns `(n_resolved int, required_audiences text[], allowed_emails text[])`: the union of audiences, and the intersection of the non-null email lists (`NULL` when every source has none; `'{}'` when the intersection is empty). It never returns content. Reads stay strict: the exception exists only inside this function.
- `write_derived` checks: ids are well formed (`doc:`, `raw:`, `derived:` plus uuid, optional `@<version>` that must equal the source's current version), 1 to 50 and de-duplicated; `n_resolved = number of ids`, else `cannot write derived record: <k> cited sources are not found or not visible to you` (missing and hidden are indistinguishable); inserts `derived_records` with the returned visibility and no `RETURNING`, then `derived_sources` with each source's version; `claims_json` must be a JSON list of objects with `text`, `source_ids` (subset of the cited ids) and one of `quote` or `timestamp`; a deferred constraint trigger `derived_visibility_matches` recomputes at commit and raises if the stored visibility differs; the derived row then queues an `embed_derived` job. The exception covers raw records only; a derived record that the same token wrote but cannot see cannot be cited (chained derivation is not covered).
- `kh recompute-visibility` (indexer role) recomputes raw visibility from the audience map, then derived visibility through `derived_sources`, when the map hash changes; it is the only code that updates visibility columns after insert.

### 3.8 Promotion

- `route_target_repo(sources, config)` (pure, in `visibility.py`). Sources must be visible by strict RLS (no provenance exception). Any source with non-null `allowed_emails` refuses: `cannot propose: a cited source is restricted to named people; no canonical repo is that narrow`. Let `U` be the union of the sources' audiences; with `restricted_audiences_imply_all: true` remove `all` from `U` when `U` has other members. If `U = {a}` the target is the canonical repo whose audience is `a`; if `|U| >= 2` refuse `cannot propose: sources span audiences <a>, <b>; no canonical repo is restricted to all of them`; `U` empty is refused. When the chosen repo's audience is wider than `U` (only possible through an explicit `target_id` in another repo) refuse `cannot propose to <repo>: its audience <a> is wider than the sources`. Widening is a human `widen-audience` PR, never a bot's.
- `propose_change` (flat arguments `target_id`, `new_path`, `content`, `rationale`, `source_ids`, `candidate_id`; exactly one of `target_id` and `new_path`): routes, enforces the dedupe key `sha256(target_repo, target_path, sorted source ids, sha256(normalised content))` (a collision returns `an identical proposal is already open: <candidate id> (PR #<n>)`), enforces the daily limit per `(domain, target_repo)` counted under RLS since 00:00 UTC (`daily promotion limit for domain <d> in <repo> reached (<n>); try again after <time> UTC`), inserts the candidate (`proposed`), then through `kh-hub-bot` creates branch `bot/promote/<candidate-id>`, commits the file, opens the PR with the PR template filled and labels `promotion` and `domain/<d>`, and sets `pr_open`. A Forgejo failure sets `failed` with the error text kept server-side. `ForgejoClient` raises if any write targets a ref outside `bot/promote/*`; it never pushes to `main`.
- PR events: a `pull_request` webhook with `action: closed` updates the candidate (`promoted` if merged, else `rejected`) and, on merge, closes the conflicts that cite the merged candidate's sources. Forgejo cannot filter PR events by action, so the hub does.

### 3.9 Tool catalogue (all arguments flat)

`search(query, domains: list[str]=[], min_tier="T1", entities: list[str]=[], limit=10, cursor="")`; `get_doc(id, version="")`; `get_entity(id)`; `list_entities(type, status="", owner_team="", limit=50, cursor="")`; `ingest_raw(source, source_ref, title, body, domain, occurred_at, series_id="", folder_id="", participants: list[str]=[], entities: list[str]=[], source_url="", content_type="text/plain", generated_by="")`; `write_derived(kind, title, body, domain, claims_json, derived_from: list[str], pipeline, model, pipeline_version)`; `propose_change(...)` as above; `flag_conflict(a, b, note)`; `list_conflicts(domain="", status="", limit=50, cursor="")`; `list_stale(domain="", owner="", limit=50, cursor="")`. Fixed error texts live in `mcp/errors.py` as one catalogue; `not found` is the single text for a missing or hidden item.

### 3.10 Embeddings and blobs

`embed/base.py` defines `EmbeddingProvider.embed(texts) -> list[list[float]]`, `.model`, `.dim`, and a deterministic `FakeEmbedder` for tests. `embed/ollama.py` calls `POST <KH_OLLAMA_URL>/api/embed`; `embed/http.py` calls a configured HTTP endpoint and indexes only chunks whose visibility is a subset of `KH_EMBED_HTTP_ALLOWED_AUDIENCES` (default `all`); query text is always embedded through Ollama. Both refuse a dimension other than `KH_EMBED_DIM`. `blobs/s3.py` `BlobStore.put/get/delete` over `boto3` with a configurable endpoint, path-style addressing and the credentials from files; a test double implements the same protocol.

### 3.11 Rejected alternatives

SQLAlchemy and Alembic (raw policies, triggers and the fusion query dominate; `psycopg` 3 with a small runner is lighter); an HNSW index from day one (older pgvector filters after the index scan, so narrow audiences lose recall); filtering in Python (forbidden by the ask); indexing synchronously in the webhook (embedding is slow and Forgejo times out); the SDK's `auth=` hook (the claim is in its own header and needs the raw body); a filesystem blob store and a second auth path (the owner declined both).

## 5. Manual steps

Every manual step runs after the last merge, because the phases build and test against fakes (a throwaway test cluster, signed test claims, a fake S3).

### Before implementation

None. Every phase runs on a cloud worker with a throwaway Postgres and signed test claims.

### After implementation

**ma1-roles-and-secrets (about 20 minutes).** Needs the compose repo, [docs/knowledge-hub.md](https://github.com/andras-tkcs/tkcs-work-compose/blob/main/docs/knowledge-hub.md).
1. In the compose repo add four database roles to the Postgres role template (`roles/postgres/templates/ensure.sql.j2`): `kh_migrator`, `kh_app`, `kh_indexer` (all LOGIN, NOSUPERUSER, NOBYPASSRLS) and `kh_provenance` (NOLOGIN), a database `kh` owned by `kh_migrator`, and `CREATE EXTENSION vector` in it.
2. Generate passwords for the three login roles and store them with the other stack secrets under `/etc/stack-secrets` (never in chat), named `pg_kh_migrator`, `pg_kh_app`, `pg_kh_indexer`; also `kh_webhook_hmac`, `kh_hub_bot_token`, `kh_indexer_token`, `kh_cursor_key`.
3. Run `ansible-playbook site.yml --tags postgres`.
Report back: `kh migrate` runs against the `kh` database without error (pass/fail).

**ma2-garage-bucket (about 30 minutes).** Add a single-node S3-compatible store (Garage is the suggested one: [Garage quick start](https://garagehq.deuxfleurs.fr/documentation/quick-start/), not verified from the planning environment) as a container on the `kh` network, create bucket `kh-raw`, and an access key limited to it; store the key id and secret as `kh_s3_access_key` and `kh_s3_secret_key` in `/etc/stack-secrets`; add the bucket's data directory under `/srv/data/garage` so restic backs it up. Report back: `aws s3 ls` style listing of the bucket works from the hub container (pass/fail).

**ma3-forgejo-settings (about 20 minutes).** In the Forgejo org `kh`, set every `canonical-*` repo to merge-commit-only (squash, rebase and fast-forward off), protect `main` as the compose document says, and create the org webhook to `http://knowledge-hub:8080/webhooks/forgejo` with secret `kh_webhook_hmac` and events `push` and `pull_request`. Record the first trusted commit of each canonical repo as `trust_anchor` in `kh.yaml`. Report back: a test merge produces an indexed T0 document (pass/fail).

**ma4-privacyfence-upstream (about 20 minutes).** Declare the hub as an upstream in PrivacyFence's operator file ([upstream-mcp.md](https://github.com/privacyfence/privacyfence/blob/main/docs/upstream-mcp.md)) with `audience` equal to `KH_PF_AUDIENCE`, `url` `http://knowledge-hub:8080/mcp`, read tools `auto` and write tools `popup`; map groups to audiences (for example `everyone@<domain>` to `all`, `mgmt@<domain>` to `mgmt`); set `KH_ALLOWED_HOSTS=knowledge-hub:8080`. Decide whether every restricted-audience holder also holds `all` and set `restricted_audiences_imply_all` in `kh.yaml` to match. Report back: `kh_search` returns hits for a person and nothing for a person outside the audience (pass/fail).

**ma5-embedding-benchmark (about 30 minutes).** On the CAX31 run `ollama pull bge-m3` and `python scripts/bench_embed.py --count 200`. Report back the chunks per second and the query-embedding latency. Accept if at least 20 chunks per second and under 300 ms per query; otherwise tell the planner to switch to a 768-dimension model (a new migration).

**ma6-live-smoke (about 30 minutes).** Run `kh rebuild`, `kh check`, and one `kh_propose_change` from a person's client; confirm the pull request appears in the right canonical repo with labels `promotion` and `domain/<d>`. Report back pass/fail. Live n8n writes need the PrivacyFence change that lets scoped tokens call gated tools; they are not part of this plan.

## 6. Risks and open questions

- The SDK may not pass `scope["state"]` to tools in stateless mode (the ContextVar fallback is named in 3.2); if the middleware cannot hand the verified caller to the tool at all, `p5` stops with `status=blocked`.
- `bge-m3` may be too slow on the arm64 box (ma5); the fallback changes `vector(1024)`, so it is a migration plus a full re-embed.
- Exact vector scans may be slow at the real corpus size; adding HNSW needs pgvector 0.8 or later with iterative scans. Research for the owner: which pgvector version the shared Postgres 17 container runs.
- The Forgejo version's API may lack `commits/{sha}/pull` or the review `official` and `dismissed` flags; `p9` probes the documented endpoints against recorded fixtures and stops with `status=blocked` and a `Research:` line if the shapes differ from section 3.6.
- Three roles plus one NOLOGIN role replace the one-role-per-app pattern of the compose repo; if that is refused, the fallback is a single login role that `SET ROLE`s into the others, which is weaker.
- The provenance exception covers raw records only; chained derivation is out of scope.
- A change to `raw_audience_map` leaves existing rows under-restricted until `kh recompute-visibility` runs; `kh check` flags a stale map hash.
- `service` principals cannot call tools through PrivacyFence; `ingest_raw` effectively accepts `n8n_workflow` only.
- If a phase finds that a name, schema or text in this plan contradicts the code it meets, it stops with `status=blocked` and does not improvise.

## Implementation manifest

```yaml
plan_slug: knowledge-hub-service
feature_branch: feature/knowledge-hub-service
tracking_issue: 1
max_parallel: 2
manual_before: []
manual_after:
  - id: ma1-roles-and-secrets
    title: Create the kh database roles and the secret files, then run kh migrate
    why: The phases test with a throwaway cluster, so only the real box proves the role split and grants
  - id: ma2-garage-bucket
    title: Run an S3-compatible store with bucket kh-raw and a scoped key
    why: The blob store is a fake in every test; only a real store proves put, get and delete
  - id: ma3-forgejo-settings
    title: Set merge-commit-only and branch protection, create the org webhook, record the trust anchors
    why: The trust walk and the webhook are tested against recorded fixtures, not the live Forgejo
  - id: ma4-privacyfence-upstream
    title: Declare the hub in PrivacyFence, map groups to audiences, set restricted_audiences_imply_all
    why: Only the live gateway proves claims, hosts and group-to-audience mapping end to end
  - id: ma5-embedding-benchmark
    title: Measure bge-m3 on the CAX31
    why: CPU speed on the arm64 box cannot be measured in the cloud environment
  - id: ma6-live-smoke
    title: Rebuild, check and one live propose_change
    why: Verifies the whole path with real repositories and a real pull request
verify_after_merge:
  - .venv/bin/python -m pytest -q -m unit
  - .venv/bin/python -m pytest -q -m integration
final_checks:
  - "`tld plan deviations check --file docs/plans/1/deviations.md --plan docs/plans/1/plan.md --all` exits 0"
  - "`.venv/bin/python -m pytest -q` passes with Postgres from scripts/ensure_test_pg.sh"
phases:
  - id: p1-skeleton
    title: Project skeleton, settings, config model and the real verify profile
    depends_on: []
    complexity: M
    touches:
      - pyproject.toml
      - .gitignore
      - .tld/toolkit.yaml
      - src/knowledge_hub/__init__.py
      - src/knowledge_hub/settings.py
      - src/knowledge_hub/config.py
      - src/knowledge_hub/cli.py
      - tests/unit/test_settings.py
      - tests/unit/test_config.py
    brief: |
      Context: greenfield repo; design in sections 3.1 (settings, dev override, config). Run `tld guard allow-profile` before editing `.tld/toolkit.yaml`.
      1. Create `pyproject.toml`: distribution `knowledge-hub`, `requires-python = ">=3.12"`, src layout, script `kh = knowledge_hub.cli:main`, dependencies `mcp==2.3.0`, `starlette`, `uvicorn`, `psycopg[binary,pool]`, `pgvector`, `PyJWT[crypto]`, `cryptography`, `httpx`, `pyyaml`, `pydantic-settings`, `boto3`; extras `dev` = `pytest`, `pytest-asyncio`, `ruff`. Pytest config: markers `unit` and `integration`, `asyncio_mode = "auto"`, `testpaths = ["tests"]`. Ruff: line length 110, select `E,F,I,B,UP`.
      2. `.gitignore` with `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `*.egg-info/`.
      3. `src/knowledge_hub/settings.py`: class `Settings(BaseSettings)` with env prefix `KH_` and every variable named in section 3.1 with the stated defaults and types; secret file settings are `Path | None`. Validator: if `env == "prod"` and `dev_identity` is set raise `ValueError("KH_DEV_IDENTITY is set while KH_ENV=prod; refusing to start")`; `dev_identity` parsed from JSON into a model with `sub`, `principal_type` (`claude_client` or `n8n_workflow` only), `audiences`, optional `email`. Function `load_settings()` returns a `Settings`.
      4. `src/knowledge_hub/config.py`: pydantic models for `kh.yaml` as in section 3.1 with the stated defaults; `load_config(path)`; validation rejects an unknown audience or domain and a repo audience not in `audiences`; error texts start with `config:`.
      5. `src/knowledge_hub/cli.py`: `main()` with subcommands `serve`, `migrate`, `rebuild`, `check`, `daily`, `recompute-visibility` registered through argparse; each prints `not implemented yet` and exits 3 except that `serve` calls `load_settings()` first and exits 2 printing the error on a settings error. `src/knowledge_hub/__init__.py` holds `__version__ = "0.1.0"`.
      6. Tests (both marked `unit`): `test_settings.py` covers defaults, the prod refusal text, dev identity accepted in `dev`, and `cli serve` exiting 2 with `KH_ENV=prod` and `KH_DEV_IDENTITY` set; `test_config.py` covers the default config, an unknown audience, and a repo audience not listed.
      7. `.tld/toolkit.yaml`: set `project.name: knowledge-hub`, `stacks: [python]`, `verify.fast` to `.venv/bin/python -m ruff check .` and `.venv/bin/python -m pytest -q -m unit`, `verify.dod` to `.venv/bin/python -m ruff check .` and `.venv/bin/python -m pytest -q`, `verify.after_merge_allow` to `[".venv/bin/python -m pytest"]`. Keep `docs.must_read` and `git`. Create the venv as the Python stack guide says and install with `.venv/bin/python -m pip install -e ".[dev]"`.
      Stop: if `mcp==2.3.0` or `pydantic-settings` cannot be installed, stop with status=blocked and the resolver output.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_settings.py tests/unit/test_config.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
      - "`tld profile validate .tld/toolkit.yaml --resolved` exits 0 and its output contains `\"stacks\": [` with python"
      - "grep -n 'KH_DEV_IDENTITY is set while KH_ENV=prod; refusing to start' src/knowledge_hub/settings.py matches"
  - id: p2-claims
    title: Vendored PrivacyFence verifier, claim middleware and test-claim helper
    depends_on: [p1-skeleton]
    complexity: M
    touches:
      - src/knowledge_hub/auth/__init__.py
      - src/knowledge_hub/auth/pf_claim_verify.py
      - src/knowledge_hub/auth/VENDORED.md
      - src/knowledge_hub/auth/claims.py
      - tests/data/pf_req_test_vectors.json
      - tests/support/__init__.py
      - tests/support/claims.py
      - tests/unit/test_claims.py
    brief: |
      Context: section 3.2. The contract is PrivacyFence `docs/identity-claim.md`; clone `https://github.com/privacyfence/privacyfence` outside this repo, check out commit `04d2ecac53428a1210b4b777f8c466359ee730bf`, read that document and the verifier first.
      1. Copy `contrib/verifier/pf_claim_verify.py` byte for byte to `src/knowledge_hub/auth/pf_claim_verify.py` and `contrib/verifier/req_test_vectors.json` to `tests/data/pf_req_test_vectors.json`. Write `auth/VENDORED.md` with the source URL, the commit and the file's sha256.
      2. `auth/claims.py`: dataclass `Caller(sub, principal_type, email, audiences: frozenset[str], token_id)`; `ReplayCache` protocol with `add_if_new(jti, expires_at) -> bool` and an `InMemoryReplayCache`; class `ClaimMiddleware(app, verifier, settings, replay_cache)` exactly as section 3.2 (path `/mcp` only, POST only, 413 over `max_body_bytes`, 401 with empty body for a missing header or any `ClaimError`, refuse `human` always and `service` on a `tools/call`, audience regex, `scope["state"]["kh_caller"]` and `["kh_call_hash"]`, body replay through a new `receive`, dev identity only when `settings.dev_identity` is set and no header is present). Function `caller_from_scope(scope) -> Caller` raising `KhAuthError`. Re-check the prod refusal in `__init__`.
      3. `tests/support/claims.py`: a module-level Ed25519 test key, `static_jwks()`, and `mint_claim(sub, principal_type, audiences, email=None, tool=None, arguments=None, lifetime=60, now=None, **overrides) -> str` producing a valid claim for a given tool call (computing `req` with the vendored `request_hash`), plus `verifier_for_tests()` returning a `ClaimVerifier` over the static JWKS.
      4. `tests/unit/test_claims.py` (marked `unit`): the six vectors reproduce through the vendored helper; a valid claim passes; missing header, expired claim, wrong `req`, a batch body, `human`, `service` on `tools/call`, a replayed `jti`, an oversize body and a bad audience name each give 401 (413 for oversize) with an empty body; dev override works with `KH_ENV=dev` and the middleware constructor raises under `prod`.
      Stop: if the vendored verifier's public names differ from those used here (`ClaimVerifier`, `ClaimError`, `CLAIM_HEADER`, `request_hash` or a documented equivalent), stop with status=blocked and list the names found.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_claims.py -q passes"
      - "`sha256sum src/knowledge_hub/auth/pf_claim_verify.py` equals the sha256 recorded in src/knowledge_hub/auth/VENDORED.md"
      - ".venv/bin/python -m ruff check src/knowledge_hub/auth tests/support tests/unit/test_claims.py passes"
  - id: p3-migrations
    title: Migration runner, migrations 0001-0004 and the test cluster fixture
    depends_on: [p1-skeleton]
    complexity: M
    touches:
      - src/knowledge_hub/db/__init__.py
      - src/knowledge_hub/db/migrate.py
      - src/knowledge_hub/migrations/0001_schema_roles_grants.sql
      - src/knowledge_hub/migrations/0002_core_tables.sql
      - src/knowledge_hub/migrations/0003_rls_functions_policies.sql
      - src/knowledge_hub/migrations/0004_chunk_triggers.sql
      - scripts/ensure_test_pg.sh
      - tests/conftest.py
      - tests/support/pg.py
      - tests/integration/test_migrate.py
      - .tld/toolkit.yaml
    brief: |
      Context: sections 3.3 and 3.4. Run `tld guard allow-profile` before editing `.tld/toolkit.yaml`.
      1. `scripts/ensure_test_pg.sh`: idempotent; if `/usr/lib/postgresql/16/bin/initdb` or `postgresql-16-pgvector` is missing run `apt-get update` and `apt-get install -y postgresql-16 postgresql-16-pgvector`; exit 0 when present.
      2. `tests/support/pg.py`: function `start_cluster()` that creates a data directory under `/var/tmp/kh-pg-<pid>`, runs `initdb -A trust -U postgres`, starts `pg_ctl` on a free port with `-c listen_addresses=` and a unix socket in that directory, and returns connection helpers; when `os.geteuid() == 0` run every PostgreSQL binary through `su postgres -s /bin/bash -c` after `chown postgres` of the directory. Function `bootstrap_roles(admin_dsn)` creates roles `kh_migrator`, `kh_app`, `kh_indexer` (LOGIN, NOSUPERUSER, NOBYPASSRLS, trust auth) and `kh_provenance` (NOLOGIN), database `kh` owned by `kh_migrator`, and `CREATE EXTENSION vector` in it.
      3. `tests/conftest.py`: session fixtures `pg_cluster` (starts and stops the cluster, drops the directory), `migrated_db` (bootstraps roles and runs the migrations as `kh_migrator`), and a function-scoped `fresh_db` that recreates the schema from a template database for isolation. Mark every test under `tests/integration` with `integration` through `pytest_collection_modifyitems`.
      4. `db/migrate.py`: `migrate(dsn)` reads `knowledge_hub/migrations/*.sql` (package data) in order, applies each in one transaction, records name and sha256 in `kh.schema_migrations` (created by `0001`), skips applied files, and raises `MigrationError` naming the file when an applied file's checksum changed. Wire `kh migrate` in `cli.py` is not in this phase's scope; expose `migrate_main()` for the later phase to call.
      5. Write migrations `0001` to `0004` exactly as listed in section 3.4 (tables, columns, `vector(1024)`, generated `tsv`, `kh_no_open_rows`, helper functions of 3.3, RLS enable and force, the policies and grants of 3.3, the two chunk triggers). Index `chunks (owner_key)`, `chunks USING gin (tsv)`, and `documents (repo, doc_key)`.
      6. `tests/integration/test_migrate.py`: applying twice is a no-op; a modified applied file raises; every table in `kh` has `relrowsecurity` and `relforcerowsecurity` true (query `pg_class`); `kh_app` is not a superuser, has no BYPASSRLS and owns no table; inserting a row with empty audiences and null emails violates `kh_no_open_rows`.
      7. Update `.tld/toolkit.yaml`: prepend `bash scripts/ensure_test_pg.sh` to `verify.dod`.
      Stop: if `apt-get install postgresql-16-pgvector` fails, stop with status=blocked and the apt output.
    acceptance:
      - "bash scripts/ensure_test_pg.sh exits 0"
      - ".venv/bin/python -m pytest tests/integration/test_migrate.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p4-rls-access
    title: Database access layer, replay cache and the table-level leak tests
    depends_on: [p2-claims, p3-migrations]
    complexity: M
    touches:
      - src/knowledge_hub/db/pool.py
      - src/knowledge_hub/db/caller_tx.py
      - src/knowledge_hub/auth/replay.py
      - tests/support/principals.py
      - tests/support/seed.py
      - tests/integration/test_rls_tables.py
      - tests/integration/test_replay_cache.py
    brief: |
      Context: section 3.3; the principal fixtures here are reused by every later leak test.
      1. `db/pool.py`: `make_app_pool(settings)` and `make_indexer_pool(settings)` returning `psycopg_pool.AsyncConnectionPool` with the `pgvector` adapter registered; each pool connects with its own DSN.
      2. `db/caller_tx.py`: async context manager `caller_tx(pool, caller)` implementing exactly the transaction of section 3.3 (four `set_config` calls bound as parameters, audiences comma-joined, `SET LOCAL statement_timeout = '5s'`).
      3. `auth/replay.py`: `PgReplayCache(pool)` implementing `add_if_new` with `INSERT INTO kh.claim_jti (jti, expires_at) VALUES (%s, %s) ON CONFLICT DO NOTHING` and returning whether a row was inserted; `purge()` deletes expired rows. Add `GRANT` of `claim_jti` to `kh_app` in the migration `0003` only if it is missing: if it is missing stop and report, do not edit migrations in this phase.
      4. `tests/support/principals.py`: named principals as `Caller` objects: `alice_all` (audiences `all`, email `alice@example.org`), `bob_mgmt` (`all`, `mgmt`, `bob@example.org`), `carol_hr` (`all`, `hr`), `dave_none` (no audiences, email `dave@example.org`), `eve_nobody` (no audiences, no email), `wf_token` (`n8n_workflow`, `all`, sub `n8n-token:t1`, no email). `tests/support/seed.py`: `seed_rows(indexer_pool)` inserting, through the indexer role, one document and one chunk per case: audience `{all}`, `{mgmt}`, `{hr}`, `{all,mgmt}`, attendee-only (`{}` with emails `[dave@example.org]`), empty-email-list `{}`/`[]`, plus one raw and one derived row of each kind.
      5. `tests/integration/test_rls_tables.py`: for every principal and every table `documents`, `chunks`, `raw_records`, `derived_records`, `candidates`, `conflicts`, `entities_cache`, selecting everything inside `caller_tx` returns exactly the rows whose visibility the principal satisfies (spell the expected ids out per principal); a guessed id of a hidden row returns zero rows; a query on the app pool outside `caller_tx` returns zero rows for every table; `kh_app` cannot UPDATE or DELETE any table and cannot read `index_jobs`; a chunk inserted by the indexer with a wrong tier inherits the owner's values; changing a document's visibility updates its chunks.
      6. `tests/integration/test_replay_cache.py`: first `add_if_new` is true, second false, after `purge` of an expired row it is true again.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_rls_tables.py tests/integration/test_replay_cache.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p5-mcp-get-doc
    title: HTTP app, MCP server, error guard and get_doc
    depends_on: [p4-rls-access]
    complexity: M
    touches:
      - src/knowledge_hub/app.py
      - src/knowledge_hub/mcp/__init__.py
      - src/knowledge_hub/mcp/server.py
      - src/knowledge_hub/mcp/errors.py
      - src/knowledge_hub/tools/__init__.py
      - src/knowledge_hub/tools/docs.py
      - tests/support/mcp_client.py
      - tests/integration/test_tool_get_doc.py
      - tests/unit/test_tool_schemas.py
    brief: |
      Context: sections 3.2, 3.5 (citation ids), 3.9. Verify first against the installed `mcp==2.3.0` that `MCPServer`, `streamable_http_app`, `StreamableHTTPSessionManager(stateless=True)`, `TransportSecuritySettings` and `ctx.request_context.request` exist; read `site-packages/mcp/server/`.
      1. `mcp/errors.py`: `KhError(code, text)` with the fixed-text catalogue (`NOT_FOUND = "not found"`, `INTERNAL = "internal error (ref {ref})"`, and one constant per text quoted in sections 3.5 to 3.8 so later phases import them); `tool_guard` decorator exactly as section 3.2.
      2. `mcp/server.py`: `build_mcp(settings)` returning the `MCPServer("knowledge-hub")` and a `register_tools(server, deps)` function that later phases extend; `Deps` dataclass holding the pools, settings, config, embedder (None for now) and blob store (None for now).
      3. `tools/docs.py`: `get_doc(id, version="")` accepting `doc:<uuid>`, `raw:<uuid>`, `derived:<uuid>`; one RLS query per kind inside `caller_tx`; returns text, tier, domain, owner, approved_by, approved_at, review_by, status, stale, entities, `supersedes` and `superseded_by` (the successor id only through an RLS join), `commit_sha`, `indexed_at`, `citation_id`, `source_link`. A malformed id and a missing or hidden row both give `not found`. `version` support is added later: here a non-empty `version` returns `version <sha> is not an indexed version of this document`.
      4. `app.py`: `build_app(settings)` as in section 3.2 (routes `/mcp` behind `ClaimMiddleware`, `/healthz`, lifespan entering the session manager, `TransportSecuritySettings` with the allowed hosts, `PgReplayCache`). The `/webhooks/forgejo` route is added in a later phase.
      5. `tests/support/mcp_client.py`: helper that runs the app in process (httpx ASGI transport) and calls tools with a signed claim from `tests/support/claims.py`, sending `initialize`, then the `tools/call` the way PrivacyFence does.
      6. Tests: `test_tool_get_doc.py` (integration): every principal of `tests/support/principals.py` gets exactly the documents it should through the MCP path; a guessed uuid, a hidden id and a malformed id give identical `not found` results; a forced `psycopg` error inside a tool returns `internal error (ref ...)` and the response text contains no row value. `test_tool_schemas.py` (unit): for every registered tool apply the PrivacyFence skip rules (object schema, no `$ref`/`$defs`/`definitions`, no `reason` property, under 32 KB).
      Stop: if the tool cannot read the verified caller in stateless mode through `scope["state"]`, use the ContextVar fallback in section 3.2; if neither works stop with status=blocked.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_tool_get_doc.py tests/unit/test_tool_schemas.py -q passes"
      - "grep -n 'allowed_hosts' src/knowledge_hub/app.py matches"
      - ".venv/bin/python -m ruff check . passes"
  - id: p6-search
    title: Embedding interface, hybrid search with RRF and its leak tests
    depends_on: [p5-mcp-get-doc]
    complexity: M
    touches:
      - src/knowledge_hub/embed/__init__.py
      - src/knowledge_hub/embed/base.py
      - src/knowledge_hub/search/__init__.py
      - src/knowledge_hub/search/sql.py
      - src/knowledge_hub/search/cursor.py
      - src/knowledge_hub/tools/search.py
      - src/knowledge_hub/mcp/server.py
      - tests/integration/test_search_ranking.py
      - tests/integration/test_search_leaks.py
      - tests/unit/test_cursor.py
    brief: |
      Context: sections 3.5, 3.10.
      1. `embed/base.py`: `EmbeddingProvider` protocol (`model`, `dim`, `async embed(texts) -> list[list[float]]`) and `FakeEmbedder(dim=1024)` returning a deterministic unit vector derived from a hash of the text so that identical texts are identical and different texts differ.
      2. `search/sql.py`: the statement of section 3.5 as one function `build_search_query(filters)` returning SQL text and parameters (vector order with `<=>`, `websearch_to_tsquery('english', ...)`, `row_number()` lists limited to `KH_SEARCH_POOL`, RRF with `KH_RRF_K`, the weight multipliers from settings, `DISTINCT ON (owner_key)`, limit and offset, and the `lower_tier_hits` aggregate over tiers below `min_tier`). Parameters are always bound, never concatenated.
      3. `search/cursor.py`: `encode_cursor(key, sub, audiences, query, offset)` and `decode_cursor(...)` as in section 3.5, raising `KhError` with the text `cursor does not belong to this query`.
      4. `tools/search.py`: tool `search(query, domains, min_tier, entities, limit, cursor)`; limit bounded 1 to 50; embeds the query with `deps.embedder`; applies the normative-answer error of section 3.5; registers in `mcp/server.py`.
      5. Tests (integration unless noted): `test_search_ranking.py`: a T0 chunk ranks above an equally relevant T3 chunk; a stale chunk ranks below a current one with the same text; a superseded one below a current one; a `disputed` T0 is down-weighted; a narrow-audience caller still gets its best chunks when many hidden chunks match more strongly (the recall test). `test_search_leaks.py`: for every principal of `tests/support/principals.py`, hits contain only visible citation ids, `lower_tier_hits` counts equal the visible T2/T3 counts and `newest_at` ignores hidden rows, a cursor from another principal is refused, and total hit count for a query matching hidden rows equals the visible count. `test_cursor.py` (unit): round trip, tamper, wrong sub.
      Stop: if `to_tsvector('english', ...)` or the `<=>` operator is unavailable in the test cluster, stop with status=blocked.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_search_ranking.py tests/integration/test_search_leaks.py tests/unit/test_cursor.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p7-http-e2e
    title: End-to-end HTTP tests and the tool leak-matrix registry
    depends_on: [p6-search]
    complexity: S
    touches:
      - tests/support/tool_cases.py
      - tests/integration/test_http_e2e.py
      - tests/integration/test_leak_matrix.py
    brief: |
      Context: sections 3.2 and 3.3. This closes milestone 1.
      1. `tests/support/tool_cases.py`: a registry `TOOL_CASES` (list of dataclass `ToolCase(name, args_for_hidden, args_for_visible, expect_hidden)`); later phases that add a tool append their cases here. Start with `search` and `get_doc`.
      2. `tests/integration/test_leak_matrix.py`: for every case and every principal, the response contains nothing outside the principal's visibility and the hidden-row case equals the missing-row case byte for byte.
      3. `tests/integration/test_http_e2e.py`: with the in-process app and signed claims, `initialize` then `tools/list` returns the tools with flat schemas; a `Host` not in `KH_ALLOWED_HOSTS` is refused; a request without a claim is 401 with an empty body; a claim whose `req` was made for other arguments is 401; a `tools/call` made with a `service` claim is 401; an `n8n_workflow` claim without email never sees an attendee-only row; a person with no groups but the matching verified email sees the attendee-only row.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_http_e2e.py tests/integration/test_leak_matrix.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p8-frontmatter-chunker
    title: Frontmatter validation and heading chunker
    depends_on: [p1-skeleton]
    complexity: S
    touches:
      - src/knowledge_hub/indexer/__init__.py
      - src/knowledge_hub/indexer/frontmatter.py
      - src/knowledge_hub/indexer/chunker.py
      - tests/unit/test_frontmatter.py
      - tests/unit/test_chunker.py
    brief: |
      Context: section 3.6 (frontmatter and chunking rules).
      1. `indexer/frontmatter.py`: `parse_document(text, path) -> ParsedDoc | Rejected`; split YAML frontmatter between `---` lines with `yaml.safe_load`; required keys `id`, `title`, `domain`, `review_by` (a date); optional `owner`, `type`, `entities`, `supersedes`, `superseded_by`, `status`; `domain` must be in the config domains (passed in). Reject with `Rejected(reason)` and the exact text `frontmatter must not set <key>; the server decides tier and audience` for any of `tier`, `audience`, `audiences`, `required_audiences`, `allowed_emails`, `visibility`, `approved_by`, `approved_at`. Other problems give `Rejected("frontmatter: <what>")`.
      2. `indexer/chunker.py`: `chunk_markdown(body, version) -> list[Chunk(ordinal, heading_path, text, content_sha)]` per section 3.6: split on `#` to `######` headings, `heading_path` is the chain joined with ` > `, pieces over 1200 characters split at paragraph boundaries into pieces of at most 1200 characters with 150 characters of overlap, pieces under 200 characters merge into the previous piece of the same section, `content_sha` is sha256 of `version|heading_path|text`.
      3. Tests (unit): every forbidden key, missing required keys, bad date, unknown domain, duplicate-free happy path; chunker cases for nested headings, a long section, a tiny trailing section, empty body, and stable `content_sha` across runs.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_frontmatter.py tests/unit/test_chunker.py -q passes"
      - ".venv/bin/python -m ruff check src/knowledge_hub/indexer tests/unit passes"
  - id: p9-trust-forgejo-read
    title: Trust walk and the Forgejo read client
    depends_on: [p1-skeleton]
    complexity: M
    touches:
      - src/knowledge_hub/forgejo/__init__.py
      - src/knowledge_hub/forgejo/client.py
      - src/knowledge_hub/indexer/trust.py
      - tests/data/forgejo/
      - tests/unit/test_trust.py
      - tests/unit/test_forgejo_client.py
    brief: |
      Context: section 3.6 (trust, API facts) and the compose repo document `docs/knowledge-hub.md` finding 6.
      1. `forgejo/client.py`: `ForgejoClient(base_url, token, org)` over `httpx.AsyncClient` with methods `list_tree(repo, sha)`, `get_raw(repo, path, sha)`, `commits_first_parent(repo, branch, since_sha)`, `pull_for_commit(repo, sha)`, `pull_reviews(repo, number)`, `compare(repo, base, head)`; the write side is added in a later phase, but a method `_guard_ref(ref)` is defined now that raises `ForgejoRefused` for any ref that does not start with `bot/promote/` and is used by every future write.
      2. `indexer/trust.py`: pure function `evaluate_chain(commits, prs, reviews, bot_logins, anchor) -> TrustResult(trusted, verdicts: dict[sha, Verdict], alerts)` implementing the rules of section 3.6 (merge commit, PR merged with `merge_commit_sha`, at least one official non-dismissed `APPROVED` review by someone who is neither author nor bot; a non-merge first-parent commit sets `trusted = False`).
      3. Fixtures under `tests/data/forgejo/` written by hand as JSON in the shapes of the Forgejo API (commit, pull request with `merged` and `merge_commit_sha`, review with `official`, `dismissed`, `state`, `user.login`), one set per scenario.
      4. Tests (unit, with `httpx.MockTransport`): all-approved chain is trusted; a self-approval does not count; a dismissed review does not count; a bot approval does not count; a non-merge commit makes the repo untrusted and names the sha; a squash-merged PR (non-merge commit) is untrusted; the client refuses a write ref outside `bot/promote/`.
      Stop: if the shapes you must assume for `official`, `dismissed` or the commit-to-PR lookup cannot be matched to Forgejo's published API without network access, keep the fixtures as the assumption, finish, and end with status=blocked and a line `Research: do the Forgejo reviews API and commits/{sha}/pull expose official, dismissed and merge_commit_sha as the fixtures assume?`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_trust.py tests/unit/test_forgejo_client.py -q passes"
      - ".venv/bin/python -m ruff check src/knowledge_hub/forgejo src/knowledge_hub/indexer/trust.py passes"
  - id: p10-embed-providers
    title: Ollama and HTTP embedding providers and the benchmark script
    depends_on: [p6-search]
    complexity: S
    touches:
      - src/knowledge_hub/embed/ollama.py
      - src/knowledge_hub/embed/http.py
      - src/knowledge_hub/embed/factory.py
      - scripts/bench_embed.py
      - tests/unit/test_embed_providers.py
    brief: |
      Context: section 3.10.
      1. `embed/ollama.py`: `OllamaEmbedder(url, model, dim)` calling `POST <url>/api/embed` with `{"model", "input": [texts]}` in batches of 32 and reading `embeddings`; raises `EmbeddingError` on a dimension other than `dim` or a non-200 status.
      2. `embed/http.py`: `HttpEmbedder(url, token, model, dim, allowed_audiences)` calling a JSON endpoint `{"model", "input"}` returning `{"data": [{"embedding": [...]}]}` with the bearer token; method `may_embed(visibility) -> bool` true only when the row's `required_audiences` is a non-empty subset of `allowed_audiences` and `allowed_emails` is null; refuses a dimension mismatch.
      3. `embed/factory.py`: `make_embedder(settings) -> EmbeddingProvider` choosing by `KH_EMBED_PROVIDER`; query embedding always uses the Ollama provider (function `make_query_embedder`).
      4. `scripts/bench_embed.py`: argparse `--count` (default 200) and `--url`; embeds `count` synthetic 800-character paragraphs and 20 short queries with the configured model and prints chunks per second and the mean and p95 query latency in milliseconds.
      5. Tests (unit, `httpx.MockTransport`): batching, dimension mismatch error, `may_embed` truth table, factory selection.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_embed_providers.py -q passes"
      - ".venv/bin/python scripts/bench_embed.py --help exits 0"
  - id: p11-sync
    title: Index jobs migration and the document sync
    depends_on: [p4-rls-access, p6-search, p8-frontmatter-chunker, p9-trust-forgejo-read]
    complexity: M
    touches:
      - src/knowledge_hub/migrations/0005_index_jobs_repo_state.sql
      - src/knowledge_hub/indexer/sync.py
      - tests/integration/test_sync.py
      - tests/support/forgejo_fake.py
    brief: |
      Context: sections 3.4 (0005) and 3.6.
      1. Write migration `0005_index_jobs_repo_state.sql` exactly as listed in section 3.4 with grants for `kh_indexer` only.
      2. `tests/support/forgejo_fake.py`: an in-memory fake of the `ForgejoClient` read methods built from a small description of repos, commits, PRs and reviews, so sync tests need no HTTP.
      3. `indexer/sync.py`: `async sync_repo(indexer_pool, forgejo, embedder, config, repo, head_sha)` that: loads `repo_state`; runs `evaluate_chain`; lists the tree at `head_sha`; parses each markdown file with `parse_document`; for rejected files removes any earlier row and writes an `index_alerts` row with the rejection text; upserts documents by `(repo, doc_key)` with tier and visibility from config (`T0`/`T1`, `required_audiences = {repo audience}`, `allowed_emails = NULL`), `status` `current`, `superseded` (when another document names it in `supersedes` or it has `superseded_by`) or `disputed` (untrusted repo, or no approved merge PR, with the alert texts of section 3.6), `approved_by`/`approved_at` from the reviews, `merge_pr`, `pr_url`; applies the `superseded_by` audience rule; handles rename (same `doc_key`, new path) and deletion (document absent from the tree); inserts a `document_versions` row per indexed commit; rewrites chunks only when the chunk hash or embedding model changed; embeds before the write transaction; writes the repo in one transaction under `pg_advisory_xact_lock(hashtext(repo))`; updates `repo_state`.
      4. Tests (integration, using the fake): approval extraction fills `approved_by` and `approved_at`; a file without an approved merge PR is `disputed` and an alert row exists; frontmatter claiming `tier` is not indexed and alerted; rename keeps the document id; deletion removes document and chunks; an untrusted repo marks everything `disputed`; syncing the same head twice leaves `indexed_at` and chunk ids unchanged.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_sync.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p12-webhook-worker
    title: Forgejo webhook, HMAC check and the job worker
    depends_on: [p11-sync, p5-mcp-get-doc]
    complexity: M
    touches:
      - src/knowledge_hub/auth/webhook_hmac.py
      - src/knowledge_hub/indexer/webhook.py
      - src/knowledge_hub/indexer/worker.py
      - src/knowledge_hub/app.py
      - tests/unit/test_webhook_hmac.py
      - tests/integration/test_webhook_worker.py
    brief: |
      Context: section 3.6 (webhook and worker).
      1. `auth/webhook_hmac.py`: `verify_forgejo_signature(body, header, secret) -> bool` (hex HMAC-SHA256, `hmac.compare_digest`, a missing or malformed header is false).
      2. `indexer/webhook.py`: Starlette endpoint `POST /webhooks/forgejo`: read the raw body (cap `KH_MAX_BODY_BYTES`), verify the signature (401 with empty body when bad), parse JSON, accept only repos of the configured org and listed in config (else 202 and ignore), and insert an `index_jobs` row: kind `git_push` for a `push` event to `refs/heads/main`, kind `pr_event` for a `pull_request` event (payload keeps repo, action, number, merged, head sha); answer 202.
      3. `indexer/worker.py`: `Worker(indexer_pool, forgejo, embedder, config)` with `run_once()` (claims one queued job with `FOR UPDATE SKIP LOCKED`, runs it, marks `done` or `failed` with attempts and `run_after` backoff of 30 seconds times attempts, at most 5 attempts) and `run_forever()`; for `git_push` call `sync_repo` with the head sha; `pr_event` jobs are only recorded now (a later phase handles them).
      4. `app.py`: add the route and start the worker in the lifespan when `KH_FORGEJO_URL` is set.
      5. Tests: unit for the signature function (valid, wrong secret, missing, tampered body); integration: a signed push payload creates a job and 202, a bad signature creates nothing and gives 401, `run_once` indexes a fake repo and marks the job done, a failing sync marks it failed and schedules a retry.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_webhook_hmac.py tests/integration/test_webhook_worker.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p13-rebuild-check
    title: kh rebuild, kh check, kh migrate, kh serve and get_doc versions
    depends_on: [p12-webhook-worker, p7-http-e2e]
    complexity: M
    touches:
      - src/knowledge_hub/indexer/rebuild.py
      - src/knowledge_hub/cli.py
      - src/knowledge_hub/tools/docs.py
      - tests/integration/test_rebuild.py
      - tests/integration/test_get_doc_version.py
    brief: |
      Context: section 3.6 (rebuild, check, get_doc versions).
      1. `indexer/rebuild.py`: `rebuild(indexer_pool, forgejo, embedder, config, repo=None)` re-walks trust and calls `sync_repo` at each configured repo's head, removes documents of the repo that are absent from the tree; `check(...)` returns a `CheckReport` listing repos whose head sha or any document `content_sha` differs from the index, chunks that differ from their owner (the drift query of section 3.3), and a stale `raw_audience_map` hash when the map table exists.
      2. `cli.py`: wire `migrate` (calls `migrate` with `KH_DATABASE_URL_MIGRATOR`), `serve` (uvicorn on `0.0.0.0:8080` unless `--port`), `rebuild [--repo R]` and `check` (exit 1 on any drift). Leave `daily` and `recompute-visibility` as `not implemented yet`.
      3. `tools/docs.py`: support `version=<sha>` as section 3.6 says: accepted only when `document_versions` holds the sha for the document (the check runs through RLS), then fetch with `ForgejoClient.get_raw` from `deps.forgejo`; otherwise `version <sha> is not an indexed version of this document`.
      4. Tests (integration, with the fake): rebuild twice leaves rows, chunk ids and `indexed_at` unchanged; rebuild after a document is deleted in the fake removes it; `check` is clean after rebuild and reports drift after a manual chunk edit; `get_doc` with an indexed older sha returns that content; with an unknown sha it returns the fixed error; a version request for a document the caller cannot see returns `not found` without calling the fake.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_rebuild.py tests/integration/test_get_doc_version.py -q passes"
      - ".venv/bin/python -m knowledge_hub.cli check --help exits 0"
  - id: p14-blobs
    title: S3-compatible blob store
    depends_on: [p1-skeleton]
    complexity: S
    touches:
      - src/knowledge_hub/blobs/__init__.py
      - src/knowledge_hub/blobs/s3.py
      - tests/support/blobs.py
      - tests/unit/test_blobs.py
    brief: |
      Context: section 3.10.
      1. `blobs/s3.py`: `BlobStore` protocol (`async put(key, data, content_type)`, `async get(key) -> bytes`, `async delete(key)`) and `S3BlobStore(endpoint, bucket, access_key, secret_key, region)` over `boto3` (`config=Config(s3={"addressing_style": "path"}, signature_version="s3v4")`), each call run with `asyncio.to_thread`; a missing key raises `BlobNotFound`.
      2. `tests/support/blobs.py`: `MemoryBlobStore` implementing the protocol, and `make_s3_store_with_stubber()` returning an `S3BlobStore` whose client is wrapped by `botocore.stub.Stubber` for the unit test.
      3. Tests (unit): `MemoryBlobStore` round trip and not-found; `S3BlobStore` builds put, get and delete requests with path-style addressing and the configured bucket (assert through the stubber's expected params).
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_blobs.py -q passes"
      - ".venv/bin/python -m ruff check src/knowledge_hub/blobs tests/support/blobs.py passes"
  - id: p15-ingest-raw
    title: ingest_raw, the audience map, raw visibility and embed jobs
    depends_on: [p14-blobs, p12-webhook-worker, p7-http-e2e]
    complexity: M
    touches:
      - src/knowledge_hub/migrations/0006_raw_records.sql
      - src/knowledge_hub/tools/raw.py
      - src/knowledge_hub/indexer/worker.py
      - src/knowledge_hub/mcp/server.py
      - src/knowledge_hub/config.py
      - tests/support/tool_cases.py
      - tests/integration/test_ingest_raw.py
    brief: |
      Context: sections 3.4 (0006), 3.7 (first bullet), 3.9.
      1. Write migration `0006_raw_records.sql` as listed in section 3.4: the table, `raw_audience_map`, the unique key, and the trigger `raw_records_assign_visibility` (before insert: look up `series:<series>` then `folder:<folder>` in `raw_audience_map`, union the audiences when both match; with a match set `required_audiences` and `allowed_emails = NULL`; with none set `required_audiences = '{}'` and `allowed_emails = lower(participants)`; raise `cannot ingest: no audience mapping for series or folder and no participants` when there is no match and no participants; the application-supplied visibility columns are always overwritten). Grants and RLS as in 3.3.
      2. `config.py`: add a function `audience_map_hash(config)`; `indexer/worker.py`: at startup load `config.raw_audience_map` into `raw_audience_map` (replace all rows in one transaction) and record the hash in `repo_state` under the pseudo repo `__audience_map__`; add job kinds `embed_raw` (read the blob from the blob store, chunk with `chunk_markdown`, embed, insert chunks; chunks inherit visibility by the trigger).
      3. `tools/raw.py`: tool `ingest_raw` with the arguments of section 3.9; refuse a principal type outside `config.ingest_principal_types` with `ingest_raw is limited to workflow and service principals`; refuse a body over `KH_MAX_RAW_BYTES` with `body too large (limit <n> bytes)`; validate domain and `occurred_at`; write the blob to `raw/<uuid4>` first, insert the row (application-generated id, no RETURNING, `ingested_by_sub` and `ingested_by_type` from the caller, excerpt of 2000 characters), and queue an `embed_raw` job through the indexer role helper; a unique-key collision returns the same success text as a new insert and changes nothing (no existence leak); returns `{"citation_id": "raw:<uuid>"}` with the id it generated.
      4. Register the tool in `mcp/server.py` and append its cases to `tests/support/tool_cases.py`.
      5. Tests (integration): mapped series gives its audiences and no emails; unmapped gives `{}` plus lower-cased participants; unmapped with no participants is rejected with the exact text; a person (`claude_client`) is refused; `wf_token` succeeds but cannot read back an attendee-only row it wrote; a body over the limit is rejected; the audience map load is idempotent; after `run_once` on the `embed_raw` job the chunks exist with the row's visibility.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_ingest_raw.py tests/integration/test_leak_matrix.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p16-derived
    title: write_derived, derived visibility function and the provenance exception
    depends_on: [p15-ingest-raw]
    complexity: M
    touches:
      - src/knowledge_hub/migrations/0007_derived.sql
      - src/knowledge_hub/visibility.py
      - src/knowledge_hub/tools/derived.py
      - src/knowledge_hub/mcp/server.py
      - src/knowledge_hub/indexer/worker.py
      - tests/support/tool_cases.py
      - tests/unit/test_visibility.py
      - tests/integration/test_write_derived.py
    brief: |
      Context: section 3.7 (everything after the first bullet), 3.4 (0007).
      1. Write migration `0007_derived.sql`: the two tables, function `kh.derived_visibility(source_ids text[])` as section 3.7 specifies (`SECURITY DEFINER`, owned by `kh_provenance`, `SET search_path = kh, pg_temp`, column-level grants to `kh_provenance`, the raw-only provenance branch), and the deferred constraint trigger `derived_visibility_matches`. `kh_app` gets `INSERT` on `derived_records` and `derived_sources`, `EXECUTE` on the function, and the RLS policies of 3.3.
      2. `visibility.py`: pure `derive_visibility(sources: list[Visibility]) -> Visibility` (union of audiences; intersection of non-null email lists; `None` when all are `None`; `[]` when the intersection is empty) used by unit tests and the later routing code.
      3. `tools/derived.py`: tool `write_derived` with the arguments of section 3.9: parse and validate ids and optional `@version` (a mismatch gives `source <id> changed since you read it (now <version>)`), `claims_json` as section 3.7 says, call `kh.derived_visibility` inside `caller_tx`, apply the `n_resolved` check with the fixed error text, insert the derived row and `derived_sources` with the returned visibility (no RETURNING, application uuid), queue an `embed_derived` job; extend `worker.py` with the `embed_derived` kind. Register in `mcp/server.py` and add cases to `tool_cases.py`.
      4. Tests: unit `test_visibility.py` (union, intersection, null versus empty, three sources); integration `test_write_derived.py`: union of audiences from a `{mgmt}` and a `{hr}` source requires a caller holding both; intersection of email lists; a caller who cannot see a cited source is rejected with the fixed text and a nonexistent id gives the identical text; `wf_token` can cite a raw record it ingested (attendee-only) and the stored derived row has `allowed_emails` equal to the participants; `wf_token` cannot cite a raw record another token ingested; the provenance exception does not let `wf_token` read the raw row afterwards; a mismatching stored visibility fails at commit (insert a wrong value through a test-only direct connection as `kh_migrator` and expect the constraint trigger to raise).
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_visibility.py tests/integration/test_write_derived.py tests/integration/test_leak_matrix.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p17-recompute
    title: kh recompute-visibility
    depends_on: [p16-derived, p13-rebuild-check]
    complexity: S
    touches:
      - src/knowledge_hub/jobs/__init__.py
      - src/knowledge_hub/jobs/recompute.py
      - src/knowledge_hub/cli.py
      - tests/integration/test_recompute.py
    brief: |
      Context: section 3.7 (last bullet).
      1. `jobs/recompute.py`: `recompute_visibility(indexer_pool, config)` loads the audience map, recomputes `required_audiences` and `allowed_emails` of every `raw_records` row with the rules of the raw trigger (series and folder mapping; unmapped keeps participants), then recomputes every `derived_records` row from its `derived_sources` in dependency order (a derived row may cite another derived row) using the same union and intersection rules in SQL, and records the map hash; chunk rows follow through `owner_propagate_to_chunks`.
      2. `cli.py`: wire `recompute-visibility`.
      3. Tests (integration): after tightening the map (series moved from `all` to `mgmt`) the raw row, its chunks and a derived row citing it become `{mgmt}`; running twice changes nothing; `check` reports a stale map hash before and clean after.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_recompute.py -q passes"
  - id: p18-routing
    title: Promotion routing and the refusal to widen
    depends_on: [p16-derived]
    complexity: S
    touches:
      - src/knowledge_hub/visibility.py
      - tests/unit/test_routing.py
    brief: |
      Context: section 3.8 (first bullet).
      1. In `visibility.py` add `route_target_repo(sources: list[Visibility], config, explicit_repo=None) -> str` implementing section 3.8 exactly: refuse sources with non-null `allowed_emails`, build `U`, apply `restricted_audiences_imply_all`, refuse `U` empty and `|U| >= 2`, map a single audience to its canonical repo (a `T0` repo of config), and when `explicit_repo` is given and its audience is not equal to `U` raise the `wider` error; all errors are `KhError` with the exact texts of section 3.8.
      2. Tests (unit): `{all}` goes to `canonical-all`; `{mgmt}` to `canonical-mgmt`; `{all, mgmt}` is refused without the flag and goes to `canonical-mgmt` with `restricted_audiences_imply_all: true`; `{mgmt, hr}` is refused; email-restricted is refused; an explicit `canonical-all` for `{mgmt}` is refused with the wider text; an explicit narrower repo is allowed only when equal.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_routing.py -q passes"
  - id: p19-propose-change
    title: propose_change, candidates, the Forgejo write side and the daily limit
    depends_on: [p18-routing, p9-trust-forgejo-read]
    complexity: M
    touches:
      - src/knowledge_hub/migrations/0008_candidates_conflicts.sql
      - src/knowledge_hub/forgejo/client.py
      - src/knowledge_hub/tools/promote.py
      - src/knowledge_hub/mcp/server.py
      - tests/support/forgejo_fake.py
      - tests/support/tool_cases.py
      - tests/unit/test_forgejo_write.py
      - tests/integration/test_propose_change.py
    brief: |
      Context: section 3.8 and 3.4 (0008).
      1. Write migration `0008_candidates_conflicts.sql` as listed in section 3.4, including the unique partial index on `dedupe_key`; RLS and grants as in 3.3 (`kh_app` inserts with `created_by_sub = kh.caller_sub()`; `kh_indexer` updates state columns).
      2. `forgejo/client.py`: write methods `create_branch(repo, name, from_sha)`, `put_file(repo, branch, path, content, message, sha=None)`, `open_pull(repo, head, base, title, body, labels)`; every one calls `_guard_ref` first and `open_pull` refuses `base` other than `main` only for the head `bot/promote/...`. Extend `tests/support/forgejo_fake.py` with the write side.
      3. `tools/promote.py`: tool `propose_change` as section 3.8 (exactly one of `target_id` and `new_path`; sources resolved by strict RLS through the ordinary read path, not the provenance function; routing through `route_target_repo`; dedupe key and the daily limit per `(domain, target_repo)` counted under RLS since 00:00 UTC with the fixed texts; PR body from the template `PR_TEMPLATE` defined in the module with headings `## What`, `## Why`, `## Sources` (list of citation ids) and `## Candidate` (id); labels `promotion` and `domain/<domain>`); a Forgejo failure sets state `failed` and returns `could not open the pull request; the proposal is saved as candidate <id> and will be retried`. Register in `mcp/server.py` and add cases to `tool_cases.py`.
      4. Tests: unit `test_forgejo_write.py` (guard refuses `main` and any other ref, accepts `bot/promote/x`); integration `test_propose_change.py`: routes `{mgmt}` sources to `canonical-mgmt` and `{all}` to `canonical-all`; refuses mixed audiences, email-restricted sources and an explicit wider repo; the second identical call returns the dedupe text; the sixth call in a domain and repo on one day is refused; the PR carries both labels and the branch `bot/promote/<candidate-id>`; the fake records that nothing was pushed to `main`; a caller cannot see another caller's candidate.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_forgejo_write.py tests/integration/test_propose_change.py tests/integration/test_leak_matrix.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p20-conflicts-stale
    title: flag_conflict, list_conflicts and list_stale
    depends_on: [p19-propose-change, p13-rebuild-check]
    complexity: M
    touches:
      - src/knowledge_hub/tools/conflicts.py
      - src/knowledge_hub/tools/docs.py
      - src/knowledge_hub/mcp/server.py
      - tests/support/tool_cases.py
      - tests/integration/test_conflicts.py
      - tests/integration/test_list_stale.py
    brief: |
      Context: sections 3.4 (conflicts), 3.9.
      1. `tools/conflicts.py`: `flag_conflict(a, b, note)`: both ids resolved under strict RLS (`not found` otherwise, identical text for missing and hidden), `a != b`, note 1 to 2000 characters; the conflict's visibility is computed through the same derivation as a derived row (union of audiences, intersection of emails of the two items) with `kh.derived_visibility`-style logic limited to strict visibility; insert conflict and `conflict_items` with application uuids and no RETURNING. `list_conflicts(domain, status, limit, cursor)` uses a keyset cursor over visible rows.
      2. `tools/docs.py`: add `list_stale(domain, owner, limit, cursor)` returning visible documents where `stale` is true or `review_by < current_date`, newest overdue first, keyset cursor; it never counts or mentions hidden rows.
      3. Register both in `mcp/server.py`; append cases to `tool_cases.py`.
      4. Tests (integration): a conflict between a `{mgmt}` and a `{hr}` item is visible only to callers holding both; a caller cannot flag a conflict on an item it cannot see; `list_conflicts` filters by domain and status; `list_stale` returns only visible overdue documents for each principal and the page count never changes with hidden rows.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_conflicts.py tests/integration/test_list_stale.py tests/integration/test_leak_matrix.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p21-pr-outcomes
    title: "Webhook outcomes: candidate promotion, rejection and conflict closing"
    depends_on: [p19-propose-change, p12-webhook-worker]
    complexity: S
    touches:
      - src/knowledge_hub/indexer/worker.py
      - tests/integration/test_pr_outcomes.py
    brief: |
      Context: section 3.8 (PR events).
      1. In `worker.py` handle `pr_event` jobs: ignore a PR whose head branch does not start with `bot/promote/`; with `action: closed` and `merged: true` set the candidate with that branch to `promoted` (record `decided_at`), and set every open `conflicts` row that has a `conflict_items` entry citing one of the candidate's sources to `closed` with `closed_by_pr`; with `merged: false` set the candidate to `rejected`. Re-running the same event changes nothing.
      2. Tests (integration): merged PR promotes and closes the related conflicts and leaves unrelated ones open; closed-unmerged rejects; a PR from another branch is ignored; the same event twice is a no-op; a PR event for an unknown candidate is recorded as done without error.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_pr_outcomes.py -q passes"
  - id: p22-entities-daily
    title: Grist entity sync, entity tools and the daily job
    depends_on: [p20-conflicts-stale, p21-pr-outcomes, p17-recompute]
    complexity: M
    touches:
      - src/knowledge_hub/grist/__init__.py
      - src/knowledge_hub/grist/client.py
      - src/knowledge_hub/jobs/daily.py
      - src/knowledge_hub/tools/entities.py
      - src/knowledge_hub/mcp/server.py
      - src/knowledge_hub/cli.py
      - src/knowledge_hub/settings.py
      - tests/support/tool_cases.py
      - tests/unit/test_grist_client.py
      - tests/integration/test_entities_daily.py
    brief: |
      Context: sections 3.1 (config `entities`), 3.4 (`entities_cache`), 3.9. The Grist document `kh-entities` has tables `Products`, `Components`, `Versions`, `Customers`, each with columns `key` (`prd:gateway`), `name`, `owner_team`, `status`, `links` (compose repo document section 7).
      1. `settings.py`: add `KH_GRIST_URL`, `KH_GRIST_DOC_ID`, `KH_GRIST_KEY_FILE`.
      2. `grist/client.py`: `GristClient(url, doc_id, key)` with `fetch_table(name) -> list[dict]` over `GET /api/docs/<doc>/tables/<name>/records` with the bearer key.
      3. `jobs/daily.py`: `run_daily(indexer_pool, grist, config, today)` that sets `documents.stale = true` where `review_by < today` and false otherwise (chunks follow by trigger), upserts `entities_cache` rows from the four tables (kind from the key prefix; visibility from `config.entities`, so `Customers` rows are `{contracts}`), deletes cache rows no longer in Grist, and purges expired `claim_jti` rows; idempotent.
      4. `tools/entities.py`: `get_entity(id)` (the row plus documents that list the entity in `entities` and the visible ones only, found under RLS) and `list_entities(type, status, owner_team, limit, cursor)` over visible rows; register both; add cases to `tool_cases.py`; wire `kh daily` in `cli.py`.
      5. Tests: unit for the Grist client with `httpx.MockTransport`; integration: daily marks the stale flag and moves a stale document below a current one in search; entity rows of kind `cus` are invisible to a caller without `contracts`; a removed entity disappears; rerun changes nothing.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_grist_client.py tests/integration/test_entities_daily.py tests/integration/test_leak_matrix.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p23-hardening
    title: Rate limits and metrics
    depends_on: [p22-entities-daily]
    complexity: M
    touches:
      - src/knowledge_hub/ratelimit.py
      - src/knowledge_hub/metrics.py
      - src/knowledge_hub/app.py
      - src/knowledge_hub/mcp/errors.py
      - tests/unit/test_ratelimit.py
      - tests/integration/test_metrics.py
    brief: |
      Context: milestone 5; section 3.1 (`KH_RATE_LIMIT_PER_MIN`).
      1. `ratelimit.py`: in-process token bucket keyed by claim `sub` (`allow(sub, now) -> bool`, capacity and refill from `KH_RATE_LIMIT_PER_MIN`); `tool_guard` in `mcp/errors.py` calls it and returns the fixed text `rate limit exceeded; retry in <n> seconds`.
      2. `metrics.py`: a tiny Prometheus text exposition (no new dependency): counters `kh_tool_calls_total{tool,outcome}`, `kh_claim_rejections_total{code}`, `kh_webhook_events_total{result}`, `kh_index_jobs{state}` gauge, histogram-like `kh_tool_seconds_sum` and `_count{tool}`. Labels never contain a sub, audience, email or document id. Route `GET /metrics` in `app.py`, answering only requests whose `Host` is in `KH_ALLOWED_HOSTS`; it is not reachable through the `/mcp` claim path.
      3. Tests: unit for the bucket (refill, burst, per-sub isolation); integration: one tool call increments the counters, a rejected claim increments `kh_claim_rejections_total`, and the metrics text contains no email, no uuid and no audience name.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_ratelimit.py tests/integration/test_metrics.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p24-deploy-backup
    title: arm64 Dockerfile, backup script and example configuration
    depends_on: [p23-hardening]
    complexity: M
    touches:
      - Dockerfile
      - .dockerignore
      - deploy/backup-kh.sh
      - deploy/kh.example.yaml
      - deploy/compose.example.yaml
      - tests/unit/test_deploy_files.py
    brief: |
      Context: milestone 5; the compose repo conventions (internal networks `db` and `kh`, `cap_drop: [ALL]`, `no-new-privileges`, read-only root fs, secrets as files under `/run/secrets`).
      1. `Dockerfile`: multi-stage on `python:3.13-slim` (build wheels in the first stage, copy into the second), non-root user `kh` (uid 10001), no build tools in the final stage, `ENTRYPOINT ["kh"]`, `CMD ["serve"]`, `EXPOSE 8080`, no secrets, no `latest` tag; works for `linux/arm64` (no architecture-specific lines). `.dockerignore` excludes `.git`, `.venv`, `tests`, `docs`.
      2. `deploy/backup-kh.sh`: `set -euo pipefail`; `pg_dump -Fc` of only the T2/T3 tables (`raw_records`, `raw_audience_map`, `derived_records`, `derived_sources`, `candidates`, `candidate_sources`, `conflicts`, `conflict_items`) with `-t` flags into `${BACKUP_DIR}/kh-t2t3-<date>.dump`, then mirrors the bucket with `aws s3 sync --endpoint-url "$KH_S3_ENDPOINT" s3://$KH_S3_BUCKET "${BACKUP_DIR}/blobs/"`; prints a final line `backup ok <date>`; documents that git-derived tables are rebuilt with `kh rebuild` and are not backed up.
      3. `deploy/kh.example.yaml` (a complete `kh.yaml` per section 3.1 with the default repos) and `deploy/compose.example.yaml` (service `knowledge-hub` on networks `db` and `kh`, env `KH_*` with `_FILE` secrets, hardening options, healthcheck on `/healthz`).
      4. `tests/unit/test_deploy_files.py` (unit): the example config loads with `load_config`; the Dockerfile has a `USER` other than root, no `latest`, and `ENTRYPOINT`; `bash -n deploy/backup-kh.sh` passes; the script names every T2/T3 table and does not name `documents` or `chunks`.
      Stop: the sandbox has no Docker daemon, so the image build is not run here; do not add a skip for it.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_deploy_files.py -q passes"
      - "bash -n deploy/backup-kh.sh exits 0"
  - id: p25-retire
    title: "Retirement: README, design document and runbook"
    depends_on: [p24-deploy-backup, p17-recompute, p21-pr-outcomes]
    complexity: M
    touches:
      - README.md
      - docs/design.md
      - docs/runbook.md
      - docs/configuration.md
    brief: |
      Context: the whole plan; this phase only writes documentation, from sections 3.1 to 3.11 and the code as built.
      1. `README.md`: what the service is, the four tiers, the audiences model in five sentences, quick start (`bash scripts/ensure_test_pg.sh`, create the venv, run the tests), the `kh` commands, and links to the three documents below. Keep the Python venv instructions of the stack guide.
      2. `docs/design.md`: the decisions of section 3 as they were built, with the rejected alternatives of 3.11 and the reasons (roles and RLS, claim middleware, trust walk, provenance exception, promotion routing, exact vector scans, S3 from day one, indexed `get_doc`); update any name that changed during implementation by reading the code.
      3. `docs/configuration.md`: a table of every `KH_*` setting with default and meaning, and the `kh.yaml` keys with an example (read `settings.py` and `config.py`).
      4. `docs/runbook.md`: install order (roles and database, secrets, `kh migrate`, `kh serve`), routine tasks (`kh rebuild`, `kh check`, `kh daily`, `kh recompute-visibility` after editing the audience map), alerts and what to do for each text in `index_alerts` (disputed file, untrusted repo, rejected frontmatter), restoring T2/T3 from `deploy/backup-kh.sh` output, rotating a webhook secret and a Forgejo token, and a note that live n8n writes depend on PrivacyFence letting scoped tokens call gated tools.
      5. Run a link and heading check by eye: every relative link resolves; no document is over 400 lines.
    acceptance:
      - "grep -c 'kh rebuild' docs/runbook.md prints a number of at least 1"
      - "grep -n 'restricted_audiences_imply_all' docs/configuration.md matches"
      - ".venv/bin/python -m pytest -q passes"
```
