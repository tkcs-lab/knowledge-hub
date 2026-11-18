# Changelog

All notable changes to knowledge-hub, the trusted core of the company knowledge layer, are listed here.

## [Unreleased]

### Added

- `archived: true` on a group and `archived_labels` in `kh.yaml` hide a group or label as if removed, without deleting any data; `kh setup` archives the group's Forgejo repo. Unarchiving needs a `kh serve` restart and `kh rebuild --repo <repo>`.
- The table `kh.usage_events` and migration `0002_usage_events.sql`: people's searches and opens, stored for 180 days, the searcher as a keyed hash.
- The tool `usage` (`kh_usage`) with the views `records` and `queries`.
- Settings `KH_USAGE_LOG` and `KH_USAGE_RETENTION_DAYS`.
- The metric `kh_usage_events_dropped_total`.
- Frontmatter key `supersedes` (list of document ids, at most 20, same repo): once the document is merged, `search` stops returning the documents it replaces; `get_doc` shows `supersedes`, `superseded_by` and `outdated`. Alerts `supersedes unknown id`, `superseded by <n> documents` and `supersedes cycle`. `propose_doc` takes `supersedes`.
- The tool `mark_record_outdated` (`kh_mark_record_outdated`, gate `popup`): hides a raw record, optionally with `replaced_by` and `note`; `outdated=false` brings it back.
- `search` argument `include_outdated`, and the fields `outdated` and `replaced_by` on every hit; `get_doc` of a record shows `outdated`, `outdated_at`, `replaced_by` and `note`. Included outdated hits are weighted by `KH_STALE_WEIGHT`.
- `ingest_record` and `propose_doc` return `similar` (up to three items of the same group) and the settings `KH_SIMILAR_LIMIT` and `KH_SIMILAR_MIN_SCORE`.
- Migration `0003_supersede.sql`: run `kh migrate`, then `kh rebuild`; restart PrivacyFence and create the n8n tokens again for the new tool. Every document is read once again, so `commit_sha`, `indexed_at` and `source_link` change once.

### Changed

- `kh daily` also deletes usage events past the retention and prints their count.
- The search statement returns `t0_fts_hits` and `t3_fts_hits` (internal).
- `list_stale` no longer lists superseded documents. Cursors issued before this release answer `cursor does not belong to this query` once.

## [0.3.1] - 2026-11-17

### Changed

- `search` now searches T0 and T3 by default (`min_tier` defaults to `T3`); `min_tier="T0"` still searches documents only and fails with `only T3 records match; repeat with min_tier T3 to see them` when only records match. The tool description says there are exactly two tiers.

### Fixed

- `ingest_record` refuses a NUL character in any text argument with `argument <name> is invalid` before storing anything; before, a NUL late in the body was stored and its embedding job failed on every attempt.
- The `embed_record` job removes NUL characters from a stored body before chunking, so records stored before this fix can be indexed with `kh requeue-embeddings`.
- `kh requeue-embeddings` no longer skips a record whose job was left `running` by a stopped worker.

## [0.3.0] - 2026-11-17

Breaking release: fresh install only. A database with the old migrations is refused (`database predates the baseline; a fresh install is required`), and the n8n tokens in PrivacyFence must be created again.

### Added

- Two tiers: T0 (Markdown on `main` of a group's Forgejo repo, merged by a pull request that one of the group's approvers merges) and T3 (raw records with `ingest_record`). Ids are `doc:<uuid>` and `rec:<uuid>`; tier weights default to `T0:1.0,T3:0.4`.
- Groups: `kh.yaml` lists `people` (Forgejo login to verified email) and `groups` (members, approvers, optional repo). A person reads the groups that list their verified email; an n8n token carries group names as audiences. A change to `people` or a group needs a `kh serve` restart.
- Labels: defined in `kh.yaml`, stored in `kh.labels`, checked on documents and records, and filterable in `search`; the new tool `list_labels` lists them.
- `kh setup plan|apply --config kh.yaml [--only STEP] [--prune] [--create-bucket]` builds and checks the whole environment from `kh.yaml`: Postgres roles, database, extension and tables, groups, labels, Forgejo teams, repos, branch protection and org webhook (read back after every write), the Ollama model and the S3 bucket. `plan` exits 1 on drift and is meant to run on a schedule. New settings `KH_SETUP_PG_ADMIN_DSN_FILE` and `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE`; the serving app never imports the setup code.
- `kh lint FILE...` checks frontmatter offline (`id`, `title`, `review_by`, optional `labels`).
- The tool `ingest_record` stores a raw record readable by a group the caller holds (body in S3 at `records/<uuid>`, 2000-character excerpt, repeat returns the existing id with `existed: true`). It replaces `ingest_raw`.
- The tool `propose_doc` opens a pull request that proposes one document in a group's repo (checked path, text, labels and size; at most `KH_PROPOSE_MAX_OPEN` open proposals). New settings `KH_PROPOSE_MAX_BYTES` and `KH_PROPOSE_MAX_OPEN`.
- The tools `search`, `get_doc` and `list_stale` return the new shapes (`min_tier` `T0` or `T3`, `labels` all-of, `audience`, 400-character snippets); `search` with `min_tier="T0"` fails with `only T3 records match; repeat with min_tier T3 to see them` when only records match.
- The Forgejo push webhook queues a `git_push` job only for `refs/heads/main` of a repo that belongs to a group; the job worker syncs the tree of `main` (a `(path, blob_sha)` diff of `*.md` files) and embeds T3 records; failed jobs retry five times. `kh check` and `kh rebuild [--repo R]` work on the new tables, and `main` being rewritten raises the alert `main of <repo> was rewritten; run kh rebuild after review`.
- Releases: the GitHub release carries the wheel, `SHA256SUMS` and `deploy/kh.example.yaml` next to the GHCR image, so an operator can install without Docker (`pip install knowledge_hub-<v>-py3-none-any.whl`, then `kh setup plan`). The `release.yml` change reaches `main` as a protected-change pull request.
- CI: the `ci` workflow lints, runs the whole test suite on Python 3.12 and 3.13 against PostgreSQL 16 with pgvector, and builds the image on every pull request and every push to `main`.

### Changed

- Forgejo is the control for T0: the indexer reads `main` as Forgejo reports it, and branch protection (no direct push, merge whitelist the `<group>-approve` team) decides what is approved.
- One migration, `0001_baseline.sql`, replaces the nine old ones: tables `groups`, `labels`, `documents`, `records`, `chunks` and the index tables, three roles, and one row-level rule `kh.can_read(audience)`. A record insert needs the audience and `created_by_sub = kh.caller_sub()`. A label is checked by a trigger (SQLSTATE `P0K05`) and a label in use cannot be deleted (`P0K06`).
- A caller's groups come from the verified email and `people`; the audiences of a `claude_client` claim are format-checked but grant nothing, and `KH_DEV_IDENTITY.audiences` is ignored for a `claude_client`. An `n8n_workflow` keeps the claim audiences that are configured groups. The PrivacyFence declaration keeps `email_for_visibility: true`, and its Workspace-group mapping is no longer used for people.
- `kh.yaml` is rewritten: `org`, `bot_login`, `indexer_login`, `forgejo_web_url`, `hub_url`, `people`, `groups` and `labels` replace `repos`, `audiences`, `entities` and the rest; unknown keys are refused. See `deploy/kh.example.yaml`.
- Settings renamed or changed: `KH_FORGEJO_ORG` is now `org:` in `kh.yaml`, `KH_MAX_RAW_BYTES` is `KH_MAX_RECORD_BYTES`, the `KH_TIER_WEIGHTS` default is `T0:1.0,T3:0.4`, and an embedder's `may_embed` takes the audience name.
- `kh daily` recomputes stale flags and purges expired claim ids; `kh requeue-embeddings` queues `embed_record` jobs.
- Deploy files: the compose example holds only the settings the hub still reads; `backup-kh.sh` dumps only `kh.records` and its restore order is `pg_restore`, `kh setup apply`, `kh rebuild`, `kh requeue-embeddings`, S3 sync; the PrivacyFence declaration lists the six tools.
- PrivacyFence: the changed tool gates void old approvals, so every n8n token is created again with group names as audiences and the gated tools `ingest_record` and `propose_doc` ticked.
- Docs: README, design, configuration and runbook are rewritten for two tiers; `docs/governance.md` is deleted.
- PrivacyFence setup: the group-to-audience map in PrivacyFence's `admin_config.json` stays for n8n tokens (people need none), and the administrators who create tokens must be members of the mapped groups through a `static` membership file or the default `snapshot` sync. The declaration example and the runbook say how.

### Removed

- T1 notes, T2 derived records, candidates, conflicts, promotion, domains, entities and Grist, `owners.yml` and the governance gates, the trust walk, the requester record (`kh/requested-by`), `allowed_emails`, any-of audiences and `raw_audience_map`.
- The tools `ingest_raw`, `write_derived`, `write_note`, `propose_change`, `propose_edit`, `get_entity`, `pr_status`, `list_my_proposals` and the other old tools; the agent write tools and `kh.proposals`.
- The commands `kh validate`, `kh domain-owners`, `kh governance`, `kh recompute-visibility` and `kh rebuild --rejudge`.
- The role `kh_provenance`, `kh.email` and the nine old migrations.
- Settings `KH_FORGEJO_NOTES_TOKEN_FILE`, every `KH_AGENT_*`, `KH_CI_STATUS_LOGINS`, `KH_PROMOTION_DAILY_LIMIT`, `KH_SUPERSEDED_WEIGHT`, `KH_DISPUTED_WEIGHT` and `KH_GRIST_*`.

## [0.2.0] - 2026-11-16

### Added

- Governance config: `reference_audiences` per entity kind (default for `cus`: contracts and mgmt), `governance.owner_teams` in `kh.yaml`, and the `owners.yml` model with `CODEOWNERS` rendering.
- `kh governance render` writes the frontmatter JSON Schema, `owners.yml`, `CODEOWNERS`, the pull request template and the `kh` Forgejo workflow for a canonical repository.
- Forgejo client: compare files, branch head lookup, team and membership reads, and `post_status` guarded to the `kh/validate` and `kh/domain-owners` contexts.
- `kh validate --repo R --base B --head H` checks the markdown files a pull request changes in a canonical repository, prints one `path: reason` line per problem, exits 0, 1 or 2 and posts the `kh/validate` commit status. Ids are checked only across the repos the token can read (unreadable ones are skipped), no Grist table is read for a kind the repo may not cite, and errors never name another repository.
- `kh governance render --runner-label` sets the runner label of the rendered workflow, so each audience can have its own runner (default `kh-validate:host`).
- `kh domain-owners --repo R --pr N` checks that the teams named in `owners.yml` (and, for a `widen-audience/<audience>` label, the audience's owner teams) have approved the current head of a pull request, prints one line per missing approval and posts the `kh/domain-owners` commit status.
- `self_merge_teams` in `kh.yaml`: a merged pull request is also trusted when its author, a member of a team that owns every touched path in `owners.yml`, merged it themselves. Such documents carry the new stamp `owner_merge`.
- `stamp` (`reviewed`, `owner_merge`, `anchor` or empty) in `search` and `get_doc` results, next to `approved_by`; `stamp` is a forbidden frontmatter key.
- Merge verdicts are stored in `kh.merge_verdicts` and reused by every sync and rebuild; `kh rebuild --rejudge` judges every merge afresh.
- `bot_logins` in `kh.yaml` (default `[kh-hub-bot]`, which is always included): bots never self-merge and their approvals never count.
- `kh domain-owners` passes a pull request whose author may self-merge as owner.

### Changed

- `kh domain-owners` prints the approval or self-merge that applied when it passes.
- An audience list in `entities.<kind>.audiences` or `raw_audience_map` now means any one of the audiences may read the rows; derived records, candidates and conflicts still need every source. Check multi-audience lists before upgrading (migration 0007).
- `generated_by` and `derived_from` are forbidden frontmatter keys, like the other keys the server writes.
- The indexer skips paths with a dot-prefixed segment, so `.forgejo/pull_request_template.md` raises no alert.
- The PrivacyFence declaration example sets `email_for_visibility: true`.

## [0.1.1] - 2026-11-16

### Changed

- Entity kinds are configurable in `kh.yaml`: each kind names its Grist table, its audiences and an optional label, so the hub is no longer limited to products, components, versions and customers. The old list form keeps working.

## [0.1.0] - 2026-11-15

### Added

- Deployment examples: the release image from GHCR, Ollama with its limits and a one-off model pull, a PrivacyFence upstream declaration, and the install order for a fresh host.
- MCP tools for agents to search, read, list and follow records, plus write tools for raw ingest and derived notes.
- Indexer that builds the database from the canonical Git repositories on every Forgejo webhook push.
- `kh` commands for serving, migrating, rebuilding, checking, recomputing visibility and requeueing embeddings.
- Row-level security that enforces audience visibility, so a caller never sees or detects records outside their audience.
- Container image published to GHCR for linux/arm64.
