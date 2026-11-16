# Changelog

All notable changes to knowledge-hub, the trusted core of the company knowledge layer, are listed here.

## [Unreleased]

### Added

- Agent writes (off by default): `write_note` saves an unreviewed note as `agent/<id>.md` in a `notes-*` repo, `propose_doc` and `propose_edit` open a T0 pull request on a `bot/propose/<uuid>` branch, `pr_status` and `list_my_proposals` read the state. Settings `KH_AGENT_NOTES_ENABLED`, `KH_AGENT_PROPOSALS_ENABLED`, `KH_FORGEJO_NOTES_TOKEN_FILE`, `KH_AGENT_EMAIL_DOMAINS`, `KH_AGENT_NOTE_DAILY_LIMIT`, `KH_AGENT_PROPOSAL_DAILY_LIMIT`, `KH_AGENT_MAX_BODY_BYTES` and `KH_CI_STATUS_LOGINS`.
- Table `kh.proposals` (migration 0007_proposals), the `agent_written` flag on documents and chunks, three Forgejo client roles (`read`, `bot`, `notes`), the `kh/requested-by` commit status and the metric `kh_agent_write_failures_total`.
- Governance config: `reference_audiences` per entity kind (default for `cus`: contracts and mgmt), `governance.owner_teams` in `kh.yaml`, and the `owners.yml` model with `CODEOWNERS` rendering.
- `kh governance render` writes the frontmatter JSON Schema, `owners.yml`, `CODEOWNERS`, the pull request template and the `kh` Forgejo workflow for a canonical repository.
- Forgejo client: compare files, branch head lookup, team and membership reads, and `post_status` guarded to the `kh/validate` and `kh/domain-owners` contexts.
- `kh validate --repo R --base B --head H` checks the markdown files a pull request changes in a canonical repository, prints one `path: reason` line per problem, exits 0, 1 or 2 and posts the `kh/validate` commit status. Ids are checked only across the repos the token can read (unreadable ones are skipped), no Grist table is read for a kind the repo may not cite, and errors never name another repository.
- `kh governance render --runner-label` sets the runner label of the rendered workflow, so each audience can have its own runner (default `kh-validate:host`).
- `kh domain-owners --repo R --pr N` checks that the teams named in `owners.yml` (and, for a `widen-audience/<audience>` label, the audience's owner teams) have approved the current head of a pull request, prints one line per missing approval and posts the `kh/domain-owners` commit status.

### Changed

- `kh domain-owners` and the trust walk ignore the requester's approval of an agent proposal and fail closed (`agent proposal has no valid requester record from kh-hub-bot`) when its `kh/requested-by` record is missing or invalid.
- Search ranks agent notes like T2, never counts them as a normative answer and flags them with `agent_written`; `get_doc` carries the flag.
- Agent proposals count `propose_change` candidates toward the daily limit per `(domain, repo)`; `propose_change` itself still counts candidates only.
- The PrivacyFence declaration example lists the five new tools; the three write tools need an explicit tick on each scoped token.
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
