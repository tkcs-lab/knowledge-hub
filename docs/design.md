# Knowledge hub design

The hub gives agents one MCP interface over canonical documents in git (T0, T1) and machine-written records in Postgres (T2 derived, T3 raw).
Every result carries its tier and is filtered by the caller's audiences inside Postgres, by row-level security, not in Python.
This document records the decisions as built, the alternatives that were rejected and one deviation from the PrivacyFence contract.

## Tiers

| Tier | Source | Written by | Trust |
|---|---|---|---|
| T0 | canonical repos (`canonical-<audience>`) | merged, approved pull requests | normative |
| T1 | `notes-*` repos | any merge; agents through `write_note` | notes |
| T2 | `derived_records` | `write_derived` | machine summary, cites sources |
| T3 | `raw_records` | `ingest_raw` | raw material, blob in S3 |

`search` defaults to `min_tier=T1`. `min_tier` includes that tier and every higher one.

## Roles and RLS matrix

- Four roles, none superuser, none BYPASSRLS: `kh_migrator` (owns schema `kh`, migrations and backups), `kh_app` (request path), `kh_indexer` (worker, rebuild, daily, recompute, requeue) and `kh_provenance` (NOLOGIN, owns the definer functions, owns no table).
- Roles are created outside the migrations (tests: `tests/support/pg.py`; production: the compose repo). Migrations only `GRANT`.
- Every table has `ENABLE` and `FORCE ROW LEVEL SECURITY`. FORCE also binds the owner, so `kh_migrator` has an explicit `USING (true)` policy on every table.
- Per request `caller_tx` opens a transaction on the `kh_app` pool and sets `kh.audiences`, `kh.email`, `kh.principal_type` and `kh.sub` with `set_config(..., true)` plus `statement_timeout = 5s`.
- `kh.visible(req, emails)` reads each element of `required_audiences` as a clause: audience names sorted and joined by `|`, so `[mgmt, finance]` is `finance|mgmt` and a plain name is a one-member clause. A row is visible when every clause shares at least one audience with the caller's audiences, and `emails IS NULL OR caller_email() = ANY(emails)`. A set of clauses is an AND of ORs. Rows of plain names keep the inlined subset test; only rows with a `|` clause call a helper.
- The check `kh_audience_clauses` (and `kh_src_audience_clauses` on the three source tables) rejects any element that is not a valid sorted, de-duplicated clause of names matching `^[a-z0-9][a-z0-9_-]{0,62}$`.
- Every visibility-bearing table has `required_audiences text[] NOT NULL`, `allowed_emails text[] NULL` and the check `kh_no_open_rows`: audiences non-empty or emails not null. `allowed_emails = '{}'` means nobody.
- `kh_app` reads rows where `visible` holds. It inserts only its own rows: `raw_records` for `n8n_workflow` callers with `ingested_by_sub = caller_sub()`, and `derived_records`, `candidates`, `conflicts` with `created_by_sub = caller_sub()`. It has no grant on `index_jobs`, `repo_state`, `index_alerts` or `raw_audience_map`.
- `kh_indexer` has ALL on the index tables and UPDATE on visibility, decision and closing columns of the record tables only (and on the two snapshot columns of the source tables).
- `kh_provenance` has column-limited SELECT on `documents`, `raw_records`, `derived_records`, `candidates`, `conflicts`, and SELECT on `raw_audience_map` and the three source tables, all `USING (true)`. Only the function bodies apply visibility.
- Definer functions owned by `kh_migrator`: `claim_seen`, `owns_derived`, `owns_candidate`, `owns_conflict`, `my_raw_id`, `candidate_mark`. The owner checks exist because a writer may not be able to see its own parent row.
- Chunks copy tier, domain, status, stale, superseded, owner key and visibility from their owner with the trigger `chunks_inherit_owner`; `owner_propagate_to_chunks` pushes later changes down. `kh check` runs a drift query that must return no rows.
- Caller-path inserts use application-generated UUIDs and never `RETURNING`, because the writer may not be able to read its row.
- No existence leaks: `get_doc` and `get_entity` run one RLS query and answer `not found` for a missing and a hidden row alike. The accepted residual is timing proportional to hidden-row volume in exact vector scans.

## Claim middleware and call binding

- `ClaimMiddleware` is pure ASGI around `/mcp` only. It buffers the body (413 past `KH_MAX_BODY_BYTES`), reads the `X-PrivacyFence-Claim` header and verifies it with PrivacyFence's vendored verifier (`auth/pf_claim_verify.py`, source commit and sha256 in `auth/VENDORED.md`). Any failure is 401 with an empty body and only the error code is logged.
- The replay cache is `kh.claim_seen(jti, expires_at)`, so a claim id works once across processes. The daily job purges expired ids.
- `human` is always refused, `service` is refused on `tools/call`. `email` is lower-cased and kept only for `claude_client`. Audiences must match `^[a-z0-9][a-z0-9_-]{0,62}$`.
- The verifier checks the `req` field, a hash of the tool name and arguments. The middleware stores the parsed name and arguments as `kh_call`. `tool_guard` refuses a dispatch whose tool name differs from `kh_call`, or whose keyword arguments are not exactly the stored arguments over the declared defaults, so the call that runs is the call that was hashed. Any refusal answers `internal error (ref ...)`, because the catalogue has no code for it.
- `GET` and `DELETE` on `/mcp` are verified and answered 405: the server is stateless.
- Errors are fixed texts. Any other exception becomes `internal error (ref <uuid>)` with the detail logged server-side only.
- Tool schemas are flat (`str`, bounded `int`, `bool`, `list[str]`, `Literal`), no argument is called `reason`, and a unit test applies PrivacyFence's skip rules to every registered schema.
- `KH_DEV_IDENTITY` is refused when `KH_ENV=prod`. A dev caller is used only for requests with no claim header.
- Limits: `/metrics` exists only when `KH_METRICS_TOKEN_FILE` is set. The rate limiter (`KH_RATE_LIMIT_PER_MIN` per caller) is per process, so a second replica doubles the allowance.

## Trust walk and anchor rules

- The webhook only queues a job after checking the HMAC signature. Everything is re-fetched from Forgejo with the indexer token, and a push syncs the head of `main` as Forgejo reports it, not the payload.
- For each canonical repo the walk follows the first-parent chain of `main` from the configured `trust_anchor` to HEAD. The anchor comes only from `kh.yaml`; `repo_state.trust_anchor_sha` is a display copy.
- A commit after the anchor passes only if it is a merge commit of a pull request merged into `main` with that merge sha, and the PR has an official, non-dismissed `APPROVED` review on its final head sha by someone other than the author and `kh-hub-bot`.
- A file takes the verdict of the last first-parent commit that changed it. Files last changed at or before the anchor carry `approved_by = ['trust-anchor']`. A file changed by a merge without an approved PR is `disputed`.
- A non-merge commit after the anchor, or an anchor not on the chain, marks the repo untrusted: every document is indexed `disputed` until an operator moves the anchor.
- `trust_anchor: null` means no anchor, so every commit including the root must pass; in practice set an anchor.
- A push that does not descend from `last_indexed_sha` (the sha must be on the first-parent chain of `main`) is refused with an alert and the index stays. After review, `kh rebuild` re-walks from the anchor; a refused sync during rebuild clears `last_indexed_sha` and syncs again.
- `generated_by` and `derived_from` are also forbidden (the server writes provenance), and paths with a dot-prefixed segment (`.forgejo/...`) are not indexed. The CI gate is described in [governance](governance.md).
- Frontmatter may not set `tier`, `audience`, `visibility`, `approved_by` and similar keys (the file is rejected), and may not set `status: disputed`. The server decides tier and audience from the repo.
- `supersedes` and `superseded_by` resolve inside one repo only. Duplicate ids reject every file not last changed by an approved merge.
- Document ids are scoped to a repo: `documents` is unique on `(repo, doc_key)`, `get_doc` takes the row's uuid and supersedes resolves in one repo, so the same id may exist in two repos without effect. There is therefore no cross-repo duplicate alert and no `index_alerts` row for it; `kh validate` checks id uniqueness only across the repos its token can read.
- A T1 repo needs no approval but must be listed in config. T1 files are indexed from `last_indexed_sha` only.
- `get_doc` serves the indexed copy. With `version=<sha>` it requires that sha in `document_versions` (checked through RLS) and returns the whole file with frontmatter, fetched from Forgejo; tier, status, approvals and `source_link` still come from the current row.

## Agent writes

Agents write through five tools: `write_note` (T1), `propose_doc` and `propose_edit` (T0 pull requests), and the reads `pr_status` and `list_my_proposals`. PrivacyFence shows them as `kh_write_note` and so on. Both write families are off until `KH_AGENT_NOTES_ENABLED` and `KH_AGENT_PROPOSALS_ENABLED` are set; each also needs `KH_AGENT_EMAIL_DOMAINS`.

### Trust boundary

- **T1 is writable by agents, unreviewed.** `write_note` commits one file, `agent/<id>.md`, straight to `main` of a `notes-<audience>` repo. No person reviews it, so a note written after an agent read hostile text can enter the index.
- **Marking.** The hub renders the frontmatter itself: `type: agent-note`, `owner: <login>`. No tool argument can set `tier`, `audience`, `visibility` or `approved_*`. The indexer sets `agent_written` on the document and its chunks when the path starts with `agent/` or the type is `agent-note`, so removing the `type` key does not unmark a note. Hits and `get_doc` carry `agent_written`.
- **Search rule.** An agent-written chunk is weighted like T2 and never counts as a normative answer. A query answered only by agent notes gets the `NORMATIVE` error when `min_tier` is T0 or T1 and passes with `min_tier` T2 or T3. When exactly one non-agent file holds an id, it is kept and the agent notes with that id are rejected, so a squatted id cannot remove a human note.
- **Limits.** One note is at most `KH_AGENT_MAX_BODY_BYTES` (128 KiB); at most `KH_AGENT_NOTE_DAILY_LIMIT` notes per requester a day; ids are unique per repo. Only the author (login and `sub`) updates a note, and an update needs `base_commit`, the commit the caller read. Text with control, hidden or bidirectional characters, HTML comments, HTML tags or link reference definitions is refused.
- **What Forgejo still allows.** `kh-notes-bot` has team write on the notes repos, so a leaked token can write any path or branch there and force-push an unprotected `main`. The restriction to `agent/<id>.md` on `main` exists only in the hub's client. The indexer's `main of <repo> was rewritten` alert detects a rewritten `main`.
- **Residual risk, accepted.** A person who approves the PrivacyFence popup can still store false but plausible text for agents of the same audience to read. The marking makes it visible and low-ranked, not impossible. Removal is a revert in the notes repo and `kh rebuild --repo notes-<audience>` (see the runbook).

### Callers and the requester

Only a `claude_client` with a verified email in `KH_AGENT_EMAIL_DOMAINS` may write; n8n is refused. The requester login is the email local part, lower-cased, and must be a member of the Forgejo organisation. Every commit message and PR body ends with `Written-by`, `Requested-by` (login, email, `sub`) and `Proposal` lines. The git author is the token's user: `kh-notes-bot` for T1, `kh-hub-bot` for T0. Targets are chosen from the audiences the caller holds, never from content, and text that cites records wider than or restricted differently from the target repo is refused.

### Three client roles

`ForgejoClient` has a role. `read` (the indexer) refuses every write. `bot` writes `bot/promote/*` and `bot/propose/*` branches, opens pull requests and posts the commit statuses; it never writes `main` and refuses `put_note`. `notes` has one write: `agent/<id>.md` on `main` of a listed T1 repo. The CI status client keeps role `bot`, but only `post_status` is reachable from the CLI and its guard accepts only `kh/validate` and `kh/domain-owners`.

### The requester record and its binding

A person who asks an agent for a T0 change must not be the person who approves it, but the pull request author is the bot. So the hub posts the commit status `kh/requested-by` as `kh-hub-bot` on the PR head, with the description `<login> <proposal-uuid>`. The hub writes a branch only in the call that creates it, checks that the commit has the base as its single parent and changes exactly one path, and posts the record on that commit only. The record is valid only on the current `head.sha`, from a login in `REQUESTER_POSTERS`, with the uuid equal to the suffix of the branch `bot/propose/<uuid>`. A person's commit on top moves the head, so the record is missing; the PR then fails closed and must be closed and proposed again.

### Approval exclusion and authority

`kh domain-owners` and the trust walk both ignore the requester's approval of a `bot/propose/*` PR and fail closed when there is no valid record (`agent proposal has no valid requester record from kh-hub-bot`; the trust walk marks the document `disputed`). For other PRs a record that names an approver also excludes that approver. `propose_change` PRs have no record and keep today's rule.

Authority: the trust walk is the authoritative control. The `kh/domain-owners` status is advisory, because a person with write access can post any status context and Forgejo does not stop a requester approving a bot-authored PR. A requester who approves, posts a fake `kh/domain-owners: success` and merges leaves the document `disputed` in the index and raises `file <path> on <repo>/main has no approved merge PR`. The CI status token must never belong to `kh-hub-bot`, or the record could be forged.

### Proposal rows

`kh.proposals` holds one row per write (kind `note`, `doc` or `edit`), under row-level security of the creator's `sub` and the visibility of the repo. State changes go through the definer function `kh.proposal_mark`. Daily counts use the definer function `kh.count_proposals_today`, which returns only a number so a token with fewer audiences cannot multiply a person's limit. T0 proposals are limited per `(domain, repo)` by `KH_PROMOTION_DAILY_LIMIT` (counting `propose_change` candidates as well) and per requester by `KH_AGENT_PROPOSAL_DAILY_LIMIT`; `propose_change` counts candidates only, so agents cannot exhaust the workflows' budget.

## Provenance exception

A workflow token that ingested a raw record can cite it in `write_derived` although it cannot read it back (the record may be restricted to attendees the token's person does not match).

- `kh.derived_visibility(source_ids)` is `SECURITY DEFINER`, owned by `kh_provenance`. A source counts when `kh.visible` holds or, for raw records only, when `caller_type() = 'n8n_workflow'` and `ingested_by_sub = caller_sub()`.
- It returns the union of the sources' clauses, so the caller must satisfy every source, the intersection of non-null email lists and the document versions, never content. Reads stay strict; the exception lives only in this function.
- The derived row inherits the full restriction. An empty email intersection (the sources share no attendee) is refused (`derived_nobody`).
- A deferred constraint trigger recomputes the visibility at commit from the snapshot columns of `derived_sources` and raises (SQLSTATE `P0K04`) when it differs or there are no sources. The tool turns that into `internal error`.
- Derived records cannot cite derived records the token cannot see: chained derivation is not covered.
- Raw visibility is assigned by trigger `raw_records_assign_visibility` from `raw_audience_map` (`series:<id>` or `folder:<id>`), else from the participants, else the insert is refused. Each matching map entry gives one clause, and the token must hold every mapped audience: ANY-of changes who reads, not who may write.

## Promotion routing and path validation

- Candidates and conflicts take their visibility from `kh.strict_visibility` (`SECURITY INVOKER`, no provenance branch) over their sources, and a deferred trigger checks it at commit. Identical sources give identical visibility, so a dedupe collision can only hit a row the caller could see.
- `route_target_repo` is a pure function. Any source restricted to named people is refused. Let `K` be the set of clauses of all sources. With `restricted_audiences_imply_all: true`, every clause containing `all` becomes `{all}`, and `{all}` is dropped when `K` has another clause. `R`, the audiences whose holders can read every source, is the intersection of the clauses of `K`.
- With `target_id` the sources must fit the target repo: `K` empty, or the repo's audience in `R`, else `WIDER`. Without it: `K` empty means the `all` repo; `R` empty is refused (`SPAN`); otherwise the T0 repos whose audience is in `R` decide: none is `NO_REPO`, one is the target, two or more are refused as `ambiguous` and the caller passes a `target_id`. Widening is a human decision, never a bot's.
- `new_path` must match `^(portfolio|product|technical)/[a-z0-9][a-z0-9/_-]*\.md$`, start with the domain folder, contain no `..` or `//`, and not exist at `main`. The bot therefore cannot write `.forgejo/`, `CODEOWNERS`, `schema/` or governance files.
- The bot works only on branches `bot/promote/<candidate-id>`: `ForgejoClient` raises on any other ref and never pushes to `main`. `open_pull` requires base `main`.
- The candidate row and every `candidate_mark` run in their own committed transaction, so a Forgejo failure keeps the `failed` row for a retry with `candidate_id`.
- A `pull_request` webhook only queues a `pr_event`. The worker re-fetches the PR, compares repo and number with the candidate and then sets `promoted` (merged), `rejected` (closed unmerged from `pr_open`) and closes open conflicts that cite the candidate's sources. A stale close cannot undo a promotion.
- The daily limit per `(domain, target_repo)` is counted under RLS since 00:00 UTC.

## Search

One statement inside `caller_tx`: an exact vector order (no ANN index) fused by RRF with a full-text list, each limited to `KH_SEARCH_POOL`, weighted by tier and by stale, superseded and disputed flags, one best chunk per owner. A `disputed` hit never counts as a normative answer: when no non-disputed T0 or T1 hit is visible but lower tiers match, the call fails with `NORMATIVE`. Cursors are HMAC-bound to the caller, the query and the filters, with a bounded offset.

## Visibility changes

- The audience-map loader does not recompute visibility when no `__config__` hash is stored. After an install or a restore the operator runs `kh recompute-visibility` once; until then `kh check` reports config drift.
- `kh recompute-visibility` rewrites `raw_audience_map` from config, then raw rows, document visibility, derived rows, candidates and conflicts in one transaction. It refreshes the source snapshots, so deleting a source never widens a row.
- `kh daily` sets entity visibility from `config.entities` on every run; recompute does not touch `entities_cache`.

## Deviation from the PrivacyFence contract (resolved)

`docs/identity-claim.md` in PrivacyFence says only `sub`, `principal_type`, `audiences` and `req` may change what a query returns, and calls `email` audit-only. The hub uses `email` for visibility: `allowed_emails` rows (meeting attendees, for example) match the claim's lower-cased `email`, and only for `claude_client` callers. The owner decided this on 2026-11-15 because attendee-only records cannot be expressed with group audiences.

PrivacyFence 8.0.0a3 ships the opt-in key `email_for_visibility` (PrivacyFence ADR 0310). The declaration sets it, so PrivacyFence puts the verified email in the claim for this upstream.

What the hub relies on: `email` is lower-cased; it is ASCII only; it is present only for `claude_client` (the hub also drops it for every other principal, `auth/claims.py`); a change to any of these is a breaking contract change in PrivacyFence.

## Rejected alternatives

| Alternative | Why not |
|---|---|
| SQLAlchemy and Alembic | Raw policies, triggers and the fusion query dominate; `psycopg` 3 with a small migration runner is lighter. |
| HNSW index from day one | Older pgvector filters after the index scan, so narrow audiences lose recall. Exact scans are correct; speed is measured after the first deploy. |
| Filtering in Python | The requirement is enforcement in the database. |
| Indexing in the webhook request | Embedding is slow and Forgejo times out; the webhook queues a job. |
| The SDK's `auth=` hook | The claim is in its own header and verification needs the raw body. |
| Filesystem blob store | Owner declined; S3 from day one (`blobs/s3.py`, path-style, one object version, history from backups). |
| A second auth path | Owner declined; one claim path, n8n tested with signed claims. |
| Policy requiring a non-empty audience list | It would hide email-only rows from people in no group. |
| Two visibility columns (any and all) | Lossy for derived rows; a set of OR-clauses in the existing column is exact. |
| `get_doc` fetching from Forgejo | Slow and unfiltered; `get_doc` is an indexed lookup under RLS and Forgejo is read only for `version=`. |
| Flat path `<domain>/<id>.md` for agent T0 documents | Fails `kh validate` for every PR; the path follows the governance layout and the hub fills `type` and `owner`. |
| Requester only in a commit trailer or the PR body | Writers can edit the body; the status is posted by a login only the hub holds. |
| Requester record valid on any commit of the PR | A person's commit on top, or a force-push of another proposal's commit, would carry a foreign record; it is valid on the head only. |
| Embedding similarity to restricted chunks as the "email-scoped" check | Slow and gives wrong refusals; the hub requires a verified-email person caller plus a citation check. |
| Refusing every caller who sees an email-restricted row | Refuses nearly everyone. |
| Reusing `kh.candidates` for agent proposals | It requires sources and a derived-record link; proposals have their own table. |
| One Forgejo client role for all | A bug could push to `main` of a canonical repo; three roles. |
| Two approvals by different people for every agent PR | Slower for every PR; excluding the requester is enough. |
