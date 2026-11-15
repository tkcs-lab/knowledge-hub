# Plan: knowledge-hub, the trusted core of the company knowledge layer

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** Agents reach the hub through PrivacyFence, which exposes it as the upstream `kh` (tools appear as `kh_search`, `kh_get_doc`, `kh_propose_change` and so on). Every hit shows its tier, domain, owner, approvals and freshness, and only what the caller's audiences allow is returned. Operators run `kh migrate`, `kh serve`, `kh rebuild`, `kh check`, `kh daily`, `kh recompute-visibility` and `kh requeue-embeddings`; Forgejo pushes to `/webhooks/forgejo` keep the T0/T1 index current.

**Scope:** A new Python service in this repo: schema and row-level security, claim verification with PrivacyFence's vendored verifier, the MCP tools, the Forgejo indexer with a trust walk, T3 ingest into S3-compatible storage, T2 derivation, promotion pull requests, conflicts, staleness, the Grist entity cache and daily job, rate limits, metrics, an arm64 image, backup script and a runbook. It does not change PrivacyFence, Forgejo or the compose repo; those are owner steps.

**Security:** Audience is enforced only by Postgres RLS under four roles with FORCE RLS and per-transaction settings, with every policy named in a matrix (3.3). Callers are verified per request, including the call hash (3.2). Errors are fixed texts. A token may cite raw records it ingested itself in `write_derived`, and the derived row still inherits the full restriction (3.7). Promotion paths are validated so the bot can never write workflow or governance files (3.8).

**Performance:** Search uses exact vector scans with no ANN index, because ANN filtering under RLS loses recall on older pgvector. bge-m3 speed on the arm64 box is measured by the owner before implementation starts.

**Review focus:** the policy matrix and definer functions (3.3, 3.7); the claim middleware and call binding (3.2); the trust walk and anchor rules (3.6); promotion path validation and routing (3.8).

## 1. Goal

Build `knowledge-hub`, the service that gives agents one MCP interface over canonical documents in git and machine-written records in Postgres, with every result carrying its trust tier and filtered by the caller's audience inside the database. Tracking issue: [tkcs-lab/knowledge-hub#1](https://github.com/tkcs-lab/knowledge-hub/issues/1). The owner's decisions, taken in conversation on 2026-11-15:

1. Callers are verified with PrivacyFence's identity claim (merged to PrivacyFence `main` on 2026-11-15), using its vendored verifier.
2. `allowed_emails` matches the claim's `email`; a token may cite raw records it ingested itself in `write_derived` (provenance exception).
3. The n8n write tools are built and tested against signed test claims of type `n8n_workflow`; live n8n writes wait for a PrivacyFence change the owner is making. No second auth path.
4. Embeddings: `bge-m3` through Ollama, 1024 dimensions, English documents.
5. T3 blobs live in S3-compatible storage from day one.
6. `get_doc` on T0/T1 serves the indexed copy from Postgres.

## 2. Current state

- This repository holds only `LICENSE`, `README.md` (one line) and a placeholder `.tld/toolkit.yaml` (`project.name: REPO`, `stacks: []`, both verify commands are `echo`). There is no code.
- PrivacyFence (`https://github.com/privacyfence/privacyfence`, commit `04d2ecac53428a1210b4b777f8c466359ee730bf`, read 2026-11-15): the contract is `docs/identity-claim.md`; the single-file verifier is `contrib/verifier/pf_claim_verify.py` (sha256 starts `ab0d3ab73a8fdb2f`) with six `req` vectors in `contrib/verifier/req_test_vectors.json`. Header `X-PrivacyFence-Claim`; header `alg` `EdDSA`, `typ` `pf-identity-claim+jwt`; keys at `<issuer>/upstream-mcp/jwks`; payload `claim_version`, `iss`, `aud`, `sub`, `principal_type` (`claude_client`, `n8n_workflow`, `service`; `human` reserved), `email` (only for a verified person), `approved_by`, `token_id`, `act`, `audiences`, `tools` (information only), `req` (only on `tools/call`), `iat`, `exp`, `jti`. A new claim is minted for every HTTP request. Every request that is not a `tools/call` carries `audiences: []`. PrivacyFence opens a fresh MCP session per call (`initialize`, `notifications/initialized`, `tools/call`, session delete) and skips any upstream tool whose input schema has `$ref`, `$defs` or `definitions`, a `reason` property, is over 32 KB or is not an object schema (`src/privacyfence/upstream_mcp_client.py:147-158`). It forwards upstream error text to the AI client (`docs/upstream-mcp.md`, "Errors and size"). A person's audiences are the union of the audiences of the Workspace groups they belong to, set by the operator's `groups` map; `all` is an ordinary audience that the operator maps to a group such as `everyone@<domain>` (`docs/upstream-mcp.md`, "Groups and audiences"). `docs/identity-claim.md` says only `sub`, `principal_type`, `audiences` and `req` may change what a query returns and calls `email` audit-only; owner decision 2 uses `email` anyway, which is a deviation recorded in section 6.
- Today a scoped token (what n8n uses) can only be given `auto`-gated upstream tools, and writes must be `popup` (`docs/upstream-mcp.md`, "Known limitation"). That is the follow-up the owner is doing in PrivacyFence.
- Garage (research report r-20261115-5, 2026-11-15, [S3 compatibility page](https://garagehq.deuxfleurs.fr/documentation/reference-manual/s3-compatibility/)): stable v2.5.0 (2026-10-24) implements Put, Get, Delete, Head and ListObjectsV2 with SigV4 and path-style addressing; the client region is `garage` unless `s3_region` is changed; the official image `dxflrs/garage:v2.5.0` has an arm64 variant; object versioning is not supported, so a bucket mirror holds one version of each object and history comes only from restic; multipart, checksum headers and `aws s3 sync` behaviour were not verified.
- Compose repo (`andras-tkcs/tkcs-work-compose`, private, read 2026-11-15): Postgres 17 with pgvector on the internal `db` network, one login role per app from `roles/postgres/templates/ensure.sql.j2`; Forgejo org `kh` is planned (`docs/knowledge-hub.md`): repos `canonical-all`, `canonical-mgmt`, `canonical-hr`, `canonical-contracts`, `notes-all`, org-level webhook to `http://knowledge-hub:8080/webhooks/forgejo`, token files `kh_hub_bot_token`, `kh_indexer_token`, `kh_webhook_hmac`; restic backs up `/srv/data`; Grist document `kh-entities` with tables `Products`, `Components`, `Versions`, `Customers` and key column `key` (`prd:gateway`). Finding 6 of that document is the indexer trust model implemented in section 3.6.
- Test environment: a cloud worker has Python 3.13, `uv`, and can `apt-get install postgresql-16 postgresql-16-pgvector` (pgvector 0.6.0 on Ubuntu 24.04). It has no Docker daemon.
- The MCP Python SDK `mcp==2.3.0` (the version PrivacyFence pins) offers `MCPServer(...).streamable_http_app(stateless_http=True, json_response=True, transport_security=...)`, `StreamableHTTPSessionManager(stateless=True)`, gives a tool handler the Starlette request through `ctx.request_context.request`, turns on DNS-rebinding protection for localhost hosts only by default, and returns a tool exception's `str()` to the caller. Phase `p5-mcp-get-doc` verifies these against the installed package. Research report r-20261115-4 (2026-11-15; sources `python-sdk` `v2.3.0` `streamable_http.py` and `streamable_http_manager.py`, and the v2 migration guide on `main`) adds: the transport builds a Starlette `Request(scope, receive)` and attaches it to every message, including the JSON branch, so an outer middleware's `scope["state"]` is reachable as `request.scope`; `StreamableHTTPSessionManager` takes `app, event_store, json_response, stateless, security_settings, retry_interval, session_idle_timeout, max_request_body_size, max_sessions`; `TransportSecuritySettings` (`mcp.server.transport_security`) has `enable_dns_rebinding_protection`, `allowed_hosts` and `allowed_origins`; allowed-host entries are `host:port` patterns, and a bare host gets HTTP 421; v2 renames `FastMCP` to `MCPServer` (`mcp.server.mcpserver`) and moves `stateless_http`, `json_response` and `transport_security` to `streamable_http_app()`; it was not confirmed that `ctx.request_context.request` is a Starlette `Request` on `MCPServer`'s context in 2.3.0 (the `p5` stop condition covers it).

## 3. Design

### 3.1 Package, settings and configuration

- Distribution `knowledge-hub`, import package `knowledge_hub` under `src/`, console script `kh`, Python `>=3.12`. Every module stays under 400 lines. SQL migrations are package data.
- Settings (`knowledge_hub/settings.py`, `pydantic-settings`, prefix `KH_`; every variable below exists from `p1`): `KH_ENV` (`dev`|`test`|`prod`, default `prod`), `KH_DEV_IDENTITY` (JSON), `KH_PF_ISSUER`, `KH_PF_AUDIENCE`, `KH_PF_JWKS_URL`, `KH_ALLOWED_HOSTS` (comma list of `host:port` entries, for example `knowledge-hub:8080`), `KH_MAX_BODY_BYTES` (default `1048576`), database: `KH_DB_HOST` (default `postgres`; a directory path means a unix socket), `KH_DB_PORT` (`5432`), `KH_DB_NAME` (`kh`), `KH_DB_MIGRATOR_PASSWORD_FILE`, `KH_DB_APP_PASSWORD_FILE`, `KH_DB_INDEXER_PASSWORD_FILE` (optional; none means no password), with a method `Settings.dsn(role)` for `migrator`, `app` and `indexer`; `KH_CONFIG_FILE`, `KH_FORGEJO_URL`, `KH_FORGEJO_ORG` (default `kh`), `KH_FORGEJO_BOT_TOKEN_FILE`, `KH_FORGEJO_INDEXER_TOKEN_FILE`, `KH_WEBHOOK_HMAC_FILE`, `KH_EMBED_PROVIDER` (`ollama`|`http`, plus `fake` accepted only when `KH_ENV` is `dev` or `test`), `KH_OLLAMA_URL`, `KH_EMBED_MODEL` (default `bge-m3`), `KH_EMBED_DIM` (default `1024`), `KH_EMBED_HTTP_URL`, `KH_EMBED_HTTP_TOKEN_FILE`, `KH_EMBED_HTTP_ALLOWED_AUDIENCES` (default `all`), `KH_S3_ENDPOINT`, `KH_S3_BUCKET`, `KH_S3_ACCESS_KEY_FILE`, `KH_S3_SECRET_KEY_FILE`, `KH_S3_REGION` (default `garage`), `KH_CURSOR_KEY_FILE`, `KH_CHUNKER_VERSION` (`1`), `KH_PROMOTION_DAILY_LIMIT` (`5`), `KH_RRF_K` (60), `KH_SEARCH_POOL` (50), `KH_TIER_WEIGHTS` (`T0:1.0,T1:0.85,T2:0.6,T3:0.4`), `KH_STALE_WEIGHT` (0.5), `KH_SUPERSEDED_WEIGHT` (0.3), `KH_DISPUTED_WEIGHT` (0.5), `KH_SEARCH_MAX_OFFSET` (100), `KH_MAX_RAW_BYTES` (1048576), `KH_RATE_LIMIT_PER_MIN` (120), `KH_METRICS_TOKEN_FILE` (none means no `/metrics` route), `KH_GRIST_URL`, `KH_GRIST_DOC_ID`, `KH_GRIST_KEY_FILE`. Secrets are read from files, never from variables.
- Dev override: `KH_DEV_IDENTITY` is a JSON object `{"sub", "principal_type", "audiences", "email"?}` (`service` and `human` are refused). The settings validator raises `KH_DEV_IDENTITY is set while KH_ENV=prod; refusing to start`; `kh serve` exits 2 on any settings error and `ClaimMiddleware.__init__` re-checks. With the override active a request with no claim header uses the dev caller and logs a warning; a request that carries a header is still verified. Tests never use the override for claims.
- Config file (`kh.yaml`, model in `knowledge_hub/config.py`): `audiences` (names allowed in `required_audiences`; default `[all, mgmt, hr, contracts]`), `participant_label: attendees` (the name used in outputs for email-scoped T3 rows; `attendees` is not an audience), `domains: [portfolio, product, technical]`, `repos` (map of Forgejo repo name to `{tier: T0|T1, audience: <name>, trust_anchor: <sha>|null}`; default `canonical-all`/`all`, `canonical-mgmt`/`mgmt`, `canonical-hr`/`hr`, `canonical-contracts`/`contracts` as T0 and `notes-all`/`all` as T1), `raw_audience_map` (`series:<id>` or `folder:<id>` to a list of audiences), `ingest_principal_types` (default `[n8n_workflow]`; allowed values only `n8n_workflow`, because the RLS insert check requires it and PrivacyFence never mints `service` for a tool call), `restricted_audiences_imply_all` (default `false`), `entities` (per kind `prd|cmp|ver|cus` the `required_audiences` of its cache rows; default `cus: [contracts]`, others `[all]`), `forgejo_web_url`. Validation rejects an unknown audience or domain anywhere, a repo audience not in `audiences`, and more than one T0 repo for the same audience.

### 3.2 Caller verification

- `knowledge_hub/auth/pf_claim_verify.py` is vendored byte for byte from PrivacyFence commit `04d2ecac53428a1210b4b777f8c466359ee730bf`; `auth/VENDORED.md` records the source URL, the commit and the file's sha256. The six vectors file is copied to `tests/data/pf_req_test_vectors.json`.
- `ClaimMiddleware` (pure ASGI, `auth/claims.py`) wraps only the `/mcp` route. For `POST`: read the body, refuse past `KH_MAX_BODY_BYTES` with 413; a missing `X-PrivacyFence-Claim` header is 401 with an empty body (unless the dev override applies); call `ClaimVerifier.verify(token, body=body)`; a `ClaimError` is 401 with an empty body and only `exc.code` logged. `GET` and `DELETE` are verified the same way with an empty body and then answered 405 (the server is stateless and has no sessions). Always refuse `principal_type` `human`, and refuse `service` on a `tools/call`. Build `Caller(sub, principal_type, email, audiences, token_id)` with `email` lower-cased and kept only when `principal_type` is `claude_client` (otherwise `None`), and audiences checked against `^[a-z0-9][a-z0-9_-]{0,62}$` (else 401). Store `kh_caller` and `kh_call` in `scope["state"]`, where `kh_call` is the tool name and the `arguments` object exactly as parsed from the request body (or `None` for a request that is not a `tools/call`), and replay the buffered body to the SDK.
- Replay cache: `kh.claim_seen(jti text, expires_at timestamptz) returns boolean` (a `SECURITY DEFINER` function owned by `kh_migrator`, `EXECUTE` for `kh_app` only) inserts `ON CONFLICT DO NOTHING` and returns true when the id is new; `auth/replay.py` `PgReplayCache.add_if_new` calls it. A purge of expired rows runs in the daily job.
- `tool_guard` (`mcp/errors.py`) wraps every tool function. It refuses a call whose `kh_caller` is missing or whose `kh_call` is `None`; refuses when the registered tool name differs from `kh_call`'s name; refuses when the keyword arguments the SDK passed are not exactly the stored `arguments` overlaid on the tool's declared defaults (extra keys refused; equal after int/float coercion), so the dispatched call is the call that was hashed; opens `caller_tx`; maps `KhError` to its fixed text; maps every other exception to `internal error (ref <uuid4>)` and logs the detail server-side only.
- HTTP app (`app.py`): root Starlette with routes `/mcp` (the middleware around the SDK's streamable handler), `/webhooks/forgejo`, `/healthz` (answers `ok` only) and, only when `KH_METRICS_TOKEN_FILE` is set, `/metrics` (needs `Authorization: Bearer <token>`). `StreamableHTTPSessionManager(stateless=True, json_response=True)` with `TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=KH_ALLOWED_HOSTS, allowed_origins=[])`. The root lifespan enters `mcp.session_manager.run()` (a mounted sub-app's lifespan would not run). If the SDK does not pass `scope["state"]` to the tool in stateless mode, fall back to a `contextvars.ContextVar` set by the middleware.
- Tool schemas are flat: `str`, bounded `int`, `bool`, `list[str]`, `Literal`. No pydantic model arguments and no argument named `reason`. A unit test applies PrivacyFence's skip rules to every registered tool schema.

### 3.3 Database roles, row-level security and the policy matrix

- Roles: `kh_migrator` (LOGIN, NOBYPASSRLS, owns schema `kh`, every table and the `kh_migrator`-owned functions; used for migrations and backups); `kh_app` (LOGIN, NOBYPASSRLS, the request path); `kh_indexer` (LOGIN, NOBYPASSRLS, webhook worker, rebuild, daily job, recompute, requeue); `kh_provenance` (NOLOGIN, owns the definer functions of 3.7, owns no table). No superuser and no BYPASSRLS. Roles are created outside the migrations (tests: `tests/support/pg.py` as the sandbox superuser; production: the Ansible template, a manual step); migrations only `GRANT`. Migration `0001` creates schema `kh`, grants `USAGE` on schema `public` to the four roles (the `vector` type lives there), revokes `CREATE` on `public` from `PUBLIC`, and revokes everything on schema `kh` from `PUBLIC`. It also runs `GRANT USAGE ON SCHEMA kh TO kh_app, kh_indexer, kh_provenance` and `GRANT CREATE ON SCHEMA kh TO kh_provenance` (needed only to own the definer functions). Role setup (tests: `bootstrap_roles`; production: ma1) also runs `GRANT kh_provenance TO kh_migrator` so migrations can transfer function ownership.
- Per request `db/caller_tx.py` opens a transaction on the `kh_app` pool and runs `SELECT set_config('kh.audiences', $1, true), set_config('kh.email', $2, true), set_config('kh.principal_type', $3, true), set_config('kh.sub', $4, true)` (audiences comma-joined after the regex check, bound as parameters) and `SET LOCAL statement_timeout = '5s'`. Nothing else uses the `kh_app` pool, except `PgReplayCache` calling `kh.claim_seen`.
- Helper functions in schema `kh` (`LANGUAGE sql STABLE`): `caller_audiences()` returns `coalesce(string_to_array(nullif(current_setting('kh.audiences', true), ''), ','), '{}')`; `caller_email()` returns `nullif(current_setting('kh.email', true), '')`; `caller_sub()`; `caller_type()`; and `visible(req text[], emails text[])` returns `req <@ caller_audiences() AND (emails IS NULL OR caller_email() = ANY(emails))`. Every visibility-bearing table has `required_audiences text[] NOT NULL`, `allowed_emails text[] NULL` (stored lower-case) and `CHECK (cardinality(required_audiences) > 0 OR allowed_emails IS NOT NULL)` named `kh_no_open_rows`. `allowed_emails = '{}'` means nobody. `visible` deliberately does not require a non-empty audience list: PrivacyFence gives a person the audience `all` only through an operator group map, so a verified person in no group must still match rows that list their email; a caller with no audiences and no email sees nothing because of `kh_no_open_rows`.
- Every table has `ENABLE` and `FORCE ROW LEVEL SECURITY`. FORCE applies to the owner too, so `kh_migrator` has `FOR ALL USING (true) WITH CHECK (true)` on every table, which is the only reason it can run migrations and dumps.
- Policy and grant matrix (a role not listed for a table has no grant):

| Table | `kh_app` | `kh_indexer` | `kh_provenance` |
|---|---|---|---|
| `documents` | SELECT where `visible` | ALL (true) | SELECT (true), columns `id, required_audiences, allowed_emails, commit_sha` |
| `document_versions` | SELECT where the parent document is visible (EXISTS under RLS) | ALL | none |
| `chunks` | SELECT where `visible` | ALL | none |
| `entities_cache` | SELECT where `visible` | ALL | none |
| `raw_records` | SELECT where `visible`; INSERT with check `caller_type() = 'n8n_workflow' AND ingested_by_sub = caller_sub()` | SELECT (true); UPDATE (true) on the visibility columns only | SELECT (true), columns `id, required_audiences, allowed_emails, ingested_by_sub` |
| `raw_audience_map` | none | ALL | SELECT (true) |
| `derived_records` | SELECT where `visible`; INSERT with check `created_by_sub = caller_sub()` | SELECT (true); UPDATE (true) on the visibility columns only | SELECT (true), columns `id, required_audiences, allowed_emails, created_by_sub` |
| `derived_sources` | SELECT where the parent derived record is visible; INSERT with check `kh.owns_derived(derived_id)` | SELECT (true) | SELECT (true) |
| `candidates` | SELECT where `visible`; INSERT with check `created_by_sub = caller_sub()` | SELECT (true); UPDATE (true) on state, decision and visibility columns | none |
| `candidate_sources` | SELECT where the parent candidate is visible; INSERT with check `kh.owns_candidate(candidate_id)` | SELECT (true) | none |
| `conflicts` | SELECT where `visible`; INSERT with check `created_by_sub = caller_sub()` | SELECT (true); UPDATE (true) on state, closing and visibility columns | none |
| `conflict_items` | SELECT where the parent conflict is visible; INSERT with check `kh.owns_conflict(conflict_id)` | SELECT (true) | none |
| `index_jobs`, `repo_state`, `index_alerts` | none | ALL | none |
| `claim_jti` | none (reached only through `kh.claim_seen`) | SELECT and DELETE where `expires_at < now()` (policies `FOR SELECT USING (expires_at < now())` and `FOR DELETE USING (expires_at < now())`) | none |
| `schema_migrations` | none | none | none |

  `kh.owns_derived(id)`, `kh.owns_candidate(id)` and `kh.owns_conflict(id)` are `SECURITY DEFINER` functions owned by `kh_migrator` that return whether the parent row's `created_by_sub` equals `caller_sub()`; they exist because a writer may not be able to see its own parent row (the provenance case). `kh.my_raw_id(source_system text, source_ref text)` is `SECURITY DEFINER` (owner `kh_migrator`) and returns the id of the raw record with `kh.caller_type() = 'n8n_workflow'`, `ingested_by_sub = caller_sub()` and that key, or null. `kh.candidate_mark(id uuid, new_state text, branch text, pr_number int, pr_url text, error text) returns boolean` is `SECURITY DEFINER` (owner `kh_migrator`, `search_path = kh, pg_temp`, `EXECUTE` for `kh_app` only): it updates only a row with `created_by_sub = kh.caller_sub()` and only along `proposed` to `pr_open` or `failed` and `failed` to `pr_open` or `failed`, returns false otherwise and never returns row data. The three source tables (`derived_sources`, `candidate_sources`, `conflict_items`) carry snapshot columns `src_required_audiences` and `src_allowed_emails`, filled by a `BEFORE INSERT` trigger `sources_snapshot_visibility` (`SECURITY DEFINER`, owner `kh_provenance`, `search_path = kh, pg_temp`) from the live source row (a missing source raises SQLSTATE `P0K03`), overwriting anything supplied; `kh_indexer` may `UPDATE` those two columns (recompute). Embed jobs are queued by `AFTER INSERT` `SECURITY DEFINER` triggers (owner `kh_migrator`) on `raw_records` and `derived_records`, so `kh_app` has no grant on `index_jobs`.
- Caller-path inserts use application-generated UUIDs and never `RETURNING` or `ON CONFLICT DO UPDATE`, because the writer may not be able to see its own row.
- Chunk consistency: a `BEFORE INSERT OR UPDATE` trigger `chunks_inherit_owner` copies tier, domain, status, stale, superseded, owner key, owner created time and the visibility columns from the owning document, raw or derived row (for a raw owner tier `T3`, for a derived owner tier `T2`, status `current`, stale and superseded false, `entities` copied from the owner; anything the application supplies is overwritten); an `AFTER UPDATE` trigger `owner_propagate_to_chunks` on the three parent tables pushes changes down. `kh check` runs a drift query that must return zero rows.
- No existence leaks: `get_doc` and `get_entity` run one RLS query and answer `not found` for both missing and hidden rows through the same code path; counts are only taken under RLS; unique and dedupe keys are designed so a collision can only hit a row of identical visibility. The one accepted residual is timing proportional to hidden-row volume in exact vector scans.

### 3.4 Tables and migrations

Numbered `.sql` files in `knowledge_hub/migrations/`, one transaction each, checksum in `kh.schema_migrations`; a changed applied file refuses the run. All tables exist from `0002`, so every leak test can cover every table from the start.

- `0001_schema_grants.sql`: schema, privileges of 3.3, `schema_migrations`, `claim_jti(jti text primary key, expires_at timestamptz not null)` with the `kh_migrator` policies only (the other roles' policies are in `0003`). The runner treats a missing `kh.schema_migrations` as no files applied.
- `0002_tables.sql`: `documents` (id uuid, repo, path, doc_key, tier `T0|T1`, domain, title, body, frontmatter jsonb, status `current|superseded|disputed`, stale bool, review_by date, owner text, entities text[], supersedes text[], superseded_by uuid, commit_sha, content_sha, approved_by text[], approved_at, merge_pr int, pr_url, indexed_at, updated_at, visibility columns; unique `(repo, doc_key)` and `(repo, path)`; `doc_key` is the frontmatter `id`); `document_versions(document_id, commit_sha, path, tier, status, approved_by, approved_at, indexed_at)`; `chunks` (id, exactly one of `document_id|raw_id|derived_id` (foreign keys, all `ON DELETE CASCADE`), entities text[] NOT NULL DEFAULT '{}', ordinal, heading_path, text, content_sha, `embedding vector(1024)` nullable, embedding_model, `tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(heading_path,'') || ' ' || text)) STORED`, owner_key (`doc:<uuid>`, `raw:<uuid>` or `derived:<uuid>`), owner_created_at, tier, domain, status, stale, superseded, visibility columns); `entities_cache(id text primary key, kind, name, owner_team, status, attrs jsonb, synced_at, visibility columns)`; `raw_records(id, source_system, source_ref, source_url, series, folder, participants text[], title, blob_key, excerpt, content_type, occurred_at, ingested_by_sub, ingested_by_type, ingested_at, domain, entities text[], generated_by, visibility columns; unique (ingested_by_sub, source_system, source_ref))`; `raw_audience_map(kind, key, audiences text[], primary key (kind, key))`; `derived_records(id, kind `summary|extracted-fact|decision-candidate|changelog`, title, body, claims jsonb, domain, entities text[] NOT NULL DEFAULT '{}', generated_by jsonb, created_by_sub, created_by_type, created_at, visibility columns)`; `derived_sources(derived_id, source_kind `doc|raw|derived`, source_id, version text null, src_required_audiences text[] not null, src_allowed_emails text[] null)`; `candidates(id, derived_id null, target_doc_id null, state `proposed|pr_open|promoted|rejected|failed|expired`, domain, target_repo, target_path, title, body_sha, dedupe_key, branch, pr_number, pr_url, created_by_sub, created_at, decided_at, error, visibility columns)` with a unique partial index on `dedupe_key` where state is `proposed` or `pr_open`; `candidate_sources(candidate_id, source_kind, source_id, src_required_audiences text[] not null, src_allowed_emails text[] null)`; `conflicts(id, state `open|closed`, domain, note, created_by_sub, created_at, closed_at, closed_by_pr, visibility columns)`; `conflict_items(conflict_id, item_kind, item_id, src_required_audiences text[] not null, src_allowed_emails text[] null)`; `index_jobs(id, kind `git_push|embed_raw|embed_derived|pr_event`, repo, payload jsonb, state `queued|running|done|failed`, attempts, last_error, created_at, run_after)`; `repo_state(repo primary key, trust_anchor_sha, last_indexed_sha, trusted bool, untrusted_reason, config_hash)`; `index_alerts(id, repo, path, message, created_at)`. `chunks.tier` has a check `T0|T1|T2|T3`, and `document_versions.document_id` is also `ON DELETE CASCADE`. Indexes: `chunks (owner_key)`, `chunks USING gin (tsv)`, `documents (repo, doc_key)`.
- `0003_rls.sql`: helper and definer functions of 3.3 (`visible`, `caller_*`, `owns_*`, `my_raw_id`, `claim_seen`, `candidate_mark`), `ENABLE/FORCE ROW LEVEL SECURITY`, every policy and grant of the matrix.
- `0004_triggers.sql`: `chunks_inherit_owner`, `owner_propagate_to_chunks` (documents, raw, derived), the `embed_raw` and `embed_derived` enqueue triggers, `raw_records_assign_visibility` (3.7), and `sources_snapshot_visibility` (3.3).
- `0005_derived_visibility.sql` (phase `p16a`): `kh.derived_visibility` and the deferred constraint trigger `derived_visibility_matches` (3.7).
- `0006_strict_visibility.sql` (phase `p19a`): `kh.strict_visibility` and the deferred constraint triggers `candidate_visibility_matches` and `conflict_visibility_matches` (3.8).

### 3.5 Search

One statement inside `caller_tx`, exact vector order (no ANN index), RRF over a vector list and a full-text list, each limited to `KH_SEARCH_POOL` candidates and filtered by domain, tier and entities (`entities` is any-of, `&&`); chunks without an embedding appear only in the full-text list; `score = rrf * tier_weight * (stale ? KH_STALE_WEIGHT : 1) * (superseded ? KH_SUPERSEDED_WEIGHT : 1) * (disputed ? KH_DISPUTED_WEIGHT : 1)`; one best chunk per owner (`DISTINCT ON (owner_key)`); order by `score DESC, id`. Vectors are only compared with chunks whose `embedding_model` equals the configured model. `min_tier` includes that tier and every higher one (`T0` is highest); `lower_tier_hits` counts matching owner records in the tiers below `min_tier` (per tier `T2`, `T3`, and `newest_at`), computed under RLS over the same fused pool. Hits carry `text`, `tier`, `domain`, `owner`, `approved_by`, `approved_at`, `review_by`, `status`, `stale`, `entities`, `source_link` and `citation_id` (`doc:<uuid>`, `raw:<uuid>`, `derived:<uuid>`). `source_link` is `<forgejo_web_url>/<org>/<repo>/src/commit/<commit_sha>/<path>` for documents, the stored `source_url` for raw records, and empty for derived records. A `disputed` hit never counts as a normative T0/T1 answer: when no non-disputed T0/T1 hit is visible for a query asked with `min_tier` `T0` or `T1` and `lower_tier_hits` is non-zero, the call fails with the `NORMATIVE` text of 3.9. Cursor: opaque `base64url(HMAC-SHA256(KH_CURSOR_KEY, sub|audience-hash|query-hash|offset) || offset)`, offset at most `KH_SEARCH_MAX_OFFSET`, re-executes the query under RLS; a foreign cursor fails with `CURSOR_FOREIGN`. List tools use keyset cursors: `list_stale` `(review_by, id)`, `list_conflicts` `(created_at, id)`, `list_entities` `(id)`.

### 3.6 Indexer and trust model

- Webhook: verify `X-Forgejo-Signature` (hex HMAC-SHA256 of the raw body, `hmac.compare_digest`, key from `KH_WEBHOOK_HMAC_FILE`); a bad signature is 401 with an empty body. The payload is only a trigger: it inserts a `git_push` or `pr_event` job and answers 202; everything is re-fetched from Forgejo with the indexer token. A `git_push` job syncs the head of `main` as fetched from Forgejo (not the payload sha) and refuses to move to a head that does not descend from `repo_state.last_indexed_sha` (alert `main of <repo> was rewritten; run kh rebuild after review`, index unchanged). A worker in the app process runs jobs, one repo at a time under `pg_advisory_xact_lock(hashtext(repo))`.
- Frontmatter: required `id` (becomes `doc_key`), `title`, `domain`, `review_by`; optional `owner`, `type`, `entities`, `supersedes`, `superseded_by`, `status` (only `current` or `superseded` accepted; `disputed` can never be set by a file). Any of `tier`, `audience`, `audiences`, `required_audiences`, `allowed_emails`, `visibility`, `approved_by`, `approved_at` rejects the file: it is not indexed, an earlier row of it is removed, and an alert reads `frontmatter must not set <key>; the server decides tier and audience`. A duplicate `id` in one repo rejects every file with that id that was not last changed by an approved merge; if more than one was, all are rejected and alerted with `duplicate id <id> in <repo>: <path>, <path>`.
- Trust (canonical repos only), pure function in `indexer/trust.py` over commits and PR/review records: walk the first-parent chain of `main` from the configured `trust_anchor` to HEAD. The anchor is read only from `kh.yaml` (`repo_state.trust_anchor_sha` is a display copy). `trust_anchor: null` means no anchor: every first-parent commit including the root must pass the rule below, so an operator must set an anchor in practice. When the anchor is not on the first-parent chain of `main`, the repo is untrusted with the alert `trust anchor <sha> is not on main of <repo>`. Every commit after the anchor must be a merge commit whose pull request was merged into `main` with `merge_commit_sha` equal to the commit, and whose reviews hold at least one non-dismissed official `APPROVED` review whose `commit_id` equals the PR's final head sha, by someone who is neither the PR author nor `kh-hub-bot`. A non-merge first-parent commit after the anchor sets `repo_state.trusted = false` with an alert `non-merge commit <sha> on main of <repo>`, and every document of that repo is indexed `disputed` until an operator reviews and moves the anchor in config. A file's verdict is the verdict of the last first-parent commit that changed it (changed files come from the compare endpoint, one call per commit); a file last changed at or before the anchor is trusted with `approved_by = ['trust-anchor']` and `approved_at` the anchor commit time. A file changed by a merge without an approved PR is `disputed` with the alert `file <path> on <repo>/main has no approved merge PR`. `approved_by` and `approved_at` otherwise come from the counted reviews (`submitted_at` of the latest). `notes-*` (T1) repos need no approval but must be listed in config. Forgejo API facts relied on (a fixture-shaped assumption in `p9a`, flagged for research): `GET /repos/{o}/{r}/commits/{sha}/pull`, `GET /repos/{o}/{r}/pulls/{n}/reviews` with `official`, `dismissed`, `state`, `commit_id`, `submitted_at`, `GET /repos/{o}/{r}/compare/{base}...{head}` (changed files), `GET /repos/{o}/{r}/git/trees/{sha}`, `GET /repos/{o}/{r}/raw/{path}?ref=<sha>`. If the commit-to-PR lookup is missing, list closed PRs and match `merge_commit_sha`.
- Documents: upsert by `(repo, doc_key)`; rename and delete handled by diffing the tree at the head against the index; `supersedes` and `superseded_by` resolve only within the same repo (same audience): a reference to an id not in the same repo makes the referring file `disputed` with the alert `supersedes/superseded_by must name a document in the same repo`, and only a document whose own status is `current` can mark another document superseded; each indexed commit adds a `document_versions` row. Chunks split on markdown headings (`heading_path` is the heading chain joined with ` > `), pieces over 1200 characters are split at paragraph boundaries into pieces of at most 1200 characters with 150 characters of overlap, pieces under 200 characters merge into the previous piece of the same section; `KH_CHUNKER_VERSION` is part of the chunk hash. Embeddings are computed before the write transaction (a chunk whose visibility the provider may not embed gets no embedding); each repo is written in one transaction. A chunk is rewritten only when `content_sha`, chunker version or embedding model changes.
- `kh rebuild [--repo R]` re-walks trust from the configured anchor, reads the tree at HEAD, upserts, deletes documents absent from the tree; running it twice changes nothing (same rows, same `indexed_at`). `kh check` compares HEAD and content shas with the index, runs the chunk drift query, compares the `config_hash` of the `repo_state` row `repo = '__config__'` (hash of the audience map, repo audiences and entity visibility) with the configuration (a missing row counts as drift; only `recompute_visibility` and the startup load write it, and the startup load treats a missing row as changed), and exits 1 on any drift.
- `get_doc` serves the indexed row and returns `commit_sha` and `indexed_at`. `version=<sha>` is accepted only when `document_versions` holds that sha for the document (checked through RLS); then the file is fetched from Forgejo; otherwise `VERSION_NOT_INDEXED`.

### 3.7 T3 and T2 visibility, and the provenance exception

- `ingest_raw` accepts only principal types in `config.ingest_principal_types`. The call stores the body in S3 under `raw/<uuid>` (uploaded inside the database transaction before commit, so a failed upload leaves no row), sets `blob_key`, stores a 2000-character excerpt, writes `ingested_by_sub` and `ingested_by_type` from the claim, and an `AFTER INSERT` trigger queues an `embed_raw` job; the worker chunks and embeds it. Visibility is assigned by the `BEFORE INSERT` trigger `raw_records_assign_visibility` (`SECURITY DEFINER`, owned by `kh_provenance`, `search_path = kh, pg_temp`, `SELECT` on `raw_audience_map`): a mapped `series:<id>` or `folder:<id>` gives `required_audiences = <mapped audiences>` (the union when both match) and `allowed_emails = NULL`, and raises SQLSTATE `P0K02` (`INGEST_AUDIENCE`) unless the mapped audiences are all held by the calling token; no match gives `required_audiences = '{}'` and `allowed_emails = lower(participants)`; no match and no participants raises SQLSTATE `P0K01` (`INGEST_NO_MAP`). The application-supplied visibility columns are always overwritten. The tool maps `P0K01` and `P0K02` to their fixed texts by SQLSTATE. The insert runs in a savepoint; a unique violation on `(ingested_by_sub, source_system, source_ref)` returns `ALREADY_INGESTED` with the id from `kh.my_raw_id(...)`, changes nothing, and writes no blob.
- `kh.derived_visibility(source_ids text[])` is `SECURITY DEFINER`, owned by `kh_provenance`, `search_path = kh, pg_temp`. It reads the columns granted in 3.3 through the `kh_provenance` policies (which are `USING (true)`); the function body alone applies visibility: a source counts when `kh.visible(...)` holds, or, for `raw_records` only, when `kh.caller_type() = 'n8n_workflow' AND ingested_by_sub = kh.caller_sub()` (the provenance exception). It returns `(n_resolved int, required_audiences text[], allowed_emails text[], versions text[])`: the union of audiences; the intersection of the non-null email lists (`NULL` when every source has none; `'{}'` when the intersection is empty); and `versions`, where `versions[i]` belongs to `source_ids[i]` (`commit_sha` for a resolved document, null for anything else). It returns no content and its output is never returned to the caller. Reads stay strict: the exception exists only inside this function.
- `write_derived` checks, in this order: ids are well formed (`doc:`, `raw:`, `derived:` plus uuid), 1 to 50, de-duplicated; `@<version>` is allowed only on `doc:` ids (else `BAD_VERSION`); `n_resolved = number of ids`, else `DERIVED_SOURCES` (missing and hidden are indistinguishable); only then each supplied `@<version>` is compared with the returned version (mismatch `VERSION_CHANGED`); insert `derived_records` with the returned visibility and no `RETURNING`, then `derived_sources` with the returned versions; `claims_json` must be a JSON list of objects with `text`, `source_ids` (a subset of the cited ids) and one of `quote` or `timestamp` (else `BAD_CLAIMS`). A deferred constraint trigger `derived_visibility_matches` (`AFTER INSERT` on `derived_records` only, `SECURITY DEFINER`, owner `kh_provenance`) recomputes at commit the union and intersection over the snapshot columns of the row's `derived_sources` (independent of the caller) and raises when there are no sources or the stored visibility differs; recompute by the indexer is an `UPDATE` and does not fire it. The exception covers raw records only; a derived record that the same token wrote but cannot see cannot be cited (chained derivation is not covered).
- `kh recompute-visibility` (indexer role) recomputes, in this order and in one transaction: raw rows from the audience map; document visibility from `config.repos`; derived rows through `derived_sources`; candidates and conflicts through their source tables; chunks follow through `owner_propagate_to_chunks`. Each source contributes its live visibility when its row exists and otherwise its snapshot columns; recompute refreshes the snapshot columns from live rows, so deleting a source never widens a row. It records `repo_state.config_hash` (pseudo repo `__config__`). The worker's startup load of `raw_audience_map` runs it in the same transaction when the hash changed, so no row is served under an old map. `kh requeue-embeddings` queues `embed_raw` and `embed_derived` jobs for every raw and derived record without chunks (used after a restore).

### 3.8 Promotion

- Candidate and conflict visibility is `kh.strict_visibility(source_ids text[])` (`SECURITY INVOKER`): the union of the sources' audiences and the intersection of their non-null email lists, over sources visible by strict RLS only (no provenance branch); it also returns `n_resolved`. The full union `U` is stored (before any `restricted_audiences_imply_all` reduction). The deferred constraint triggers `candidate_visibility_matches` and `conflict_visibility_matches` (`AFTER INSERT`, `SECURITY DEFINER`, owner `kh_provenance`) recompute at commit over the snapshot columns of the row's source rows and raise when there are no sources or the stored visibility differs. Identical sources therefore give identical visibility, which keeps the dedupe collision safe.
- `route_target_repo(sources, config, explicit_repo=None)` (pure, in `visibility.py`). Any source with non-null `allowed_emails` refuses: `EMAIL_SOURCES`. Let `U` be the union of the sources' audiences; with `restricted_audiences_imply_all: true` remove `all` from `U` when `U` has other members. Automatic routing (`new_path`): `U` empty means the T0 repo whose audience is `all`; `U = {a}` means the T0 repo whose audience is `a` (`NO_REPO` when none is configured); `|U| >= 2` is refused with `SPAN`. With `target_id` the repo is that document's repo and is accepted only when `U` is a subset of `{audience of that repo}` (`WIDER` otherwise). Widening is a human `widen-audience` PR, never a bot's.
- `propose_change(target_id, new_path, domain, title, content, rationale, source_ids, candidate_id)` (flat arguments; exactly one of `target_id` and `new_path`; `domain` must be in config; the daily limit and the label use that `domain`): 1 to 50 `source_ids`, resolved by strict RLS through the ordinary read path; the target path comes from the indexed document row for `target_id` (it must also match the path regex below), and for `new_path` must match `^(portfolio|product|technical)/[a-z0-9][a-z0-9/_-]*\.md$`, start with the folder of the given `domain`, contain no `..` or `//`, and not already exist at `main` (else `BAD_PATH`), so the bot can never write `.forgejo/`, `CODEOWNERS`, `schema/` or governance files. `candidate_id` is only for retry: it must name a candidate with state `failed` created by the same `sub`, and the call re-uses its branch name. `candidates.derived_id` is the single `derived:` source when exactly one is cited, else null. The dedupe key is `sha256(target_repo, target_path, sorted source ids, sha256(normalised content))` where normalised content has CRLF turned into LF, trailing whitespace removed from each line and leading and trailing blank lines removed; a collision returns `DUPLICATE`. The daily limit per `(domain, target_repo)` is counted under RLS since 00:00 UTC (`LIMIT`). Then the candidate is inserted (`proposed`), and through `kh-hub-bot` the client creates branch `bot/promote/<candidate-id>`, commits the file, opens the PR with the template body (headings `## What`, `## Why`, `## Sources` as a list of citation ids, `## Candidate` with the id) and labels `promotion` and `domain/<domain>`, and calls `kh.candidate_mark` to set `pr_open`. A Forgejo failure calls `kh.candidate_mark` with `failed` (the error text stays server-side) and answers `PR_FAILED`; the caller retries with `candidate_id`. `ForgejoClient` raises if any write targets a ref outside `bot/promote/*`, and `open_pull` requires `base` `main` and a head starting with `bot/promote/`; it never pushes to `main`.
- PR events: a `pull_request` webhook only triggers a `pr_event` job; the candidate id is the uuid after `bot/promote/` in the PR head ref; the worker re-fetches the PR from Forgejo and requires its repo and number to equal the candidate's `target_repo` and `pr_number` (mismatch alert `pull request <repo>#<n> does not match candidate <id>`). Closed and merged: `promoted` (`decided_at` set) and the open conflicts that cite the candidate's sources close (`closed_by_pr`); closed unmerged: `rejected`. Forgejo cannot filter PR events by action, so the hub does.

### 3.9 Tool catalogue and the fixed-text catalogue

Tools (all arguments flat): `search(query, domains: list[str]=[], min_tier="T1", entities: list[str]=[], limit=10, cursor="")`; `get_doc(id, version="")` (`id` is `doc:<uuid>`, `raw:<uuid>` or `derived:<uuid>`; raw returns the full text from the blob store, derived its body); `get_entity(id)`; `list_entities(type, status="", owner_team="", limit=50, cursor="")` (`type` is one of `prd`, `cmp`, `ver`, `cus`); `ingest_raw(source, source_ref, title, body, domain, occurred_at, series_id="", folder_id="", participants: list[str]=[], entities: list[str]=[], source_url="", content_type="text/plain", generated_by="")` (`occurred_at` ISO 8601 with offset); `write_derived(kind, title, body, domain, claims_json, derived_from: list[str], pipeline, model, pipeline_version)`; `propose_change(...)` as in 3.8; `flag_conflict(a, b, note)`; `list_conflicts(domain="", status="", limit=50, cursor="")`; `list_stale(domain="", owner="", limit=50, cursor="")`.

`mcp/errors.py` holds the whole catalogue from `p5` (later phases import, never add, except `RATE_LIMIT` in `p23`). Constant → text (`{x}` are format fields): `NOT_FOUND` → `not found`; `INTERNAL` → `internal error (ref {ref})`; `CURSOR_FOREIGN` → `cursor does not belong to this query`; `NORMATIVE` → `cannot answer normatively from T2/T3 only: {n} lower-tier hits; call search with min_tier="T3" to see them`; `VERSION_NOT_INDEXED` → `version {sha} is not an indexed version of this document`; `INGEST_FORBIDDEN` → `ingest_raw is limited to workflow principals`; `INGEST_TOO_LARGE` → `body too large (limit {n} bytes)`; `INGEST_NO_MAP` → `cannot ingest: no audience mapping for series or folder and no participants`; `INGEST_AUDIENCE` → `cannot ingest: your token does not hold the audience this series or folder maps to`; `ALREADY_INGESTED` → `already ingested: {id}`; `BAD_ID` → `malformed id {x}`; `BAD_KIND` → `kind must be one of summary, extracted-fact, decision-candidate, changelog`; `BAD_CLAIMS` → `claims_json must be a JSON list of objects with text, source_ids and a quote or timestamp`; `BAD_VERSION` → `@version is only allowed on doc: ids`; `TOO_MANY_SOURCES` → `at most 50 sources`; `VERSION_CHANGED` → `source {id} changed since you read it (now {version})`; `DERIVED_SOURCES` → `cannot write derived record: {k} cited sources are not found or not visible to you`; `EMAIL_SOURCES` → `cannot propose: a cited source is restricted to named people; no canonical repo is that narrow`; `SPAN` → `cannot propose: sources span audiences {a}, {b}; no canonical repo is restricted to all of them`; `NO_REPO` → `cannot propose: no canonical repository is configured for audience {a}`; `WIDER` → `cannot propose to {repo}: its audience {a} is wider than or different from the sources' audience {u}`; `BAD_PATH` → `new_path must be a new markdown file under portfolio/, product/ or technical/ in the given domain (lowercase letters, digits, - _ /)`; `DUPLICATE` → `an identical proposal is already open: {id} (PR #{n})`; `LIMIT` → `daily promotion limit for domain {d} in {repo} reached ({n}); try again after {time} UTC`; `BAD_CANDIDATE` → `candidate_id must name one of your own failed proposals`; `PR_FAILED` → `could not open the pull request; call propose_change again with candidate_id={id} to retry`; `RATE_LIMIT` → `rate limit exceeded; retry in {n} seconds`.

### 3.10 Embeddings and blobs

`embed/base.py` defines `EmbeddingProvider` (`model`, `dim`, `async embed(texts) -> list[list[float]]`, `may_embed(visibility) -> bool`) and a deterministic `FakeEmbedder` for tests. `embed/ollama.py` calls `POST <KH_OLLAMA_URL>/api/embed` (`may_embed` always true). `embed/http.py` calls a configured HTTP endpoint that serves the same model (`KH_EMBED_MODEL` must equal the endpoint's model, so vectors are comparable) and embeds only chunks whose `required_audiences` is a non-empty subset of `KH_EMBED_HTTP_ALLOWED_AUDIENCES` with null `allowed_emails`; other chunks get no embedding and are found by full text only. The provider is chosen once for the deployment (`KH_EMBED_PROVIDER`) and used for documents and queries. Both refuse a dimension other than `KH_EMBED_DIM`. `blobs/s3.py` `BlobStore.put/get/delete` over `boto3` with a configurable endpoint, path-style addressing and credentials from files; a test double implements the same protocol.

### 3.11 Rejected alternatives

SQLAlchemy and Alembic (raw policies, triggers and the fusion query dominate; `psycopg` 3 with a small runner is lighter); an HNSW index from day one (older pgvector filters after the index scan, so narrow audiences lose recall); filtering in Python (forbidden by the ask); indexing synchronously in the webhook (embedding is slow and Forgejo times out); the SDK's `auth=` hook (the claim is in its own header and needs the raw body); a filesystem blob store and a second auth path (the owner declined both); a policy that requires a non-empty audience list (it would hide email-only rows from people in no group).

### 3.12 Entities and the daily job

`jobs/daily.py` runs `kh daily`: sets `documents.stale` where `review_by < today` (chunks follow by trigger), upserts `entities_cache` from the four Grist tables (kind from the key prefix; visibility from `config.entities`, so `Customers` rows are `{contracts}` by default), deletes cache rows no longer in Grist, and deletes expired `claim_jti` rows. `get_entity(id)` returns the cache row plus the visible documents that list the entity.

## 5. Manual steps

Manual steps happen once before implementation and after the last merge; every phase runs on a cloud worker with a throwaway Postgres, signed test claims, a fake Forgejo and a fake S3.

### Before implementation

**mb1-embedding-benchmark (about 30 minutes).** `vector(1024)` is fixed in the first migration, so measure bge-m3 on the target box first.
1. On the Hetzner CAX31 install Ollama and run `ollama pull bge-m3`.
2. Run this loop: `time (for i in $(seq 1 50); do curl -s localhost:11434/api/embed -d '{"model":"bge-m3","input":["'"$(head -c 800 /dev/urandom | base64 -w0 | head -c 800)"'"]}' >/dev/null; done)`.
3. Divide 50 by the elapsed seconds (chunks per second) and note the time of one call (query latency).
Done when: at least 20 chunks per second and under 300 ms per single call. If not, tell the planner before implementation starts; the fallback is a 768-dimension model and a different `vector(N)`.

### After implementation

**ma1-roles-and-secrets (about 30 minutes).** See the compose repo's [docs/knowledge-hub.md](https://github.com/andras-tkcs/tkcs-work-compose/blob/main/docs/knowledge-hub.md).
1. In the compose repo's Postgres role template add roles `kh_migrator`, `kh_app`, `kh_indexer` (LOGIN, NOSUPERUSER, NOBYPASSRLS) and `kh_provenance` (NOLOGIN), a database `kh` owned by `kh_migrator`, `CREATE EXTENSION vector` in it, and `GRANT kh_provenance TO kh_migrator`.
2. Generate passwords for the three login roles and store them under `/etc/stack-secrets` (never in chat) as `pg_kh_migrator`, `pg_kh_app`, `pg_kh_indexer`; also `kh_webhook_hmac`, `kh_hub_bot_token`, `kh_indexer_token`, `kh_cursor_key`, `kh_metrics_token`, and the Grist read-only key as `kh_grist_key`.
3. Run `ansible-playbook site.yml --tags postgres`.
Report back: `kh migrate` runs against the `kh` database without error (pass/fail).

**ma2-garage-bucket (about 30 minutes).** Add a single-node S3-compatible store (Garage is the suggested one: [Garage quick start](https://garagehq.deuxfleurs.fr/documentation/quick-start/), not verified from the planning environment) on the `kh` network, create bucket `kh-raw` and an access key limited to it, store the key id and secret as `kh_s3_access_key` and `kh_s3_secret_key` under `/etc/stack-secrets`, and keep its data under `/srv/data/garage` so restic backs it up. The official image is `dxflrs/garage:v2.5.0` (it has an arm64 variant); Garage has no object versioning, so deleted or overwritten blobs are recoverable only from restic snapshots. Also run `aws s3 sync` of the bucket into an empty directory and back into a second bucket and compare the file lists (Garage's `sync` behaviour is unverified). Report back: put, get, delete and the sync round trip of a test object work from the hub container (pass/fail).

**ma3-forgejo-settings (about 30 minutes).** In the Forgejo org `kh`: set every `canonical-*` repo to merge-commit-only (squash, rebase and fast-forward off); protect `main` as the compose document says and enable dismiss stale approvals; create the org webhook to `http://knowledge-hub:8080/webhooks/forgejo` with secret `kh_webhook_hmac` and events `push` and `pull_request`; create users `kh-hub-bot` and `kh-indexer` with their tokens. Record the first trusted commit of each canonical repo as `trust_anchor` in `kh.yaml`. Report back: a test merge produces an indexed T0 document (pass/fail).

**ma4-privacyfence-upstream (about 20 minutes).** Declare the hub as upstream id `kh` in PrivacyFence's operator file ([upstream-mcp.md](https://github.com/privacyfence/privacyfence/blob/main/docs/upstream-mcp.md)) with `audience` equal to `KH_PF_AUDIENCE`, `url` `http://knowledge-hub:8080/mcp`, read tools `auto` and write tools `popup`; map groups to audiences (for example `everyone@<domain>` to `all`, `mgmt@<domain>` to `mgmt`); set `KH_PF_ISSUER`, `KH_PF_AUDIENCE`, `KH_PF_JWKS_URL` and `KH_ALLOWED_HOSTS=knowledge-hub:8080`. Set `restricted_audiences_imply_all: true` in `kh.yaml` only if every group that maps to a restricted audience is also a member of the group mapped to `all`; otherwise leave it `false`. Report back: `kh_search` returns hits for a person and nothing for a person outside the audience (pass/fail).

**ma5-deploy-image (about 30 minutes).** On the CAX31 build the image (`docker build -t knowledge-hub .`, the sandbox has no Docker daemon, so this is the first real build), add the service and an Ollama service to the compose stack from `deploy/compose.example.yaml`, then run `kh migrate`, `kh rebuild`, `kh daily`. Also run `deploy/backup-kh.sh` once and confirm it prints `backup ok` (the pg_dump behaviour for a FORCE-RLS owner with a permissive policy is unverified). Report back: `/healthz` answers `ok`, `kh check` exits 0 and the backup ran (pass/fail). The runbook is [docs/runbook.md](https://github.com/tkcs-lab/knowledge-hub/blob/feature/knowledge-hub-service/docs/runbook.md).

**ma6-live-smoke (about 30 minutes).** From a person's client run one `kh_search` and one `kh_propose_change`; confirm the pull request appears in the right canonical repo with labels `promotion` and `domain/<d>` and that its branch is `bot/promote/<candidate-id>`. Report back pass/fail. Live n8n writes need the PrivacyFence change that lets scoped tokens call gated tools; they are not part of this plan.

## 6. Risks and open questions

- Owner decision 2 uses the claim's `email` for visibility, but PrivacyFence's contract calls `email` audit-only. Follow-up for the owner: ask PrivacyFence to bless email matching in `docs/identity-claim.md`; the runbook documents the deviation.
- An n8n token chooses `series_id`, `folder_id` and `participants`. The hub binds writes to the token's own audiences (a mapped series needs its audiences held by the token) but cannot stop a token from naming arbitrary participants on an unmapped record.
- The SDK may not pass `scope["state"]` to tools in stateless mode (the ContextVar fallback is named in 3.2); if the verified caller cannot reach the tool at all, `p5` stops with `status=blocked`. Research for the owner if needed: whether a `mcp==2.3.0` tool handler can read the raw `params.arguments`.
- `bge-m3` may be too slow on the arm64 box (mb1); the fallback changes `vector(1024)`, so it is a migration plus a full re-embed.
- Exact vector scans may be slow at the real corpus size. Research r-20261115-2 (2026-11-15, [pgvector changelog](https://github.com/pgvector/pgvector/blob/master/CHANGELOG.md), Docker Hub tags): the image `pgvector/pgvector:pg17` ships pgvector 0.8.7 (released 2026-10-01) with arm64 builds, and `hnsw.iterative_scan` exists since 0.8.0; whether iterative scans recover recall when RLS filters rows after the index scan is unconfirmed and capped by `hnsw.max_scan_tuples`, so exact scans stay until a measurement says otherwise. The cloud test cluster has pgvector 0.6.0 and production has 0.8.x, which is safe because the plan uses only exact `<=>` ordering.
- The Forgejo version's API may lack `commits/{sha}/pull`, the review `official`, `dismissed` and `commit_id` fields, or dismiss-stale-approvals on protected branches; `p9a` builds on fixtures shaped as in 3.6 and reports a `Research:` line in its final report.
- Three login roles plus one NOLOGIN role replace the one-role-per-app pattern of the compose repo; if that is refused, the fallback is a single login role that `SET ROLE`s into the others, which is weaker.
- The provenance exception covers raw records only; chained derivation is out of scope.
- `INGEST_AUDIENCE` tells a token that a series maps to an audience it lacks; that reveals the shape of the audience map, not any content, and is accepted.
- Backup and restore of FORCE-RLS tables: research r-20261115-3 (2026-11-15, [pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html), [pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html)) says `pg_dump` defaults to `row_security=off` and errors for a role that cannot bypass RLS, `--enable-row-security` avoids that, its output should be INSERT format because `COPY FROM` does not support row security, and `pg_restore --disable-triggers` needs a superuser. The plan therefore dumps with `--enable-row-security --inserts` as `kh_migrator` and restores as the Postgres superuser; the exact pg_dump behaviour for an owner with a `USING (true)` policy was not run and is checked by the owner in ma5.
- `service` principals cannot call tools through PrivacyFence, so `ingest_raw` accepts `n8n_workflow` only.
- If a phase finds that a name, schema or text in this plan contradicts the code it meets, it stops with `status=blocked` and does not improvise.

## Implementation manifest

```yaml
plan_slug: knowledge-hub-service
feature_branch: feature/knowledge-hub-service
tracking_issue: 1
max_parallel: 2
manual_before:
  - id: mb1-embedding-benchmark
    title: "Measure bge-m3 on the CAX31"
    why: "vector(1024) is fixed in migration 0002; CPU speed on the arm64 box cannot be measured in the cloud environment"
    done_when: "At least 20 chunks per second and under 300 ms per single embedding call are reported"
manual_after:
  - id: ma1-roles-and-secrets
    title: "Create the kh database roles and secret files, then run kh migrate"
    why: "The phases test with a throwaway cluster, so only the real box proves the role split and grants"
  - id: ma2-garage-bucket
    title: "Run an S3-compatible store with bucket kh-raw and a scoped key"
    why: "The blob store is a fake in every test; only a real store proves put, get and delete"
  - id: ma3-forgejo-settings
    title: "Set merge-commit-only, protect main, create the webhook and users, record the trust anchors"
    why: "The trust walk and the webhook are tested against recorded fixtures, not the live Forgejo"
  - id: ma4-privacyfence-upstream
    title: "Declare the hub in PrivacyFence and map groups to audiences"
    why: "Only the live gateway proves claims, hosts and group-to-audience mapping end to end"
  - id: ma5-deploy-image
    title: "Build the arm64 image and deploy it with Ollama"
    why: "The sandbox has no Docker daemon, so the image is never built before the box"
  - id: ma6-live-smoke
    title: "Run a live search and a live propose_change"
    why: "Verifies the whole path with real repositories and a real pull request"
verify_after_merge:
  - .venv/bin/python -m pytest -q -m unit
  - .venv/bin/python -m pytest -q -m integration
final_checks:
  - "`tld plan deviations check --file docs/plans/1/deviations.md --plan docs/plans/1/plan.md --all` exits 0"
  - "`.venv/bin/python -m pytest -q` passes with Postgres from scripts/ensure_test_pg.sh"
phases:
  - id: p1-skeleton
    title: "Project skeleton, settings, config model and the real verify profile"
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
      - tests/__init__.py
      - tests/support/__init__.py
      - tests/unit/test_settings.py
      - tests/unit/test_config.py
      - tests/integration/test_smoke.py
    brief: |
      Context: greenfield repo; design in section 3.1 (settings, dev override, config). Run `tld guard allow-profile` before editing `.tld/toolkit.yaml`.
      1. Create `pyproject.toml`: distribution `knowledge-hub`, `requires-python = ">=3.12"`, src layout, `[tool.setuptools.package-data]` including `knowledge_hub = ["migrations/*.sql"]`, script `kh = knowledge_hub.cli:main`, dependencies `mcp==2.3.0`, `starlette`, `uvicorn`, `psycopg[binary,pool]`, `pgvector`, `PyJWT[crypto]`, `cryptography`, `httpx`, `pyyaml`, `pydantic-settings`, `boto3`; extras `dev` = `pytest`, `pytest-asyncio`, `ruff`. Pytest config: `pythonpath = ["."]`, markers `unit` and `integration`, `asyncio_mode = "auto"`, `testpaths = ["tests"]`. Ruff: line length 110, select `E,F,I,B,UP`.
      2. `.gitignore` with `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `*.egg-info/`. Create empty `tests/__init__.py` and `tests/support/__init__.py`.
      3. `src/knowledge_hub/settings.py`: class `Settings(BaseSettings)` with env prefix `KH_` and every variable of section 3.1 with the stated defaults and types (secret file settings are `Path | None`); method `Settings.dsn(role)` for `migrator`, `app`, `indexer` building a libpq DSN from `db_host` (a path means a unix socket directory), `db_port`, `db_name`, the role name `kh_<role>` and the optional password file contents. Validators: `env == "prod"` with `dev_identity` raises `ValueError("KH_DEV_IDENTITY is set while KH_ENV=prod; refusing to start")`; `embed_provider == "fake"` outside `dev` and `test` raises `ValueError("KH_EMBED_PROVIDER=fake is only allowed when KH_ENV is dev or test")`; `dev_identity` is parsed from JSON into a model with `sub`, `principal_type` (`claude_client` or `n8n_workflow` only), `audiences`, optional `email`. `load_settings()` returns a `Settings`.
      4. `src/knowledge_hub/config.py`: pydantic models for `kh.yaml` as in section 3.1 with the stated defaults; `load_config(path)`; validation rejects an unknown audience or domain, a repo audience not in `audiences`, more than one T0 repo for one audience, and an `ingest_principal_types` value other than `n8n_workflow`; error texts start with `config:`.
      5. `src/knowledge_hub/cli.py`: `main()` with subcommands `serve`, `migrate`, `rebuild`, `check`, `daily`, `recompute-visibility`, `requeue-embeddings` registered through argparse; each prints `not implemented yet` and exits 3 except that `serve` calls `load_settings()` first and exits 2 printing the error on a settings error; end the module with `if __name__ == "__main__": raise SystemExit(main())`. `src/knowledge_hub/__init__.py` holds `__version__ = "0.1.0"`.
      6. Tests: `tests/unit/test_settings.py` (marked `unit`) covers defaults, the prod refusal text, the fake-provider refusal, dev identity accepted in `dev`, the `dsn("app")` string for a socket directory, and `cli serve` exiting 2 with `KH_ENV=prod` and `KH_DEV_IDENTITY` set; `tests/unit/test_config.py` (marked `unit`) covers the default config, an unknown audience, a repo audience not listed, two T0 repos with one audience, and `ingest_principal_types: [service]` refused. `tests/integration/test_smoke.py` (marked `integration`) imports the package and asserts `__version__ == "0.1.0"`, so `-m integration` is never empty.
      7. `.tld/toolkit.yaml`: set `project.name: knowledge-hub`, `stacks: [python]`, `verify.fast` to `.venv/bin/python -m ruff check .` and `.venv/bin/python -m pytest -q -m unit`, `verify.dod` to `.venv/bin/python -m ruff check .` and `.venv/bin/python -m pytest -q`, `verify.after_merge_allow` to `[".venv/bin/python -m pytest"]`. Keep `docs.must_read` and `git`. Create the venv as the Python stack guide says and install with `.venv/bin/python -m pip install -e ".[dev]"`.
      Stop: if `mcp==2.3.0` or `pydantic-settings` cannot be installed, stop with status=blocked and the resolver output.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_settings.py tests/unit/test_config.py tests/integration/test_smoke.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
      - "`tld profile validate .tld/toolkit.yaml --resolved` exits 0"
      - "grep -n 'stacks:' .tld/toolkit.yaml matches a line containing python"
      - "grep -n 'KH_DEV_IDENTITY is set while KH_ENV=prod; refusing to start' src/knowledge_hub/settings.py matches"
  - id: p2-claims
    title: "Vendored PrivacyFence verifier, claim middleware and test-claim helper"
    depends_on: [p1-skeleton]
    complexity: M
    touches:
      - src/knowledge_hub/auth/__init__.py
      - src/knowledge_hub/auth/pf_claim_verify.py
      - src/knowledge_hub/auth/VENDORED.md
      - src/knowledge_hub/auth/claims.py
      - tests/data/pf_req_test_vectors.json
      - tests/support/claims.py
      - tests/unit/test_claims.py
    brief: |
      Context: section 3.2. The contract is PrivacyFence `docs/identity-claim.md`; clone `https://github.com/privacyfence/privacyfence` outside this repo, check out commit `04d2ecac53428a1210b4b777f8c466359ee730bf`, read that document and the verifier first.
      1. Copy `contrib/verifier/pf_claim_verify.py` byte for byte to `src/knowledge_hub/auth/pf_claim_verify.py` and `contrib/verifier/req_test_vectors.json` to `tests/data/pf_req_test_vectors.json`. Write `auth/VENDORED.md` with the source URL, the commit and the file's sha256.
      2. `auth/claims.py`: dataclass `Caller(sub, principal_type, email, audiences: frozenset[str], token_id)`; `ReplayCache` protocol with `add_if_new(jti, expires_at) -> bool` and an `InMemoryReplayCache`; class `ClaimMiddleware(app, verifier, settings, replay_cache)` exactly as section 3.2 (path `/mcp` only; POST, GET and DELETE as described; 413 over `max_body_bytes`; 401 with empty body for a missing header or any `ClaimError`; refuse `human` always and `service` on a `tools/call`; email kept only for `claude_client`; audience regex; `scope["state"]["kh_caller"]` and `["kh_call"]` with the tool name and parsed `arguments`; body replay through a new `receive`; dev identity only when `settings.dev_identity` is set and no header is present). Function `caller_from_scope(scope) -> Caller` raising `KhAuthError`. Re-check the prod refusal in `__init__`.
      3. `tests/support/claims.py`: a module-level Ed25519 test key, `static_jwks()`, and `mint_claim(sub, principal_type, audiences, email=None, tool=None, arguments=None, lifetime=60, now=None, **overrides) -> str` producing a valid claim for a given tool call (computing `req` with the vendored `request_hash`), plus `verifier_for_tests()` returning a `ClaimVerifier` over the static JWKS.
      4. `tests/unit/test_claims.py` (marked `unit`): the six vectors reproduce through the vendored helper; a valid claim passes; missing header, expired claim, wrong `req`, a batch body, `human`, `service` on `tools/call`, a replayed `jti`, an oversize body and a bad audience name each give 401 (413 for oversize) with an empty body; GET and DELETE with a valid claim give 405; an `n8n_workflow` claim carrying an email has `Caller.email` equal to `None`; dev override works with `KH_ENV=dev` and the middleware constructor raises under `prod`.
      Stop: if the vendored verifier's public names differ from those used here (`ClaimVerifier`, `ClaimError`, `CLAIM_HEADER`, `request_hash` or a documented equivalent), stop with status=blocked and list the names found.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_claims.py -q passes"
      - "`sha256sum src/knowledge_hub/auth/pf_claim_verify.py` equals the sha256 recorded in src/knowledge_hub/auth/VENDORED.md"
      - ".venv/bin/python -m ruff check src/knowledge_hub/auth tests/support tests/unit/test_claims.py passes"
  - id: p3a-runner-fixture
    title: "Migration runner, test cluster fixtures and migration 0001"
    depends_on: [p1-skeleton]
    complexity: M
    touches:
      - src/knowledge_hub/db/__init__.py
      - src/knowledge_hub/db/migrate.py
      - src/knowledge_hub/migrations/0001_schema_grants.sql
      - scripts/ensure_test_pg.sh
      - tests/conftest.py
      - tests/support/pg.py
      - tests/support/fixtures_db.py
      - tests/support/fixtures_app.py
      - tests/integration/test_migrate_runner.py
    brief: |
      Context: sections 3.3 (roles, `0001`) and 3.4.
      1. `scripts/ensure_test_pg.sh`: idempotent; if `/usr/lib/postgresql/16/bin/initdb` or the pgvector extension files are missing run `apt-get update` and `apt-get install -y postgresql-16 postgresql-16-pgvector`; exit 0 when present.
      2. `tests/support/pg.py`: class `Cluster` with `start()`, `stop()`, `admin_dsn()` and `dsn(role, dbname="kh")`; `start()` runs `bash scripts/ensure_test_pg.sh` first when `initdb` is missing, creates a data directory under `/var/tmp/kh-pg-<pid>`, runs `initdb -A trust -U postgres`, starts `pg_ctl` with `-c listen_addresses=` and a unix-socket directory in that path; when `os.geteuid() == 0` every PostgreSQL binary runs through `su postgres -s /bin/bash -c` after `chown postgres` of the directory. Function `bootstrap_roles(cluster)` as the sandbox superuser creates roles `kh_migrator`, `kh_app`, `kh_indexer` (LOGIN, NOSUPERUSER, NOBYPASSRLS) and `kh_provenance` (NOLOGIN), database `kh` owned by `kh_migrator`, `CREATE EXTENSION vector` in it, and `GRANT kh_provenance TO kh_migrator`.
      3. `tests/support/fixtures_db.py`: pytest fixtures `pg_cluster` (session: starts and stops the cluster, removes the directory), `migrated_db` (session: bootstraps roles and runs the migrations as `kh_migrator`, returns the cluster) and `fresh_db` (function: a database recreated from a template made after migration). `tests/support/fixtures_app.py`: an empty module with a docstring (later phases add fixtures to it). `tests/conftest.py`: `pytest_plugins = ["tests.support.fixtures_db", "tests.support.fixtures_app"]` and a `pytest_collection_modifyitems` hook that marks every test under `tests/integration` with `integration` and every test under `tests/unit` with `unit`. This file never changes in later phases.
      4. `db/migrate.py`: `migrate(dsn)` reads `knowledge_hub/migrations/*.sql` (package data) in order, applies each in one transaction, records name and sha256 in `kh.schema_migrations` (created by `0001`), skips applied files, treats a missing `kh.schema_migrations` as no files applied, and raises `MigrationError` naming the file when an applied file's checksum changed; `migrate_main()` is for the later CLI phase.
      5. Migration `0001_schema_grants.sql` exactly as listed in section 3.4 and the first bullet of section 3.3 (schema `kh`, the `public` schema grants and revokes, `schema_migrations`, `claim_jti`, FORCE RLS and the `kh_migrator` policy on both tables).
      6. `tests/integration/test_migrate_runner.py`: applying twice is a no-op; a modified applied file raises `MigrationError` naming it; `kh.schema_migrations` lists `0001`; `kh_app` is not a superuser and has no BYPASSRLS.
      Stop: if `apt-get install postgresql-16-pgvector` fails, stop with status=blocked and the apt output.
    acceptance:
      - "bash scripts/ensure_test_pg.sh exits 0"
      - ".venv/bin/python -m pytest tests/integration/test_migrate_runner.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p3b-tables
    title: "Migration 0002: every table"
    depends_on: [p3a-runner-fixture]
    complexity: M
    touches:
      - src/knowledge_hub/migrations/0002_tables.sql
      - tests/integration/test_schema_tables.py
    brief: |
      Context: section 3.4 (`0002`). Write the migration exactly as listed there: every table, column, type, foreign key, unique constraint, partial index and index, `CHECK kh_no_open_rows` on each visibility-bearing table (`documents`, `chunks`, `entities_cache`, `raw_records`, `derived_records`, `candidates`, `conflicts`), the `chunks` constraint `num_nonnulls(document_id, raw_id, derived_id) = 1`, the generated `tsv` column and `embedding vector(1024)` nullable. Use `uuid` primary keys without server-side defaults for the caller-insert tables. Do not add policies or triggers (later phases).
      Tests (integration): every table of section 3.4 exists in schema `kh` with the listed columns (compare against a literal dict in the test); inserting as the sandbox superuser a row with empty audiences and null emails violates `kh_no_open_rows` for each visibility table; a chunk with two owners or none violates its check; the duplicate `dedupe_key` index rejects a second `proposed` candidate and allows a second after the first is `rejected`; `tsv` is generated.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_schema_tables.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p3c-rls
    title: "Migration 0003: functions, row-level security and the policy matrix"
    depends_on: [p3b-tables]
    complexity: M
    worker_level: strong
    worker_level_reason: "Security-critical SQL: a wrong policy leaks data across audiences or blocks the roles entirely"
    touches:
      - src/knowledge_hub/migrations/0003_rls.sql
      - tests/integration/test_rls_policies.py
    brief: |
      Context: section 3.3 (the whole section, including the matrix). Write migration `0003_rls.sql`.
      1. Functions in schema `kh` exactly as 3.3 names them: `caller_audiences`, `caller_email`, `caller_sub`, `caller_type`, `visible`; `owns_derived`, `owns_candidate`, `owns_conflict`, `my_raw_id`, `claim_seen` and `candidate_mark` as `SECURITY DEFINER` owned by `kh_migrator` with `SET search_path = kh, pg_temp`; `EXECUTE` on those six is revoked from `PUBLIC` and granted to `kh_app` only; `caller_*` and `visible` keep `EXECUTE` for `kh_app` and `kh_provenance`, revoked from `PUBLIC`.
      2. `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY` on every table of schema `kh` (including `claim_jti` and `schema_migrations` if not yet done), a `kh_migrator` policy `FOR ALL USING (true) WITH CHECK (true)` on every table, and every other policy and grant of the matrix in 3.3, one policy per role and command. Column-level `GRANT SELECT` for `kh_provenance` and column-level `GRANT UPDATE` for `kh_indexer` exactly as the matrix says; `kh_app` gets table-level `SELECT` or `INSERT` only where the matrix names it and nothing else.
      3. Tests (integration, as the three login roles through `Cluster.dsn`): the catalog check that every table in `kh` has `relrowsecurity` and `relforcerowsecurity`; a table-by-role matrix test generated from a literal dict copy of the matrix in 3.3 that asserts, for each role and table, which of SELECT, INSERT, UPDATE and DELETE succeed or fail as an empty or permission-denied result (seed rows through the sandbox superuser); `kh.visible` truth table with set_config values for audiences-only rows, email-only rows, mixed rows and the no-audience-no-email caller; `kh.claim_seen` returns true then false for one id and `kh_app` cannot select `claim_jti` directly; the three `owns_*` functions and `my_raw_id` return the right answers for the right `sub`; the migration runs as `kh_migrator` (not a superuser) and `pg_proc.proowner` of each definer function is the role 3.3 names; `kh_indexer` can DELETE expired `claim_jti` rows and no others; `kh.candidate_mark` moves a candidate only along the allowed states for its creator and returns false for another `sub`.
      Stop: if a policy in the matrix cannot be expressed as written, stop with status=blocked and name the row.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_rls_policies.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p3d-triggers
    title: "Migration 0004: chunk inheritance, embed queue and raw visibility triggers"
    depends_on: [p3c-rls]
    complexity: M
    touches:
      - src/knowledge_hub/migrations/0004_triggers.sql
      - tests/integration/test_triggers.py
    brief: |
      Context: sections 3.3 (chunk consistency, embed jobs) and 3.7 (first bullet). Write migration `0004_triggers.sql`.
      1. `chunks_inherit_owner` (`BEFORE INSERT OR UPDATE` on `chunks`) and `owner_propagate_to_chunks` (`AFTER UPDATE` on `documents`, `raw_records`, `derived_records`) exactly as 3.3 says.
      2. `AFTER INSERT` triggers `raw_records_enqueue_embed` and `derived_records_enqueue_embed` (`SECURITY DEFINER`, owner `kh_migrator`, `search_path = kh, pg_temp`) that insert an `index_jobs` row of kind `embed_raw` or `embed_derived` with payload `{"id": "<uuid>"}`.
      3. `raw_records_assign_visibility` (`BEFORE INSERT` on `raw_records`, `SECURITY DEFINER`, owner `kh_provenance`, `search_path = kh, pg_temp`) exactly as the first bullet of 3.7: mapped series or folder (union when both match) sets `required_audiences` and null `allowed_emails` and raises SQLSTATE `P0K02` unless the mapped audiences are a subset of `kh.caller_audiences()`; no match sets `{}` and `lower(participants)`; no match and no participants raises SQLSTATE `P0K01`; application-supplied visibility is overwritten.
      4. `sources_snapshot_visibility` (`BEFORE INSERT` on `derived_sources`, `candidate_sources` and `conflict_items`, `SECURITY DEFINER`, owner `kh_provenance`, `search_path = kh, pg_temp`) copying the live source's visibility (looked up by `source_kind` or `item_kind` and the id; a missing source raises SQLSTATE `P0K03`) into `src_required_audiences` and `src_allowed_emails`, overwriting supplied values.
      5. Tests (integration): a chunk inserted by the indexer with a wrong tier inherits the owner's values; changing a document's visibility updates its chunks (and a raw and a derived owner's); inserting a raw record as `kh_app` with `caller_type` `n8n_workflow` creates exactly one `embed_raw` job that `kh_app` cannot read; a mapped series gives the mapped audiences and null emails; an unmapped one gives `{}` and lower-cased participants; unmapped without participants fails with SQLSTATE `P0K01`; a mapped series whose audiences the caller does not hold fails with `P0K02`; supplied visibility is overwritten; a source row's snapshot copies the live visibility and overwrites supplied values; a missing source raises `P0K03`; chunks of a raw owner get tier `T3` and of a derived owner `T2`.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_triggers.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p4-rls-access
    title: "Database access layer, replay cache and the table-level leak tests"
    depends_on: [p2-claims, p3d-triggers]
    complexity: M
    touches:
      - src/knowledge_hub/db/pool.py
      - src/knowledge_hub/db/caller_tx.py
      - src/knowledge_hub/auth/replay.py
      - tests/support/fixtures_db.py
      - tests/support/principals.py
      - tests/support/seed.py
      - tests/integration/test_rls_tables.py
      - tests/integration/test_replay_cache.py
    brief: |
      Context: section 3.3; the principal fixtures here are reused by every later leak test.
      1. `db/pool.py`: `make_app_pool(settings)` and `make_indexer_pool(settings)` returning `psycopg_pool.AsyncConnectionPool` with the `pgvector` adapter registered, each with its own DSN from `Settings.dsn`.
      2. `db/caller_tx.py`: async context manager `caller_tx(pool, caller)` implementing exactly the transaction of section 3.3.
      3. `auth/replay.py`: `PgReplayCache(pool)` whose `add_if_new(jti, expires_at)` runs `SELECT kh.claim_seen(%s, %s)` on the app pool and returns the boolean.
      4. `tests/support/principals.py`: named principals as `Caller` objects: `alice_all` (audiences `all`, email `alice@example.org`), `bob_mgmt` (`all`, `mgmt`, `bob@example.org`), `carol_hr` (`all`, `hr`), `dave_none` (no audiences, email `dave@example.org`), `eve_nobody` (no audiences, no email), `wf_token` (`n8n_workflow`, `all`, sub `n8n-token:t1`, no email). `tests/support/seed.py`: `seed_rows(admin_dsn)` inserting on the sandbox-superuser connection inside one transaction with `SET LOCAL session_replication_role = replica` (no user triggers fire), writing every column explicitly (chunk columns and source snapshot columns included, consistent with their owners), one document, one chunk and one row of each other visibility table for each case: audience `{all}`, `{mgmt}`, `{hr}`, `{all,mgmt}`, attendee-only (`{}` with emails `[dave@example.org]`) and empty-email-list (`{}` with `[]`); every derived, candidate and conflict case gets a source row of the same visibility; the seed ends by running the chunk drift query of 3.3 and asserting zero rows. Add the fixtures `app_pool`, `indexer_pool` and `seeded_db` to `tests/support/fixtures_db.py`.
      5. `tests/integration/test_rls_tables.py`: for every principal and every visibility table (`documents`, `chunks`, `raw_records`, `derived_records`, `candidates`, `conflicts`, `entities_cache`) selecting everything inside `caller_tx` returns exactly the rows whose visibility the principal satisfies (spell the expected case names out per principal); a guessed id of a hidden row returns zero rows; a query on the app pool outside `caller_tx` returns zero rows for every table; `kh_app` cannot UPDATE or DELETE any table and cannot read `index_jobs`; child tables (`derived_sources`, `candidate_sources`, `conflict_items`, `document_versions`) show only rows of visible parents.
      6. `tests/integration/test_replay_cache.py`: first `add_if_new` is true, second false, and the app pool cannot select `claim_jti`.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_rls_tables.py tests/integration/test_replay_cache.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p5-mcp-get-doc
    title: "HTTP app, MCP server, error catalogue and get_doc"
    depends_on: [p4-rls-access]
    complexity: M
    touches:
      - src/knowledge_hub/app.py
      - src/knowledge_hub/mcp/__init__.py
      - src/knowledge_hub/mcp/server.py
      - src/knowledge_hub/mcp/errors.py
      - src/knowledge_hub/tools/__init__.py
      - src/knowledge_hub/tools/docs.py
      - tests/support/fixtures_app.py
      - tests/support/mcp_client.py
      - tests/integration/test_tool_get_doc.py
      - tests/unit/test_tool_schemas.py
    brief: |
      Context: sections 3.2, 3.5 (citation ids), 3.9. Verify first against the installed `mcp==2.3.0` that `MCPServer`, `streamable_http_app`, `StreamableHTTPSessionManager(stateless=True)`, `TransportSecuritySettings` and `ctx.request_context.request` exist; read `site-packages/mcp/server/`.
      1. `mcp/errors.py`: `KhError(code, text)`; the whole fixed-text catalogue of section 3.9 as module constants with exactly those names and texts (format fields filled with `str.format`), except `RATE_LIMIT`; `tool_guard` decorator exactly as section 3.2 (tool name check, stored-arguments-overlaid-on-defaults check, `caller_tx`, `KhError` mapping, `INTERNAL` mapping).
      2. `mcp/server.py`: `build_mcp(deps)` returning the `MCPServer("knowledge-hub")` with tools registered by `register_tools(server, deps)` (later phases extend it); `Deps` dataclass with fields `settings`, `config`, `app_pool`, `indexer_pool`, `embedder`, `blobs`, `forgejo`, `grist`, `verifier`, `ratelimiter`, `worker` (the last seven default to `None`).
      3. `tools/docs.py`: `get_doc(id, version="")` accepting `doc:<uuid>`, `raw:<uuid>`, `derived:<uuid>`; one RLS query per kind inside `caller_tx`; returns text (document body, derived body, raw excerpt for now), tier, domain, owner, approved_by, approved_at, review_by, status, stale, entities, `supersedes`, `superseded_by` (the successor id only through an RLS join), `commit_sha`, `indexed_at`, `citation_id`, `source_link` as in 3.5. A malformed id and a missing or hidden row both give `NOT_FOUND`. A non-empty `version` returns `VERSION_NOT_INDEXED` for now (a later phase implements it).
      4. `app.py`: `build_app(settings, deps, verifier)` as section 3.2 (routes `/mcp` behind `ClaimMiddleware`, `/healthz`, lifespan entering the session manager, `TransportSecuritySettings` with the allowed hosts, `PgReplayCache`). Other routes are added by later phases.
      5. `tests/support/mcp_client.py`: helper that runs the app in process (httpx ASGI transport) and calls tools with a signed claim from `tests/support/claims.py`, sending `initialize` and then the `tools/call` the way PrivacyFence does; fixtures for it go in `tests/support/fixtures_app.py`.
      6. Tests: `test_tool_get_doc.py` (integration): every principal of `tests/support/principals.py` gets exactly the documents it should through the MCP path; a guessed uuid, a hidden id and a malformed id give identical `not found` results; a forced `psycopg` error inside a tool returns `internal error (ref ...)` and the response text contains no row value; a call whose omitted defaults the SDK filled in is accepted, and one whose stored arguments differ from the received ones is refused. `test_tool_schemas.py` (unit): for every registered tool apply PrivacyFence's skip rules (object schema, no `$ref`/`$defs`/`definitions`, no `reason` property, under 32 KB).
      Stop: if the tool cannot read the verified caller in stateless mode through `scope["state"]`, use the ContextVar fallback in section 3.2; if neither works stop with status=blocked.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_tool_get_doc.py tests/unit/test_tool_schemas.py -q passes"
      - "grep -n 'allowed_hosts' src/knowledge_hub/app.py matches"
      - ".venv/bin/python -m ruff check . passes"
  - id: p6a-search-core
    title: "Embedding interface, fake embedder and search cursors"
    depends_on: [p5-mcp-get-doc]
    complexity: S
    touches:
      - src/knowledge_hub/embed/__init__.py
      - src/knowledge_hub/embed/base.py
      - src/knowledge_hub/search/__init__.py
      - src/knowledge_hub/search/cursor.py
      - tests/unit/test_cursor.py
    brief: |
      Context: sections 3.5 (cursor) and 3.10.
      1. `embed/base.py`: `EmbeddingProvider` protocol (`model`, `dim`, `async embed(texts) -> list[list[float]]`, `may_embed(visibility) -> bool`) and `FakeEmbedder(dim=1024)` returning a deterministic unit vector derived from a hash of the text (identical texts give identical vectors, different texts differ) with `may_embed` always true and `model == "fake"`.
      2. `search/cursor.py`: `encode_cursor(key, sub, audiences, query, offset)` and `decode_cursor(key, sub, audiences, query, token)` as in section 3.5, raising `KhError` with `CURSOR_FOREIGN` for a tampered cursor, another `sub` or another query.
      3. `tests/unit/test_cursor.py`: round trip, tamper, wrong sub, wrong query, offset above the maximum; `FakeEmbedder` determinism.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_cursor.py -q passes"
      - ".venv/bin/python -m ruff check src/knowledge_hub/embed src/knowledge_hub/search tests/unit/test_cursor.py passes"
  - id: p6b-search-tool
    title: "Hybrid search with RRF and its leak tests"
    depends_on: [p6a-search-core]
    complexity: M
    touches:
      - src/knowledge_hub/search/sql.py
      - src/knowledge_hub/tools/search.py
      - src/knowledge_hub/mcp/server.py
      - tests/integration/test_search_ranking.py
      - tests/integration/test_search_leaks.py
    brief: |
      Context: section 3.5.
      1. `search/sql.py`: the statement of section 3.5 as one function `build_search_query(filters)` returning SQL text and parameters (vector order with `<=>` over chunks whose `embedding_model` equals the configured model, `websearch_to_tsquery('english', ...)`, `row_number()` lists limited to `KH_SEARCH_POOL`, RRF with `KH_RRF_K`, the weight multipliers from settings, `DISTINCT ON (owner_key)`, limit and offset, and the `lower_tier_hits` aggregate over tiers below `min_tier`). Parameters are always bound, never concatenated.
      2. `tools/search.py`: tool `search(query, domains, min_tier, entities, limit, cursor)`; limit bounded 1 to 50; embeds the query with `deps.embedder`; applies the normative-answer rule of section 3.5 (`NORMATIVE`); builds hit fields including `source_link` as in 3.5; registers in `mcp/server.py`.
      3. Tests (integration, using `FakeEmbedder` through `Deps`): `test_search_ranking.py`: a T0 chunk ranks above an equally relevant T3 chunk (seed both through the indexer role with identical text); a stale chunk ranks below a current one with the same text; a superseded one below a current one; a `disputed` T0 is down-weighted and a query with only a disputed T0 hit and visible T3 hits fails with `NORMATIVE`; a narrow-audience caller still gets its best chunks when many hidden chunks match more strongly (the recall test). `test_search_leaks.py`: for every principal of `tests/support/principals.py`, hits contain only visible citation ids, `lower_tier_hits` counts equal the visible T2/T3 counts and `newest_at` ignores hidden rows, a cursor from another principal is refused, and the total hit count for a query matching hidden rows equals the visible count.
      Stop: if `to_tsvector('english', ...)` or the `<=>` operator is unavailable in the test cluster, stop with status=blocked.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_search_ranking.py tests/integration/test_search_leaks.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p7-http-e2e
    title: "End-to-end HTTP tests and the tool leak-matrix registry"
    depends_on: [p6b-search-tool]
    complexity: M
    touches:
      - tests/support/tool_cases.py
      - tests/integration/test_http_e2e.py
      - tests/integration/test_leak_matrix.py
    brief: |
      Context: sections 3.2 and 3.3. This closes milestone 1.
      1. `tests/support/tool_cases.py`: a registry `TOOL_CASES` (list of dataclass `ToolCase(name, args_for_hidden, args_for_visible, expect_hidden)`); later phases that add a tool append their cases here. Start with `search` and `get_doc`.
      2. `tests/integration/test_leak_matrix.py`: for every case and every principal, the response contains nothing outside the principal's visibility and the hidden-row case equals the missing-row case byte for byte.
      3. `tests/integration/test_http_e2e.py`: with the in-process app and signed claims, `initialize` then `tools/list` returns the tools with flat schemas; a `Host` not in `KH_ALLOWED_HOSTS` is refused; a request without a claim is 401 with an empty body; a claim whose `req` was made for other arguments is 401; a `tools/call` made with a `service` claim is 401; an `n8n_workflow` claim never sees an attendee-only row; a person with no groups but the matching verified email sees the attendee-only row; the same `jti` sent twice gives 401 the second time.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_http_e2e.py tests/integration/test_leak_matrix.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p8-frontmatter-chunker
    title: "Frontmatter validation and heading chunker"
    depends_on: [p1-skeleton]
    complexity: M
    touches:
      - src/knowledge_hub/indexer/__init__.py
      - src/knowledge_hub/indexer/frontmatter.py
      - src/knowledge_hub/indexer/chunker.py
      - tests/unit/test_frontmatter.py
      - tests/unit/test_chunker.py
    brief: |
      Context: section 3.6 (frontmatter and chunking rules).
      1. `indexer/frontmatter.py`: `parse_document(text, path, domains) -> ParsedDoc | Rejected`; split YAML frontmatter between `---` lines with `yaml.safe_load`; required keys `id`, `title`, `domain`, `review_by` (a date); optional `owner`, `type`, `entities`, `supersedes`, `superseded_by`, `status` (only `current` or `superseded`; `disputed` is rejected as `frontmatter: status disputed cannot be set by a file`); `domain` must be in `domains`. Reject with `Rejected(reason)` and the exact text `frontmatter must not set <key>; the server decides tier and audience` for any of `tier`, `audience`, `audiences`, `required_audiences`, `allowed_emails`, `visibility`, `approved_by`, `approved_at`. Other problems give `Rejected("frontmatter: <what>")`.
      2. `indexer/chunker.py`: `chunk_markdown(body, version) -> list[Chunk(ordinal, heading_path, text, content_sha)]` per section 3.6: split on `#` to `######` headings, `heading_path` is the chain joined with ` > `, pieces over 1200 characters split at paragraph boundaries into pieces of at most 1200 characters with 150 characters of overlap, pieces under 200 characters merge into the previous piece of the same section, `content_sha` is sha256 of `version|heading_path|text`.
      3. Tests (unit): every forbidden key, `status: disputed`, missing required keys, bad date, unknown domain, happy path; chunker cases for nested headings, a long section, a tiny trailing section, empty body, and stable `content_sha` across runs.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_frontmatter.py tests/unit/test_chunker.py -q passes"
      - ".venv/bin/python -m ruff check src/knowledge_hub/indexer tests/unit passes"
  - id: p9a-trust
    title: "Trust walk"
    depends_on: [p8-frontmatter-chunker]
    complexity: M
    touches:
      - src/knowledge_hub/indexer/trust.py
      - tests/data/forgejo/
      - tests/unit/test_trust.py
    brief: |
      Context: section 3.6 (trust) and the compose repo document `docs/knowledge-hub.md` finding 6.
      1. `indexer/trust.py`: pure function `evaluate_chain(repo, commits, prs, reviews, changed_files, bot_logins, anchor) -> TrustResult(trusted, commit_verdicts: dict[sha, Verdict], file_verdicts: dict[path, Verdict], alerts)` implementing every rule of section 3.6: anchor semantics (null anchor means every first-parent commit passes the rule; anchor missing from the chain is untrusted with `trust anchor <sha> is not on main of <repo>`), merge commit, PR merged with `merge_commit_sha`, at least one official non-dismissed `APPROVED` review whose `commit_id` equals the PR's final head sha by someone who is neither author nor bot, non-merge first-parent commit sets untrusted, last-change attribution per file, files at or before the anchor trusted as `trust-anchor`, `approved_at` from `submitted_at`.
      2. Fixtures under `tests/data/forgejo/` written by hand as JSON in the shapes of the Forgejo API (commit, pull request with `merged`, `merge_commit_sha` and `head.sha`, review with `official`, `dismissed`, `state`, `commit_id`, `submitted_at`, `user.login`, compare result with changed files), one set per scenario.
      3. Tests (unit): all-approved chain is trusted; a self-approval does not count; a dismissed review does not count; a bot approval does not count; an approval on head A with merge at head B gives `disputed`; a non-merge commit makes the repo untrusted and names the sha; a squash-merged PR is untrusted; a force-pushed new root with a null anchor is untrusted; an anchor missing from the chain is untrusted; a file last changed before the anchor is trusted as `trust-anchor`.
      Research: the fixture shapes are an assumption about Forgejo's API; finish the work and put in your final report the line `Research: do the Forgejo reviews API and commits/{sha}/pull expose official, dismissed, commit_id and merge_commit_sha as the fixtures assume?` The phase does not block on it.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_trust.py -q passes"
      - ".venv/bin/python -m ruff check src/knowledge_hub/indexer/trust.py passes"
  - id: p9b-forgejo-client
    title: "Forgejo read client and the write guard"
    depends_on: [p1-skeleton]
    complexity: S
    touches:
      - src/knowledge_hub/forgejo/__init__.py
      - src/knowledge_hub/forgejo/client.py
      - tests/unit/test_forgejo_client.py
    brief: |
      Context: section 3.6 (API facts) and 3.8 (write guard).
      1. `forgejo/client.py`: `ForgejoClient(base_url, token, org)` over `httpx.AsyncClient` with read methods `list_tree(repo, sha)`, `get_raw(repo, path, sha)`, `commits_first_parent(repo, branch, since_sha)`, `pull_for_commit(repo, sha)`, `pull(repo, number)`, `pull_reviews(repo, number)`, `changed_files(repo, base, head)`, `head_sha(repo, branch)`. Define `_guard_ref(ref)` raising `ForgejoRefused` for any ref that does not start with `bot/promote/`; every write added later calls it first.
      2. Tests (unit, with inline JSON and `httpx.MockTransport`): each read method builds the expected URL and parses the expected fields; a non-200 raises `ForgejoError` with no token in the message; `_guard_ref` refuses `main`, `refs/heads/main` and `feature/x`, accepts `bot/promote/abc`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_forgejo_client.py -q passes"
      - ".venv/bin/python -m ruff check src/knowledge_hub/forgejo passes"
  - id: p10-embed-providers
    title: "Ollama and HTTP embedding providers and the benchmark script"
    depends_on: [p6a-search-core]
    complexity: M
    touches:
      - src/knowledge_hub/embed/ollama.py
      - src/knowledge_hub/embed/http.py
      - src/knowledge_hub/embed/factory.py
      - scripts/bench_embed.py
      - tests/unit/test_embed_providers.py
    brief: |
      Context: section 3.10.
      1. `embed/ollama.py`: `OllamaEmbedder(url, model, dim)` calling `POST <url>/api/embed` with `{"model", "input": [texts]}` in batches of 32 and reading `embeddings`; `may_embed` always true; raises `EmbeddingError` on a dimension other than `dim` or a non-200 status.
      2. `embed/http.py`: `HttpEmbedder(url, token, model, dim, allowed_audiences)` calling a JSON endpoint `{"model", "input"}` returning `{"data": [{"embedding": [...]}]}` with the bearer token; `may_embed(visibility)` true only when `required_audiences` is a non-empty subset of `allowed_audiences` and `allowed_emails` is null; refuses a dimension mismatch.
      3. `embed/factory.py`: `make_embedder(settings)` choosing by `KH_EMBED_PROVIDER` (`ollama`, `http`, `fake`); one provider serves documents and queries.
      4. `scripts/bench_embed.py`: argparse `--count` (default 200) and `--url`; embeds `count` synthetic 800-character paragraphs and 20 short queries with the configured model and prints chunks per second and the mean and p95 query latency in milliseconds.
      5. Tests (unit, `httpx.MockTransport`): batching, dimension mismatch error, `may_embed` truth table, factory selection including `fake`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_embed_providers.py -q passes"
      - ".venv/bin/python scripts/bench_embed.py --help exits 0"
  - id: p11a1-sync-plan
    title: "Sync plan: trust, parsing and the pure document plan"
    depends_on: [p8-frontmatter-chunker, p9a-trust, p9b-forgejo-client]
    complexity: M
    touches:
      - src/knowledge_hub/indexer/sync_plan.py
      - tests/support/forgejo_fake.py
      - tests/unit/test_sync_plan.py
    brief: |
      Context: section 3.6 (documents, trust, supersedes, duplicate ids). This phase builds the plan from Forgejo reads; the next phase writes it to the database.
      1. `tests/support/forgejo_fake.py`: an in-memory fake with the read methods of `ForgejoClient` built from a small description of repos, commits, PRs, reviews and file contents, so sync tests need no HTTP.
      2. `indexer/sync_plan.py`: `async build_sync_plan(forgejo, config, repo, repo_state, existing_docs) -> SyncPlan` that: fetches the head of `main`; refuses a head that does not descend from `repo_state.last_indexed_sha` (the alert text of 3.6, empty plan otherwise unchanged); gathers commits, PRs, reviews and changed files and calls `evaluate_chain(repo, ...)`; lists the tree at the head; parses each markdown file with `parse_document`; for rejected files plans removal of any earlier row and an `index_alerts` row with the rejection text; applies the duplicate-`id` rule with its alert text; plans document upserts keyed by `(repo, doc_key)` with tier and visibility from config (`required_audiences = {repo audience}`, `allowed_emails = NULL`), `status` `current`, `superseded` (per the same-repo rule of 3.6) or `disputed` (untrusted repo, no approved merge PR, or a cross-repo `supersedes`/`superseded_by`, with the alert texts of 3.6), `approved_by` and `approved_at` from the file verdict, `merge_pr`, `pr_url`; plans renames (same `doc_key`, new path), deletions (existing document absent from the tree) and one `document_versions` row per indexed commit; returns the new `repo_state` values. `SyncPlan` is a dataclass of plain lists; the function makes no database call.
      3. Tests (unit, using the fake and `existing_docs` dicts): approval extraction fills `approved_by` and `approved_at`; a file without an approved merge PR is planned `disputed` with its alert; frontmatter claiming `tier` is planned for removal with the alert; a rename keeps the `doc_key`; a missing file is planned for deletion; an untrusted repo plans everything `disputed`; a `canonical-mgmt` file that supersedes a `canonical-all` id is planned `disputed` and plans no change in `canonical-all`; two files with one `id` follow the duplicate rule; a head that does not descend from the last indexed sha gives an empty plan and the alert.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_sync_plan.py -q passes"
      - ".venv/bin/python -m ruff check src/knowledge_hub/indexer tests/support/forgejo_fake.py passes"
  - id: p11a2-sync-write
    title: "Sync write: the repo transaction"
    depends_on: [p11a1-sync-plan, p4-rls-access, p6a-search-core]
    complexity: S
    touches:
      - src/knowledge_hub/indexer/sync.py
      - tests/integration/test_sync.py
    brief: |
      Context: section 3.6. Chunks are written by the next phase; this phase writes documents, versions, alerts and `repo_state`.
      1. `indexer/sync.py`: `async sync_repo(indexer_pool, forgejo, config, repo)` that loads `repo_state` and the existing documents of the repo through the indexer pool, calls `build_sync_plan`, and writes the plan in one transaction under `pg_advisory_xact_lock(hashtext(repo))`: upserts documents, deletes documents absent from the tree, inserts `document_versions` and `index_alerts`, and updates `repo_state` (`trusted`, `untrusted_reason`, `last_indexed_sha`, `trust_anchor_sha` display copy).
      2. Tests (integration, using the fake): approval fields land in the table; a `disputed` file has its alert row; rename keeps the document id; deletion removes the document; syncing the same head twice leaves `indexed_at` unchanged; a rejected-frontmatter file leaves no row; a rewritten main leaves the index unchanged and alerts.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_sync.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p11b-sync-chunks
    title: "Chunk writing, embedding and idempotence in the sync"
    depends_on: [p11a2-sync-write, p10-embed-providers]
    complexity: M
    touches:
      - src/knowledge_hub/indexer/sync_chunks.py
      - src/knowledge_hub/indexer/sync.py
      - tests/integration/test_sync_chunks.py
    brief: |
      Context: sections 3.6 (chunks) and 3.10.
      1. `indexer/sync_chunks.py`: `async plan_chunks(embedder, config, document_rows) -> ChunkPlan` that chunks each changed document with `chunk_markdown` and `KH_CHUNKER_VERSION`, embeds the texts through `embedder` (skipping chunks the provider's `may_embed` refuses, which get a null embedding), and compares `content_sha`, chunker version and embedding model with the stored chunks so only changed chunks are rewritten; `apply_chunks(conn, plan)` writes the chunks inside the sync transaction.
      2. `indexer/sync.py`: call `plan_chunks` before opening the write transaction and `apply_chunks` inside it (signature gains `embedder`).
      3. Tests (integration): chunks exist with the document's visibility, tier and status after sync; syncing twice keeps chunk ids and `indexed_at`; changing one section rewrites only its chunks; changing the embedding model re-embeds all chunks of the repo; a `may_embed` refusal leaves a null embedding that full-text search still finds (use the HTTP provider fake with a restricted audience list).
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_sync_chunks.py tests/integration/test_sync.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p12-webhook-worker
    title: "Forgejo webhook, HMAC check and the job worker"
    depends_on: [p11b-sync-chunks, p5-mcp-get-doc]
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
      2. `indexer/webhook.py`: Starlette endpoint `POST /webhooks/forgejo`: read the raw body (cap `KH_MAX_BODY_BYTES`), verify the signature (401 with empty body when bad), parse JSON, accept only repos of the configured org and listed in config (else 202 and ignore), and insert an `index_jobs` row through the indexer pool: kind `git_push` for a `push` event to `refs/heads/main`, kind `pr_event` for a `pull_request` event (payload keeps repo, action, number only); answer 202.
      3. `indexer/worker.py`: `Worker(indexer_pool, forgejo, embedder, config, blobs=None)` with `run_once()` (claims one queued job with `FOR UPDATE SKIP LOCKED`, runs it, marks `done` or `failed` with attempts and `run_after` backoff of 30 seconds times attempts, at most 5 attempts) and `run_forever()`; `git_push` calls `sync_repo`; `pr_event` jobs are recorded as done (a later phase handles them).
      4. `app.py`: add the route (a later phase starts the worker in the lifespan).
      5. Tests: unit for the signature function (valid, wrong secret, missing, tampered body); integration: a signed push payload creates a job and answers 202, a bad signature creates nothing and gives 401, `run_once` indexes a fake repo and marks the job done, a failing sync marks it failed and schedules a retry.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_webhook_hmac.py tests/integration/test_webhook_worker.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p14-blobs
    title: "S3-compatible blob store"
    depends_on: [p1-skeleton]
    complexity: M
    touches:
      - src/knowledge_hub/blobs/__init__.py
      - src/knowledge_hub/blobs/s3.py
      - tests/support/blobs.py
      - tests/unit/test_blobs.py
    brief: |
      Context: section 3.10.
      1. `blobs/s3.py`: `BlobStore` protocol (`async put(key, data, content_type)`, `async get(key) -> bytes`, `async delete(key)`) and `S3BlobStore(endpoint, bucket, access_key, secret_key, region)` over `boto3` (`config=Config(s3={"addressing_style": "path"}, signature_version="s3v4")`), each call run with `asyncio.to_thread`; a missing key raises `BlobNotFound`.
      2. `tests/support/blobs.py`: `MemoryBlobStore` implementing the protocol, and `make_s3_store_with_stubber()` returning an `S3BlobStore` whose client is wrapped by `botocore.stub.Stubber`.
      3. Tests (unit): `MemoryBlobStore` round trip and not-found; `S3BlobStore` builds put, get and delete requests with path-style addressing and the configured bucket (assert through the stubber's expected params); a missing key maps to `BlobNotFound`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_blobs.py -q passes"
      - ".venv/bin/python -m ruff check src/knowledge_hub/blobs tests/support/blobs.py passes"
  - id: p13a-wiring
    title: "Build the real dependencies for the server and the CLI"
    depends_on: [p10-embed-providers, p12-webhook-worker, p14-blobs, p6b-search-tool]
    complexity: M
    touches:
      - src/knowledge_hub/deps.py
      - src/knowledge_hub/app.py
      - src/knowledge_hub/mcp/server.py
      - src/knowledge_hub/cli.py
      - tests/integration/test_wiring.py
    brief: |
      Context: sections 3.1, 3.2, 3.10. No phase has built `Deps` from settings yet.
      1. `deps.py`: `async build_deps(settings, config) -> Deps` creating the app and indexer pools (`db/pool.py`), the embedder (`make_embedder`), the blob store (`S3BlobStore` from the S3 settings, or `None` when `KH_S3_ENDPOINT` is unset), the `ForgejoClient` (`None` when `KH_FORGEJO_URL` is unset), the `Worker` (given the blob store) and the production `ClaimVerifier` over `KH_PF_JWKS_URL` with issuer `KH_PF_ISSUER` and audience `KH_PF_AUDIENCE` (field `verifier`; building it with any of the three unset raises a settings error unless `KH_DEV_IDENTITY` is set); `async close_deps(deps)`.
      2. `app.py`: `build_app(settings, deps, verifier)` starts and stops `deps.worker` in the lifespan; `cli.py`: `serve` loads settings and config (`KH_CONFIG_FILE`), builds the app with `build_deps` and runs uvicorn on `0.0.0.0:8080` (override with `--port`).
      3. Tests (integration, `KH_ENV=test`, `KH_EMBED_PROVIDER=fake`): `build_deps` against the test cluster returns working pools, a `FakeEmbedder`, `None` blobs and `None` forgejo (set `KH_PF_*` in the test environment); `build_deps` with `KH_PF_JWKS_URL` unset and no dev identity raises a settings error; the app built from it answers `/healthz`; `kh serve` with an invalid setting exits 2.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_wiring.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p13b-rebuild-check
    title: "kh rebuild, kh check, kh migrate and get_doc versions"
    depends_on: [p13a-wiring, p7-http-e2e]
    complexity: M
    touches:
      - src/knowledge_hub/indexer/rebuild.py
      - src/knowledge_hub/cli.py
      - src/knowledge_hub/tools/docs.py
      - tests/integration/test_rebuild.py
      - tests/integration/test_get_doc_version.py
    brief: |
      Context: section 3.6 (rebuild, check, get_doc versions).
      1. `indexer/rebuild.py`: `rebuild(deps, repo=None)` re-walks trust and calls `sync_repo` for each configured repo's head and removes documents absent from the tree; `check(deps)` returns a `CheckReport` listing repos whose head sha or any document `content_sha` differs from the index, chunks that differ from their owner (the drift query of section 3.3), and the `config_hash` of the `repo_state` row `repo = '__config__'` differing from the hash of the configuration (a missing row counts as drift) (audience map, repo audiences, entity visibility; helper `config_hash(config)` in this module).
      2. `cli.py`: wire `migrate` (calls `migrate` with the migrator DSN), `rebuild [--repo R]` and `check` (exit 1 on any drift). Leave `daily`, `recompute-visibility` and `requeue-embeddings` as `not implemented yet`.
      3. `tools/docs.py`: support `version=<sha>` as section 3.6 says: accepted only when `document_versions` holds the sha for the document (the check runs through RLS), then fetch with `deps.forgejo.get_raw`; otherwise `VERSION_NOT_INDEXED`.
      4. Tests (integration, with the fake): rebuild twice leaves rows, chunk ids and `indexed_at` unchanged; rebuild after a document is deleted in the fake removes it; `check` is clean after rebuild and reports drift after a manual chunk edit and after a config hash change; `get_doc` with an indexed older sha returns that content; with an unknown sha it returns the fixed error; a version request for a document the caller cannot see returns `not found` without calling the fake.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_rebuild.py tests/integration/test_get_doc_version.py -q passes"
      - ".venv/bin/python -m knowledge_hub.cli check --help exits 0"
  - id: p15-ingest-raw
    title: "ingest_raw, the audience map loader and embed jobs"
    depends_on: [p13b-rebuild-check, p14-blobs, p7-http-e2e]
    complexity: M
    touches:
      - src/knowledge_hub/jobs/__init__.py
      - src/knowledge_hub/jobs/audience_map.py
      - src/knowledge_hub/tools/raw.py
      - src/knowledge_hub/tools/docs.py
      - src/knowledge_hub/indexer/worker.py
      - src/knowledge_hub/app.py
      - src/knowledge_hub/mcp/server.py
      - tests/support/tool_cases.py
      - tests/integration/test_ingest_raw.py
    brief: |
      Context: sections 3.4, 3.7 (first bullet), 3.9. The migration and triggers already exist (`0004`).
      1. `jobs/audience_map.py`: `async load_audience_map(indexer_pool, config)` replaces all rows of `raw_audience_map` from `config.raw_audience_map` in one transaction and returns the new hash from `config_hash(config)` (`indexer/rebuild.py`); the app lifespan in `app.py` calls it at startup.
      2. `indexer/worker.py`: add job kind `embed_raw`: read the blob from `deps.blobs`, chunk with `chunk_markdown`, embed through the provider (honouring `may_embed`), insert chunks (visibility is inherited by the trigger).
      3. `tools/raw.py`: tool `ingest_raw` with the arguments of 3.9: refuse a principal type outside `config.ingest_principal_types` with `INGEST_FORBIDDEN`; refuse a body over `KH_MAX_RAW_BYTES` with `INGEST_TOO_LARGE`; validate `domain` and `occurred_at`; inside `caller_tx` insert the row in a savepoint (application uuid, no RETURNING, `ingested_by_sub` and `ingested_by_type` from the caller, 2000-character excerpt, `blob_key = raw/<uuid>`), upload the blob before commit, map SQLSTATE `P0K01` to `INGEST_NO_MAP` and `P0K02` to `INGEST_AUDIENCE`, and on a unique violation answer `ALREADY_INGESTED` with the id from `kh.my_raw_id(...)` without writing a blob; return `{"citation_id": "raw:<uuid>"}`.
      4. `tools/docs.py`: `get_doc` on a `raw:` id returns the full text from `deps.blobs` after the RLS check. Register `ingest_raw` in `mcp/server.py` and append its cases to `tests/support/tool_cases.py`.
      5. Tests (integration, `MemoryBlobStore`): mapped series gives its audiences when the token holds them and `INGEST_AUDIENCE` when it does not; unmapped gives `{}` plus lower-cased participants; unmapped with no participants gives `INGEST_NO_MAP`; a person (`claude_client`) gets `INGEST_FORBIDDEN`; `wf_token` succeeds but cannot read back an attendee-only row it wrote; an oversize body is rejected; a second identical ingest returns `ALREADY_INGESTED` with the first id and one blob; a failed upload leaves no row; after `run_once` on the `embed_raw` job the chunks exist with the row's visibility; `get_doc` on the raw id returns the blob text to a visible caller and `not found` to others.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_ingest_raw.py tests/integration/test_leak_matrix.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p16a-derived-sql
    title: "Migration 0005: derived visibility function and constraint trigger"
    depends_on: [p4-rls-access]
    complexity: M
    worker_level: strong
    worker_level_reason: "Security-critical SQL: a SECURITY DEFINER function that decides who can see derived data"
    touches:
      - src/knowledge_hub/migrations/0005_derived_visibility.sql
      - src/knowledge_hub/visibility.py
      - tests/unit/test_visibility.py
      - tests/integration/test_derived_visibility_sql.py
    brief: |
      Context: section 3.7 (second and third bullets) and 3.3 (matrix: `kh_provenance`).
      1. Write migration `0005_derived_visibility.sql`: function `kh.derived_visibility(source_ids text[])` returning `(n_resolved int, required_audiences text[], allowed_emails text[], versions text[])` exactly as 3.7 specifies (`SECURITY DEFINER`, owner `kh_provenance`, `search_path = kh, pg_temp`, a source counts when `kh.visible(...)` holds or, for `raw_records` only, when `kh.caller_type() = 'n8n_workflow' AND ingested_by_sub = kh.caller_sub()`; source ids are the prefixed forms `doc:<uuid>`, `raw:<uuid>`, `derived:<uuid>`; `EXECUTE` for `kh_app` only) and the deferred constraint trigger `derived_visibility_matches` (`AFTER INSERT` on `derived_records` only, `SECURITY DEFINER`, owner `kh_provenance`, `DEFERRABLE INITIALLY DEFERRED`) that recomputes the union and intersection over the snapshot columns of the row's `derived_sources` and raises when there are no sources or the stored visibility differs. `kh_app` also needs `INSERT` on `derived_sources`; confirm the policies of `0003` already allow it and stop with status=blocked if not.
      2. `visibility.py`: pure `derive_visibility(sources: list[Visibility]) -> Visibility` (union of audiences; intersection of non-null email lists; `None` when all are `None`; `[]` when the intersection is empty) and a `Visibility` dataclass.
      3. Tests: unit `test_visibility.py` (union, intersection, null versus empty, three sources); integration `test_derived_visibility_sql.py` (direct SQL as callers with `set_config`): union from a `{mgmt}` and a `{hr}` source needs a caller holding both; intersection of email lists; an unseen source lowers `n_resolved`; a missing uuid and a hidden uuid give identical results; `wf_token` resolves a raw record it ingested (attendee-only) and the returned `allowed_emails` equals the participants; `wf_token` does not resolve a raw record another token ingested; document versions are returned only for resolved documents; the provenance branch does not apply to documents or derived rows; the constraint trigger raises when a derived row is inserted with different visibility than its sources (insert through the sandbox superuser connection with `SET session_replication_role` left default), a derived row with no sources fails at commit, and the trigger does not fire on an `UPDATE` by `kh_indexer`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_visibility.py tests/integration/test_derived_visibility_sql.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p16b-write-derived
    title: "write_derived tool and the embed_derived job"
    depends_on: [p16a-derived-sql, p15-ingest-raw]
    complexity: M
    touches:
      - src/knowledge_hub/tools/derived.py
      - src/knowledge_hub/mcp/server.py
      - src/knowledge_hub/indexer/worker.py
      - tests/support/tool_cases.py
      - tests/integration/test_write_derived.py
    brief: |
      Context: section 3.7 (`write_derived` checks), 3.9.
      1. `tools/derived.py`: tool `write_derived` with the arguments of 3.9 implementing the checks of 3.7 in the stated order with the catalogue texts `BAD_ID`, `BAD_KIND`, `TOO_MANY_SOURCES`, `BAD_VERSION`, `DERIVED_SOURCES`, `VERSION_CHANGED`, `BAD_CLAIMS`; call `kh.derived_visibility` inside `caller_tx`; insert `derived_records` and then `derived_sources` (application uuid, no RETURNING, `versions` from the function); return `{"citation_id": "derived:<uuid>"}`. Register in `mcp/server.py`; add cases to `tool_cases.py`.
      2. `indexer/worker.py`: job kind `embed_derived` (chunk the body, embed, insert chunks).
      3. Tests (integration): a caller who cannot see a cited source gets `DERIVED_SOURCES`, identical for a nonexistent id; a hidden `doc:` id with a wrong `@version` also gives `DERIVED_SOURCES` and never the version; a correct `@version` on a visible document passes and a stale one gives `VERSION_CHANGED`; `@version` on a `raw:` id gives `BAD_VERSION`; `wf_token` can cite its own attendee-only raw record and the stored derived row has `allowed_emails` equal to the participants; `wf_token` cannot cite another token's raw record; `wf_token` cannot read the raw row afterwards; claims validation errors; after `run_once` the derived chunks exist with the row's visibility.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_write_derived.py tests/integration/test_leak_matrix.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p17-recompute-requeue
    title: "kh recompute-visibility and kh requeue-embeddings"
    depends_on: [p16b-write-derived, p13b-rebuild-check]
    complexity: M
    touches:
      - src/knowledge_hub/jobs/recompute.py
      - src/knowledge_hub/jobs/requeue.py
      - src/knowledge_hub/jobs/audience_map.py
      - src/knowledge_hub/cli.py
      - tests/integration/test_recompute.py
    brief: |
      Context: section 3.7 (last bullet).
      1. `jobs/recompute.py`: `recompute_visibility(indexer_pool, config)` runs in one transaction and recomputes, in this order: raw rows from the audience map (rules of the raw trigger; unmapped keeps participants); document visibility from `config.repos`; derived rows through `derived_sources` in dependency order (a derived row may cite another) with the union and intersection rules in SQL; candidates and conflicts through their source tables (each source contributes its live visibility when its row exists, otherwise its snapshot columns; refresh the snapshot columns from live rows); then records `repo_state.config_hash` under the pseudo repo `__config__`.
      2. `jobs/audience_map.py`: `load_audience_map` calls `recompute_visibility` in the same transaction when the stored hash differs from the new one.
      3. `jobs/requeue.py`: `requeue_embeddings(indexer_pool)` inserts `embed_raw` and `embed_derived` jobs for every raw and derived record that has no chunks.
      4. `cli.py`: wire `recompute-visibility` and `requeue-embeddings`.
      5. Tests (integration): after tightening the map (series moved from `all` to `mgmt`) the raw row, its chunks, a derived row citing it, a candidate and a conflict citing it all become `{mgmt}`; narrowing a repo audience in config narrows its documents and the derived rows that cite them; deleting a cited `{mgmt}` document and recomputing leaves the derived row, the candidate and the conflict at `{mgmt}` or narrower; running twice changes nothing; `check` reports a stale config hash before and clean after; `requeue_embeddings` queues jobs only for records without chunks.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_recompute.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p18-routing
    title: "Promotion routing and the refusal to widen"
    depends_on: [p16b-write-derived]
    complexity: S
    touches:
      - src/knowledge_hub/visibility.py
      - tests/unit/test_routing.py
    brief: |
      Context: section 3.8 (second bullet).
      1. In `visibility.py` add `route_target_repo(sources: list[Visibility], config, explicit_repo=None) -> str` implementing section 3.8 exactly with `KhError` texts `EMAIL_SOURCES`, `SPAN`, `NO_REPO`, `WIDER`.
      2. Tests (unit): `{all}` goes to `canonical-all`; `{mgmt}` to `canonical-mgmt`; `{all, mgmt}` is refused with `SPAN` without the flag and goes to `canonical-mgmt` with `restricted_audiences_imply_all: true`; `{mgmt, hr}` is refused; email-restricted is refused; an audience with no T0 repo gives `NO_REPO`; with an explicit repo `canonical-all` and sources `{mgmt}` gives `WIDER`; an explicit `canonical-mgmt` with sources `{all}` and no flag gives `WIDER`; an explicit `canonical-mgmt` with `U` empty is accepted.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_routing.py -q passes"
  - id: p19a-candidates-sql
    title: "Migration 0006: strict visibility for candidates and conflicts"
    depends_on: [p16a-derived-sql]
    complexity: S
    touches:
      - src/knowledge_hub/migrations/0006_strict_visibility.sql
      - tests/integration/test_strict_visibility.py
    brief: |
      Context: section 3.8 (first bullet).
      1. Write `0006_strict_visibility.sql`: `kh.strict_visibility(source_ids text[])` (`SECURITY INVOKER`, reads `documents`, `raw_records`, `derived_records` through the caller's RLS only) returning `(n_resolved, required_audiences, allowed_emails)` with the same union and intersection rules as `kh.derived_visibility` and no provenance branch; deferred constraint triggers `candidate_visibility_matches` (on `candidates`, reading `candidate_sources`) and `conflict_visibility_matches` (on `conflicts`, reading `conflict_items`), `AFTER INSERT`, `DEFERRABLE INITIALLY DEFERRED`, `SECURITY DEFINER` owned by `kh_provenance`, that recompute at commit over the snapshot columns and raise when there are no sources or the stored visibility differs; `EXECUTE` for `kh_app`.
      2. Tests (integration): union and intersection over visible sources; a hidden source lowers `n_resolved`; `wf_token` does not resolve a raw record it ingested but cannot see (no provenance branch); a candidate inserted with narrower visibility than its sources fails at commit; two callers with identical sources compute identical visibility; a `kh_indexer` visibility `UPDATE` does not fire the triggers.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_strict_visibility.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p19b1-forgejo-write
    title: "Forgejo write side"
    depends_on: [p9b-forgejo-client, p13a-wiring]
    complexity: S
    touches:
      - src/knowledge_hub/forgejo/client.py
      - tests/support/forgejo_fake.py
      - tests/unit/test_forgejo_write.py
    brief: |
      Context: section 3.8 (last sentences of the `propose_change` bullet).
      1. `forgejo/client.py`: write methods `create_branch(repo, name, from_sha)`, `put_file(repo, branch, path, content, message)`, `open_pull(repo, head, base, title, body, labels)`; each calls `_guard_ref` on the branch it writes, and `open_pull` also requires `base == "main"` and a head starting with `bot/promote/`. Add the read method `path_exists(repo, path, ref)`; it is a read and is not guarded.
      2. Extend `tests/support/forgejo_fake.py` with the write side (branches, files, pull requests, labels, and a record of every ref written so tests can assert nothing was written to `main`).
      3. Tests (unit): the guard refuses `main` and any other ref and accepts `bot/promote/x`; `open_pull` refuses a base other than `main` and a head outside `bot/promote/`; `path_exists` works on `main`; a Forgejo error carries no token.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_forgejo_write.py -q passes"
      - ".venv/bin/python -m ruff check src/knowledge_hub/forgejo tests/support/forgejo_fake.py passes"
  - id: p19b2-propose-change
    title: "propose_change, candidates and the daily limit"
    depends_on: [p19b1-forgejo-write, p18-routing, p19a-candidates-sql]
    complexity: M
    touches:
      - src/knowledge_hub/tools/promote.py
      - src/knowledge_hub/mcp/server.py
      - tests/support/tool_cases.py
      - tests/integration/test_propose_change.py
    brief: |
      Context: section 3.8 (the whole section) and 3.9.
      1. `tools/promote.py`: tool `propose_change` exactly as 3.8: 1 to 50 `source_ids`, path validation with `BAD_PATH` (also for a `target_id` document's path), routing through `route_target_repo`, dedupe key, daily limit with `LIMIT`, retry through `candidate_id` with `BAD_CANDIDATE`, candidate and `candidate_sources` inserts with visibility from `kh.strict_visibility`, `kh.candidate_mark` for every state change (`proposed` to `pr_open` or `failed`, `failed` to `pr_open` or `failed`), the PR body from the module constant `PR_TEMPLATE` with headings `## What`, `## Why`, `## Sources`, `## Candidate`, labels `promotion` and `domain/<domain>`, `PR_FAILED` on a Forgejo error. Register in `mcp/server.py` and add cases to `tool_cases.py`.
      2. Tests (integration, with the fake): routes `{mgmt}` sources to `canonical-mgmt` and `{all}` to `canonical-all`; refuses mixed audiences, email-restricted sources, an explicit wider repo and the paths `.forgejo/workflows/x.md`, `../x.md`, `CODEOWNERS`, `product/../x.md`, an existing path and a path outside the given domain; refuses 0 and 51 source ids; the second identical call returns `DUPLICATE`; the sixth call in a domain and repo on one day returns `LIMIT`; the PR carries both labels and the branch `bot/promote/<candidate-id>`; the fake records that nothing was written to `main`; a caller cannot see a candidate whose sources it cannot see; a failed Forgejo call leaves a `failed` candidate that a retry with `candidate_id` completes without a dedupe collision; a retry by another `sub` gives `BAD_CANDIDATE`; `kh_app` cannot update a candidate except through `kh.candidate_mark`.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_propose_change.py tests/integration/test_leak_matrix.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p20-conflicts-stale
    title: "flag_conflict, list_conflicts and list_stale"
    depends_on: [p19b2-propose-change, p13b-rebuild-check]
    complexity: M
    touches:
      - src/knowledge_hub/tools/conflicts.py
      - src/knowledge_hub/tools/docs.py
      - src/knowledge_hub/mcp/server.py
      - tests/support/tool_cases.py
      - tests/integration/test_conflicts.py
      - tests/integration/test_list_stale.py
    brief: |
      Context: sections 3.4 (conflicts), 3.8 (visibility), 3.9.
      1. `tools/conflicts.py`: `flag_conflict(a, b, note)`: both ids resolved under strict RLS (`NOT_FOUND` otherwise, identical for missing and hidden), `a != b`, note 1 to 2000 characters; visibility from `kh.strict_visibility([a, b])`; insert `conflicts` and `conflict_items` with application uuids and no RETURNING. `list_conflicts(domain, status, limit, cursor)` uses a `(created_at, id)` keyset cursor over visible rows.
      2. `tools/docs.py`: add `list_stale(domain, owner, limit, cursor)` returning visible documents where `stale` is true or `review_by < current_date`, oldest `review_by` first, `(review_by, id)` keyset cursor; it never counts or mentions hidden rows.
      3. Register both in `mcp/server.py`; append cases to `tool_cases.py`.
      4. Tests (integration): a conflict between a `{mgmt}` and a `{hr}` item is visible only to callers holding both; a caller cannot flag a conflict on an item it cannot see; `list_conflicts` filters by domain and status; `list_stale` returns only visible overdue documents for each principal and the page contents never change with hidden rows.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_conflicts.py tests/integration/test_list_stale.py tests/integration/test_leak_matrix.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p21-pr-outcomes
    title: "Webhook outcomes: candidate promotion, rejection and conflict closing"
    depends_on: [p19b2-propose-change, p12-webhook-worker]
    complexity: M
    touches:
      - src/knowledge_hub/indexer/worker.py
      - tests/integration/test_pr_outcomes.py
    brief: |
      Context: section 3.8 (PR events).
      1. In `worker.py` handle `pr_event` jobs: re-fetch the PR with `forgejo.pull(repo, number)`; ignore a PR whose head branch does not start with `bot/promote/`; the candidate id is the uuid after `bot/promote/`; require the PR's repo and number to equal the candidate's `target_repo` and `pr_number` (else record the job done with the alert `pull request <repo>#<n> does not match candidate <id>`); when merged set the candidate to `promoted` (record `decided_at`) and set every open `conflicts` row that has a `conflict_items` entry citing one of the candidate's sources to `closed` with `closed_by_pr`; when closed unmerged set `rejected`. Re-running the same event changes nothing.
      2. Tests (integration): merged PR promotes and closes the related conflicts and leaves unrelated ones open; closed-unmerged rejects; a PR from another branch is ignored; a PR whose number differs from the candidate's is ignored with an alert; the same event twice is a no-op; a PR event for an unknown candidate is recorded as done without error.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_pr_outcomes.py -q passes"
  - id: p22a-grist-daily
    title: "Grist client and the daily job"
    depends_on: [p20-conflicts-stale, p21-pr-outcomes, p17-recompute-requeue]
    complexity: M
    touches:
      - src/knowledge_hub/grist/__init__.py
      - src/knowledge_hub/grist/client.py
      - src/knowledge_hub/jobs/daily.py
      - src/knowledge_hub/cli.py
      - tests/unit/test_grist_client.py
      - tests/integration/test_daily.py
    brief: |
      Context: section 3.12. The Grist document `kh-entities` has tables `Products`, `Components`, `Versions`, `Customers`, each with columns `key` (`prd:gateway`), `name`, `owner_team`, `status`, `links`.
      1. `grist/client.py`: `GristClient(url, doc_id, key)` with `fetch_table(name) -> list[dict]` over `GET /api/docs/<doc>/tables/<name>/records` with the bearer key; fields come from the `fields` object of each record.
      2. `jobs/daily.py`: `run_daily(indexer_pool, grist, config, today)` exactly as 3.12 (stale flags, entity upsert and delete with visibility from `config.entities`, expired `claim_jti` purge); idempotent. `cli.py`: wire `daily` (build the Grist client from settings; exit 2 with a clear message when `KH_GRIST_URL` is unset).
      3. Tests: unit for the Grist client with `httpx.MockTransport`; integration: daily marks the stale flag and a stale document ranks below a current one in search; entity rows of kind `cus` are invisible to a caller without `contracts`; a removed entity disappears; expired jti rows are purged; rerun changes nothing.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_grist_client.py tests/integration/test_daily.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p22b-entities-tools
    title: "get_entity and list_entities"
    depends_on: [p22a-grist-daily]
    complexity: S
    touches:
      - src/knowledge_hub/tools/entities.py
      - src/knowledge_hub/mcp/server.py
      - tests/support/tool_cases.py
      - tests/integration/test_entity_tools.py
    brief: |
      Context: sections 3.9 and 3.12.
      1. `tools/entities.py`: `get_entity(id)` returns the cache row plus the visible documents whose `entities` contain the id (under RLS); `list_entities(type, status, owner_team, limit, cursor)` over visible rows with an `(id)` keyset cursor; `type` must be one of `prd`, `cmp`, `ver`, `cus` (else `BAD_ID`-style error `type must be one of prd, cmp, ver, cus`, defined as a local constant in this module). Register both and add cases to `tool_cases.py`.
      2. Tests (integration): a `cus:` entity is `not found` for callers without `contracts`; `get_entity` lists only visible documents; `list_entities` pagination is stable and never counts hidden rows.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_entity_tools.py tests/integration/test_leak_matrix.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p23-hardening
    title: "Rate limits and metrics"
    depends_on: [p22b-entities-tools]
    complexity: M
    touches:
      - src/knowledge_hub/ratelimit.py
      - src/knowledge_hub/deps.py
      - src/knowledge_hub/metrics.py
      - src/knowledge_hub/app.py
      - src/knowledge_hub/mcp/errors.py
      - src/knowledge_hub/auth/claims.py
      - src/knowledge_hub/indexer/webhook.py
      - tests/unit/test_ratelimit.py
      - tests/integration/test_metrics.py
    brief: |
      Context: milestone 5; sections 3.1 (`KH_RATE_LIMIT_PER_MIN`, `KH_METRICS_TOKEN_FILE`) and 3.2.
      1. `ratelimit.py`: in-process token bucket keyed by claim `sub` (`allow(sub, now) -> (bool, retry_seconds)`, capacity and refill from `KH_RATE_LIMIT_PER_MIN`; one `RateLimiter` lives on `Deps.ratelimiter`, created in `build_deps` in `deps.py`); `tool_guard` in `mcp/errors.py` calls it and raises `KhError(RATE_LIMIT)` (add the constant `RATE_LIMIT` in this phase).
      2. `metrics.py`: a tiny Prometheus text exposition (no new dependency): counters `kh_tool_calls_total{tool,outcome}`, `kh_claim_rejections_total{code}`, `kh_webhook_events_total{result}`, gauge `kh_index_jobs{state}`, and `kh_tool_seconds_sum` and `kh_tool_seconds_count{tool}`. Labels never contain a sub, audience, email or document id. Increment the rejection counter in `auth/claims.py` and the webhook counter in `indexer/webhook.py`. Route `GET /metrics` in `app.py`, present only when `KH_METRICS_TOKEN_FILE` is set and answering only requests with `Authorization: Bearer <that token>` (else 401).
      3. Tests: unit for the bucket (refill, burst, per-sub isolation); integration: one tool call increments the counters, a rejected claim increments `kh_claim_rejections_total`, `/metrics` without or with a wrong token is 401, the route is absent when no token file is configured, and the metrics text contains no email, no uuid and no audience name.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_ratelimit.py tests/integration/test_metrics.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p24-deploy-backup
    title: "arm64 Dockerfile, backup script and example configuration"
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
      1. `Dockerfile`: multi-stage on `python:3.13-slim` (build the wheel in the first stage, install into the second), non-root user `kh` (uid 10001), no build tools in the final stage, `ENTRYPOINT ["kh"]`, `CMD ["serve"]`, `EXPOSE 8080`, no secrets, no `latest` tag, no architecture-specific lines. `.dockerignore` excludes `.git`, `.venv`, `tests`, `docs`.
      2. `deploy/backup-kh.sh`: `set -euo pipefail`; runs as role `kh_migrator` (its policy lets `pg_dump` read FORCE-RLS tables) with the password from `KH_DB_MIGRATOR_PASSWORD_FILE`; `pg_dump -Fc --no-owner --enable-row-security --inserts` of only the T2/T3 tables (`raw_records`, `raw_audience_map`, `derived_records`, `derived_sources`, `candidates`, `candidate_sources`, `conflicts`, `conflict_items`) with `-t` flags into `${BACKUP_DIR}/kh-t2t3-<date>.dump`, then mirrors the bucket with `aws s3 sync --endpoint-url "$KH_S3_ENDPOINT" s3://$KH_S3_BUCKET "${BACKUP_DIR}/blobs/"`; prints a final line `backup ok <date>`; comments state that git-derived tables are rebuilt with `kh rebuild` and that after a restore `kh requeue-embeddings` rebuilds the chunks.
      3. `deploy/kh.example.yaml` (a complete `kh.yaml` per section 3.1 with the default repos) and `deploy/compose.example.yaml` (services `knowledge-hub` and `ollama` on the networks `db` and `kh`, env `KH_*` with `_FILE` secrets, hardening options, healthcheck on `/healthz`).
      4. `tests/unit/test_deploy_files.py` (unit): the example config loads with `load_config`; the Dockerfile has a `USER` other than root, no `latest`, and `ENTRYPOINT`; `bash -n deploy/backup-kh.sh` passes; the script names every T2/T3 table, contains `--enable-row-security` and `--inserts` and does not name `documents` or `chunks`; the compose example names no secret value.
      Stop: the sandbox has no Docker daemon, so the image build is not run here (manual step ma5); do not add a skip for it.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_deploy_files.py -q passes"
      - "bash -n deploy/backup-kh.sh exits 0"
  - id: p25a-docs-design
    title: "Retirement part 1: design and configuration documents"
    depends_on: [p24-deploy-backup, p17-recompute-requeue, p21-pr-outcomes]
    complexity: M
    touches:
      - docs/design.md
      - docs/configuration.md
    brief: |
      Context: the whole plan; this phase only writes documentation, from sections 3.1 to 3.12 and the code as built (read the code and update any name that changed during implementation).
      1. `docs/design.md` (at most 250 lines): the decisions of section 3 as built, with the rejected alternatives of 3.11 and the reasons (roles and RLS matrix, claim middleware and call binding, trust walk and anchor rules, provenance exception, promotion routing and path validation, exact vector scans, S3 from day one, indexed `get_doc`), and a section "Deviation from the PrivacyFence contract" explaining that the hub uses the claim's `email` for visibility although `docs/identity-claim.md` calls it audit-only, and what the owner should ask PrivacyFence to bless.
      2. `docs/configuration.md` (at most 200 lines): a table of every `KH_*` setting with default and meaning (read `settings.py`), and the `kh.yaml` keys with an example (read `config.py` and `deploy/kh.example.yaml`).
      3. Each document starts with `# Title` and a summary of at most three lines.
    acceptance:
      - "grep -n 'Deviation from the PrivacyFence contract' docs/design.md matches"
      - "grep -n 'restricted_audiences_imply_all' docs/configuration.md matches"
      - ".venv/bin/python -m pytest -q passes"
  - id: p25b-docs-ops
    title: "Retirement part 2: README, runbook and the docs link test"
    depends_on: [p25a-docs-design]
    complexity: M
    touches:
      - README.md
      - docs/runbook.md
      - tests/unit/test_docs_links.py
    brief: |
      Context: the whole plan; documentation only plus one test.
      1. `README.md` (at most 80 lines): what the service is, the four tiers, the audience model in five sentences, quick start (`bash scripts/ensure_test_pg.sh`, create the venv as the Python stack guide says, run the tests), the `kh` commands (`serve`, `migrate`, `rebuild`, `check`, `daily`, `recompute-visibility`, `requeue-embeddings`), and relative links to `docs/design.md`, `docs/configuration.md` and `docs/runbook.md`.
      2. `docs/runbook.md` (at most 250 lines): install order (roles and database, secrets, `kh migrate`, `kh serve`, Ollama), routine tasks (`kh rebuild`, `kh check`, `kh daily`, `kh recompute-visibility` after editing the audience map or repo audiences), what to do for each alert text in `index_alerts` (disputed file, untrusted repo, rejected frontmatter, rewritten main, anchor not on main), restoring T2/T3 from `deploy/backup-kh.sh` output as the Postgres superuser with `pg_restore --data-only --disable-triggers`, then `kh recompute-visibility` and `kh requeue-embeddings`, rotating a webhook secret and a Forgejo token, the PrivacyFence follow-ups (scoped tokens calling gated tools; blessing email matching), and the accepted risk that an n8n token chooses participants of unmapped records.
      3. `tests/unit/test_docs_links.py` (unit): every relative markdown link in `README.md` and `docs/*.md` resolves to an existing file, and no markdown file in the repository outside `docs/plans/` has more than 400 lines.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_docs_links.py -q passes"
      - "grep -c 'kh rebuild' docs/runbook.md prints a number of at least 1"
      - ".venv/bin/python -m pytest -q passes"
```
