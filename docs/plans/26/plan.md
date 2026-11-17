# Plan: Two tiers (T0 and T3), group access, Postgres labels, a setup script

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** The hub has two tiers. T0 is Markdown in Forgejo repos, one repo per group; a change reaches `main` only through a pull request that a member of the group's approver list merges (Claude, n8n or a person opens it; a person may merge their own). T3 is records that n8n or Claude put in with `ingest_record`, each with its own audience group. Everyone reads what their PrivacyFence claim's groups allow. An operator writes one `kh.yaml` (groups, members, approvers, labels) and runs `kh setup plan` then `kh setup apply` to create Postgres roles and tables, labels, Forgejo teams, repos, branch protection and the webhook, and the Ollama model; adding a group later is an edit and a re-run.

**Scope:** The hub is rewritten around two tiers. Removed: T1 notes, T2 derived records, candidates, conflicts, promotion, domains, entities and Grist, `owners.yml` and the governance CI gates, the trust walk (anchors, verdicts, stamps), the requester record, emails and any-of audiences, `raw_audience_map`. Kept: claim verification, RLS, chunking, embeddings, search, S3 bodies. Added: `kh setup`, labels, `propose_doc`, `ingest_record`, `list_labels`, a wheel attached to the GitHub release. A fresh install only; no data migration.

**Security:** T0 trust now rests entirely on Forgejo branch protection and team membership, so `kh setup plan` checks them (3.7). One bot account writes every T0 pull request and every T3 record. The hub never holds admin credentials; `kh setup` does.

**Performance:** No measurable change expected; search keeps its exact vector scan, filtered by one `audience` column.

**Review focus:** forge-only trust and what `plan` must verify (3.2, 3.7); the group-to-audience authority split between PrivacyFence and `kh.yaml` (3.3); the delete-first phase order (7, p1 to p3); the protected `release.yml` change (3.10).

## 1. Goal

Replace the four-tier, domain-and-audience-clause design with two tiers and one flat access model, as asked in [#26](https://github.com/tkcs-lab/knowledge-hub/issues/26): T0 canonical and human-approved, T3 generic raw records; groups instead of domains; labels in Postgres instead of Grist entity types; and a released setup script next to the Docker image that builds and later changes the whole environment from one config file. Where a rewrite is simpler than a refactor, the old component is deleted.

## 2. Current state

- 11.1k source lines under `src/knowledge_hub`, 15 MCP tools registered in `mcp/server.py:32`, nine migrations (`0001` to `0009`), four DB roles including `kh_provenance`.
- Visibility is a set of any-of clauses plus `allowed_emails` (`migrations/0007_any_of_audiences.sql`, `visibility.py`), assigned to raw rows by a trigger from `raw_audience_map` or participants.
- T0 trust is a first-parent walk with anchors, review verdicts, self-merge by `owners.yml` and a requester record (`indexer/trust.py`, `indexer/sync_plan.py`, `merge_rule.py`, `merge_io.py`, `approvals.py`, `proposals.py`).
- Agent writes use `kh.proposals`, three Forgejo client roles and the `kh/requested-by` status (`agent/`, `tools/agent_*.py`, `forgejo/client.py`).
- Entities come from Grist (`grist/`, `jobs/daily.py`); governance CI gates are `kh validate` and `kh domain-owners` (`governance/`).
- Raw bodies live in S3 (`blobs/s3.py`); `raw_records` holds a blob key and a 2000-character excerpt.
- Release builds only a GHCR image (`.github/workflows/release.yml`); there is no installer.
- Module-by-module map of keep, edit and delete was produced for this plan; its verdicts are applied in the phases below.

## 3. Design

### 3.1 Vocabulary and tiers

- **Group**: a name matching `^[a-z0-9][a-z0-9_-]{0,62}$`. It is both the audience name in the PrivacyFence claim and the name of a T0 repo's access group. Every group has exactly one T0 repo.
- **T0**: a row of `kh.documents`, from a `*.md` file on `main` of a group's repo. **T3**: a row of `kh.records`, body in S3. Tier weights `T0:1.0,T3:0.4`.
- **Label**: a name matching `^[a-z0-9][a-z0-9_.:-]{0,62}$` (so `product:ember` is legal) defined in `kh.yaml` and stored in `kh.labels`. Labels replace entity types. Label names are visible to every caller; do not put secrets in them.
- Ids returned by tools: `doc:<uuid>` and `rec:<uuid>`.

### 3.2 Trust: Forgejo is the control (owner decision `trust: forge-only`)

The indexer reads the tree of `main` as Forgejo reports it. No trust walk, no anchor, no verdicts, no stamps, no `approved_by`, no `disputed` status, no `document_versions`, no `supersedes`. The approval is the merge: branch protection forbids direct push, the merge whitelist is the group's approver team, required approvals are 0, so an approver may merge their own pull request (owner decision `requester: yes`). Forgejo's merge API takes `head_commit_id` and answers 409 when the head moved, so an approver's UI merge is bound to the head they saw (research r-20261117-1, https://codeberg.org/forgejo/forgejo/raw/branch/forgejo/routers/api/v1/repo/pull.go, 2026-11-17). Rejected: a formal-review walk (closest to today, much more code), and a trust walk by merger (still needs a walk and verdict storage). One cheap check stays: the indexer alerts `main of <repo> was rewritten` when the new head does not descend from `repo_state.last_indexed_sha`.

### 3.3 Access model

- The caller's audiences are the claim's `audiences` (unchanged verifier). `kh.can_read(a)` is `a = ANY(kh.caller_audiences())`. A document or chunk is visible when its `audience` is held; a record likewise; inserting a record needs the audience too.
- **Authority split.** PrivacyFence's audience mapping decides what a caller reads through the hub; `kh.yaml` decides Forgejo team membership. The hub cannot reconcile them (emails are gone). `kh setup plan` prints, per group, the audience name PrivacyFence must emit, and the runbook says "joiners and leavers: change both". A user in the config but not mapped reads nothing; a user mapped but not in the config reads through the hub but cannot open pull requests.
- **Accepted residuals** (documented in `docs/design.md`): a caller holding two groups can `ingest_record` or `propose_doc` into the wider one, the PrivacyFence popup shows `audience` and `group` and is the only gate; a leaked bot token can create branches and pull requests and push to open pull-request branches in every repo and read every T0 repo, but cannot merge; with required approvals 0 the approver must review the final diff.

### 3.4 Config file `kh.yaml`

One file for the hub and for `kh setup` (unknown keys refused, errors start `config:`):

```yaml
org: kh                       # Forgejo organisation (replaces KH_FORGEJO_ORG)
bot_login: kh-bot             # writes every T0 PR and stamps every T3 record
indexer_login: kh-indexer     # read-only token owner
forgejo_web_url: null
groups:
  all:
    repo: kb-all              # default: kb-<group>
    members: [alice, bob]
    approvers: [alice]        # non-empty subset of members
  mgmt:
    members: [alice]
    approvers: [alice]
labels:
  product:ember: Ember product
```

Validation (class `KhConfig`, `knowledge_hub/config.py`, pure, no I/O): group names match the group regex, are not `owners` in any case, and `<name>-approve` collides with no group; logins match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}$`, unique per list after case-folding; `approvers` non-empty and a subset of `members` (error `config: group <g> approver <login> is not a member`); repo names match `^[a-z0-9][a-z0-9._-]{0,99}$`, unique; `bot_login` and `indexer_login` differ and appear in no member list; labels match the label regex, description at most 200 characters, at most 500 labels; `forgejo_web_url` null or https. Derived Forgejo teams: `<group>` (write on its repo) and `<group>-approve` (read on its repo, in the merge whitelist). Rejected: A1 `repos:` map with a separate approve group (lets an approve group become an audience with no repo).

### 3.5 Schema (single migration `0001_baseline.sql`)

Fresh install only. `db/migrate.py` refuses to run when `kh.schema_migrations` holds a name the package does not ship: `database predates the baseline; a fresh install is required`. Roles `kh_migrator`, `kh_app`, `kh_indexer` (no `kh_provenance`), all NOSUPERUSER NOBYPASSRLS, created outside migrations. Every table has ENABLE and FORCE RLS and a `kh_migrator_all` policy (`USING (true) WITH CHECK (true)`).

| Table | Columns (all NOT NULL unless noted) | kh_app | kh_indexer |
|---|---|---|---|
| `schema_migrations`, `claim_jti` | copy of old `0001` | as old | as old |
| `groups` | `name` PK (group regex), `repo` UNIQUE | SELECT | SELECT |
| `labels` | `name` PK (label regex), `description` default `''`, `created_at` | SELECT | SELECT |
| `documents` | `id` uuid PK default `gen_random_uuid()`, `repo`, `path`, `doc_key`, `audience` FK `groups(name)`, `title`, `body`, `labels text[]` default `{}`, `review_by date`, `stale bool` default false, `commit_sha`, `blob_sha`, `indexed_at`, `updated_at`; UNIQUE `(repo,doc_key)`, `(repo,path)` | SELECT where `can_read(audience)` | ALL |
| `records` | `id` uuid PK, `audience` FK `groups(name)`, `source`, `source_ref`, `source_url` default `''`, `title`, `blob_key`, `excerpt`, `content_type`, `occurred_at timestamptz`, `labels text[]`, `written_by`, `created_by_sub`, `created_by_type`, `ingested_at`; UNIQUE `(audience,source,source_ref)` | SELECT where `can_read(audience)`; INSERT with check `can_read(audience) AND created_by_sub = kh.caller_sub()`; no UPDATE or DELETE | SELECT |
| `chunks` | `id`, exactly one of `document_id` FK cascade or `record_id` FK cascade (CHECK), `owner_key`, `owner_created_at`, `ordinal`, `heading_path`, `text`, `content_sha`, `embedding vector(1024)` null, `embedding_model` null, `tsv` generated english, `tier` CHECK in (`T0`,`T3`), `audience`, `labels text[]`, `stale`; indexes `owner_key`, GIN `tsv` | SELECT where `can_read(audience)` | ALL |
| `index_jobs` | `id` identity, `kind` CHECK in (`git_push`,`embed_record`), `repo` null, `payload jsonb`, `state`, `attempts`, `last_error`, `created_at`, `run_after` | none | ALL |
| `repo_state` | `repo` PK, `last_indexed_sha`, `indexed_at` | none | ALL |
| `index_alerts` | `id` identity, `repo`, `path`, `message`, `created_at` | none | ALL |

Functions and triggers: `kh.caller_audiences()`, `kh.caller_sub()`, `kh.caller_type()` (no `kh.email`); `kh.can_read(text)`; `kh.claim_seen(jti, expires_at)` (definer, copy); `chunks_inherit_owner` (BEFORE INSERT, copies tier, audience, labels, stale, owner key and created time from the owner row); `owner_propagate_to_chunks` (AFTER UPDATE of audience, labels, stale on `documents`); `records_enqueue_embed` (AFTER INSERT on `records`, inserts `index_jobs(kind='embed_record', payload={"id": <uuid>})`); `kh.assert_labels_exist(text[])` raising SQLSTATE `P0K05` used by BEFORE INSERT OR UPDATE OF labels triggers on `documents` and `records`; a BEFORE DELETE trigger on `labels` raising `P0K06` while any document or record uses the label (definer function owned by `kh_migrator`). Dedupe: re-ingesting the same `(audience, source, source_ref)` returns the existing row's id through a SELECT under RLS, safe because inserting already requires the audience (this avoids revealing records of other audiences; research: architect finding 4). Rejected: label join tables (arrays plus the two triggers give the same guard with far less SQL); a global unique `(source, source_ref)` (leaks existence).

### 3.6 Indexer (T0)

- Webhook: one org-level hook (push events only) to `POST /webhooks/forgejo`. The repo name comes from `repository.full_name`, falling back to `repository.owner.login + "/" + repository.name` (full_name was not confirmed in research r-20261117-1). HMAC check unchanged; only `refs/heads/main` of a repo listed under a group queues a `git_push` job.
- `sync_repo`: take `pg_advisory_xact_lock(hashtext(repo))`; list the tree of `main` HEAD (path to blob sha); diff against `kh.documents` rows of the repo by `(path, blob_sha)`; files rejected earlier are retried every push because they are not stored. Included files: `*.md`, no dot-prefixed path segment, not the root `README.md`, at most 1 MiB. Fetch changed blobs, parse, reject on error with an `index_alerts` row, then in one transaction delete vanished rows, then upsert, then chunk. `commit_sha` is the head sha; `audience` is the repo's group, always.
- Frontmatter: required `id` (`^[a-z0-9][a-z0-9._-]{0,99}$`), `title`, `review_by` (`YYYY-MM-DD`); optional `labels` (list of strings, each must exist in `kh.labels`, else the file is rejected `unknown label <x>`); other keys are ignored. A duplicate `id` in one repo rejects every file that holds it. `stale` is `review_by < today`, recomputed by `kh daily`.
- Embedding: unchanged providers (`ollama`, `http`, `fake`); `may_embed` becomes "audience in `KH_EMBED_HTTP_ALLOWED_AUDIENCES`" for the http provider. T3 bodies are read from S3 by `embed_record` jobs.
- CLI: `kh serve`, `kh migrate`, `kh rebuild [--repo R]`, `kh check`, `kh daily`, `kh requeue-embeddings`, `kh lint FILE...` (offline frontmatter check, exit 1 on a problem), `kh setup ...`. Gone: `validate`, `domain-owners`, `governance`, `recompute-visibility`, `--rejudge`.

### 3.7 `kh setup`

`kh setup plan|apply --config kh.yaml [--only postgres|labels|forgejo|ollama|s3] [--prune]`. `plan` is strictly read-only and exits 0 (no drift), 1 (drift, one line each) or 2 (error); run it on a schedule to catch protection drift (`kh setup plan` in cron, non-zero pages someone). `apply` exits 0 or 2 and is idempotent. Printed line prefixes: `+ create ...`, `~ update ...`, `- remove ...` (only with `--prune`), `? unmanaged ...`, `! refused ...`. Credentials come from files, never the YAML: `KH_SETUP_PG_ADMIN_DSN_FILE` (optional), `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE`, plus the existing `KH_DB_*_PASSWORD_FILE`, `KH_FORGEJO_URL`, `KH_FORGEJO_BOT_TOKEN_FILE`, `KH_FORGEJO_INDEXER_TOKEN_FILE`, `KH_WEBHOOK_HMAC_FILE`, `KH_OLLAMA_URL`, `KH_EMBED_MODEL`, `KH_S3_*`. The `kh serve` service never mounts the two `KH_SETUP_*` files. Setup never creates Forgejo users or tokens; a missing `bot_login` or `indexer_login` account is an error.

Steps, in order (one module each under `knowledge_hub/setup/`, each a `Step` with `plan() -> list[Change]` and `apply(prune: bool) -> list[Change]`):

1. **postgres** with an admin DSN: create the three roles (passwords from the `KH_DB_*_PASSWORD_FILE` files, always re-set on apply because they cannot be read back), the database owned by `kh_migrator`, `CREATE EXTENSION vector`; then run pending migrations as `kh_migrator`. Without an admin DSN: check only (roles, database, extension exist; pending migrations are drift). Then sync `kh.groups` and the group-owned documents index (groups added; groups removed only with `--prune`, refused when records exist, and their documents are deleted).
2. **labels**: insert new, update descriptions; a removed label is deleted only with `--prune` and only when unused (the `P0K06` trigger refuses; the line says `! refused label <x> in use`). After adding a label apply inserts one `git_push` job per repo so files rejected for an unknown label are retried.
3. **forgejo** (admin token): per group: team `<group>` (permission write, `includes_all_repositories` false, `can_create_org_repo` false, members exactly the config) and team `<group>-approve` (permission read on the repo, members exactly the approvers); the repo (private, default branch `main`, created with `auto_init` so `main` exists; Actions disabled, `default_delete_branch_after_merge` true); collaborators: bot write, indexer read; branch protection on `main` through `rule_name: main`, `enable_push: false`, `enable_push_whitelist: false`, `enable_merge_whitelist: true`, `merge_whitelist_teams: ["<group>-approve"]`, `required_approvals: 0`, `block_on_rejected_reviews: true`, `apply_to_admins: true` (field names from research r-20261117-1, https://codeberg.org/forgejo/forgejo/raw/branch/forgejo/modules/structs/repo_branch.go, 2026-11-17; `branch_name` is deprecated). Forgejo silently drops whitelist teams without read access to the repo (https://codeberg.org/forgejo/forgejo/raw/branch/forgejo/models/git/protected_branch.go, 2026-11-17), so the approve team is added to the repo first and apply re-reads the protection after writing and reports any difference as drift. One org-level webhook (push) with the secret from `KH_WEBHOOK_HMAC_FILE`, rewritten on every apply (Forgejo never returns it; plan says `webhook secret not verifiable`). Identity checks when the bot and indexer token files are set: `GET /user` equals the configured login; the bot has no repo admin permission, the indexer no push; neither is an org owner or site admin. Report-only line listing members of the Owners team as people who can bypass protection. Unmanaged teams and repos print `? unmanaged`; `--prune` removes unmanaged teams only; repos are never deleted. A group's repo is immutable: a changed `repo` is `! refused group <g> changed repo; remove the group and add a new one`.
4. **ollama**: `plan` checks the model is listed and that one embedding has length `KH_EMBED_DIM` (1024 in the schema); `apply` pulls it (`POST /api/pull`, stream off).
5. **s3**: check only by default (bucket exists; put, get and delete of `kh-setup-probe/<uuid>`); `apply --create-bucket` also creates a missing bucket (Garage often forbids this to ordinary keys).

`plan` ends with the audience list line per group (`audience all`, ...) for PrivacyFence (3.3).

### 3.8 MCP tools (six)

All take flat arguments (`str`, `int`, `bool`, `list[str]`, `Literal`), none named `reason` (`tests/unit/test_tool_schemas.py`). Reads are `auto`, writes `popup` in the PrivacyFence declaration, with `allowed_principal_types: [claude_client, n8n_workflow]` and no `email_for_visibility`.

| Tool | Signature | Result |
|---|---|---|
| `search` | `query: str, min_tier: Literal["T0","T3"]="T0", labels: list[str]=[], audience: str="", limit: int=10 (1..50), cursor: str=""` | `{hits: [{id, tier, title, audience, labels, repo, path, source_url, occurred_at, blob_sha, stale, score, snippet}], next_cursor, extra: {t3_hits}}`; `labels` is all-of; with `min_tier="T0"` only T0 is searched and, when no T0 hit exists but T3 hits do, the call fails with the `NORMATIVE` text |
| `get_doc` | `id: str` | `doc:` returns `{id, tier, repo, path, audience, labels, title, body, review_by, stale, commit_sha, blob_sha, source_link}`; `rec:` returns `{id, tier, audience, labels, title, body (from S3), source, source_ref, source_url, occurred_at, written_by}` |
| `list_stale` | `audience: str="", limit: int=50 (1..50), cursor: str=""` | documents with `stale` true, oldest `review_by` first |
| `list_labels` | none | `[{name, description}]` |
| `ingest_record` | `source: str, source_ref: str, title: str, body: str, audience: str, occurred_at: str (ISO 8601), labels: list[str]=[], source_url: str="", content_type: str="text/plain"` | `{id, existed: bool}`; refuses unless the caller holds `audience` and it is a configured group, or when the body exceeds `KH_MAX_RECORD_BYTES` (1048576), or a label is unknown; stores the body at S3 key `records/<uuid>`, `excerpt` the first 2000 characters, `written_by` the config's `bot_login`, `created_by_sub` and type from the caller |
| `propose_doc` | `group: str, path: str, id: str, title: str, review_by: str, body: str, labels: list[str]=[], summary: str="", base_blob: str=""` | `{pr_url, pr_number, branch}` |

`propose_doc` rules: caller holds `group` and the group's repo exists; `path` matches `^[a-z0-9][a-z0-9/_-]*\.md$` with no `..`, `//` or dot-segment and is not `README.md`; `base_blob` empty means the path must not exist, otherwise it is passed as `sha` to Forgejo's contents API so Forgejo enforces the race; `id` must not already exist at another path in the repo; text is refused if it holds control characters other than tab and newline, bidirectional controls, HTML comments or tags, or exceeds `KH_PROPOSE_MAX_BYTES` (131072); labels must exist; the bot lists its open pull requests in the repo and refuses at `KH_PROPOSE_MAX_OPEN` (20). The hub renders the file itself (frontmatter keys in the order `id`, `title`, `review_by`, `labels`, strings JSON-quoted, then `---` and the body), parses it back with the indexer's parser and refuses on mismatch, creates branch `bot/propose/<uuid4>` from the head of `main`, puts the one file, opens a pull request to `main` titled `Propose: <title>` (at most 120 characters) whose body is `summary` plus the lines `Requested-by: <sub>`, `Principal: <type>`, `Written-by: <bot_login>` (information only, not enforced). The bot client can only write branches starting `bot/` and open pull requests with base `main` (code guard, kept from today's design).

Fixed error texts (module `mcp/errors.py`, one constant each): `NOT_FOUND` "not found", `INTERNAL` "internal error (ref {ref})", `RATE_LIMIT` (unchanged), `CURSOR_FOREIGN` (unchanged), `NORMATIVE` "only T3 records match; repeat with min_tier T3 to see them", `BAD_ID` "id must be doc:<uuid> or rec:<uuid>", `BAD_LABEL` "unknown label", `BAD_ARG` "argument <name> is invalid", `AUDIENCE_NOT_HELD` "audience is not held by this caller or not configured", `TOO_LARGE` "body is too large", `BAD_TEXT` "text contains forbidden characters", `PATH_EXISTS` "path already exists; pass base_blob to edit it", `BLOB_CHANGED` "the file changed since base_blob", `ID_IN_USE` "id is used by another file in this repo", `TOO_MANY_OPEN` "too many open proposals in this repo", `NOT_CONFIGURED` "writes are not configured", `PROPOSE_FAILED` "could not open the pull request". Obsolete constants are deleted.

### 3.9 Settings

Kept: `KH_ENV`, `KH_DEV_IDENTITY`, `KH_PF_ISSUER/AUDIENCE/JWKS_URL`, `KH_ALLOWED_HOSTS`, `KH_MAX_BODY_BYTES`, `KH_DB_*`, `KH_CONFIG_FILE`, `KH_FORGEJO_URL`, `KH_FORGEJO_BOT_TOKEN_FILE`, `KH_FORGEJO_INDEXER_TOKEN_FILE`, `KH_WEBHOOK_HMAC_FILE`, `KH_EMBED_*`, `KH_OLLAMA_URL`, `KH_S3_*`, `KH_CURSOR_KEY_FILE`, `KH_CHUNKER_VERSION`, `KH_RRF_K`, `KH_SEARCH_POOL`, `KH_STALE_WEIGHT`, `KH_SEARCH_MAX_OFFSET`, `KH_RATE_LIMIT_PER_MIN`, `KH_METRICS_TOKEN_FILE`. Changed: `KH_TIER_WEIGHTS` default `T0:1.0,T3:0.4`; `KH_MAX_RAW_BYTES` becomes `KH_MAX_RECORD_BYTES`. New: `KH_PROPOSE_MAX_BYTES`, `KH_PROPOSE_MAX_OPEN`, `KH_SETUP_PG_ADMIN_DSN_FILE`, `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE`. Removed: `KH_FORGEJO_ORG`, `KH_FORGEJO_NOTES_TOKEN_FILE`, every `KH_AGENT_*`, `KH_CI_STATUS_LOGINS`, `KH_PROMOTION_DAILY_LIMIT`, `KH_SUPERSEDED_WEIGHT`, `KH_DISPUTED_WEIGHT`, `KH_GRIST_*`.

### 3.10 Release

The GitHub release gets the wheel, `SHA256SUMS` and `deploy/kh.example.yaml` next to the GHCR image. `.github/workflows/release.yml` is a protected path: phase p12 does not edit it and reports `protected-change: .github/workflows/release.yml - attach the wheel to the release` with a unified diff made from these literal edits: (a) in job `build`, after the step "Smoke test", insert a step "Build and smoke-test the wheel" whose `run` starts with `set -euo pipefail`, creates `$RUNNER_TEMP/build-venv`, installs `build`, runs `python -m build --wheel --outdir dist`, installs `dist/*.whl` into a second venv `$RUNNER_TEMP/check-venv`, runs `kh setup --help` there, then `(cd dist && sha256sum ./*.whl > SHA256SUMS)`; and a step `uses: actions/upload-artifact@v4` with `name: wheel`, `path: dist/`, `if-no-files-found: error`; (b) in job `publish`, before "Create tag and GitHub release", a step `uses: actions/download-artifact@v4` with `name: wheel`, `path: dist`; and `dist/*.whl dist/SHA256SUMS deploy/kh.example.yaml` appended to the `gh release create` command. The job set, permissions and strict-shell rule pinned by `tests/unit/test_release_files.py` stay true. Operator install without Docker: `pip install knowledge_hub-<v>-py3-none-any.whl`, then `kh setup plan --config kh.yaml`.

### 3.11 What is deleted (by phase)

p1: tools `derived`, `promote`, `conflicts`, `entities`, `agent_notes`, `agent_proposals`, `agent_edit`, `agent_status`, their tests and declaration entries. p2: `agent/`, `governance/`, `grist/`, `visibility.py`, `jobs/recompute.py`, `jobs/audience_map.py`, the CLI gates, Grist and agent settings, `pyproject.toml` package-data for governance templates. p3: `tools/docs.py`, `tools/search.py`, `tools/raw.py`, `search/sql.py`, `indexer/{trust,sync_plan,sync,sync_chunks,worker,webhook,rebuild}.py`, `approvals.py`, `merge_rule.py`, `merge_io.py`, `proposals.py`, `jobs/daily.py`, `jobs/requeue.py` (rewritten later). p4: migrations `0001` to `0009`, `kh_provenance`. p5: `forgejo/client.py` (split into `reader.py` and `bot.py`; the admin client is new in p11). p13: `docs/governance.md`. Pure file deletion counts as one change per file in the sizing of a phase.

### 3.12 Phase order and why

Delete from the leaves inward so every phase keeps `ruff check .` and the whole suite green: unregister tools with their tests and declaration entries (p1); delete modules nothing references (p2); remove the old indexer and tool modules that depend on the old schema, leaving a server with no tools (p3); then new schema (p4), config and Forgejo clients (p5), indexer (p6, p7), tools (p8, p9), setup (p10, p11), release and deploy files (p12), docs (p13). The phases run in a strict chain because `tests/support/*` and `mcp/server.py` are touched by almost all of them. CI also runs the integration suite, so "green" includes `tests/integration`. Rejected: building the new modules beside the old ones (two schemas, double the test-support work).

## 5. Manual steps

### Before implementation

None. Every phase runs against fakes and a throwaway Postgres.

### After implementation

**ma1-live-probe: run `kh setup` against a sandbox (about 45 minutes).**
1. Prepare a sandbox Forgejo with an organisation, the accounts `kh-bot` and `kh-indexer` and their tokens, an empty Postgres 16 with pgvector available, an Ollama server and an S3 bucket. Store tokens in files, never in the chat; see https://github.com/tkcs-lab/knowledge-hub/blob/claude/two-tier-refactoring-plan-7v6q2m/docs/runbook.md (written by the last phase).
2. Write a `kh.yaml` with two groups and two labels and run `kh setup plan --config kh.yaml`. You should see `+ create` lines only.
3. Run `kh setup apply --config kh.yaml`, then `kh setup plan --config kh.yaml` again. You should see no lines and exit code 0; any line is a bug.
4. In Forgejo open a repo's Settings, Branches: direct push is off, the merge whitelist is exactly `<group>-approve`, and admins are bound. Open a pull request with `propose_doc` (or by hand) and merge it as an approver; confirm the document appears in `search`.
5. Add a third group to `kh.yaml`, apply, and confirm its team, repo and protection appear without touching the first two.
Report back: pass or fail per step 2, 3, 4, 5 in the pull request.

**ma2-release-dry-run (about 15 minutes).** After the protected-change pull request for `release.yml` is merged, run the release workflow with `dry_run` true; confirm the job `build` uploads an artifact named `wheel`. Report pass or fail.

**ma3-privacyfence (about 20 minutes).** Replace the declaration with `deploy/privacyfence-upstream.example.yaml`, map every group name to its audience name, restart PrivacyFence and create the n8n tokens again (changed gates void existing approvals, PrivacyFence ADR 0300). Report pass or fail.

## 6. Risks and open questions

- **Forgejo semantics differ from the fake.** Early signal: `plan` right after `apply` still reports drift on a real instance (ma1). Research left open: per-field first versions, whether `apply_to_admins` also stops bypass of the merge whitelist, and the push payload's `repository.full_name`. A worker who sees the fake and the brief disagree stops with `status=blocked`.
- **Seeding by Claude is one popup per record** (flat-schema rule rules out a batch tool). If that is impractical, a follow-up plan adds an operator `kh seed` command; not planned here.
- **Deleting before rebuilding leaves a server with no tools** between p3 and p8; nothing is released from the feature branch until the end.
- **Two lists of members** (PrivacyFence mapping and `kh.yaml`) can drift (3.3); the setup output lists the audience names to map.
- **`tests/unit/test_privacyfence_declaration.py` asserts registered tools equal declared tools**; p3 sets both to empty sets and p8 and p9 fill them. A worker who finds another test coupled to the old tool list stops with `status=blocked`.
- **Protected files.** `.github/workflows/release.yml` and `.tld/toolkit.yaml` are never edited by a phase.
- **Reviewer note on sizing.** p1 to p3 delete many files; they are `M` because deleted files carry no logic (3.11).

## Implementation manifest

```yaml
plan_slug: two-tier-rewrite
feature_branch: claude/two-tier-refactoring-plan-7v6q2m
tracking_issue: 26
max_parallel: 1
manual_before: []
manual_after:
  - id: ma1-live-probe
    title: Run kh setup plan, apply, plan against a sandbox Forgejo, Postgres, Ollama and S3
    why: Only a real Forgejo shows whether branch protection, team access and the webhook come out as planned; the fakes cannot
  - id: ma2-release-dry-run
    title: Run the release workflow as a dry run after the release.yml change is merged
    why: The wheel step lives in a protected workflow no phase can test
  - id: ma3-privacyfence
    title: Update the PrivacyFence declaration, map group names to audiences, recreate n8n tokens
    why: Tool gates and names changed; existing n8n token approvals are void
verify_after_merge:
  - .venv/bin/python -m pytest -q
final_checks:
  - tld plan deviations check --file docs/plans/26/deviations.md --plan docs/plans/26/plan.md --all exits 0
  - the changelog has entries under "## [Unreleased]" and no version heading
  - grep -rEn "raw_audience_map|allowed_emails|kh_provenance|requested-by|owners\.yml|grist|Grist" src deploy finds nothing
phases:
  - id: p1-drop-removed-tools
    title: Unregister and delete the tools of the removed concepts
    depends_on: []
    complexity: M
    touches:
      - src/knowledge_hub/tools/derived.py
      - src/knowledge_hub/tools/promote.py
      - src/knowledge_hub/tools/conflicts.py
      - src/knowledge_hub/tools/entities.py
      - src/knowledge_hub/tools/agent_notes.py
      - src/knowledge_hub/tools/agent_proposals.py
      - src/knowledge_hub/tools/agent_edit.py
      - src/knowledge_hub/tools/agent_status.py
      - src/knowledge_hub/mcp/server.py
      - deploy/privacyfence-upstream.example.yaml
      - tests/**
    brief: |
      Goal: remove the eight tools of the dropped concepts (3.11, p1) while ruff and the whole suite stay green.
      1. In src/knowledge_hub/mcp/server.py `register_tools`, remove the lazy imports and `register` calls of derived, promote, conflicts, entities, agent_notes, agent_proposals, agent_edit and agent_status. Leave docs, search and raw registered.
      2. `git rm` the eight tool files named in `touches`.
      3. In deploy/privacyfence-upstream.example.yaml delete the entries of the tools write_derived, propose_change, flag_conflict, list_conflicts, get_entity, list_entities, write_note, propose_doc, propose_edit, pr_status and list_my_proposals. Keep the comment header.
      4. In tests/unit/test_privacyfence_declaration.py set READ_TOOLS to {"search", "get_doc", "list_stale"} and WRITE_TOOLS to {"ingest_raw"}.
      5. Delete every test whose subject is one of the removed tools (grep tests/ for the removed module names and tool names: tests/integration/test_agent_*.py, test_conflicts.py, test_entity_tools.py, test_write_derived.py, test_propose_change.py, test_pr_outcomes.py, test_derived_visibility_sql.py, test_strict_visibility.py, and the unit tests test_agent_*.py, test_routing.py). Edit shared test code (tests/support/tool_cases.py, tests/unit/test_tool_schemas.py, any parametrised list) to drop the removed tools; do not loosen any assertion about the tools that remain.
      6. Do not delete modules outside the eight tool files (p2 does). Run `.venv/bin/python -m ruff check .` and `.venv/bin/python -m pytest -q` until both pass.
      Stop condition: if removing a tool breaks a test of a tool that stays (search, get_doc, list_stale, ingest_raw), stop with status=blocked and name the test.
    acceptance:
      - ".venv/bin/python -m ruff check . passes"
      - ".venv/bin/python -m pytest -q passes"
      - "grep -rnE \"agent_notes|agent_proposals|agent_edit|agent_status|tools\\.(derived|promote|conflicts|entities)\" src tests finds nothing"
  - id: p2-drop-unreferenced-modules
    title: Delete agent, governance, grist, visibility, recompute and their CLI, settings and tests
    depends_on: [p1-drop-removed-tools]
    complexity: M
    touches:
      - src/knowledge_hub/agent/**
      - src/knowledge_hub/governance/**
      - src/knowledge_hub/grist/**
      - src/knowledge_hub/visibility.py
      - src/knowledge_hub/jobs/recompute.py
      - src/knowledge_hub/jobs/audience_map.py
      - src/knowledge_hub/jobs/daily.py
      - src/knowledge_hub/cli.py
      - src/knowledge_hub/app.py
      - src/knowledge_hub/settings.py
      - src/knowledge_hub/deps.py
      - src/knowledge_hub/metrics.py
      - pyproject.toml
      - tests/**
    brief: |
      Goal: delete the modules nothing in the remaining flow references (3.11, p2).
      1. `git rm -r` src/knowledge_hub/agent, src/knowledge_hub/governance, src/knowledge_hub/grist; `git rm` src/knowledge_hub/visibility.py, jobs/recompute.py, jobs/audience_map.py.
      2. cli.py: delete the subcommands governance, validate, domain-owners and recompute-visibility and their handlers; keep serve, migrate, rebuild, check, daily, requeue-embeddings.
      3. app.py: remove the import and call of `load_audience_map` in the lifespan.
      4. jobs/daily.py: remove the Grist entity-sync part; keep the stale marking and the claim_jti purge. `kh daily` then needs no Grist settings.
      5. settings.py and deps.py: remove KH_GRIST_URL, KH_GRIST_DOC_ID, KH_GRIST_KEY_FILE, every KH_AGENT_* setting, KH_CI_STATUS_LOGINS, KH_PROMOTION_DAILY_LIMIT, KH_FORGEJO_NOTES_TOKEN_FILE, their validators and the grist and forgejo_notes fields of Deps and build_deps. metrics.py: remove agent_write_failure and WRITE_FAILURE_REASONS.
      6. pyproject.toml: change package-data to `knowledge_hub = ["migrations/*.sql"]`.
      7. Delete tests of the deleted modules (unit: test_agent_*, test_cli_validate, test_domain_owners, test_governance_*, test_validate_rules, test_grist_client, test_visibility, test_cli_* for removed commands; integration: test_recompute*.py, test_any_of_migration.py only if it imports a deleted module; tests/data/validate, tests/golden). Edit tests of kept code (test_settings, test_daily, test_metrics, tests/support/forgejo_http.py `grist_transport`) to match. Never loosen an assertion of kept behaviour.
      8. indexer/trust.py, sync_plan.py, merge_rule.py, merge_io.py, approvals.py, proposals.py and forgejo/client.py stay in this phase (p3 and p5 remove them).
      Stop condition: if a kept module imports a file you deleted, restore only what is needed and stop with status=blocked naming the import.
    acceptance:
      - ".venv/bin/python -m ruff check . passes"
      - ".venv/bin/python -m pytest -q passes"
      - "grep -rnE \"grist|governance|audience_map|knowledge_hub\\.visibility\" src pyproject.toml finds nothing"
  - id: p3-demolish-old-core
    title: Delete the old indexer, search and read/write tools; leave a server with no tools
    depends_on: [p2-drop-unreferenced-modules]
    complexity: M
    touches:
      - src/knowledge_hub/tools/docs.py
      - src/knowledge_hub/tools/search.py
      - src/knowledge_hub/tools/raw.py
      - src/knowledge_hub/search/sql.py
      - src/knowledge_hub/indexer/**
      - src/knowledge_hub/approvals.py
      - src/knowledge_hub/merge_rule.py
      - src/knowledge_hub/merge_io.py
      - src/knowledge_hub/proposals.py
      - src/knowledge_hub/jobs/**
      - src/knowledge_hub/cli.py
      - src/knowledge_hub/app.py
      - src/knowledge_hub/deps.py
      - src/knowledge_hub/mcp/server.py
      - deploy/privacyfence-upstream.example.yaml
      - tests/**
    brief: |
      Goal: remove everything that depends on the old schema or the old trust model; the later phases rewrite these from scratch. Read the originals with `git show origin/main:<path>` when a later phase needs them.
      1. `git rm` the files named in `touches` except `indexer/chunker.py`, `indexer/frontmatter.py` and `search/cursor.py`, which stay. Delete the whole indexer package otherwise (trust, sync_plan, sync, sync_chunks, worker, webhook, rebuild) and src/knowledge_hub/jobs entirely.
      2. cli.py: keep only `serve` and `migrate`; app.py: keep /mcp, /healthz and /metrics; remove the webhook route, the worker task and its import; deps.py: remove the worker, blob and embedder construction that only the worker used if nothing else uses it (keep the embedder and blob store factories; later phases use them).
      3. mcp/server.py: `register_tools` registers nothing (keep the function and `build_mcp`); deploy/privacyfence-upstream.example.yaml: `tools: {}`; tests/unit/test_privacyfence_declaration.py: READ_TOOLS and WRITE_TOOLS become empty sets.
      4. Delete every unit and integration test of a deleted module, and every test that needs a registered tool (tests/integration/test_http_e2e.py, test_wiring.py, test_get_doc_*.py, test_tool_get_doc.py, test_list_stale.py, test_search_*.py, test_ingest_raw.py, test_sync*.py, test_rebuild.py, test_webhook_worker.py, test_metrics.py parts about jobs). Keep tests of claims, replay cache, ratelimit, cursor, chunker, embed providers, frontmatter (old, rewritten in p5), config (old, rewritten in p5), forgejo client (old, rewritten in p5), settings, migrate runner, schema, RLS and triggers (old schema, rewritten in p4), release and deploy files.
      5. Edit shared test code (tests/conftest.py, tests/support/*) only to drop imports of deleted modules.
      Stop condition: if ruff or a kept test needs a deleted module, stop with status=blocked and name it.
    acceptance:
      - ".venv/bin/python -m ruff check . passes"
      - ".venv/bin/python -m pytest -q passes"
      - "ls src/knowledge_hub/indexer shows only __init__.py, chunker.py and frontmatter.py"
      - "grep -rn \"approvals\\|merge_rule\\|merge_io\\|proposals\" src finds nothing outside forgejo/client.py"
  - id: p4-new-schema
    title: One baseline migration, plain-audience RLS, three roles
    depends_on: [p3-demolish-old-core]
    complexity: M
    touches:
      - src/knowledge_hub/migrations/**
      - src/knowledge_hub/db/migrate.py
      - src/knowledge_hub/db/caller_tx.py
      - src/knowledge_hub/auth/claims.py
      - tests/support/pg.py
      - tests/support/seed.py
      - tests/support/principals.py
      - tests/support/fixtures_db.py
      - tests/integration/**
      - tests/unit/test_claims.py
    brief: |
      Read first: section 3.3 and 3.5 of this plan.
      Goal: replace migrations 0001 to 0009 with the one baseline of 3.5.
      1. `git rm` the nine files in src/knowledge_hub/migrations. Create 0001_baseline.sql implementing exactly the tables, grants, policies, functions and triggers of 3.5. Copy the schema, grant and schema_migrations/claim_jti/claim_seen parts from `git show origin/main:src/knowledge_hub/migrations/0001_schema_grants.sql` and `0003_rls.sql`; copy `chunks_inherit_owner` and `owner_propagate_to_chunks` from `0004_triggers.sql` and cut them down to the columns of 3.5. Drop every mention of kh_provenance, allowed_emails, any-of clauses and kh.email.
      2. db/migrate.py: when `kh.schema_migrations` lists a name that the package does not ship, raise MigrationError("database predates the baseline; a fresh install is required"). Add a unit-level test with a fake connection or an integration test that inserts a foreign name.
      3. db/caller_tx.py: stop setting `kh.email`. auth/claims.py: remove `email` from `Caller` and from `_build_caller`; the claim's email is ignored. Update tests/unit/test_claims.py.
      4. tests/support/pg.py `bootstrap_roles`: create kh_migrator, kh_app, kh_indexer only; no kh_provenance.
      5. Rewrite tests/support/seed.py and principals.py for the new tables: groups `all`, `mgmt`, `hr` (rows in kh.groups), one document and one record per group, callers `alice_all`, `bob_mgmt`, `carol_hr`, `dave_none`, `multi` (all+mgmt), `wf_token` (n8n_workflow, audience all).
      6. Rewrite integration tests test_schema_tables.py, test_rls_tables.py, test_rls_policies.py, test_triggers.py, test_migrate_runner.py for the new schema: forced RLS on every table; a caller reads only held audiences (documents, records, chunks); insert into records needs the audience and `created_by_sub = caller_sub()`; no UPDATE or DELETE for kh_app; the unique key (audience, source, source_ref); unknown label raises SQLSTATE P0K05; deleting a used label raises P0K06; chunk triggers copy audience, labels, stale; the embed_record job is queued on record insert; a foreign schema_migrations name is refused. Delete integration tests that no longer apply (test_recompute_matches_migration.py and similar).
      Stop condition: if a kept test needs email or a clause, stop with status=blocked.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_schema_tables.py tests/integration/test_rls_tables.py tests/integration/test_rls_policies.py tests/integration/test_triggers.py tests/integration/test_migrate_runner.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
      - "ls src/knowledge_hub/migrations shows only 0001_baseline.sql"
      - "grep -rniE \"kh_provenance|allowed_emails|kh\\.email\" src tests finds nothing"
  - id: p5-config-frontmatter-forgejo
    title: New KhConfig, frontmatter parser and split Forgejo reader and bot clients
    depends_on: [p4-new-schema]
    complexity: M
    touches:
      - src/knowledge_hub/config.py
      - src/knowledge_hub/indexer/frontmatter.py
      - src/knowledge_hub/forgejo/**
      - src/knowledge_hub/settings.py
      - deploy/kh.example.yaml
      - tests/support/forgejo_fake.py
      - tests/support/forgejo_http.py
      - tests/unit/test_config.py
      - tests/unit/test_frontmatter.py
      - tests/unit/test_forgejo_client.py
      - tests/unit/test_forgejo_write.py
      - tests/unit/test_forgejo_status.py
      - tests/unit/test_forgejo_fake.py
      - tests/unit/test_settings.py
      - tests/unit/test_deploy_files.py
    brief: |
      Read first: 3.4, 3.6, 3.8 (propose_doc), 3.9.
      1. config.py: rewrite as `KhConfig` (pydantic, extra forbid) with the keys and validation of 3.4; `load_config(path)` and `parse_config(dict)` keep their names; errors are `ConfigError` with a `config:` prefix; expose `config.repo_for_group(name)`, `config.group_for_repo(repo)` and `config.bots`. Reuse AUDIENCE_RE and LOGIN_RE from the old file.
      2. deploy/kh.example.yaml: rewrite to the example of 3.4 with two groups and two labels; update tests/unit/test_deploy_files.py `test_example_config_loads` and remove the entity default test.
      3. indexer/frontmatter.py: rewrite `parse_document(text, path)` to return `ParsedDoc(id, title, review_by, labels, body)` or `Rejected(reason)` per 3.6 (required id, title, review_by; optional labels; ignore other keys; no domain, owner, type, entities, supersedes). Label existence is checked by the sync, not here.
      4. forgejo/: `git rm client.py`; create reader.py `ForgejoReader(base_url, token, org, *, transport=None, timeout=30.0)` with `head_sha(repo, branch)`, `list_tree(repo, sha)` (recursive, returns dicts with path, sha, type), `get_raw(repo, path, sha)`, `is_ancestor(repo, base, head)` (compare API; a 404 or error means False) and `aclose`; create bot.py `ForgejoBot(base_url, token, org, *, transport=None, timeout=30.0)` with `head_sha`, `blob_sha(repo, path, ref)`, `create_branch(repo, name, from_sha)`, `put_file(repo, branch, path, content, message, prev_blob: str | None)` (passes `sha` when prev_blob is given), `open_pull(repo, head, base, title, body)`, `list_open_pulls(repo, author)` and `aclose`; the guard `_guard_ref` accepts only names starting `bot/` with a non-empty suffix, no `..`, no `//`; `open_pull` requires base `main`. Keep ForgejoError and ForgejoRefused in forgejo/__init__.py. Copy the HTTP plumbing from `git show origin/main:src/knowledge_hub/forgejo/client.py`.
      5. settings.py: remove KH_FORGEJO_ORG (the org is `config.org`); nothing else changes here.
      6. Rewrite tests: test_config.py (every rule of 3.4 with one failing case each), test_frontmatter.py, new test_forgejo_reader.py and test_forgejo_bot.py with httpx.MockTransport, tests/support/forgejo_fake.py as an in-memory repo (tree of path to content, head sha, pulls) used by later phases; delete test_forgejo_client.py, test_forgejo_write.py, test_forgejo_status.py if replaced.
      Stop condition: if a kept module imports the old client or the old Config fields, stop with status=blocked and name it.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_config.py tests/unit/test_frontmatter.py tests/unit/test_forgejo_reader.py tests/unit/test_forgejo_bot.py tests/unit/test_deploy_files.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
      - "ls src/knowledge_hub/forgejo shows __init__.py, bot.py and reader.py"
  - id: p6-sync-core
    title: Tree-diff sync of main into documents and chunks
    depends_on: [p5-config-frontmatter-forgejo]
    complexity: M
    touches:
      - src/knowledge_hub/indexer/sync_plan.py
      - src/knowledge_hub/indexer/sync.py
      - src/knowledge_hub/indexer/sync_chunks.py
      - tests/unit/test_sync_plan.py
      - tests/integration/test_sync.py
      - tests/integration/test_sync_chunks.py
    brief: |
      Read first: 3.5, 3.6. Pattern: `git show origin/main:src/knowledge_hub/indexer/sync_chunks.py` (chunk planning, apply) and `sync.py` (transaction shape); write the new modules from scratch.
      1. sync_plan.py: pure function `build_sync_plan(config, repo, head_sha, tree, stored, texts, known_labels) -> SyncPlan` where `tree` is the list of `{path, sha}`, `stored` the rows `(path, blob_sha, doc_key)` and `texts` maps path to fetched text for changed files. Apply the include rules of 3.6 (md only, no dot segment, not root README.md, at most 1 MiB), parse with `parse_document`, reject unknown labels and duplicate ids (all holders rejected), and return `upserts`, `deletes` (stored paths absent from the tree or rejected) and `alerts` (`(path, message)`). Alert messages: `file rejected: <reason>`, `duplicate id <id>`.
      2. sync_chunks.py: `plan_chunks` and `apply_chunks` copied in shape from the old file for table `kh.chunks` with the new columns; tier is `T0`; embed only when `embedder.may_embed(audience)`.
      3. sync.py: `async sync_repo(indexer_pool, reader, config, repo, embedder, *, chunker_version)`: read `repo_state` and stored rows; `head = reader.head_sha(repo, "main")`; alert `main of <repo> was rewritten; run kh rebuild after review` when `last_indexed_sha` is set and `reader.is_ancestor` is False (then still sync); fetch the changed blobs; plan; chunk and embed outside the transaction; then one transaction with `pg_advisory_xact_lock(hashtext(repo))`: delete vanished rows, upsert documents (`audience = config group of repo`, `commit_sha = head`), replace changed chunks, insert alerts (deduplicate by repo, path, message), upsert repo_state.
      4. Tests: unit test of the plan (every include and reject rule, duplicate ids, unknown label, delete of a vanished path, a rejected file retried when stored lacks it); integration tests with tests/support/forgejo_fake.py and the migrated DB: first sync, an edit, a delete, a rename, an unknown label then the label added, duplicate id, rewrite alert, chunk reuse when content is unchanged.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_sync_plan.py tests/integration/test_sync.py tests/integration/test_sync_chunks.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p7-worker-wiring
    title: Worker, webhook, rebuild, daily, requeue and the CLI and app wiring
    depends_on: [p6-sync-core]
    complexity: M
    touches:
      - src/knowledge_hub/indexer/worker.py
      - src/knowledge_hub/indexer/webhook.py
      - src/knowledge_hub/indexer/rebuild.py
      - src/knowledge_hub/jobs/daily.py
      - src/knowledge_hub/jobs/requeue.py
      - src/knowledge_hub/jobs/__init__.py
      - src/knowledge_hub/cli.py
      - src/knowledge_hub/app.py
      - src/knowledge_hub/deps.py
      - tests/unit/test_cli_rebuild.py
      - tests/integration/test_webhook_worker.py
      - tests/integration/test_rebuild.py
      - tests/integration/test_daily.py
    brief: |
      Read first: 3.6, 3.9. Pattern: `git show origin/main:src/knowledge_hub/indexer/worker.py`, `webhook.py`, `rebuild.py`, `jobs/daily.py`, `jobs/requeue.py`; rewrite for the new schema.
      1. webhook.py: POST /webhooks/forgejo; body cap `max_body_bytes` (413); HMAC check unchanged; repo name from `repository.full_name` else `repository.owner.login/repository.name`; ignore other orgs and repos not in `config.repo_for_group`; only a `push` to `refs/heads/main` queues `git_push`; others answer 202 ignored.
      2. worker.py: `Worker(indexer_pool, reader, embedder, config, blobs, *, chunker_version, poll_seconds)`; job kinds `git_push` (sync_repo) and `embed_record` (read the body from S3 by `blob_key`, chunk, embed when `embedder.may_embed(audience)`, replace chunks in one transaction); claim, retry (30 s times attempts, five attempts) as before.
      3. rebuild.py: `rebuild(deps, repo=None)` runs sync_repo for every configured repo after clearing `last_indexed_sha`; `check(deps)` reports index drift (head sha versus stored, content drift of chunks, documents whose audience differs from the config group); exit 1 on drift.
      4. jobs/daily.py: mark stale (`review_by < current_date`) and purge expired claim_jti; jobs/requeue.py: enqueue `embed_record` for records without chunks.
      5. cli.py: add back `rebuild [--repo R]`, `check`, `daily`, `requeue-embeddings` and `lint FILE...` (runs `parse_document` per file, prints `path: reason`, exit 1 on any); app.py: webhook route and worker task back; deps.py builds ForgejoReader from `KH_FORGEJO_INDEXER_TOKEN_FILE` and `config.org`.
      6. Tests: webhook (HMAC, wrong repo, wrong ref, full_name fallback), worker (git_push and embed_record, retry), rebuild and check, daily, cli lint.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_webhook_worker.py tests/integration/test_rebuild.py tests/integration/test_daily.py tests/unit/test_cli_rebuild.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p8-read-tools
    title: search, get_doc, list_stale and list_labels
    depends_on: [p7-worker-wiring]
    complexity: M
    touches:
      - src/knowledge_hub/search/sql.py
      - src/knowledge_hub/tools/search.py
      - src/knowledge_hub/tools/docs.py
      - src/knowledge_hub/tools/labels.py
      - src/knowledge_hub/mcp/server.py
      - src/knowledge_hub/mcp/errors.py
      - src/knowledge_hub/settings.py
      - deploy/privacyfence-upstream.example.yaml
      - tests/unit/test_privacyfence_declaration.py
      - tests/integration/test_search_ranking.py
      - tests/integration/test_search_leaks.py
      - tests/integration/test_tool_get_doc.py
      - tests/integration/test_list_stale.py
      - tests/integration/test_http_e2e.py
      - tests/support/tool_cases.py
    brief: |
      Read first: 3.8 (tools, errors), 3.9. Pattern: `git show origin/main:src/knowledge_hub/search/sql.py`, `tools/search.py`, `tools/docs.py`, `mcp/errors.py`.
      1. mcp/errors.py: define exactly the constants of 3.8's error list (copy `KhError`, `kh_error`, `ToolCall`, `tool_guard` unchanged); remove obsolete constants.
      2. search/sql.py: `SearchFilters(query, query_vector, embedding_model, min_tier, labels, audience, limit, offset, pool, rrf_k, tier_weights, stale_weight)`; the single-statement shape of the old query (vector list, full-text list, RRF fusion, one best chunk per owner, weights), tiers T0 and T3, `labels @> %(labels)s`, optional `audience = %(audience)s`; no superseded, disputed, agent or domain parts; `parse_tier_weights` requires T0 and T3. settings.py: `KH_TIER_WEIGHTS` default `T0:1.0,T3:0.4`; remove KH_SUPERSEDED_WEIGHT and KH_DISPUTED_WEIGHT.
      3. tools/search.py, tools/docs.py (get_doc, list_stale), tools/labels.py (list_labels): signatures and result shapes exactly as the table in 3.8; the `NORMATIVE` rule; `source_link` from `config.forgejo_web_url`; get_doc of `rec:` reads the body from S3 through `deps.blobs`. Each module has `register(server, deps)` wrapping with `tool_guard`.
      4. mcp/server.py registers docs, search, labels. Declaration: add search, get_doc, list_stale, list_labels as `gate: auto` with `verb: search` or `read`; remove `email_for_visibility`; keep `allowed_principal_types`. Test: READ_TOOLS = those four, WRITE_TOOLS empty until p9.
      5. Tests: ranking and leak matrix for the new model (a caller holding `all` never sees `mgmt` hits, chunks or get_doc; hidden and missing ids give the same NOT_FOUND), cursor binding, label filters, NORMATIVE, list_stale paging, http e2e with signed claims, tool schemas.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_search_ranking.py tests/integration/test_search_leaks.py tests/integration/test_tool_get_doc.py tests/integration/test_list_stale.py tests/integration/test_http_e2e.py tests/unit/test_privacyfence_declaration.py tests/unit/test_tool_schemas.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p9-write-tools
    title: ingest_record and propose_doc
    depends_on: [p8-read-tools]
    complexity: M
    touches:
      - src/knowledge_hub/tools/records.py
      - src/knowledge_hub/tools/propose.py
      - src/knowledge_hub/propose_render.py
      - src/knowledge_hub/mcp/server.py
      - src/knowledge_hub/settings.py
      - src/knowledge_hub/deps.py
      - deploy/privacyfence-upstream.example.yaml
      - tests/unit/test_privacyfence_declaration.py
      - tests/unit/test_propose_render.py
      - tests/integration/test_ingest_record.py
      - tests/integration/test_propose_doc.py
      - tests/support/tool_cases.py
    brief: |
      Read first: 3.8 (ingest_record, propose_doc), 3.9, 3.5 (dedupe). Pattern: `git show origin/main:src/knowledge_hub/tools/raw.py` (S3 put then insert).
      1. settings.py: rename KH_MAX_RAW_BYTES to KH_MAX_RECORD_BYTES; add KH_PROPOSE_MAX_BYTES (131072) and KH_PROPOSE_MAX_OPEN (20). deps.py builds `forgejo_bot` (ForgejoBot with `KH_FORGEJO_BOT_TOKEN_FILE`) only when the token file is set.
      2. tools/records.py `ingest_record`: validations and result of 3.8; order: check audience held and configured, size, labels exist (SELECT from kh.labels under the caller), existing row lookup by (audience, source, source_ref) returning `{id, existed: true}`, S3 put at `records/<uuid>`, INSERT with application-generated uuid; id format `rec:<uuid>`.
      3. propose_render.py: pure `render_file(id, title, review_by, labels, body)`, `check_text(text)` (control, bidi, HTML comment and tag refusal) and `validate_path(path)`; tools/propose.py `propose_doc` per 3.8: needs `deps.forgejo_bot` else `NOT_CONFIGURED`; blob check, id-in-use check against `kh.documents` of the repo, open-PR cap through `list_open_pulls(repo, bot_login)`, branch `bot/propose/<uuid4>`, one file, one pull request; any ForgejoError becomes `PROPOSE_FAILED` with the detail logged only.
      4. mcp/server.py registers records and propose; declaration: `ingest_record` and `propose_doc` as `gate: popup` with verb `add` (ingest_record) and `update` (propose_doc); test WRITE_TOOLS = those two.
      5. Tests: unit tests of render, text and path rules; integration tests with FakeForgejo: ingest (new, duplicate returns existing, audience not held, unknown label, too large, S3 failure leaves no row), propose (create, edit with base_blob, stale base_blob, path exists, id in use, cap, caller lacks group, PR body trailer lines), RLS: a record written by `wf_token` is invisible to a caller without the audience.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_propose_render.py tests/integration/test_ingest_record.py tests/integration/test_propose_doc.py tests/unit/test_privacyfence_declaration.py tests/unit/test_tool_schemas.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p10-setup-core
    title: Setup framework with the postgres, labels, ollama and s3 steps
    depends_on: [p9-write-tools]
    complexity: M
    touches:
      - src/knowledge_hub/setup/__init__.py
      - src/knowledge_hub/setup/steps.py
      - src/knowledge_hub/setup/postgres.py
      - src/knowledge_hub/setup/labels.py
      - src/knowledge_hub/setup/ollama.py
      - src/knowledge_hub/setup/s3.py
      - tests/unit/test_setup_steps.py
      - tests/unit/test_setup_ollama.py
      - tests/unit/test_setup_s3.py
      - tests/integration/test_setup_postgres.py
    brief: |
      Read first: 3.7 (steps 1, 2, 4, 5).
      1. steps.py: `@dataclass(frozen=True) Change(kind: Literal["create","update","remove","unmanaged","refused"], text: str)`, `class Step(Protocol): name: str; async def plan(self) -> list[Change]; async def apply(self, prune: bool) -> list[Change]`, `render(change) -> str` giving the prefixes `+ `, `~ `, `- `, `? `, `! ` of 3.7, and `run(steps, mode, prune) -> int` implementing the exit codes (plan 0, 1 or 2; apply 0 or 2; an exception from a step prints `setup: <step>: <message>` without credentials and exits 2).
      2. postgres.py: `PostgresStep(config, settings, admin_dsn: str | None)`: with an admin DSN create roles from the password files (always re-set), the database owned by kh_migrator and the vector extension; then pending migrations through `db.migrate` (plan lists them as `+ create migration <name>`); then sync `kh.groups` rows from the config (insert; update repo is refused; removal only with prune and refused while records exist, deleting the group's documents first); without an admin DSN plan reports missing roles, database or extension as `! refused` lines and apply changes nothing outside migrations and groups.
      3. labels.py: `LabelsStep` as 3.7 step 2, including the `git_push` jobs after an added label and the `P0K06` refusal text `! refused label <x> in use`.
      4. ollama.py: `OllamaStep(settings, transport=None)` per step 4 using httpx (`GET /api/tags`, `POST /api/embed`, `POST /api/pull` with `{"model": ..., "stream": false}`); s3.py: `S3Step(settings, client=None)` per step 5 using boto3 (`head_bucket`, put, get, delete of `kh-setup-probe/<uuid>`, `create_bucket` only when a `create_bucket=True` constructor argument is set).
      5. Tests: steps run/exit codes, Ollama with httpx.MockTransport (model missing, present, wrong dimension, pull), S3 with botocore Stubber, Postgres integration with tests/support/pg.py (plan on an empty cluster, apply, plan again clean, add a group, prune with and without records, label in use).
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_setup_steps.py tests/unit/test_setup_ollama.py tests/unit/test_setup_s3.py tests/integration/test_setup_postgres.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p11-setup-forgejo-cli
    title: Forgejo admin client, forgejo step and the kh setup command
    depends_on: [p10-setup-core]
    complexity: M
    touches:
      - src/knowledge_hub/forgejo/admin.py
      - src/knowledge_hub/setup/forgejo.py
      - src/knowledge_hub/setup/cli.py
      - src/knowledge_hub/cli.py
      - src/knowledge_hub/settings.py
      - tests/support/forgejo_admin_fake.py
      - tests/unit/test_forgejo_admin.py
      - tests/unit/test_setup_forgejo.py
      - tests/unit/test_setup_cli.py
      - tests/unit/test_import_boundary.py
    brief: |
      Read first: 3.7 (step 3 in full), 3.4.
      1. forgejo/admin.py `ForgejoAdmin(base_url, token, org, *, transport=None)`: methods for org teams (list, create, edit, set members by diff, add repo to team, list team repos), repos (get, create with auto_init and private, edit settings, collaborators put, list), branch protection (get, create, edit by `rule_name`), org hooks (list, create, edit), `whoami()` (`GET /user`), `user_permission(repo, login)` and `org_owners()`. Branch-protection body exactly as 3.7 step 3.
      2. tests/support/forgejo_admin_fake.py: a stateful in-memory Forgejo (httpx.MockTransport) implementing those endpoints, including the silent drop of a merge-whitelist team that lacks read access to the repo.
      3. setup/forgejo.py `ForgejoStep(config, settings, admin, bot_login_token, indexer_login_token)`: plan and apply as 3.7 step 3: order team, repo, team-to-repo access, collaborators, protection (then read back and compare), org hook, identity checks, owners report, unmanaged lines, prune of teams only, the immutable repo rule. Plan after apply on the fake must be empty.
      4. settings.py: add `KH_SETUP_PG_ADMIN_DSN_FILE` and `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE` (optional paths). setup/cli.py `main(argv)` with `plan|apply`, `--config`, `--only`, `--prune`, `--create-bucket`, building the five steps in order and calling `steps.run`; at the end of `plan` print `audience <group>` per group. cli.py: `setup` subcommand delegating to it.
      5. tests/unit/test_import_boundary.py: importing `knowledge_hub.app` must not import `knowledge_hub.forgejo.admin` or `knowledge_hub.setup` (check `sys.modules` in a subprocess).
      6. Tests: admin client with MockTransport, the step on the fake (clean first plan, apply, empty second plan, a group added, a member removed, protection tampered, approve team missing from the repo, unmanaged team, prune), cli exit codes and `--only`.
      Stop condition: if the fake and the brief disagree on a Forgejo field, follow section 3.7 and stop with status=blocked naming the field.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_forgejo_admin.py tests/unit/test_setup_forgejo.py tests/unit/test_setup_cli.py tests/unit/test_import_boundary.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
      - ".venv/bin/python -m knowledge_hub.cli setup --help exits 0"
  - id: p12-release-deploy
    title: Compose, backup, declaration and the protected release workflow change
    depends_on: [p11-setup-forgejo-cli]
    complexity: S
    touches:
      - deploy/compose.example.yaml
      - deploy/backup-kh.sh
      - deploy/privacyfence-upstream.example.yaml
      - tests/unit/test_deploy_files.py
      - tests/unit/test_privacyfence_declaration.py
    brief: |
      Read first: 3.9, 3.10.
      1. deploy/compose.example.yaml: set the environment of `knowledge-hub` to the kept and new settings of 3.9 (remove KH_AGENT_*, KH_GRIST_*, KH_FORGEJO_ORG, KH_FORGEJO_NOTES_TOKEN_FILE, KH_PROMOTION_DAILY_LIMIT); keep every secret as a `*_FILE` under /run/secrets and do not mount the two KH_SETUP_* files into this service. Update image tag text only if it says a version that no longer exists; do not invent one.
      2. deploy/backup-kh.sh: dump only `kh.records` (the non-rebuildable table; groups and labels come from kh.yaml, documents and chunks are rebuilt); keep `--enable-row-security`, `--inserts`, `set -euo pipefail`; replace the restore comment with `pg_restore`, `kh setup apply`, `kh rebuild`, `kh requeue-embeddings`, the S3 bucket sync.
      3. tests/unit/test_deploy_files.py: TABLES becomes ["records"]; the assertion that documents and chunks are not dumped stays.
      4. deploy/privacyfence-upstream.example.yaml: header comment explains group audiences and removes the email paragraph; test_privacyfence_declaration asserts `email_for_visibility` is absent.
      5. Do NOT edit .github/workflows/release.yml (protected). Report, in the phase report, one line `protected-change: .github/workflows/release.yml - attach the wheel and example config to the release` followed by the unified diff built from the literal edits of section 3.10, paths relative to the repository root, applied and checked against a scratch copy of the file (`patch --dry-run -p1`), and confirm the result still satisfies every assertion of tests/unit/test_release_files.py by running those tests' logic against the scratch copy (copy the test to the scratch directory and point WORKFLOW at the copy).
      Stop condition: if the scratch copy fails an existing release-file assertion, stop with status=blocked and show the failing assertion.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_deploy_files.py tests/unit/test_privacyfence_declaration.py tests/unit/test_release_files.py -q passes"
      - "bash -n deploy/backup-kh.sh exits 0"
  - id: p13-docs-retirement
    title: README, design, configuration, runbook, changelog
    depends_on: [p12-release-deploy]
    complexity: M
    touches:
      - README.md
      - docs/design.md
      - docs/configuration.md
      - docs/runbook.md
      - docs/governance.md
      - CHANGELOG.md
      - tests/unit/test_docs_links.py
    brief: |
      Read first: sections 3.1 to 3.10 of this plan, README.md, docs/design.md, docs/configuration.md, docs/runbook.md, `kh --help` and `kh setup --help` output.
      1. README.md: rewrite for two tiers (table T0, T3), the group model, the tool list (six tools), the `kh` commands of 3.6 and 3.7, quick start (unchanged commands), and links to the three remaining docs. Remove every mention of T1, T2, domains, entities, Grist, owners.yml, governance.
      2. docs/design.md: rewrite from the decisions of section 3: tiers, forge-only trust and what `plan` verifies, access model and the authority split, the accepted residuals of 3.3, the schema and RLS matrix of 3.5, claim middleware and call binding (keep the existing text, drop email), indexer, search, tools, the rejected alternatives table (add the rejections named in section 3). Replace the "Deviation from the PrivacyFence contract" section with one line saying email is no longer used.
      3. docs/configuration.md: every setting of 3.9 and every `kh.yaml` key of 3.4 with defaults and the validation rules; the commands table of 3.6; the `kh setup` options, exit codes, credential files and steps of 3.7.
      4. docs/runbook.md: install order with `kh setup plan` and `apply`, adding a group (edit kh.yaml, apply, restart `kh serve`, change the PrivacyFence mapping), joiners and leavers ("change both"), scheduled `kh setup plan`, the live probe of ma1, backup and restore, rotation, releasing (the wheel and the manual protected-change step for release.yml).
      5. `git rm docs/governance.md`; fix every link to it. Keep each markdown file at most 400 lines (tests/unit/test_docs_links.py).
      6. CHANGELOG.md: under `## [Unreleased]` add Added, Changed, Removed entries covering: two tiers, groups, labels, `kh setup`, `ingest_record`, `propose_doc`, `list_labels`, the wheel on releases, the removed tools, commands, settings and migrations, the breaking fresh-install requirement and the PrivacyFence token recreation. Do not open a version heading.
      7. Run the final_checks greps of the manifest and make them pass.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_docs_links.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
      - "grep -rEn \"raw_audience_map|allowed_emails|kh_provenance|requested-by|owners\\.yml|grist|Grist\" src deploy finds nothing"
```
