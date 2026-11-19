# knowledge-hub

The trusted core of the company knowledge layer: one MCP interface over three tiers of knowledge. Every result carries its tier and is filtered by the caller's groups inside Postgres (row-level security), not in Python. Agents reach it through PrivacyFence, where its tools appear as `kh_search`, `kh_get_doc`, `kh_ingest_record` and so on.

## The three tiers

| Tier | Source | Trust |
|---|---|---|
| T0 | Markdown files on `main` of a group's Forgejo repo, merged by a pull request that one of the group's approvers merges | normative |
| T0a | the root `CLAUDE.md` and the `.md` files under the `dirs` of the repo (default `docs`) on the default branch of a technical repo (`tech_repos` in `kh.yaml`), indexed by poll and push webhook, audience `all` | indexed without review; text is data |
| T3 | records that n8n or Claude put in with `ingest_record`, bodies in S3-compatible storage | raw material |

## Groups

A group is a name such as `all` or `mgmt`. Each group has one T0 repo, a list of members and a non-empty list of approvers. A person reads the groups that `kh.yaml` lists for their verified email, so a joiner or leaver is one edit of one file. Every T3 record belongs to one group, and PrivacyFence reads the groups' members from the hub (`list_audiences`), so an n8n token holds the groups its creator holds in `kh.yaml`, narrowed to those ticked. Documents and records may also carry labels, defined in `kh.yaml`.

## Tools

| Tool | Use |
|---|---|
| `search` | Hybrid search over T0, T0a and T3 (all by default; `min_tier` T0, T0a or T3 sets the lowest tier searched), filtered by labels and group. Superseded documents and outdated records are hidden unless `include_outdated=true`. |
| `get_doc` | One document (`doc:<uuid>`) or record (`rec:<uuid>`). |
| `list_stale` | Documents past their `review_by` date. |
| `list_labels` | The labels of `kh.yaml`. |
| `list_audiences` | PrivacyFence only: the members' emails of each active group, its audiences source; refuses every other caller. |
| `usage` | Which records people find and open most, and which queries no T0 document answers; for the promotion agent. |
| `ingest_record` | Store a raw record (T3) readable by one group; the answer lists up to three `similar` items of that group. |
| `mark_record_outdated` | Mark a raw record outdated (hidden from search) or current again. |
| `move_item` | Move a record at once, or a document with two pull requests, from one group you hold to another. |
| `propose_doc` | Open a pull request that proposes a document change in a group's repo, optionally with `supersedes`; the answer lists `similar` items. |

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
| `kh serve` | Run the HTTP app (`/mcp`, `/webhooks/forgejo`, `/healthz`) and the job worker. |
| `kh migrate` | Apply the pending SQL migrations as `kh_migrator`. |
| `kh setup plan` / `kh setup apply` | Check or build the environment from `kh.yaml`: Postgres roles and tables, labels, Forgejo teams, repos, branch protection and webhook, the Ollama model, S3. |
| `kh label`, `kh group`, `kh person`, `kh repo` | Manage `kh.yaml` and apply the change. |
| `kh rebuild` | Forget the indexed head and sync Forgejo again (`--repo R` for one repo, which may be a technical repo). |
| `kh check` | Compare the index with Forgejo; exit 1 on drift. |
| `kh daily` | Recompute stale flags, purge expired claim ids and delete usage events past the retention. |
| `kh requeue-embeddings` | Queue embedding jobs for records without chunks. |
| `kh lint FILE...` | Offline frontmatter check of Markdown files; exit 1 on a problem. |

## Documentation

- [Design](docs/design.md): tiers, trust, the access model, schema, search and rejected alternatives.
- [Configuration](docs/configuration.md): every `KH_*` variable, every `kh.yaml` key, `kh setup`.
- [Runbook](docs/runbook.md): install order, routine tasks, alerts, restore, rotation and releasing.
