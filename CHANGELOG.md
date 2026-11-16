# Changelog

All notable changes to knowledge-hub, the trusted core of the company knowledge layer, are listed here.

## [Unreleased]

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
