# Changelog

All notable changes to knowledge-hub, the trusted core of the company knowledge layer, are listed here.

## [Unreleased]

### Added

- MCP tools for agents to search, read, list and follow records, plus write tools for raw ingest and derived notes.
- Indexer that builds the database from the canonical Git repositories on every Forgejo webhook push.
- `kh` commands for serving, migrating, rebuilding, checking, recomputing visibility and requeueing embeddings.
- Row-level security that enforces audience visibility, so a caller never sees or detects records outside their audience.
- Container image published to GHCR for linux/arm64.
