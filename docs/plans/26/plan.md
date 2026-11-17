# Plan: Two tiers (T0 and T3), group access, Postgres labels, a setup script

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** Breaking release, fresh install only. The hub has two tiers. T0 is Markdown in Forgejo repos, one repo per group; a change reaches `main` only through a pull request that a member of the group's approver list merges (Claude, n8n or a person opens it). T3 is records that n8n or Claude put in with `ingest_record`, each with its own audience group. A person reads the groups that `kh.yaml` lists for the email PrivacyFence verified. An operator writes one `kh.yaml` (people, groups, members, approvers, labels) and runs `kh setup plan` then `kh setup apply` to create the Postgres roles and tables, labels, Forgejo teams, repos, branch protection and webhook, and the Ollama model; adding a group later is an edit and a re-run.

**Scope:** The hub is rewritten around two tiers. Removed: T1 notes, T2 derived records, candidates, conflicts, promotion, domains, entities and Grist, `owners.yml` and the governance gates, the trust walk, the requester record, `allowed_emails`, any-of audiences, `raw_audience_map`. Kept: claim verification, RLS, chunking, embeddings, search, S3 bodies. Added: `kh setup`, `kh lint`, labels, a new `propose_doc`, `ingest_record` (replaces `ingest_raw`), `list_labels`, a wheel on the GitHub release. Changed: `search`, `get_doc`, `list_stale` arguments and results, ids `doc:<uuid>` and `rec:<uuid>`, `kh.yaml` (`people:` and `groups:` replace `repos:`), `KH_FORGEJO_ORG` moved to `org:`, `KH_MAX_RAW_BYTES` renamed `KH_MAX_RECORD_BYTES`, backups dump only `kh.records`; n8n tokens are recreated.

**Security:** T0 trust rests entirely on Forgejo branch protection and team membership, so `kh setup plan` checks them (3.7). One bot account opens every T0 pull request and is stamped on every T3 record. Accepted residuals (3.3): a holder of two groups can write into the wider one, a leaked bot token reads every T0 repo, with 0 required approvals the approver must review the final diff, and a reassigned email must leave `people` first. The hub never holds admin credentials; `kh setup` does.

**Performance:** No measurable change.

**Review focus:** forge-only trust and what `plan` verifies (3.2, 3.7); the email-to-group resolution (3.3); the RLS insert and dedupe rules and definer triggers (3.5); `propose_doc` text and path checks (3.8); the demolition phase p1 and its keep-list (3.11, 3.12).

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

- **Group**: a name matching `^[a-z0-9][a-z0-9_-]{0,62}$`. It is the audience name stored on rows, the audience name an n8n token carries in its claim, and the name of a T0 repo's access group. Every group has exactly one T0 repo.
- **T0**: a row of `kh.documents`, from a `*.md` file on `main` of a group's repo. **T3**: a row of `kh.records`, body in S3. Tier weights `T0:1.0,T3:0.4`.
- **Label**: a name matching `^[a-z0-9][a-z0-9_.:-]{0,62}$` (so `product:ember` is legal) defined in `kh.yaml` and stored in `kh.labels`. Labels replace entity types. Label names are visible to every caller; do not put secrets in them.
- Ids returned by tools: `doc:<uuid>` and `rec:<uuid>`.

### 3.2 Trust: Forgejo is the control (owner decision `trust: forge-only`)

The indexer reads the tree of `main` as Forgejo reports it. No trust walk, no anchor, no verdicts, no stamps, no `approved_by`, no `disputed` status, no `document_versions`, no `supersedes`. The approval is the merge: branch protection forbids direct push, the merge whitelist is the group's approver team, required approvals are 0, so an approver may merge their own pull request (owner decision `requester: yes`). Forgejo's merge API takes `head_commit_id` and answers 409 when the head moved, so an approver's UI merge is bound to the head they saw (research r-20261117-1, https://codeberg.org/forgejo/forgejo/raw/branch/forgejo/routers/api/v1/repo/pull.go, 2026-11-17). Rejected: a formal-review walk (closest to today, much more code), and a trust walk by merger (still needs a walk and verdict storage). One cheap check stays: the indexer alerts `main of <repo> was rewritten` when the new head does not descend from `repo_state.last_indexed_sha`.

### 3.3 Access model

- The caller's audiences are resolved by the hub in `ClaimMiddleware` from the verified claim (owner decision 2026-11-17, `group-source: khyaml-email`). A `claude_client` holds the groups of `kh.yaml` in which a member's `people` email equals the claim's verified email, compared in lower case; the claim's `audiences` are ignored, and a claim without an email gives no audience. An `n8n_workflow` holds the claim's `audiences` that are configured groups: a workflow has no `people` entry, so its groups live in its PrivacyFence token. A `service` calls no tool. The config is read when `kh serve` starts, so a change to `people` or to a group needs a restart. `kh.can_read(a)` is `a = ANY(kh.caller_audiences())`. A document or chunk is visible when its `audience` is held; a record likewise; inserting a record needs the audience too.
- **One list for people.** `kh.yaml` decides both a person's Forgejo teams and what the hub lets them read, so a joiner or leaver changes one file. PrivacyFence keeps authentication, the verified email (`email_for_visibility: true` stays in the declaration) and the write popups; its group-to-audience mapping is no longer used for people. A person whose PrivacyFence email differs from their `people` email reads nothing, and the runbook names that as the first thing to check. Rejected: a `sync_teams` job copying Forgejo team members into Postgres with a staleness limit (a second store and a freshness rule for a list the hub already loads), and read-only `readers:` per group (owner decision `readers: no`: every member reads and writes, approvers merge).
- **Accepted residuals** (documented in `docs/design.md`): a caller holding two groups can `ingest_record` or `propose_doc` into the wider one, the PrivacyFence popup shows `audience` and `group` and is the only gate; a leaked bot token can create branches and pull requests and push to open pull-request branches in every repo and read every T0 repo, but cannot merge; with required approvals 0 the approver must review the final diff. The verified email is the identity: a Workspace email that is reassigned gives its new holder the old person's groups until `people` is edited.

### 3.4 Config file `kh.yaml`

One file for the hub and for `kh setup` (unknown keys refused, errors start `config:`):

```yaml
org: kh                       # Forgejo organisation (replaces KH_FORGEJO_ORG)
bot_login: kh-bot             # writes every T0 PR and stamps every T3 record
indexer_login: kh-indexer     # read-only token owner
forgejo_web_url: null
hub_url: http://knowledge-hub:8080   # how Forgejo reaches the hub; only `kh setup` needs it
people:                       # Forgejo login -> the email PrivacyFence verifies for that person
  alice: alice@example.com
  bob: bob@example.com
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

Validation (class `KhConfig`, `knowledge_hub/config.py`, pure, no I/O): group names match the group regex, are not `owners` in any case, and `<name>-approve` collides with no group; logins match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}$`, unique per list after case-folding; `approvers` non-empty and a subset of `members` (error `config: group <g> approver <login> is not a member`); repo names match `^[a-z0-9][a-z0-9._-]{0,99}$`, unique; `bot_login` and `indexer_login` differ and appear in no member list; labels match the label regex, description at most 200 characters, at most 500 labels; `forgejo_web_url` null or https. `KhConfig()` with no arguments is valid (org `kh`, bot `kh-bot`, indexer `kh-indexer`, no people, no groups, no labels, `hub_url` null); `config.bots` is `frozenset({bot_login, indexer_login})`; `hub_url` is null or an http(s) URL without a path. `people` logins match the login regex and are unique after case-folding; an email contains exactly one `@`, has at most 254 characters, is stored lower-case and is unique across people (`config: email <e> belongs to two people`); `bot_login` and `indexer_login` are not in `people`; every group member is in `people` (`config: group <g> member <login> is not in people`). `KhConfig.audiences_for_email(email: str | None) -> frozenset[str]` returns the groups whose members include the person with that lower-cased email, and an empty set for `None` or an unknown email. Derived Forgejo teams: `<group>` (write on its repo) and `<group>-approve` (read on its repo, in the merge whitelist). Rejected: A1 `repos:` map with a separate approve group (lets an approve group become an audience with no repo).

### 3.5 Schema (single migration `0001_baseline.sql`)

Fresh install only. `db/migrate.py` refuses to run when `kh.schema_migrations` holds a name the package does not ship: `database predates the baseline; a fresh install is required`. Roles `kh_migrator`, `kh_app`, `kh_indexer` (no `kh_provenance`), all NOSUPERUSER NOBYPASSRLS, created outside migrations. Every table has ENABLE and FORCE RLS and a `kh_migrator_all` policy (`USING (true) WITH CHECK (true)`).

| Table | Columns (all NOT NULL unless noted) | kh_app | kh_indexer |
|---|---|---|---|
| `schema_migrations`, `claim_jti` | copy of old `0001` and `0002` (table DDL) | as old `0003_rls.sql:300-316` | as old `0003_rls.sql:300-316` (SELECT and DELETE of expired rows) |
| `groups` | `name` PK (group regex), `repo` UNIQUE | SELECT | SELECT |
| `labels` | `name` PK (label regex), `description` default `''`, `created_at` | SELECT | SELECT |
| `documents` | `id` uuid PK default `gen_random_uuid()`, `repo`, `path`, `doc_key`, `audience` FK `groups(name)`, `title`, `body`, `labels text[]` default `{}`, `review_by date`, `stale bool` default false, `commit_sha`, `blob_sha`, `indexed_at`, `updated_at`; UNIQUE `(repo,doc_key)`, `(repo,path)` | SELECT where `can_read(audience)` | ALL |
| `records` | `id` uuid PK, `audience` FK `groups(name)`, `source`, `source_ref`, `source_url` default `''`, `title`, `blob_key`, `excerpt`, `content_type`, `occurred_at timestamptz`, `labels text[]`, `written_by`, `created_by_sub`, `created_by_type`, `ingested_at`; UNIQUE `(audience,source,source_ref)` | SELECT where `can_read(audience)`; INSERT with check `can_read(audience) AND created_by_sub = kh.caller_sub()`; no UPDATE or DELETE | SELECT |
| `chunks` | `id`, exactly one of `document_id` FK cascade or `record_id` FK cascade (CHECK), `owner_key`, `owner_created_at`, `ordinal`, `heading_path`, `text`, `content_sha`, `embedding vector(1024)` null, `embedding_model` null, `tsv` generated english, `tier` CHECK in (`T0`,`T3`), `audience`, `labels text[]`, `stale`; indexes `owner_key`, GIN `tsv` | SELECT where `can_read(audience)` | ALL |
| `index_jobs` | `id` identity, `kind` CHECK in (`git_push`,`embed_record`), `repo` null, `payload jsonb`, `state`, `attempts`, `last_error`, `created_at`, `run_after` | none | ALL |
| `repo_state` | `repo` PK, `last_indexed_sha`, `indexed_at` | none | ALL |
| `index_alerts` | `id` identity, `repo`, `path`, `message`, `created_at` | none | ALL |

Policies: with forced RLS every grant needs a policy. `kh_app_select` and `kh_indexer_select` `USING (true)` on `groups` and `labels`; `kh_app_select` `USING (kh.can_read(audience))` on documents, records and chunks; `kh_app_insert` on records `WITH CHECK (kh.can_read(audience) AND created_by_sub = kh.caller_sub())`; `kh_indexer_all` `USING (true) WITH CHECK (true)` on every table where the indexer has ALL, and `kh_indexer_select` `USING (true)` on records. Functions and triggers: `kh.caller_audiences()`, `kh.caller_sub()`, `kh.caller_type()` (no `kh.email`); `kh.can_read(text)`; `kh.claim_seen(jti, expires_at)` (definer, copy); `chunks_inherit_owner` (BEFORE INSERT, copies tier, audience, labels, stale, owner key and created time from the owner row); `owner_propagate_to_chunks` (AFTER UPDATE of audience, labels, stale on `documents`); `records_enqueue_embed` (AFTER INSERT on `records`; a SECURITY DEFINER function owned by `kh_migrator` with `SET search_path = kh, pg_temp`, because `kh_app` has no grant on `index_jobs`; it inserts `index_jobs(kind='embed_record', payload={"id": <uuid>})`); `kh.assert_labels_exist(text[])` raising SQLSTATE `P0K05` used by BEFORE INSERT OR UPDATE OF labels triggers on `documents` and `records`; a BEFORE DELETE trigger on `labels` raising `P0K06` while any document or record uses the label (definer function owned by `kh_migrator`). Dedupe: re-ingesting the same `(audience, source, source_ref)` returns the existing row's id through a SELECT under RLS, safe because inserting already requires the audience (this avoids revealing records of other audiences); a concurrent duplicate that hits the unique key is caught as a unique violation and answered by the same SELECT. Rejected: label join tables (arrays plus the two triggers give the same guard with far less SQL); a global unique `(source, source_ref)` (leaks existence).

### 3.6 Indexer (T0)

- `repo` is the bare repository name everywhere (`documents.repo`, `index_jobs.repo`, `repo_state.repo`, `KhConfig`); a webhook's `full_name` is split at `/` and its owner must equal `config.org`.
- Webhook: one org-level hook (push events only) to `POST /webhooks/forgejo`. The repo name comes from `repository.full_name`, falling back to `repository.owner.login + "/" + repository.name` (full_name was not confirmed in research r-20261117-1). HMAC check unchanged; only `refs/heads/main` of a repo listed under a group queues a `git_push` job.
- `sync_repo`: take `pg_advisory_xact_lock(hashtext(repo))`; list the tree of `main` HEAD (path to blob sha); diff against `kh.documents` rows of the repo by `(path, blob_sha)`; files rejected earlier are retried every push because they are not stored. Included files: `*.md`, no dot-prefixed path segment, not the root `README.md`, at most 1048576 bytes (the tree entry's `size`). Fetch changed blobs, parse, reject on error with an `index_alerts` row, then in one transaction delete vanished rows, then upsert, then chunk. `commit_sha` is the head sha; `audience` is the repo's group, always.
- Frontmatter: required `id` (`^[a-z0-9][a-z0-9._-]{0,99}$`), `title`, `review_by` (`YYYY-MM-DD`); optional `labels` (list of strings, each must exist in `kh.labels`, else the file is rejected `unknown label <x>`); other keys are ignored. A duplicate `id` in one repo rejects every file that holds it. `stale` is `review_by < today`, recomputed by `kh daily`.
- Embedding: unchanged providers (`ollama`, `http`, `fake`); `may_embed` becomes "audience in `KH_EMBED_HTTP_ALLOWED_AUDIENCES`" for the http provider. T3 bodies are read from S3 by `embed_record` jobs.
- CLI: `kh serve`, `kh migrate`, `kh rebuild [--repo R]`, `kh check`, `kh daily`, `kh requeue-embeddings`, `kh lint FILE...` (offline frontmatter check, exit 1 on a problem), `kh setup ...`. Gone: `validate`, `domain-owners`, `governance`, `recompute-visibility`, `--rejudge`.

### 3.7 `kh setup`

`kh setup plan|apply --config kh.yaml [--only postgres|labels|forgejo|ollama|s3] [--prune] [--create-bucket]` (`--create-bucket` only matters for `apply`). `plan` is strictly read-only. Line format `<prefix> <object kind> <name>[: <detail>]` (the prefix carries the verb), with prefixes `+` (create), `~` (update), `-` (remove, only with `--prune`), `!` (refused: a change the script will not make, for example `! group all: changed repo; remove the group and add a new one`) and the informational `?` (unmanaged object) and `i` (information: `i webhook secret not verifiable`, `i owners: <logins> can bypass protection`). Drift is any `+`, `~`, `-` or `!` line. `plan` exits 0 when there is no drift, 1 when there is, 2 on an error; run it on a schedule to catch protection drift. `apply` performs every possible change, prints the same lines, is idempotent, and exits 0, or 2 when an error occurred or any `!` line remains. `?` and `i` lines never change an exit code. Tests assert prefixes and object names, not the detail text. Credentials come from files, never the YAML: `KH_SETUP_PG_ADMIN_DSN_FILE` (optional), `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE`, plus the existing `KH_DB_*_PASSWORD_FILE`, `KH_FORGEJO_URL`, `KH_FORGEJO_BOT_TOKEN_FILE`, `KH_FORGEJO_INDEXER_TOKEN_FILE`, `KH_WEBHOOK_HMAC_FILE`, `KH_OLLAMA_URL`, `KH_EMBED_MODEL`, `KH_S3_*`. The `kh serve` service never mounts the two `KH_SETUP_*` files. Setup never creates Forgejo users or tokens; a missing `bot_login` or `indexer_login` account is an error.

Steps, in order (one module each under `knowledge_hub/setup/`, each a `Step` with `plan() -> list[Change]` and `apply(prune: bool) -> list[Change]`):

1. **postgres** with an admin DSN: create the three roles (passwords from the `KH_DB_*_PASSWORD_FILE` files, always re-set on apply because they cannot be read back), the database owned by `kh_migrator`, `CREATE EXTENSION vector`; then run pending migrations as `kh_migrator`. Without an admin DSN: check only (roles, database, extension exist; pending migrations are drift). Then sync `kh.groups` and the group-owned documents index (groups added; groups removed only with `--prune`, refused when records exist, and their documents are deleted).
2. **labels**: insert new, update descriptions; a removed label is deleted only with `--prune` and only when unused (the `P0K06` trigger refuses; the line says `! label <x>: in use`). After adding a label apply inserts one `git_push` job per repo so files rejected for an unknown label are retried.
3. **forgejo** (admin token): per group: team `<group>` (permission write, `units_map` `{"repo.code": "write", "repo.pulls": "write"}`, `includes_all_repositories` false, `can_create_org_repo` false, members exactly the config) and team `<group>-approve` (permission read, `units_map` `{"repo.code": "read", "repo.pulls": "read"}`, members exactly the approvers); the repo (private, default branch `main`, created with `auto_init` so `main` exists; `has_actions` false, `default_delete_branch_after_merge` true); collaborators: bot write, indexer read; branch protection on `main` through `rule_name: main`, `enable_push: false`, `enable_push_whitelist: false`, `enable_merge_whitelist: true`, `merge_whitelist_teams: ["<group>-approve"]`, `required_approvals: 0`, `block_on_rejected_reviews: true`, `apply_to_admins: true` (field names from research r-20261117-1, https://codeberg.org/forgejo/forgejo/raw/branch/forgejo/modules/structs/repo_branch.go, 2026-11-17; `branch_name` is deprecated). Forgejo silently drops whitelist teams without read access to the repo (https://codeberg.org/forgejo/forgejo/raw/branch/forgejo/models/git/protected_branch.go, 2026-11-17), so the approve team is added to the repo first and apply re-reads the protection after writing and reports any difference as drift. One org-level webhook (type `forgejo`, events `["push"]`, content type `json`, URL `<hub_url>/webhooks/forgejo`) with the secret from `KH_WEBHOOK_HMAC_FILE`, rewritten on every apply (Forgejo never returns it; plan prints the `i` line). A config without `hub_url` makes this step print `! hub_url: not set`. Identity checks when the bot and indexer token files are set: `GET /user` equals the configured login; the bot has no repo admin permission, the indexer no push; neither is an org owner or site admin. Report-only line listing members of the Owners team as people who can bypass protection. Unmanaged teams and repos print `? unmanaged`; `--prune` removes unmanaged teams only; repos are never deleted. A group's repo is immutable: a changed `repo` is `! refused group <g> changed repo; remove the group and add a new one`.
4. **ollama**: `plan` checks the model is listed and that one embedding has length `KH_EMBED_DIM` (1024 in the schema); `apply` pulls it (`POST /api/pull`, stream off).
5. **s3**: check only by default (bucket exists; put, get and delete of `kh-setup-probe/<uuid>`); `apply --create-bucket` also creates a missing bucket (Garage often forbids this to ordinary keys).

The labels and groups sync connect as `kh_migrator` (`settings.dsn("migrator")`); only `PostgresStep` and `LabelsStep` hold that DSN.

### 3.8 MCP tools (six)

All take flat arguments (`str`, `int`, `bool`, `list[str]`, `Literal`), none named `reason` (`tests/unit/test_tool_schemas.py`). Reads are `auto`, writes `popup` in the PrivacyFence declaration (verbs: `search` for search; `read` for get_doc, list_stale, list_labels; `add` for ingest_record; `update` for propose_doc; `effect` strings `Stores a raw record readable by the named group.` and `Opens a pull request that proposes a document change.`), with `allowed_principal_types: [claude_client, n8n_workflow]`; the upstream keeps `email_for_visibility: true`.

| Tool | Signature | Result |
|---|---|---|
| `search` | `query: str, min_tier: Literal["T0","T3"]="T0", labels: list[str]=[], audience: str="", limit: int=10 (1..50), cursor: str=""` | `{hits: [{id, tier, title, audience, labels, repo, path, source_url, occurred_at, blob_sha, stale, score, snippet}], next_cursor}` (`snippet` is the best chunk's text cut to 400 characters; fields that do not apply to a tier are empty strings); `labels` is all-of; with `min_tier="T0"` only T0 is searched and, when no T0 hit exists but T3 hits do, the call fails with the `NORMATIVE` text |
| `get_doc` | `id: str` | `doc:` returns `{id, tier, repo, path, audience, labels, title, body, review_by, stale, commit_sha, blob_sha, source_link}` (`source_link` is `<forgejo_web_url>/<org>/<repo>/src/commit/<commit_sha>/<path>`, or an empty string when `forgejo_web_url` is null); `rec:` returns `{id, tier, audience, labels, title, body (from S3), source, source_ref, source_url, occurred_at, written_by}` |
| `list_stale` | `audience: str="", limit: int=50 (1..50), cursor: str=""` | documents with `stale` true, oldest `review_by` first |
| `list_labels` | none | `[{name, description}]` |
| `ingest_record` | `source: str, source_ref: str, title: str, body: str, audience: str, occurred_at: str (ISO 8601 with a timezone; a naive time is refused with BAD_ARG), labels: list[str]=[], source_url: str="", content_type: str="text/plain"` | `{id, existed: bool}`; refuses unless the caller holds `audience` and it is a configured group, or when the body exceeds `KH_MAX_RECORD_BYTES` (1048576), or a label is unknown; stores the body at S3 key `records/<uuid>`, `excerpt` the first 2000 characters, `written_by` the config's `bot_login`, `created_by_sub` and type from the caller |
| `propose_doc` | `group: str, path: str, id: str, title: str, review_by: str, body: str, labels: list[str]=[], summary: str="", base_blob: str=""` | `{pr_url, pr_number, branch}` |

`propose_doc` rules: caller holds `group` and the group's repo exists; `path` matches `^[a-z0-9][a-z0-9/_-]*\.md$` with no `..`, `//` or dot-segment and is not `README.md`; `base_blob` empty means the path must not exist, otherwise it is passed as `sha` to Forgejo's contents API so Forgejo enforces the race; `id` must not already exist at another path in the repo; text is refused if it holds control characters other than tab and newline, bidirectional controls, HTML comments or tags, or exceeds `KH_PROPOSE_MAX_BYTES` (131072); labels must exist; the bot lists its open pull requests in the repo and refuses at `KH_PROPOSE_MAX_OPEN` (20). The hub renders the file itself (frontmatter keys in the order `id`, `title`, `review_by`, `labels`, strings JSON-quoted, then `---` and the body), parses it back with the indexer's parser and refuses on mismatch, creates branch `bot/propose/<uuid4>` from the head of `main`, puts the one file (commit message `Propose: <title>` plus a blank line and `Requested-by: <sub>`), opens a pull request to `main` titled `Propose: <title>` (at most 120 characters) whose body is `summary` plus the lines `Requested-by: <sub>`, `Principal: <type>`, `Written-by: <bot_login>` (information only, not enforced). The bot client can only write branches starting `bot/` and open pull requests with base `main` (code guard, kept from today's design).

Fixed error texts (module `mcp/errors.py`, one constant each): `NOT_FOUND` "not found", `INTERNAL` "internal error (ref {ref})", `RATE_LIMIT` (unchanged), `CURSOR_FOREIGN` (unchanged), `NORMATIVE` "only T3 records match; repeat with min_tier T3 to see them", `BAD_ID` "id must be doc:<uuid> or rec:<uuid>", `BAD_LABEL` "unknown label", `BAD_ARG` "argument {name} is invalid" (callers pass `name=`), `AUDIENCE_NOT_HELD` "audience is not held by this caller or not configured", `TOO_LARGE` "body is too large", `BAD_TEXT` "text contains forbidden characters", `PATH_EXISTS` "path already exists; pass base_blob to edit it", `BLOB_CHANGED` "the file changed since base_blob", `ID_IN_USE` "id is used by another file in this repo", `TOO_MANY_OPEN` "too many open proposals in this repo", `NOT_CONFIGURED` "writes are not configured", `PROPOSE_FAILED` "could not open the pull request". Obsolete constants are deleted.

### 3.9 Settings

Kept: `KH_ENV`, `KH_DEV_IDENTITY`, `KH_PF_ISSUER/AUDIENCE/JWKS_URL`, `KH_ALLOWED_HOSTS`, `KH_MAX_BODY_BYTES`, `KH_DB_*`, `KH_CONFIG_FILE`, `KH_FORGEJO_URL`, `KH_FORGEJO_BOT_TOKEN_FILE`, `KH_FORGEJO_INDEXER_TOKEN_FILE`, `KH_WEBHOOK_HMAC_FILE`, `KH_EMBED_*`, `KH_OLLAMA_URL`, `KH_S3_*`, `KH_CURSOR_KEY_FILE`, `KH_CHUNKER_VERSION`, `KH_RRF_K`, `KH_SEARCH_POOL`, `KH_STALE_WEIGHT`, `KH_SEARCH_MAX_OFFSET`, `KH_RATE_LIMIT_PER_MIN`, `KH_METRICS_TOKEN_FILE`. Changed: `KH_TIER_WEIGHTS` default `T0:1.0,T3:0.4`; `KH_MAX_RAW_BYTES` becomes `KH_MAX_RECORD_BYTES`. New: `KH_PROPOSE_MAX_BYTES`, `KH_PROPOSE_MAX_OPEN`, `KH_SETUP_PG_ADMIN_DSN_FILE`, `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE`. Removed: `KH_FORGEJO_ORG`, `KH_FORGEJO_NOTES_TOKEN_FILE`, every `KH_AGENT_*`, `KH_CI_STATUS_LOGINS`, `KH_PROMOTION_DAILY_LIMIT`, `KH_SUPERSEDED_WEIGHT`, `KH_DISPUTED_WEIGHT`, `KH_GRIST_*`.

### 3.10 Release

The GitHub release gets the wheel, `SHA256SUMS` and `deploy/kh.example.yaml` next to the GHCR image. `.github/workflows/release.yml` is a protected path: phase p16 does not edit it and reports `protected-change: .github/workflows/release.yml - attach the wheel to the release` with a unified diff made from these literal edits: (a) in job `build`, after the step "Smoke test", insert a step "Build and smoke-test the wheel" whose `run` starts with `set -euo pipefail`, creates `$RUNNER_TEMP/build-venv`, installs `build`, runs `python -m build --wheel --outdir dist`, installs `dist/*.whl` into a second venv `$RUNNER_TEMP/check-venv`, runs `kh --help` there (main does not contain `kh setup` until the feature merges), then `(cd dist && sha256sum ./*.whl > SHA256SUMS)`; and a step `uses: actions/upload-artifact@v4` with `name: wheel`, `path: dist/`, `if-no-files-found: error`; (b) in job `publish`, before "Create tag and GitHub release", a step `uses: actions/download-artifact@v4` with `name: wheel`, `path: dist`; and `dist/*.whl dist/SHA256SUMS deploy/kh.example.yaml` appended to the `gh release create` command. The job set, permissions and strict-shell rule pinned by `tests/unit/test_release_files.py` stay true. Operator install without Docker: `pip install knowledge_hub-<v>-py3-none-any.whl`, then `kh setup plan --config kh.yaml`.

### 3.11 What is deleted, and the keep-list of the demolition phase

Phase p1 deletes everything of the old world except this keep-list, then rewires the shared modules until `ruff check .` and the whole suite pass. Source kept: `__init__.py`, `app.py`, `auth/**`, `blobs/**`, `cli.py`, `config.py` (rewritten in p1), `db/**`, `deps.py`, `embed/**` (`may_embed` takes an audience string), `indexer/__init__.py`, `indexer/chunker.py`, `indexer/frontmatter.py` (rewritten in p3), `mcp/**`, `metrics.py`, `migrations/**` (replaced in p2), `ratelimit.py`, `search/__init__.py`, `search/cursor.py`, `settings.py`, `tools/__init__.py`. Everything else under `src/knowledge_hub` is deleted: `agent/`, `forgejo/`, `governance/`, `grist/`, `jobs/`, `approvals.py`, `merge_rule.py`, `merge_io.py`, `proposals.py`, `visibility.py`, `indexer/{trust,sync_plan,sync,sync_chunks,worker,webhook,rebuild}.py`, `search/sql.py`, every `tools/*.py` except `__init__.py`. Tests kept: unit `test_blobs`, `test_chunker`, `test_claims`, `test_config` (rewritten), `test_cursor`, `test_deploy_files`, `test_docs_links`, `test_embed_providers` (edited), `test_frontmatter`, `test_privacyfence_declaration` (edited), `test_ratelimit`, `test_release_files`, `test_settings` (edited), `test_tool_schemas` (edited), `test_webhook_hmac`; integration `test_migrate_runner`, `test_replay_cache`, `test_rls_policies`, `test_rls_tables`, `test_schema_tables`, `test_smoke`, `test_triggers`; support `__init__.py`, `claims.py`, `fixtures_app.py`, `fixtures_db.py`, `mcp_client.py`, `pg.py`, `principals.py`, `seed.py`, `blobs.py`; also `tests/__init__.py`, `tests/conftest.py` and `tests/data/pf_req_test_vectors.json` (read by `test_claims.py`). All other tests and support files are deleted and recreated by the phase that rebuilds their subject. Later deletions: p2 migrations `0001` to `0009`; p17 `docs/governance.md`. No test module imports from another test module (today `test_list_stale`, `test_sync` and others import helpers from other test files, which makes deleting one break many); shared helpers live in `tests/support/`.

### 3.12 Phase order and why

The old modules import each other and the tests import each other (`config` is imported by 60 files, `forgejo/client.py` by `proposals`, `governance.owners` by `trust` and `merge_rule`), so deleting leaf by leaf cannot keep the suite green. One demolition phase (p1) deletes against the keep-list and rewrites `config.py` and the wiring in the same step. Then: schema (p2), frontmatter and Forgejo clients (p3), sync (p4), worker and webhook (p5), rebuild, jobs and CLI (p6), tools: search (p7), get_doc, list_stale, list_labels (p8), ingest_record (p9), propose_doc (p10), setup core (p11), Ollama and S3 steps (p12), Forgejo admin client (p13), Forgejo step (p14), setup CLI (p15), release and deploy files (p16), docs (p17). Strict chain: `tests/support/*` and `mcp/server.py` are touched by almost every phase. CI runs the integration suite, so green means `tests/integration` too. Rejected: building the new modules beside the old ones (two schemas, double test-support work); deleting leaf by leaf (shown impossible above).

## 5. Manual steps

### Before implementation

None. Every phase runs against fakes and a throwaway Postgres.

### After implementation

**ma1-live-probe: run `kh setup` against a sandbox (about 45 minutes).**
1. Create a sandbox Forgejo organisation in your Forgejo's admin area (Site administration, Organizations) and the two accounts `kh-bot` and `kh-indexer` (Site administration, User accounts). You should see both accounts listed.
2. Create one access token per account (each account's Settings, Applications) and save each in its own file readable only by you; the admin token goes in a third file. Never paste a token into chat.
3. Prepare an empty Postgres 16 with the `vector` extension available, an Ollama server reachable from the machine, and an S3 bucket.
4. Write `kh.yaml` from `deploy/kh.example.yaml` (https://github.com/tkcs-lab/knowledge-hub/blob/claude/two-tier-refactoring-plan-7v6q2m/deploy/kh.example.yaml) with two groups and two labels, and set the `KH_*` file variables listed in https://github.com/tkcs-lab/knowledge-hub/blob/claude/two-tier-refactoring-plan-7v6q2m/docs/runbook.md.
5. Run `kh setup plan --config kh.yaml`. You should see `+` lines, exit code 1.
6. Run `kh setup apply --config kh.yaml`, then `kh setup plan --config kh.yaml` again. You should see only `i` lines and exit code 0; any other line is a bug.
7. In Forgejo open a repo's Settings, Branches: direct push is off, the merge whitelist is exactly `<group>-approve`, admins are bound. Merge a pull request opened by `propose_doc` as an approver and confirm the document appears in `search`. Then, as a person listed in `people`, call `search` from Claude and confirm you read only your groups' documents; a person whose PrivacyFence email is not in `people` must read nothing.
8. Add a third group, run `apply`, and confirm its team, repo and protection appear without changing the first two.
Report back as a pass or fail line per step 5, 6, 7, 8.

**ma2-release-dry-run (about 15 minutes).**
1. After the pull request that applies the `release.yml` change is merged, open https://github.com/tkcs-lab/knowledge-hub/actions/workflows/release.yml.
2. Choose Run workflow on `main` and leave `dry_run` ticked.
3. When the run ends, open it and look under Artifacts for an item named `wheel`.
Report back pass or fail.

**ma3-privacyfence (about 20 minutes).**
1. In PrivacyFence, replace the hub's upstream declaration with the file https://github.com/tkcs-lab/knowledge-hub/blob/claude/two-tier-refactoring-plan-7v6q2m/deploy/privacyfence-upstream.example.yaml and restart PrivacyFence.
2. People need no PrivacyFence mapping: delete the old Workspace-group to audience mapping for this upstream, and check that each person's PrivacyFence email equals their `people` email in `kh.yaml`.
3. Create the n8n tokens again in PrivacyFence (the changed tool gates void old approvals), give each token the group name as its audience, tick the gated tools its workflow calls (`ingest_record`, and `propose_doc` when it proposes changes), and store each new token in the n8n credential that used the old one.
Report back pass or fail.

## 6. Risks and open questions

- **Unresearched Forgejo fields**: the team `units_map` keys and `has_actions` are from memory of the Gitea API; ma1 is the check, and a worker whose fake disagrees stops with `status=blocked`.
- **Forgejo semantics differ from the fake.** Early signal: `plan` right after `apply` still reports drift on a real instance (ma1). Research left open: per-field first versions, whether `apply_to_admins` also stops bypass of the merge whitelist, and the push payload's `repository.full_name`. A worker who sees the fake and the brief disagree stops with `status=blocked`.
- **Seeding by Claude is one popup per record** (flat-schema rule rules out a batch tool). If that is impractical, a follow-up plan adds an operator `kh seed` command; not planned here.
- **The server has no tools between p1 and p7**; nothing is released from the feature branch until the end.
- **p1 is large** (mass deletion plus the `config.py` rewrite and rewiring of `cli.py`, `app.py`, `deps.py`, `settings.py`, `mcp/server.py`, embed). It is deletion-dominated and its keep-list is explicit; a worker who finds a kept file that cannot compile without a deleted one stops with `status=blocked`.
- **Two places name groups.** People's groups live in `kh.yaml`; an n8n workflow's groups live in its PrivacyFence token (3.3). A wrong audience name on an n8n token fails closed (`AUDIENCE_NOT_HELD`). A person whose PrivacyFence email differs from `people` reads nothing, which looks like a bug; the runbook lists it first.
- **`tests/unit/test_privacyfence_declaration.py` asserts registered tools equal declared tools**; p1 sets both to empty sets and p7 to p10 fill them. A worker who finds another test coupled to the old tool list stops with `status=blocked`.
- **Protected files.** `.github/workflows/release.yml` and `.tld/toolkit.yaml` are never edited by a phase.
- **Reviewer note on sizing.** p1 counts deleted files as no logic; its real edits are listed in its brief.

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
    title: Update the PrivacyFence declaration, drop the old group mapping, recreate n8n tokens with group audiences
    why: Tool gates and names changed; existing n8n token approvals are void
verify_after_merge:
  - .venv/bin/python -m pytest -q
final_checks:
  - tld plan deviations check --file docs/plans/26/deviations.md --plan docs/plans/26/plan.md --all exits 0
  - the changelog has entries under "## [Unreleased]" and no version heading
  - grep -rEn "raw_audience_map|allowed_emails|kh_provenance|requested-by|owners\.yml|grist|Grist" src deploy finds nothing
phases:
  - id: p1-demolish-and-config
    title: Delete the old world against the keep-list, rewrite config.py, rewire the shell
    depends_on: []
    complexity: M
    touches:
      - src/knowledge_hub/agent/**
      - src/knowledge_hub/forgejo/**
      - src/knowledge_hub/governance/**
      - src/knowledge_hub/grist/**
      - src/knowledge_hub/jobs/**
      - src/knowledge_hub/indexer/**
      - src/knowledge_hub/tools/**
      - src/knowledge_hub/search/sql.py
      - src/knowledge_hub/approvals.py
      - src/knowledge_hub/merge_rule.py
      - src/knowledge_hub/merge_io.py
      - src/knowledge_hub/proposals.py
      - src/knowledge_hub/visibility.py
      - src/knowledge_hub/config.py
      - src/knowledge_hub/cli.py
      - src/knowledge_hub/app.py
      - src/knowledge_hub/deps.py
      - src/knowledge_hub/settings.py
      - src/knowledge_hub/mcp/server.py
      - src/knowledge_hub/embed/**
      - pyproject.toml
      - deploy/kh.example.yaml
      - deploy/privacyfence-upstream.example.yaml
      - tests/**
    brief: |
      Read first: sections 3.4, 3.9, 3.11 and 3.12 of this plan. Originals are available with `git show origin/main:<path>`.
      Goal: after this phase the repository holds only the keep-list of 3.11, `ruff check .` and `pytest -q` pass, and the server registers no tools.
      1. Delete with `git rm -r` every source path of 3.11 that is not on the keep-list: agent/, forgejo/, governance/, grist/, jobs/, approvals.py, merge_rule.py, merge_io.py, proposals.py, visibility.py, indexer/{trust,sync_plan,sync,sync_chunks,worker,webhook,rebuild}.py, search/sql.py, every tools/*.py except __init__.py. Delete every test file and tests/support file that is not on the test keep-list of 3.11, plus tests/data/** except tests/data/pf_req_test_vectors.json, and tests/golden/**; keep tests/__init__.py, tests/conftest.py and tests/support/__init__.py.
      2. config.py: rewrite as `KhConfig` per 3.4 (pydantic, extra forbid). Names: `ConfigError`, `AUDIENCE_RE`, `GROUP_RE` (same pattern as `AUDIENCE_RE`), `LOGIN_RE`, `LABEL_RE` (the label regex of 3.1), `load_config(path: str | Path | None = None)` (None returns `KhConfig()`), `parse_config(data)`; p3, p4 and p10 import `LABEL_RE` from here. Add `KhConfig.repo_for_group(name) -> str`, `KhConfig.group_for_repo(repo) -> str | None`, `KhConfig.audiences_for_email(email)` (3.4), property `bots`. Defaults: `org="kh"`, `bot_login="kh-bot"`, `indexer_login="kh-indexer"`, `forgejo_web_url=None`, `hub_url=None`, `people={}`, `groups={}`, `labels={}`; a group's `repo` defaults to `kb-<name>`. Every validation rule of 3.4 raises `ConfigError` with a message starting `config:`. Do not import approvals or any deleted module. Export `Config = KhConfig` is NOT allowed; update every importer to `KhConfig`.
      3. embed/: change `may_embed` to take `audience: str`; the Fake and Ollama embedders return True, the http embedder returns `audience in self.allowed_audiences`. Update tests/unit/test_embed_providers.py (no import of visibility).
      4. settings.py: remove the settings listed as removed in 3.9, rename `KH_MAX_RAW_BYTES` to `KH_MAX_RECORD_BYTES`, change the `KH_TIER_WEIGHTS` default to `T0:1.0,T3:0.4`; where settings imported a deleted module for validation, keep the field as a plain string and let p7 validate it. Leave `DevIdentity.email` as it is.
      5. cli.py keeps only `serve` and `migrate`. app.py keeps /mcp, /healthz and /metrics; remove the webhook route, the worker task and the audience-map load. deps.py builds verifier, embedder, blob store, the two pools and the rate limiter, and nothing from forgejo or the worker. mcp/server.py: `Deps(settings, config: KhConfig, app_pool, indexer_pool, embedder=None, blobs=None, verifier=None, ratelimiter=None)`; `register_tools` registers nothing; `build_mcp` stays. cli.py keeps its `if __name__ == "__main__"` guard (p15 runs `python -m knowledge_hub.cli`).
      6. deploy/kh.example.yaml: the example of 3.4 with a `people` entry for every member, groups `all` and `mgmt` and two labels. deploy/privacyfence-upstream.example.yaml: `tools: {}` and the existing `email_for_visibility: true` line kept. pyproject.toml: package-data `knowledge_hub = ["migrations/*.sql"]`.
      7. Edit the kept tests: test_config.py (rewrite: one passing and one failing case per rule of 3.4, including `KhConfig()` valid, and `audiences_for_email` for a member, a non-member, an unknown email, an upper-case email and `None`), test_privacyfence_declaration.py (READ_TOOLS and WRITE_TOOLS empty; its `email_for_visibility` assertions stay), test_tool_schemas.py (no tool is required to exist; keep the rules applied to whatever is registered), test_settings.py, test_deploy_files.py (`test_example_config_loads` asserts groups `all` and `mgmt`; remove the entity default test; leave the backup tests), tests/support/fixtures_app.py (use `KhConfig()`), principals.py, seed.py and the kept integration tests only as far as needed to import and pass. Never loosen an assertion of kept behaviour.
      Stop condition: if a kept file cannot import without a deleted module, or a kept test needs a deleted fixture, stop with status=blocked and name the file.
    acceptance:
      - ".venv/bin/python -m ruff check . passes"
      - ".venv/bin/python -m pytest -q passes"
      - "grep -rnE \"knowledge_hub\\.(agent|forgejo|governance|grist|jobs|approvals|merge_rule|merge_io|proposals|visibility)\" src tests finds nothing"
      - ".venv/bin/python -m pytest tests/unit/test_config.py -q passes"
  - id: p2-new-schema
    title: One baseline migration, plain-audience RLS, three roles
    depends_on: [p1-demolish-and-config]
    complexity: M
    touches:
      - src/knowledge_hub/migrations/**
      - src/knowledge_hub/db/migrate.py
      - src/knowledge_hub/db/caller_tx.py
      - src/knowledge_hub/auth/claims.py
      - src/knowledge_hub/app.py
      - tests/support/pg.py
      - tests/support/seed.py
      - tests/support/principals.py
      - tests/support/mcp_client.py
      - tests/support/fixtures_db.py
      - tests/support/fixtures_app.py
      - tests/integration/**
      - tests/unit/test_claims.py
    brief: |
      Read first: sections 3.3, 3.4 and 3.5. Pattern: `git show origin/main:src/knowledge_hub/migrations/0001_schema_grants.sql`, `0002_tables.sql`, `0003_rls.sql` (`claim_seen` at line 74, claim_jti policies at lines 300-316), `0004_triggers.sql`.
      1. `git rm` the nine files in src/knowledge_hub/migrations. Create 0001_baseline.sql implementing exactly the tables, column lists, grants, policies, functions and triggers of 3.5, including: forced RLS on every table with the `kh_migrator_all` policy; `kh.can_read`; the definer `records_enqueue_embed` with `SET search_path = kh, pg_temp`; `kh.assert_labels_exist` raising SQLSTATE P0K05; the BEFORE DELETE guard on `labels` raising P0K06; `chunks_inherit_owner` and `owner_propagate_to_chunks` cut down to the new columns. No mention of kh_provenance, allowed_emails, any-of clauses or kh.email.
      2. db/migrate.py: when `kh.schema_migrations` lists a name the package does not ship, raise `MigrationError("database predates the baseline; a fresh install is required")`; test it in test_migrate_runner.py by inserting a foreign name with an admin connection and calling `migrate`.
      3. db/caller_tx.py: stop setting `kh.email` (RLS no longer reads it). auth/claims.py: `ClaimMiddleware.__init__` gets `config: KhConfig` and app.py passes `deps.config`; `_build_caller` resolves the audiences as 3.3 says (`claude_client`: `config.audiences_for_email(email)` and the claim's `audiences` are ignored; `n8n_workflow`: the claim's audiences that are keys of `config.groups`; `service`: none); the dev identity goes through the same function. `Caller.email` stays. Update test_claims.py: a person gets the groups of their email though the claim lists others; an unknown email and a claim without an email get none; an upper-case email matches; an n8n claim keeps only configured groups.
      4. tests/support/pg.py `bootstrap_roles`: create kh_migrator, kh_app, kh_indexer only (LOGIN NOSUPERUSER NOBYPASSRLS), database owned by kh_migrator, extension vector.
      5. tests/support/seed.py and principals.py: seed groups `all`, `mgmt`, `hr` into kh.groups, one document and one record per group (with chunks), and callers `alice_all`, `bob_mgmt`, `carol_hr`, `dave_none`, `multi` (all and mgmt), `wf_token` (principal n8n_workflow, audience all). Each person caller has the email `<login>@example.test`, and `principals.py` builds it through the resolver of step 3, never by listing audiences by hand; the email of `dave_none` is in no group. In tests/support/fixtures_app.py add a fixture `kh_config` returning a `KhConfig` with groups `all`, `mgmt`, `hr` (each with one member who is also its approver, and a `people` entry `<member>: <member>@example.test` for each) and make the `deps` fixture use it. Provide helper functions `add_doc(conn, **fields)`, `add_record(conn, **fields)` and `add_chunk(conn, **fields)` returning ids.
      6. Rewrite the integration tests test_schema_tables.py, test_rls_tables.py, test_rls_policies.py, test_triggers.py and test_migrate_runner.py for the new schema: forced RLS on every table; a caller reads only held audiences in documents, records and chunks; kh_app and kh_indexer read all labels and groups; inserting a record needs the audience and `created_by_sub = caller_sub()`; kh_app cannot UPDATE or DELETE; the unique key (audience, source, source_ref) and a duplicate insert; P0K05 and P0K06; chunk triggers copy audience, labels, stale; an embed_record job is queued when kh_app inserts a record; a foreign schema_migrations name is refused. Delete any integration test that no longer applies.
      Stop condition: if a kept test needs a visibility clause, stop with status=blocked.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_schema_tables.py tests/integration/test_rls_tables.py tests/integration/test_rls_policies.py tests/integration/test_triggers.py tests/integration/test_migrate_runner.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
      - "ls src/knowledge_hub/migrations/*.sql prints only src/knowledge_hub/migrations/0001_baseline.sql"
      - "grep -rniE \"kh_provenance|allowed_emails|kh\\.email\" src/knowledge_hub/migrations src/knowledge_hub/db src/knowledge_hub/auth tests/support finds nothing"
  - id: p3-frontmatter-forgejo
    title: New frontmatter parser, Forgejo reader and bot clients, fake Forgejo
    depends_on: [p2-new-schema]
    complexity: M
    touches:
      - src/knowledge_hub/indexer/frontmatter.py
      - src/knowledge_hub/forgejo/__init__.py
      - src/knowledge_hub/forgejo/reader.py
      - src/knowledge_hub/forgejo/bot.py
      - tests/support/forgejo_fake.py
      - tests/unit/test_frontmatter.py
      - tests/unit/test_forgejo_reader.py
      - tests/unit/test_forgejo_bot.py
    brief: |
      Read first: 3.6 and the propose_doc rules of 3.8. Pattern for HTTP plumbing: `git show origin/main:src/knowledge_hub/forgejo/client.py` (`_send`, error handling that never leaks a token) and `indexer/frontmatter.py`.
      1. indexer/frontmatter.py: rewrite `parse_document(text: str, path: str) -> ParsedDoc | Rejected`; `ParsedDoc(id, title, review_by: date, labels: list[str], body)`, `Rejected(reason: str)`. Rules of 3.6: required `id` (`^[a-z0-9][a-z0-9._-]{0,99}$`), `title` (non-empty string), `review_by` (`YYYY-MM-DD`), optional `labels` (list of strings matching the label regex); other keys are ignored; a file without frontmatter or with invalid YAML is rejected. Reason texts: `missing frontmatter`, `invalid frontmatter`, `missing <key>`, `invalid <key>`.
      2. forgejo/__init__.py exports `ForgejoError` (message holds method, path and status only), `ForgejoRefused(ForgejoError)`, `ForgejoReader`, `ForgejoBot`. reader.py `ForgejoReader(base_url, token, org, *, transport=None, timeout=30.0)` with `head_sha(repo, branch)`, `list_tree(repo, sha) -> list[dict]` (recursive, paged, entries with `path`, `sha`, `type`, `size`), `get_raw(repo, path, sha) -> str`, `is_ancestor(repo, base, head) -> bool` (compare API; any 404 or error answers False) and `aclose()`.
      3. bot.py `ForgejoBot(base_url, token, org, *, transport=None, timeout=30.0)` with `head_sha`, `blob_sha(repo, path, ref) -> str | None`, `create_branch(repo, name, from_sha)`, `put_file(repo, branch, path, content, message, prev_blob: str | None)` (sends `sha` only when prev_blob is given), `open_pull(repo, head, base, title, body) -> dict`, `list_open_pulls(repo, author) -> list[dict]`, `aclose()`. The guard refuses (ForgejoRefused) any branch not starting `bot/` with a non-empty suffix, containing `..` or `//`, and any pull request whose base is not `main`.
      4. tests/support/forgejo_fake.py: `FakeForgejo` an in-memory Forgejo behind `httpx.MockTransport`, with `add_repo(name)`, `commit(repo, {path: text | None})` (None deletes; each commit gets a new head sha and every file a git blob sha), and the endpoints the two clients call, including open pulls; used by later phases.
      5. Tests: test_frontmatter.py (every rule), test_forgejo_reader.py, test_forgejo_bot.py (every method and every guard) over the fake.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_frontmatter.py tests/unit/test_forgejo_reader.py tests/unit/test_forgejo_bot.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p4-sync-core
    title: Tree-diff sync of main into documents and chunks
    depends_on: [p3-frontmatter-forgejo]
    complexity: M
    touches:
      - src/knowledge_hub/indexer/sync_plan.py
      - src/knowledge_hub/indexer/sync.py
      - src/knowledge_hub/indexer/sync_chunks.py
      - tests/support/forgejo_fake.py
      - tests/unit/test_sync_plan.py
      - tests/integration/test_sync.py
      - tests/integration/test_sync_chunks.py
    brief: |
      Read first: 3.5, 3.6. Pattern: `git show origin/main:src/knowledge_hub/indexer/sync_chunks.py` (chunk planning and apply) and `sync.py` (transaction shape); write the new modules from scratch.
      1. sync_plan.py: pure `build_sync_plan(config, repo, head_sha, tree, stored, texts, known_labels) -> SyncPlan`; `tree` is a list of `{path, sha}`, `stored` rows `(path, blob_sha, doc_key)`, `texts` maps changed paths to text. Include rules of 3.6 (md only, no dot-prefixed segment, not root README.md, `size` at most 1048576); parse with `parse_document`; reject a file with an unknown label (`file rejected: unknown label <x>`) and every holder of a duplicate id (`duplicate id <id>`); output `upserts`, `deletes` (stored paths absent from the tree or now rejected) and `alerts` as `(path, message)`; rejection messages start `file rejected: `.
      2. sync_chunks.py: `plan_chunks` and `apply_chunks` for table kh.chunks with the p2 columns; tier `T0`; embed only when `embedder.may_embed(audience)`; chunks with an unchanged `content_sha` and model are kept.
      3. sync.py: `async sync_repo(indexer_pool, reader, config, repo, embedder, *, chunker_version)`: read repo_state and stored rows; `head = await reader.head_sha(repo, "main")`; when `last_indexed_sha` is set and `reader.is_ancestor` is False add the alert (path `''`) `main of <repo> was rewritten; run kh rebuild after review` and carry on; read the known labels from kh.labels; fetch changed blobs; plan; chunk and embed outside the transaction; then one transaction: `pg_advisory_xact_lock(hashtext(repo))`, delete vanished rows, upsert documents (`audience = config.group_for_repo(repo)`, `commit_sha = head`, `blob_sha` from the tree), apply chunks, insert alerts that do not already exist for the same (repo, path, message), upsert repo_state. A repo that is in no group is a ValueError.
      4. Tests (no import from other test modules; helpers in tests/support): unit tests of the plan for every rule above and a rejected file retried when it is not stored; integration tests with FakeForgejo and the migrated DB: first sync, edit, delete, rename, unknown label then the label added, duplicate id, rewrite alert, chunk reuse, audience equals the group.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_sync_plan.py tests/integration/test_sync.py tests/integration/test_sync_chunks.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p5-worker-webhook
    title: Job worker, push webhook, app and deps wiring
    depends_on: [p4-sync-core]
    complexity: M
    touches:
      - src/knowledge_hub/indexer/worker.py
      - src/knowledge_hub/indexer/webhook.py
      - src/knowledge_hub/app.py
      - src/knowledge_hub/deps.py
      - src/knowledge_hub/mcp/server.py
      - tests/integration/test_webhook_worker.py
      - tests/integration/test_wiring.py
      - tests/integration/test_metrics.py
    brief: |
      Read first: 3.6, 3.9. Pattern: `git show origin/main:src/knowledge_hub/indexer/worker.py`, `webhook.py`, `app.py`, `deps.py`, `tests/integration/test_metrics.py`.
      1. webhook.py `make_webhook_endpoint(deps)`: POST /webhooks/forgejo; body cap `settings.max_body_bytes` (413); HMAC check with `auth.webhook_hmac`; repo from `repository.full_name`, falling back to `repository.owner.login + "/" + repository.name`; ignore another org and any repo for which `config.group_for_repo` is None (202 ignored); only a `push` event on `refs/heads/main` inserts `index_jobs(kind='git_push', repo)`; everything else 202 ignored.
      2. worker.py `Worker(indexer_pool, reader, embedder, config, blobs, *, chunker_version, poll_seconds=2.0)`: job claim and retry exactly as the old file (five attempts, 30 s times attempts); `git_push` runs `sync_repo`; `embed_record` reads the body from S3 by `blob_key`, chunks it with `chunk_markdown`, embeds when `embedder.may_embed(audience)` and replaces the record's chunks in one transaction.
      3. deps.py: build `ForgejoReader` (token `KH_FORGEJO_INDEXER_TOKEN_FILE`, org `config.org`) and the Worker when `KH_FORGEJO_URL` is set; `Deps` in mcp/server.py gets `reader` and `worker` fields (default None). app.py: webhook route and worker task back, as in the old lifespan (start, stop, wait 10 s, cancel).
      4. Tests: webhook (bad HMAC 401, wrong org, unconfigured repo, wrong ref, full_name fallback, queued job), worker (git_push over FakeForgejo, embed_record with an in-memory blob store, retry then failed after five attempts), wiring and `/metrics` as in the old tests but for the new job kinds. No import from other test modules.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_webhook_worker.py tests/integration/test_wiring.py tests/integration/test_metrics.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p6-rebuild-jobs-cli
    title: rebuild and check, daily, requeue, lint and their CLI commands
    depends_on: [p5-worker-webhook]
    complexity: M
    touches:
      - src/knowledge_hub/indexer/rebuild.py
      - src/knowledge_hub/jobs/__init__.py
      - src/knowledge_hub/jobs/daily.py
      - src/knowledge_hub/jobs/requeue.py
      - src/knowledge_hub/cli.py
      - tests/unit/test_cli_rebuild.py
      - tests/unit/test_cli_lint.py
      - tests/integration/test_rebuild.py
      - tests/integration/test_daily.py
    brief: |
      Read first: 3.6. Pattern: `git show origin/main:src/knowledge_hub/indexer/rebuild.py`, `jobs/daily.py`, `jobs/requeue.py`, `cli.py`.
      1. rebuild.py: `rebuild(deps, repo=None)` clears `repo_state.last_indexed_sha` for the repo (or every configured repo) and runs `sync_repo`; `check(deps) -> list[str]` returns drift lines: `head of <repo> is <a>, indexed <b>`, `document <path> in <repo> has no chunks`, `record <id> has no chunks` (only records older than five minutes), `document <path> in <repo> has audience <a>, config says <b>`.
      2. jobs/daily.py `run_daily(indexer_pool)`: set `stale = (review_by < current_date)` on documents and purge expired `claim_jti`; jobs/requeue.py `requeue_embeddings(indexer_pool)`: insert an `embed_record` job for every record without chunks.
      3. cli.py: add `rebuild [--repo R]`, `check` (prints the drift lines, exit 1 when any), `daily`, `requeue-embeddings`, `lint FILE...` (runs `parse_document` per file, prints `<path>: <reason>`, exit 1 if any problem, else 0). Exit code 2 for settings or config errors, as the old CLI.
      4. Tests: rebuild and check (clean, head moved, missing chunks, audience drift), daily (stale flips, purge), requeue, cli parsing and `lint` (good file, each bad file). No import from other test modules.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_cli_rebuild.py tests/unit/test_cli_lint.py tests/integration/test_rebuild.py tests/integration/test_daily.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p7-search-tool
    title: The search tool, its SQL and the error catalogue
    depends_on: [p6-rebuild-jobs-cli]
    complexity: M
    touches:
      - src/knowledge_hub/mcp/errors.py
      - src/knowledge_hub/search/sql.py
      - src/knowledge_hub/tools/search.py
      - src/knowledge_hub/settings.py
      - src/knowledge_hub/mcp/server.py
      - deploy/privacyfence-upstream.example.yaml
      - tests/unit/test_privacyfence_declaration.py
      - tests/support/seed.py
      - tests/integration/test_search_ranking.py
      - tests/integration/test_search_leaks.py
    brief: |
      Read first: 3.8. Pattern: `git show origin/main:src/knowledge_hub/search/sql.py`, `tools/search.py`, `mcp/errors.py`.
      1. mcp/errors.py: keep `KhError`, `kh_error`, `ToolCall`, `tool_guard` unchanged; replace the constants with exactly the list of 3.8 (names and texts); `RATE_LIMIT` and `CURSOR_FOREIGN` keep their old texts.
      2. search/sql.py: `SearchFilters(query, query_vector, embedding_model, min_tier, labels, audience, limit, offset, pool, rrf_k, tier_weights, stale_weight)`; `parse_tier_weights(text)` requires exactly T0 and T3 with positive numbers; `build_search_query(filters) -> (sql, params)` with the old single-statement shape (vector list, full-text list, RRF, one best chunk per owner, weights) over tiers T0 and T3, `labels @> %(labels)s`, optional `audience = %(audience)s`; no superseded, disputed, agent or domain parts; the meta row counts T0 hits and T3 hits for the NORMATIVE rule. settings.py validates `KH_TIER_WEIGHTS` with `parse_tier_weights`.
      3. tools/search.py `search` with the signature and result of 3.8 (snippet 400 characters, cursor as the old `search/cursor.py`); `min_tier="T0"` searches T0 only and raises NORMATIVE when no T0 hit exists but T3 hits do; `register(server, deps)` wraps with `tool_guard`; mcp/server.py registers it.
      4. Declaration: add `search` (`gate: auto`, `verb: search`); test_privacyfence_declaration READ_TOOLS = {"search"}.
      5. tests/support/seed.py: helpers to create chunked documents and records with embeddings from the fake embedder. Tests: ranking (tier weights, stale weight, RRF), label all-of filter, audience filter, cursor binding to caller and query, NORMATIVE, and the leak matrix: a caller never sees hits of an audience it lacks, in documents, records and chunks, and `dave_none` sees nothing.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_search_ranking.py tests/integration/test_search_leaks.py tests/unit/test_privacyfence_declaration.py tests/unit/test_tool_schemas.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p8-doc-tools
    title: get_doc, list_stale and list_labels
    depends_on: [p7-search-tool]
    complexity: S
    touches:
      - src/knowledge_hub/tools/docs.py
      - src/knowledge_hub/tools/labels.py
      - src/knowledge_hub/mcp/server.py
      - deploy/privacyfence-upstream.example.yaml
      - tests/unit/test_privacyfence_declaration.py
      - tests/integration/test_tool_get_doc.py
      - tests/integration/test_list_stale.py
      - tests/integration/test_list_labels.py
      - tests/integration/test_http_e2e.py
    brief: |
      Read first: 3.8. Pattern: `git show origin/main:src/knowledge_hub/tools/docs.py` (id parsing, keyset cursor helpers).
      1. tools/docs.py: `get_doc(id)` and `list_stale(audience, limit, cursor)` with the arguments and results of 3.8; id parsing `doc:<uuid>` or `rec:<uuid>` else BAD_ID; a missing and a hidden row both answer NOT_FOUND; `rec:` bodies are read through `deps.blobs`; `source_link` as in 3.8; `list_stale` orders by `review_by` then id with a keyset cursor bound to the caller.
      2. tools/labels.py: `list_labels()` returns `[{name, description}]` ordered by name. Register all three in mcp/server.py.
      3. Declaration: `get_doc`, `list_stale`, `list_labels` as `gate: auto`, `verb: read`; test READ_TOOLS = search, get_doc, list_stale, list_labels.
      4. Tests: get_doc for doc and record, hidden equals missing, bad id; list_stale paging and caller filtering; list_labels; an end-to-end test through the HTTP app with signed claims (tests/support/claims.py and mcp_client.py) that calls each of the four read tools.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_tool_get_doc.py tests/integration/test_list_stale.py tests/integration/test_list_labels.py tests/integration/test_http_e2e.py tests/unit/test_privacyfence_declaration.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p9-ingest-record
    title: The ingest_record tool
    depends_on: [p8-doc-tools]
    complexity: S
    touches:
      - src/knowledge_hub/tools/records.py
      - src/knowledge_hub/mcp/server.py
      - deploy/privacyfence-upstream.example.yaml
      - tests/unit/test_privacyfence_declaration.py
      - tests/integration/test_ingest_record.py
    brief: |
      Read first: 3.5 (dedupe), 3.8 (ingest_record). Pattern: `git show origin/main:src/knowledge_hub/tools/raw.py`.
      1. tools/records.py `ingest_record` with the signature and result of 3.8. Order: the audience must be held by the caller and exist in `config.groups` (else AUDIENCE_NOT_HELD); `len(body.encode()) <= settings.max_record_bytes` (else TOO_LARGE); `occurred_at` parsed with `datetime.fromisoformat`, naive refused with BAD_ARG; every label exists in kh.labels (SELECT under the caller, else BAD_LABEL); SELECT an existing row by (audience, source, source_ref) and return `{id, existed: true}`; put the body to S3 key `records/<uuid>` through `deps.blobs`; INSERT with an application-generated uuid (`written_by = config.bot_login`, `created_by_sub`, `created_by_type` from the caller, `excerpt` the first 2000 characters); a unique violation is answered by the same SELECT. Return `{id: "rec:<uuid>", existed: false}`.
      2. Register in mcp/server.py; declaration entry `ingest_record` with `gate: popup`, `verb: add`, `effect: Stores a raw record readable by the named group.`; test WRITE_TOOLS = {"ingest_record"}.
      3. Tests: new record, duplicate returns the existing id, concurrent duplicate, audience not held, audience not configured, naive time, too large, unknown label, S3 failure leaves no row, an embed_record job exists after insert, and a record written by `wf_token` is invisible to a caller without the audience.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_ingest_record.py tests/unit/test_privacyfence_declaration.py tests/unit/test_tool_schemas.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p10-propose-doc
    title: The propose_doc tool
    depends_on: [p9-ingest-record]
    complexity: M
    touches:
      - src/knowledge_hub/propose_render.py
      - src/knowledge_hub/tools/propose.py
      - src/knowledge_hub/settings.py
      - src/knowledge_hub/deps.py
      - src/knowledge_hub/mcp/server.py
      - deploy/privacyfence-upstream.example.yaml
      - tests/unit/test_privacyfence_declaration.py
      - tests/unit/test_propose_render.py
      - tests/integration/test_propose_doc.py
    brief: |
      Read first: 3.8 (propose_doc), 3.9.
      1. settings.py: add `KH_PROPOSE_MAX_BYTES` (131072) and `KH_PROPOSE_MAX_OPEN` (20). deps.py: build `ForgejoBot` from `KH_FORGEJO_BOT_TOKEN_FILE` and `config.org` only when `KH_FORGEJO_URL` and the token file are set; `Deps` gets `forgejo_bot` (default None).
      2. propose_render.py (pure): `validate_path(path) -> bool` (regex and rules of 3.8), `check_text(text) -> bool` (refuse control characters other than tab and newline, bidi controls U+202A to U+202E and U+2066 to U+2069, `<!--`, and any `<tag` HTML tag), `render_file(id, title, review_by, labels, body) -> str` (frontmatter order id, title, review_by, labels; strings JSON-quoted; labels as a flow list; then `---` and the body).
      3. tools/propose.py `propose_doc` per 3.8: NOT_CONFIGURED without `deps.forgejo_bot`; AUDIENCE_NOT_HELD when the caller lacks `group` or the group is unknown; BAD_ARG for a bad path, id, review_by; BAD_TEXT; labels exist (BAD_LABEL); size at most `KH_PROPOSE_MAX_BYTES` (TOO_LARGE); the open-PR cap through `list_open_pulls(repo, config.bot_login)` (TOO_MANY_OPEN); `base_blob` empty requires `blob_sha` None (else PATH_EXISTS), otherwise the live blob sha must equal `base_blob` (else BLOB_CHANGED) and is passed as `prev_blob`; the `id` must not belong to another path of the repo in kh.documents (ID_IN_USE); render, parse back with `parse_document` and refuse on mismatch; branch `bot/propose/<uuid4>` from `head_sha(repo, "main")`; put the file; open the pull request; every ForgejoError becomes PROPOSE_FAILED with the detail logged only. Return `{pr_url, pr_number, branch}`.
      4. Register in mcp/server.py; declaration entry `propose_doc` with `gate: popup`, `verb: update`, `effect: Opens a pull request that proposes a document change.`; test WRITE_TOOLS = {"ingest_record", "propose_doc"}.
      5. Tests: unit tests of render, text and path rules; integration tests over FakeForgejo: create, edit with base_blob, stale base_blob, path exists, id in use, cap reached, caller lacks the group, not configured, the pull request title and body trailer lines, a branch name outside `bot/` is impossible.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_propose_render.py tests/integration/test_propose_doc.py tests/unit/test_privacyfence_declaration.py tests/unit/test_tool_schemas.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p11-setup-core
    title: Setup framework with the postgres and labels steps
    depends_on: [p10-propose-doc]
    complexity: M
    touches:
      - src/knowledge_hub/setup/__init__.py
      - src/knowledge_hub/setup/steps.py
      - src/knowledge_hub/setup/postgres.py
      - src/knowledge_hub/setup/labels.py
      - src/knowledge_hub/db/migrate.py
      - tests/unit/test_setup_steps.py
      - tests/integration/test_setup_postgres.py
    brief: |
      Read first: 3.7 (output format, steps 1 and 2).
      1. steps.py: `@dataclass(frozen=True) Change(prefix: Literal["+","~","-","!","?","i"], text: str)` with `render()` returning `<prefix> <text>`; `class Step(Protocol)`: `name: str`, `async plan() -> list[Change]`, `async apply(prune: bool) -> list[Change]`; `async run(steps, mode: Literal["plan","apply"], prune: bool) -> int` printing every line and returning the exit code of 3.7 (plan 0, 1 or 2; apply 0 or 2). A step that raises prints `setup: <step>: <message>` (never a credential) and the run returns 2.
      2. postgres.py `PostgresStep(config, settings, admin_dsn: str | None)`. With an admin DSN: create roles kh_migrator, kh_app, kh_indexer (LOGIN NOSUPERUSER NOBYPASSRLS; password from `settings.db_*_password_file`, re-set on every apply), the database `settings.db_name` owned by kh_migrator, `CREATE EXTENSION IF NOT EXISTS vector` in it. Without one: missing roles, database or extension are `!` lines and apply changes nothing there. Then pending migrations: add `pending(dsn) -> list[str]` to db/migrate.py (names of shipped files not yet applied) and use it (plan: one `+ migration <name>` line each). Then `kh.groups`: insert missing groups (`+ group <name>`), a changed `repo` is `! group <name>: changed repo; remove the group and add a new one`; a group missing from the config is `? unmanaged group <name>` and, with prune, deleted (its documents first) unless records exist, in which case `! group <name>: still has <n> records`.
      3. labels.py `LabelsStep(config, migrator_dsn: str)`: missing labels `+ label <name>`; changed description `~ label <name>`; a label not in the config `? unmanaged label <name>`, with prune deleted only when unused (the P0K06 refusal becomes `! label <name>: used by <n> rows`). After at least one label was added, apply inserts one `git_push` job per configured repo so rejected files are retried.
      4. Tests: run() exit codes and rendering; the Postgres step integration on its own module-scoped `Cluster()` from tests/support/pg.py (roles are cluster-wide, so the session cluster is never empty): (plan on an empty cluster, apply, second plan has no `+ ~ - !` line, add a group, prune with and without records, wrong repo change, without an admin DSN); labels (add, update, unused removal, used refusal, jobs queued).
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_setup_steps.py tests/integration/test_setup_postgres.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p12-setup-ollama-s3
    title: The ollama and s3 setup steps
    depends_on: [p11-setup-core]
    complexity: S
    touches:
      - src/knowledge_hub/setup/ollama.py
      - src/knowledge_hub/setup/s3.py
      - tests/unit/test_setup_ollama.py
      - tests/unit/test_setup_s3.py
    brief: |
      Read first: 3.7 (steps 4 and 5), 3.9.
      1. ollama.py `OllamaStep(settings, *, transport=None)` (httpx): plan lists the model `settings.embed_model` via `GET <KH_OLLAMA_URL>/api/tags`; absent is `+ model <name>`; present, it embeds one short text with `POST /api/embed` and a length other than `settings.embed_dim` is `! model <name>: returns <n> dimensions, expected <dim>`. apply pulls an absent model with `POST /api/pull` `{"model": <name>, "stream": false}`. Without `KH_OLLAMA_URL` the step raises `KH_OLLAMA_URL is not set`.
      2. s3.py `S3Step(settings, *, create_bucket: bool = False, client=None)` (boto3): a missing bucket is `+ bucket <name>`; apply creates it only when `create_bucket` is true, else the line is `! bucket <name>: does not exist; create it or pass --create-bucket`; an existing bucket is probed with put, get and delete of `kh-setup-probe/<uuid4>` (a failure is `! bucket <name>: probe failed <ErrorCode>`). plan makes no write except the probe.
      3. Tests: Ollama over `httpx.MockTransport` (absent, present, wrong dimension, pull), S3 over botocore Stubber (absent, present, probe failure, create flag).
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_setup_ollama.py tests/unit/test_setup_s3.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p13-forgejo-admin
    title: Forgejo admin client and a stateful fake admin Forgejo
    depends_on: [p12-setup-ollama-s3]
    complexity: M
    touches:
      - src/knowledge_hub/forgejo/admin.py
      - tests/support/forgejo_admin_fake.py
      - tests/unit/test_forgejo_admin.py
    brief: |
      Read first: 3.7 step 3 (branch-protection fields and the silent whitelist drop).
      1. forgejo/admin.py `ForgejoAdmin(base_url, token, org, *, transport=None)` (httpx, same error handling as ForgejoReader) with: `org_teams()`, `create_team(name, permission, description)`, `edit_team(id, **fields)`, `team_members(id)`, `add_team_member(id, login)`, `remove_team_member(id, login)`, `team_repos(id)`, `add_team_repo(id, repo)`; `repos()`, `get_repo(repo)`, `create_repo(repo)` (private, default branch `main`, `auto_init` true), `edit_repo(repo, **fields)`, `collaborators(repo)`, `put_collaborator(repo, login, permission)`; `protection(repo)` (rule `main` or None), `put_protection(repo, body)` (create or edit by `rule_name`); `org_hooks()`, `create_org_hook(body)`, `edit_org_hook(id, body)`; `whoami(token=None)` (`GET /user`, with an explicit token for the identity checks), `user_repo_permission(repo, login)`, `org_owners()`. Every method returns plain dicts and lists.
      2. tests/support/forgejo_admin_fake.py: `FakeForgejoAdmin`, a stateful in-memory implementation of those endpoints behind `httpx.MockTransport`, including: a merge-whitelist team without read access to the repo is silently dropped on write (as research r-20261117-1 reports), unknown team names give 422, protection is stored by `rule_name`.
      3. tests/unit/test_forgejo_admin.py: every method against the fake, plus the drop and the 422.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_forgejo_admin.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p14-forgejo-step
    title: The forgejo setup step with drift checks
    depends_on: [p13-forgejo-admin]
    complexity: M
    worker_level: strong
    worker_level_reason: The step decides what counts as a security drift of branch protection and teams; a missed field means T0 is silently unprotected
    touches:
      - src/knowledge_hub/setup/forgejo.py
      - tests/unit/test_setup_forgejo.py
    brief: |
      Read first: 3.2, 3.3, 3.7 (step 3 in full), 3.4.
      1. setup/forgejo.py `ForgejoStep(config, admin: ForgejoAdmin, *, webhook_secret: str | None, bot_token: str | None, indexer_token: str | None)`. plan and apply cover, per group in config order: team `<group>` (permission write, `includes_all_repositories` false, `can_create_org_repo` false, members exactly `members`) and team `<group>-approve` (permission read, members exactly `approvers`); the repo `config.repo_for_group(group)` (private, default branch main, created with auto_init; `has_actions` false; `default_delete_branch_after_merge` true); both teams on the repo (the approve team before the protection is written); collaborators: `bot_login` write, `indexer_login` read; the protection of 3.7 (`rule_name main`, `enable_push false`, `enable_push_whitelist false`, `enable_merge_whitelist true`, `merge_whitelist_teams ["<group>-approve"]`, `required_approvals 0`, `block_on_rejected_reviews true`, `apply_to_admins true`); after writing it, apply reads the protection back and any difference is a `!` line. Org level: one webhook `<hub_url>/webhooks/forgejo` (type forgejo, events push, content type json, active) whose secret is rewritten on every apply; without `hub_url` print `! hub_url: not set`; plan prints `i webhook secret not verifiable`.
      2. Identity checks when tokens are given: `whoami(token)` login equals `bot_login` or `indexer_login`; the bot has no admin permission on any group repo and the indexer has no push (`user_repo_permission`); neither login is an org owner. Print `i owners: <logins> can bypass protection` for the Owners team members. A missing `bot_login` or `indexer_login` account is `! account <login>: does not exist`.
      3. Unmanaged teams and repos in the org that are not derived from the config print `? unmanaged team <name>` or `? unmanaged repo <name>`; with prune only unmanaged teams are deleted; repos are never deleted. Every line follows the prefixes of 3.7; the drift of every checked field is a `~` line naming the field.
      4. Tests over FakeForgejoAdmin: clean first plan lists only `+` lines; after apply a second plan has no `+ ~ - !` line; each of these drifts is detected by plan and repaired by apply: member added or removed, approver removed, repo setting changed, collaborator missing, protection field changed (each field of step 1), approve team removed from the repo (and the silent whitelist drop caught by the read-back), webhook missing; an unmanaged team and repo; prune removes only the team; identity mismatch; bot with admin permission.
      Stop condition: if the fake and 3.7 disagree on a field, follow 3.7 and stop with status=blocked naming the field.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_setup_forgejo.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
  - id: p15-setup-cli
    title: The kh setup command and the import boundary
    depends_on: [p14-forgejo-step]
    complexity: S
    touches:
      - src/knowledge_hub/setup/cli.py
      - src/knowledge_hub/cli.py
      - src/knowledge_hub/settings.py
      - tests/unit/test_setup_cli.py
      - tests/unit/test_import_boundary.py
    brief: |
      Read first: 3.7 (synopsis, credentials), 3.9.
      1. settings.py: add optional path settings `KH_SETUP_PG_ADMIN_DSN_FILE` and `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE`.
      2. setup/cli.py `main(argv) -> int`: arguments `plan|apply`, `--config` (required), `--only` (one of postgres, labels, forgejo, ollama, s3), `--prune`, `--create-bucket`; build the steps in the order postgres, labels, forgejo, ollama, s3 (PostgresStep with the admin DSN read from its file when set, LabelsStep with `settings.dsn("migrator")`, ForgejoStep with the admin token file, the webhook secret from `KH_WEBHOOK_HMAC_FILE` and the bot and indexer token files when set, OllamaStep, S3Step with `create_bucket`), filter by `--only`, call `steps.run`; on a missing required file or setting print `setup: <name> is not set` and return 2. Credentials are never printed.
      3. cli.py: add the `setup` subcommand delegating to `setup.cli.main`.
      4. tests/unit/test_import_boundary.py: in a subprocess, importing `knowledge_hub.app` must leave `knowledge_hub.forgejo.admin` and `knowledge_hub.setup` out of `sys.modules`.
      5. tests/unit/test_setup_cli.py: argument parsing, `--only`, exit codes with fake steps, missing credentials.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_setup_cli.py tests/unit/test_import_boundary.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
      - ".venv/bin/python -m knowledge_hub.cli setup --help exits 0"
  - id: p16-release-deploy
    title: Compose, backup, declaration and the protected release workflow change
    depends_on: [p15-setup-cli]
    complexity: S
    touches:
      - deploy/compose.example.yaml
      - deploy/backup-kh.sh
      - deploy/privacyfence-upstream.example.yaml
      - tests/unit/test_deploy_files.py
      - tests/unit/test_privacyfence_declaration.py
    brief: |
      Read first: 3.9, 3.10.
      1. deploy/compose.example.yaml: the `knowledge-hub` environment holds no setting outside the kept list of 3.9 plus `KH_PROPOSE_MAX_BYTES` and `KH_PROPOSE_MAX_OPEN`, and never `KH_DEV_IDENTITY` or `KH_SETUP_*` (so no KH_AGENT_*, KH_GRIST_*, KH_FORGEJO_ORG, KH_FORGEJO_NOTES_TOKEN_FILE, KH_PROMOTION_DAILY_LIMIT); remove the `kh_grist_key` secret and any header comment about Grist; every secret stays a `*_FILE` under /run/secrets; do not mount the two KH_SETUP_* files in this service. Do not invent a version tag.
      2. deploy/backup-kh.sh: dump only `kh.records` (groups and labels come from kh.yaml, documents and chunks are rebuilt); keep `--enable-row-security`, `--inserts`, `set -euo pipefail`; the restore comment becomes: `pg_restore`, `kh setup apply`, `kh rebuild`, `kh requeue-embeddings`, S3 bucket sync. tests/unit/test_deploy_files.py: `TABLES = ["records"]`; the check that documents and chunks are not dumped stays.
      3. deploy/privacyfence-upstream.example.yaml: the header comment explains that a person's groups come from `people:` in kh.yaml through the verified email, that an n8n token carries group names as audiences, and keeps the `email_for_visibility` paragraph. Add a test that the header mentions `people`.
      4. Do NOT edit .github/workflows/release.yml (protected). In the phase report put one line `protected-change: .github/workflows/release.yml - attach the wheel and example config to the release` followed by the unified diff built from the literal edits of 3.10, with this one change: the smoke test inside the new step runs `kh --help` (not `kh setup --help`), because main does not yet contain `kh setup`. Build the diff against a scratch copy of the file outside the repository, check `patch --dry-run -p1` exits 0, then copy tests/unit/test_release_files.py to the scratch directory, point its WORKFLOW at the patched scratch copy and run it: it must pass.
      Stop condition: if the patched scratch copy fails an existing release-file assertion, stop with status=blocked and show the failing assertion.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_deploy_files.py tests/unit/test_privacyfence_declaration.py tests/unit/test_release_files.py -q passes"
      - "bash -n deploy/backup-kh.sh exits 0"
      - "grep -rEn \"raw_audience_map|allowed_emails|kh_provenance|requested-by|owners\\.yml|grist|Grist\" src deploy finds nothing"
      - "the phase report contains a protected-change line, a diff that patch --dry-run -p1 accepts, and a passing run of test_release_files.py against the patched scratch copy"
  - id: p17-docs-retirement
    title: README, design, configuration, runbook, changelog
    depends_on: [p16-release-deploy]
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
      Read first: sections 3.1 to 3.10 of this plan, README.md, docs/design.md, docs/configuration.md, docs/runbook.md, and the output of `kh --help` and `kh setup --help`.
      1. README.md: rewrite for two tiers (a two-row tier table), the group model, the six tools, the `kh` commands of 3.6 and 3.7, the unchanged quick start, and links to the three remaining docs. No mention of T1, T2, domains, entities, Grist, owners.yml or governance.
      2. docs/design.md: rewrite from section 3: tiers, forge-only trust and what `plan` verifies, the access model and the one list for people, the accepted residuals of 3.3, the schema and RLS matrix of 3.5, claim middleware and call binding (keep the existing text; email now selects groups), the indexer, search, tools, and a rejected-alternatives table that includes the rejections named in section 3. Keep the PrivacyFence-contract deviation section and extend it: email now selects a person's groups through `people` (owner decision 2026-11-17), and the claim's `audiences` are used only for n8n workflows.
      3. docs/configuration.md: every setting of 3.9, every `kh.yaml` key of 3.4 with defaults and validation, the command table of 3.6, and `kh setup` (options, exit codes, line prefixes, credential files, steps) of 3.7.
      4. docs/runbook.md: install order with `kh setup plan` and `apply`, adding a group (edit kh.yaml, apply, restart `kh serve`), joiners and leavers (edit `people` and the group lists, apply, restart `kh serve`: one file), the first check when a person reads nothing (their PrivacyFence email equals their `people` email), how people change T0 (they open a pull request and an approver merges it; direct push to `main` is off), how an administrator ticks the gated tools on an n8n token (`ingest_record`, `propose_doc`), a scheduled `kh setup plan`, the live probe of ma1, backup and restore, key rotation, releasing (the wheel; the release.yml change reaches main as a protected-change pull request).
      5. `git rm docs/governance.md` and fix every link to it. Every markdown file outside docs/plans stays at most 400 lines.
      6. CHANGELOG.md: under `## [Unreleased]` add Added, Changed and Removed entries for: two tiers, groups, labels, `kh setup`, `kh lint`, `ingest_record`, `propose_doc`, `list_labels`, the wheel on releases, the removed tools, commands, settings and migrations, the breaking fresh-install requirement, the settings renames and the PrivacyFence token recreation. Do not open a version heading.
      7. Run the final_checks greps of the manifest and make them pass.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_docs_links.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
      - "grep -rEn \"raw_audience_map|allowed_emails|kh_provenance|requested-by|owners\\.yml|grist|Grist\" src deploy finds nothing"
```
