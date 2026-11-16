# knowledge-hub

The trusted core of the company knowledge layer: one MCP interface over canonical documents in git and machine-written records in Postgres. Every result carries its trust tier and is filtered by the caller's audiences inside Postgres (row-level security), not in Python. Agents reach it through PrivacyFence, where its tools appear as `kh_search`, `kh_get_doc`, `kh_propose_change` and so on.

## The four tiers

| Tier | Source | Trust |
|---|---|---|
| T0 | canonical Forgejo repos, merged after an approved pull request | normative |
| T1 | `notes-*` repos, any merge | notes |
| T2 | derived records written by `write_derived`, citing their sources | machine summary |
| T3 | raw records ingested by workflows, bodies in S3-compatible storage | raw material |

## Audience model

PrivacyFence signs an identity claim for every request, and the hub verifies it with PrivacyFence's vendored verifier. The claim lists the caller's audiences (`all`, `mgmt`, `hr`, `contracts`, ...), which an operator maps from Workspace groups. A row's `required_audiences` is a set of clauses, each one audience or an any-of list, and the row is visible when the caller holds at least one audience of every clause. Rows may also list `allowed_emails`, which must then match the verified email of a person (meeting attendees, for example). A derived record inherits the clauses of all its sources, so it needs every source and can never be wider than its sources. Postgres enforces all of this with four roles under forced row-level security.

## Quick start

```bash
bash scripts/ensure_test_pg.sh        # PostgreSQL 16 with pgvector for the tests
python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
```

## CI

The `ci` workflow (`.github/workflows/ci.yml`) runs on every pull request and every push to `main`: `lint` (ruff), `test (3.12)` and `test (3.13)` (the whole pytest suite against a throwaway PostgreSQL 16 with pgvector) and `image` (builds the Dockerfile on arm64 and runs `kh --help` in it). The commands are the ones in Quick start. A release dry run requires these four checks green on its commit by name (`.github/workflows/release.yml`), so a job renamed in `ci.yml` must be renamed there and in the `main` ruleset too.

## The `kh` commands

| Command | Use |
|---|---|
| `kh serve` | Run the HTTP app (`/mcp`, `/webhooks/forgejo`, `/healthz`) and the webhook worker. |
| `kh migrate` | Apply the SQL migrations as `kh_migrator`. |
| `kh rebuild` | Re-walk trust from the configured anchor and re-index Forgejo. `--rejudge` judges every merge afresh. |
| `kh check` | Compare the index and the config with Forgejo; exit 1 on drift. |
| `kh daily` | Mark stale documents, sync entities from Grist, purge expired claim ids. |
| `kh recompute-visibility` | Rewrite visibility from the config. |
| `kh requeue-embeddings` | Queue embedding jobs for records without chunks. |
| `kh validate` | CI gate for a canonical repo pull request: frontmatter, layout, ids, entities. See [governance](docs/governance.md). |
| `kh domain-owners` | CI gate: owner-team approvals or self-merge by an owner of a pull request. See [governance](docs/governance.md). |
| `kh governance render` | Write the seed files (schema, `owners.yml`, CODEOWNERS, PR template, workflow) of a canonical repo. See [governance](docs/governance.md). |

## Documentation

- [Design](docs/design.md): tiers, roles, row-level security, trust model, decisions and rejected alternatives.
- [Configuration](docs/configuration.md): every `KH_*` variable and every `kh.yaml` key.
- [Runbook](docs/runbook.md): install order, routine tasks, alerts, restore and rotation.
