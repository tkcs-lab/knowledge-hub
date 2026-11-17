# Changelog

All notable changes to knowledge-hub, the trusted core of the company knowledge layer, are listed here.

## [Unreleased]

### Removed

- Two-tier rewrite, step 1 (breaking, fresh install only): the old world is deleted: T1 notes, T2 derived records, candidates, conflicts, promotion, entities and Grist, the governance gates (`kh validate`, `kh domain-owners`, `kh governance`), the trust walk, the agent write tools, the webhook route and the worker. The server registers no tools until the new ones land. The CLI keeps `serve` and `migrate`.
- Settings `KH_FORGEJO_ORG` (now `org:` in `kh.yaml`), `KH_FORGEJO_NOTES_TOKEN_FILE`, every `KH_AGENT_*`, `KH_CI_STATUS_LOGINS`, `KH_PROMOTION_DAILY_LIMIT`, `KH_SUPERSEDED_WEIGHT`, `KH_DISPUTED_WEIGHT` and `KH_GRIST_*`.

### Added

- Two-tier rewrite, step 14: the `forgejo` setup step. Per group it keeps the teams `<group>` (write) and `<group>-approve` (read) with exactly the configured members and approvers, the private repo with `main`, the bot (write) and indexer (read) collaborators, and the branch protection of `main` (no direct push, merge whitelist the approve team, admins bound), read back after every write. It also keeps one org webhook to `<hub_url>/webhooks/forgejo` and rewrites its secret on every apply. It checks the bot and indexer accounts and tokens, lists the org owners who can bypass protection, and reports unmanaged teams and repos; `--prune` deletes unmanaged teams, never repos. The CLI wiring follows.
- Two-tier rewrite, step 13: `ForgejoAdmin` (`knowledge_hub.forgejo.admin`), the admin client for `kh setup` (teams, repositories, collaborators, branch protection by rule name, org webhooks, identity checks). The `forgejo` step follows.
- Two-tier rewrite, step 12: the `ollama` setup step (the embedding model is listed and returns `KH_EMBED_DIM` dimensions; apply pulls it) and the `s3` step (the bucket exists and takes a put, get and delete of a probe key; apply creates a missing bucket only with `--create-bucket`). The CLI wiring follows.
- Two-tier rewrite, step 11: the `kh setup` framework (`knowledge_hub.setup`: change lines, step runner, exit codes) with the `postgres` step (roles, database, `vector` extension, pending migrations, `kh.groups` sync) and the `labels` step (labels sync, retry jobs after a label is added); `knowledge_hub.db.migrate.pending` lists unapplied migrations. The CLI wiring follows.
- Two-tier rewrite, step 10: the `propose_doc` tool opens a pull request that proposes one document in a group's repository (checked path, text, labels and size; at most `KH_PROPOSE_MAX_OPEN` open proposals). New settings `KH_PROPOSE_MAX_BYTES` (131072) and `KH_PROPOSE_MAX_OPEN` (20); the PrivacyFence declaration lists the tool as a `popup` write.

### Changed

- Two-tier rewrite, step 2 (breaking, fresh install only): the nine migrations are replaced by one `0001_baseline.sql` with the tables `groups`, `labels`, `documents`, `records`, `chunks` and the index tables. Row-level security reads one rule, `kh.can_read(audience)`; a record insert needs the audience and `created_by_sub = kh.caller_sub()`. Labels are checked by a trigger (SQLSTATE `P0K05`) and a label in use cannot be deleted (`P0K06`). The role `kh_provenance` and `kh.email` are gone; `kh migrate` refuses a database that holds an older migration name (`database predates the baseline; a fresh install is required`).
- A caller's groups now come from the verified email and the `people`/`groups` of `kh.yaml`; the audiences in a `claude_client` claim are format-checked but no longer grant anything. An `n8n_workflow` claim keeps only the audiences that are configured groups.
- `kh.yaml` is rewritten: `people` (Forgejo login to verified email), `groups` (members, approvers, optional repo) and `labels` replace `repos`, `audiences`, `entities` and the rest; unknown keys are refused. See `deploy/kh.example.yaml`.
- `KH_MAX_RAW_BYTES` is now `KH_MAX_RECORD_BYTES`; the `KH_TIER_WEIGHTS` default is `T0:1.0,T3:0.4`; an embedder's `may_embed` takes the audience name.

### Added

- Two-tier rewrite, step 3: the frontmatter parser takes `id`, `title`, `review_by` and optional `labels` and ignores other keys (rejections read `missing frontmatter`, `invalid frontmatter`, `missing <key>`, `invalid <key>`); a read-only Forgejo client for the indexer (`head_sha`, `list_tree`, `get_raw`, `is_ancestor`) and a bot client that can only write `bot/` branches and open pull requests to `main`.
- Two-tier rewrite, step 4: the sync of `main` into `kh.documents` and `kh.chunks`: a tree diff by `(path, blob_sha)`, `*.md` files only (no dot-prefixed segment, not the root `README.md`, at most 1 MiB), rejection alerts `file rejected: unknown label <x>` and `file rejected: duplicate id <id>`, an alert when `main` was rewritten (`run kh rebuild after review`), and chunk reuse when the content and embedding model are unchanged.
- Two-tier rewrite, step 5: the Forgejo push webhook (`POST /webhooks/forgejo`) queues a `git_push` job only for a push to `refs/heads/main` of a repo that belongs to a group of the configured org, and the job worker is back: `git_push` runs the sync, `embed_record` chunks and embeds a T3 record from its S3 body; failed jobs retry five times with a growing delay. The worker and the read-only Forgejo client start when `KH_FORGEJO_URL` is set.
- Two-tier rewrite, step 6: `kh rebuild [--repo R]` forgets the indexed head and syncs again; `kh check` prints drift (`head of <repo> is <a>, indexed <b>`, `document <path> in <repo> has no chunks`, `record <id> has no chunks` for records older than five minutes, `document <path> in <repo> has audience <a>, config says <b>`) and exits 1 when there is any; `kh daily` recomputes `stale` and purges expired replay-cache rows; `kh requeue-embeddings` queues an `embed_record` job for every record without chunks; `kh lint FILE...` checks frontmatter offline and exits 1 on a problem. Settings and config errors exit 2.
- Two-tier rewrite, step 8: the tools `get_doc` (`doc:<uuid>` or `rec:<uuid>`; a missing and a hidden row both answer `not found`; record bodies come from S3), `list_stale` (stale documents, oldest `review_by` first, keyset cursor bound to the caller) and `list_labels`, all declared `auto`/`read` in the PrivacyFence upstream declaration.
- Two-tier rewrite, step 9: the `ingest_record` tool stores a raw record (T3) readable by a group the caller holds: the body goes to S3 at `records/<uuid>`, the row keeps the first 2000 characters as its excerpt, `written_by` is the bot login, and a repeat of the same `(audience, source, source_ref)` returns the existing id with `existed: true`. A naive `occurred_at`, an oversized body, an unknown label and an audience the caller does not hold are refused. It is declared in the PrivacyFence example as a `popup` write.
- Two-tier rewrite, step 7: the `search` tool is back. It takes `query`, `min_tier` (`T0` or `T3`), `labels` (all of), `audience`, `limit` and `cursor`, fuses vector and full-text lists over T0 documents and T3 records, and returns one best chunk per owner with a 400-character snippet. `min_tier="T0"` fails with `only T3 records match; repeat with min_tier T3 to see them` when only records match. `KH_TIER_WEIGHTS` must name exactly `T0` and `T3`. The error texts of `mcp/errors.py` are the new catalogue.
- CI: the `ci` workflow lints, runs the whole test suite on Python 3.12 and 3.13 against PostgreSQL 16 with pgvector, and builds the image on every pull request and every push to `main`.
- Agent writes (off by default): `write_note` saves an unreviewed note as `agent/<id>.md` in a `notes-*` repo, `propose_doc` and `propose_edit` open a T0 pull request on a `bot/propose/<uuid>` branch, `pr_status` and `list_my_proposals` read the state. Settings `KH_AGENT_NOTES_ENABLED`, `KH_AGENT_PROPOSALS_ENABLED`, `KH_FORGEJO_NOTES_TOKEN_FILE`, `KH_AGENT_EMAIL_DOMAINS`, `KH_AGENT_NOTE_DAILY_LIMIT`, `KH_AGENT_PROPOSAL_DAILY_LIMIT`, `KH_AGENT_MAX_BODY_BYTES` and `KH_CI_STATUS_LOGINS`.
- Table `kh.proposals` (migration 0009_proposals), the `agent_written` flag on documents and chunks, three Forgejo client roles (`read`, `bot`, `notes`), the `kh/requested-by` commit status and the metric `kh_agent_write_failures_total`.

### Changed

- `kh domain-owners` and the trust walk ignore the requester's approval of an agent proposal and fail closed (`agent proposal has no valid requester record from kh-hub-bot`) when its `kh/requested-by` record is missing or invalid; a requester named by such a record never passes as an owner self-merge.
- Search ranks agent notes like T2, never counts them as a normative answer and flags them with `agent_written`; `get_doc` carries the flag.
- Agent proposals count `propose_change` candidates toward the daily limit per `(domain, repo)`; `propose_change` itself still counts candidates only.
- The PrivacyFence declaration example lists the five new tools; the three write tools need an explicit tick on each scoped token.

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
