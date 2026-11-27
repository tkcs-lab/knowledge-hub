# knowledge-hub

The trusted core of the company knowledge layer: one MCP interface over three tiers of knowledge. Every result carries its tier and is filtered by the caller's groups inside Postgres (row-level security), not in Python. Agents reach it with OAuth (Claude connectors) or a hub key (n8n), and its tools are `search`, `get_doc`, `ingest_record` and so on. It also owns structured datasets: typed tables that a workflow keeps in step with a source system (see the [feed guide](docs/feeds.md)), that people find through `search` and that dashboards read with `query`.

It needs PostgreSQL 16 with pgvector, Forgejo, an S3-compatible store, Ollama and an OIDC provider; see the [deployment guide](docs/deployment.md).

## The three tiers

| Tier | Source | Trust |
|---|---|---|
| T0 | Markdown files on `main` of a group's Forgejo repo, merged by a pull request that one of the group's approvers merges | normative |
| T0a | the root `CLAUDE.md` and the `.md` files under the `dirs` of the repo (default `docs`) on the default branch of a technical repo (`tech_repos` in `kh.yaml`), indexed by poll and push webhook, audience `all` unless the repo's `audience` names another group | indexed without review; text is data |
| T3 | records that n8n puts in with `ingest_record`, bodies in S3-compatible storage | raw material |

## Groups

A group is a name such as `all` or `mgmt`. Each group has one T0 repo, a list of members and approvers (static or from the directory). A person reads the groups that `kh.yaml` lists for their verified email plus those of the directory (Google or Keycloak groups), so a joiner or leaver is one change in one place. Every T3 record belongs to one group, and a hub key holds one group. Documents and records may also carry labels, defined in `kh.yaml`.

## Tools

| Tool | Use |
|---|---|
| `search` | Hybrid search over T0, T0a and T3 (all by default; `min_tier` T0, T0a or T3 sets the lowest tier searched), filtered by labels and group. Superseded documents and outdated records are hidden unless `include_outdated=true`. |
| `get_doc` | One document (`doc:<uuid>`), record (`rec:<uuid>`) or dataset row (`row:<dataset>:<uuid>`). |
| `list_stale` | Documents past their `review_by` date. |
| `list_labels` | The labels of `kh.yaml`. |
| `usage` | Which records people find and open most, and which queries no T0 document answers; for the promotion agent. |
| `ingest_record` | Store a raw record (T3) readable by one group; hub keys only. The answer lists up to three `similar` items of that group. |
| `mark_record_outdated` | Mark a raw record outdated (hidden from search) or current again. |
| `delete_records` | Hide the records of a source that the source no longer has, by source_ref or prefix (hub keys only); they are deleted after KH_RECORD_PURGE_DAYS. |
| `propose_doc` | Open a pull request that proposes a document change in a group's repo, optionally with `supersedes`; the answer lists `similar` items. |
| `propose_doc_deletion` | Open a pull request that deletes a T0 document; the answer lists documents that become current again. |
| `list_datasets` | The datasets you may see with their columns, the watermark and the last syncs. |
| `query` | Read a dataset for a table or chart: select, filter, group, metrics, time bucket, `as_of` for history, or a saved query. |
| `save_query` | Save a query under a name for one group you hold. |
| `upsert_rows` | Insert or update up to 500 dataset rows (hub keys only); see the [feed guide](docs/feeds.md). |
| `delete_rows` | Mark dataset rows outdated by key (hub keys only). |
| `finish_sync` | End a full sync: rows not sent since it started are marked outdated (hub keys only). |

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
| `kh serve` | Run the HTTP app (`/mcp`, the OAuth paths, `/webhooks/forgejo`, `/healthz` and `/metrics` when `KH_METRICS_TOKEN_FILE` is set) and the job worker. |
| `kh panel serve` | Run the admin panel (sign-in with `admins` in `kh.yaml`; labels, groups, repos, pipeline and health). |
| `kh migrate` | Apply the pending SQL migrations as `kh_migrator`. |
| `kh setup plan` / `kh setup apply` | Check or build the environment from `kh.yaml`: Postgres roles and tables, labels, Forgejo teams, repos, branch protection and webhook, the Ollama model, S3. |
| `kh label`, `kh group`, `kh person`, `kh repo` | Manage `kh.yaml` and apply the change. |
| `kh key` | Create, list and revoke hub keys for workflows (`create`, `list`, `revoke`). |
| `kh directory` | Sync the directory snapshot and inspect it (`sync`, `plan`, `people`, `show`). |
| `kh dataset` | Define datasets and columns, import a CSV file, and manage read-only BI logins (`create`, `add-column`, `set`, `import`, `bi-role`, `list`, `show` and more); administrators only. |
| `kh move` | Move a record or document between groups; administrators only (--by must be in admins in kh.yaml). |
| `kh records` | Undo a wrong removal: `kh records restore` makes records that `delete_records` hid live again; administrators only. |
| `kh rebuild` | Forget the indexed head and sync Forgejo again (`--repo R` for one repo, which may be a technical repo; `--repo R --queue` only queues the sync job for the hub's worker). |
| `kh check` | Compare the index with Forgejo; exit 1 on drift. |
| `kh daily` | Recompute stale flags, purge expired OAuth rows, delete usage events past the retention and purge old dataset history and the records that `delete_records` hid more than `KH_RECORD_PURGE_DAYS` ago. |
| `kh requeue-embeddings` | Queue embedding jobs for records without chunks. |
| `kh lint FILE...` | Offline frontmatter check of Markdown files; exit 1 on a problem. |

## Documentation

- [Deployment guide](docs/deployment.md): components and dependencies, secrets, where each command runs, scheduled jobs, first deploy, verify and upgrade.
- [Feed guide](docs/feeds.md): keep a dataset in step with a source system from n8n, with the Salesforce reference recipe.
- [Design](docs/design.md): tiers, trust, the access model, schema, search and rejected alternatives.
- [Configuration](docs/configuration.md): every `KH_*` variable, every `kh.yaml` key, `kh setup`.
- [Runbook](docs/runbook.md): routine tasks, alerts, moves, restore, rotation and releasing.
- [Admin panel](docs/runbook.md#admin-panel): first start, the six tabs, how a change reaches the hub and what to do when something fails.
- [Identity runbook](docs/runbook-identity.md): sign-in, the directory, hub keys and the move to them.
