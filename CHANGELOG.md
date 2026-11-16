# Changelog

All notable changes to knowledge-hub, the trusted core of the company knowledge layer, are listed here.

## [Unreleased]

### Added

- Governance config: `reference_audiences` per entity kind (default for `cus`: contracts and mgmt), `governance.owner_teams` in `kh.yaml`, and the `owners.yml` model with `CODEOWNERS` rendering.
- `kh governance render` writes the frontmatter JSON Schema, `owners.yml`, `CODEOWNERS`, the pull request template and the `kh` Forgejo workflow for a canonical repository.
- Forgejo client: compare files, branch head lookup, team and membership reads, and `post_status` guarded to the `kh/validate` and `kh/domain-owners` contexts.
- `kh validate --repo R --base B --head H` checks the markdown files a pull request changes in a canonical repository, prints one `path: reason` line per problem, exits 0, 1 or 2 and posts the `kh/validate` commit status.

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
