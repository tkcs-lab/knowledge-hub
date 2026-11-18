# Knowledge hub design

The hub gives agents one MCP interface over three tiers: canonical documents in git (T0), technical documents indexed without review (T0a) and raw records in Postgres (T3).
Every result carries its tier and is filtered by the caller's groups inside Postgres, by row-level security, not in Python.
This document records the decisions as built, the alternatives that were rejected and one deviation from the PrivacyFence contract.

## Tiers and groups

| Tier | Source | Written by | Trust |
|---|---|---|---|
| T0 | `kh.documents`, from `*.md` files on `main` of a group's repo | a pull request merged by one of the group's approvers | normative |
| T0a | `kh.documents` (tier T0a), from `docs/**/*.md` and the root `CLAUDE.md` on the default branch of a tech repo | the hourly poll and the push webhook, no approval | indexed without review |
| T3 | `kh.records`, body in S3 | `ingest_record` | raw material |

- A **group** is a name matching `^[a-z0-9][a-z0-9_-]{0,62}$`. It is the audience stored on rows, the audience name an n8n token carries and the access group of one T0 repo. Every group has exactly one repo.
- A **label** matches `^[a-z0-9][a-z0-9_.:-]{0,62}$` (`product:ember` is legal), is defined in `kh.yaml` and stored in `kh.labels`. Label names are visible to every caller; do not put secrets in them.
- An **archived group** (`archived: true` on the group in `kh.yaml`) acts as removed without deleting data: nobody holds it, so its documents and records are hidden, `ingest_record` and `propose_doc` refuse it and nothing is indexed for it. The database is unchanged; the state lives in `kh.yaml` and the hub reads it at start.
- An **archived label** (a name listed under `archived_labels` in `kh.yaml`) vanishes from `list_labels`, from result labels (`search`, `get_doc`, `list_stale`, `usage`) and from search filters, and writes using it fail with `unknown label`. The database is unchanged: the label stays in `kh.labels`, so documents that name it are still indexed, and the state lives in `kh.yaml`.
- Tools return the ids `doc:<uuid>` and `rec:<uuid>`. `search` defaults to `min_tier=T3`, which searches T0, T0a and T3; `min_tier=T0a` searches T0 and T0a; `min_tier=T0` searches approved documents only; tier weights are `T0:1.0,T0a:0.7,T3:0.4`.

## Trust: Forgejo is the control

- The indexer reads the tree of `main` as Forgejo reports it. There is no trust walk, no anchor, no review verdict, no `approved_by`, no `disputed` status and no version table.
- The approval is the merge. Branch protection forbids direct push, the merge whitelist is the group's `<group>-approve` team, and required approvals are 0, so an approver may merge their own pull request. Forgejo's merge API takes `head_commit_id` and answers 409 when the head moved, so a merge through the UI is bound to the head the approver saw.
- One cheap check remains: the indexer raises the alert `main of <repo> was rewritten; run kh rebuild after review` when the new head does not descend from `repo_state.last_indexed_sha`.
- The webhook checks the HMAC signature and only queues a job; everything is re-fetched with the indexer token. Only `refs/heads/main` of a repo listed under a group queues a `git_push` job.

### What `kh setup plan` verifies

T0 trust rests entirely on Forgejo, so `plan` checks it and reports any difference as drift:

- per group, the teams `<group>` (write on the repo) and `<group>-approve` (read, in the merge whitelist) with exactly the configured members and approvers;
- an archived group is checked only for its Forgejo repo being archived;
- the repo is private with `main`, the bot has write and the indexer has read;
- branch protection on `main`: no direct push, the merge whitelist is exactly the approve team, `required_approvals: 0`, `apply_to_admins: true`, read back after every write (Forgejo silently drops whitelist teams that cannot read the repo);
- the identity of the bot and indexer tokens (`GET /user` equals the configured login, the bot has no repo admin, the indexer no push, neither is an org owner or site admin);
- the org webhook exists. Forgejo never returns its secret, so `plan` can only say `i webhook secret not verifiable`;
- for each group with `slack_channel`, exactly one Slack hook on its repo for `pull_request` events with that channel. The URL is a credential that plan cannot compare, so it says `i slack hook <repo>: url not verifiable`. The hook only notifies; it is not part of the trust chain;
- a report-only line lists the members of the Owners team as people who can bypass protection.

`plan` exits 1 on drift, so running it on a schedule catches a protection change made in the Forgejo UI.

## Technical documents (T0a)

- **Trust.** Nothing approves a T0a document. The indexer reads the default branch of a tech repo as Forgejo reports it, so whoever can push there puts text in front of every caller: every row has audience `all`, decided by the owner. Its text is data, not instructions; `search` and `get_doc` say so.
- **Source.** `tech_org` and `tech_repos` in `kh.yaml`. The operator creates the mirror or fork in `tech_org`; `kh setup` creates nothing there. The files are the root `CLAUDE.md` and `docs/**/*.md` (no dot-prefixed segment, at most 1 MiB). The webhook queues a job for a push to the default branch, and the worker polls every tech repo every `KH_TECH_POLL_MINUTES`.
- **Synthesised header.** A tech file is not parsed for frontmatter: a leading `---` block is dropped, so no file is rejected. The title is `<repo>: CLAUDE.md` for the root file, else the first `# ` heading outside a fenced block, else the file name; the key is the path (shortened with a hash past 100 characters); `review_by` is 100 years ahead, so a T0a document is never stale; `supersedes` is empty; labels come only from `labels` of the repo in `kh.yaml`. A label change moves no blob, so the plan relabels the stored documents.
- **No rewrite alert.** Clones and forks are force-pushed upstream all the time, so a tech sync never raises `main ... was rewritten` and the next sync simply follows the head.
- **Prune.** A repo removed from `tech_repos` keeps its T0a documents until `kh setup apply --prune` deletes them and the repo's `repo_state` row; `plan` and `apply` only report them.
- **Usage signals.** `outranked_t0`, `related_docs` and `t0_fts_hits` count T0 only, so a technical document never hides a gap from the promotion agent. `get_doc` returns the real tier and links under `tech_org`.
- **`similar`.** `similar` filters by audience only, so `ingest_record` and `propose_doc` into `all` may list T0a items; their `tier` says T0a.

## Access model

- **Resolution.** `ClaimMiddleware` resolves the caller's groups from the verified claim. A `claude_client` holds the groups of `kh.yaml` in which a member's `people` email equals the claim's verified email, compared in lower case; a claim without an email gives no group, and the claim's `audiences` are ignored. An `n8n_workflow` holds the claim's `audiences` that are configured, non-archived groups, because a workflow has no `people` entry. A `service` calls only `list_audiences`. The config is read when `kh serve` starts, so a change to `people` or to a group needs a restart.
- **The rule.** `kh.can_read(a)` is `a = ANY(kh.caller_audiences())`. A document, record or chunk is visible when its audience is held; inserting a record needs the audience too.
- **One list for people.** `kh.yaml` decides both a person's Forgejo teams and what the hub lets them read, so a joiner or leaver changes one file. PrivacyFence keeps authentication, the verified email and the write popups; its group-to-audience mapping is no longer used for people. A person whose PrivacyFence email differs from their `people` email reads nothing.
- **PrivacyFence reads the groups from the hub.** The declaration sets `audiences_tool: list_audiences`, so PrivacyFence calls that tool itself with a `service` claim (`sub` `privacyfence`) at startup and every `audiences_refresh_seconds` (default 300). The answer, `{"version": 1, "audiences": {group: [email, ...]}}`, lists the `people` emails of each active group. PrivacyFence then needs no `upstream_mcp.kh.groups` map (one that exists is ignored), no membership source for this upstream and no approvals. A scoped token's audiences are its creator's hub groups at each call, intersected with those ticked at creation (PrivacyFence ADR 0298); existing tokens stay valid because the source is not in the declaration digest. A change to `kh.yaml` reaches PrivacyFence after `kh serve` restarts and within one interval. A failed refresh keeps the last answer; an answer older than three intervals gives nobody any audience. The hub's rule is unchanged: it ignores a `claude_client` claim's audiences and keeps an `n8n_workflow` claim's audiences that are configured groups. The tool refuses every caller but PrivacyFence's `service` claim (`service_only`); it is not under `tools` in the declaration, so PrivacyFence never forwards it for a person or workflow.
- **Limits on `kh.yaml`.** PrivacyFence refuses the whole answer, and keeps the previous one, when it has more than 200 audiences, more than 20000 addresses in all, or more than 1000000 characters (it caps the text, which the SDK indents, and the structured content separately). `kh.yaml` is therefore refused at load with more than 200 active groups, more than 20000 group members in all, or an audience table over 1000000 characters. Checking at call time would fail silently inside PrivacyFence and, three intervals later, every n8n token would lose every audience; at start-up and in `kh setup plan` the operator sees it at once.
- **Non-ASCII emails.** They are sent as they are; PrivacyFence drops them, and such a person gets no `email` in a claim anyway, so they read nothing either way.

### Accepted residuals

- A caller holding two groups can `ingest_record` or `propose_doc` into the wider one. The PrivacyFence popup shows `audience` and `group` and is the only gate.
- A leaked bot token can create branches and pull requests, push to open pull-request branches in every repo and read every T0 repo. It cannot merge.
- With required approvals 0 the approver must review the final diff before merging.
- The verified email is the identity: a Workspace email that is reassigned gives its new holder the old person's groups until `people` is edited. Remove a leaver from `people` first.
- A move is not a purge: the old repo's history, past usage queries and backups keep the old copy. The two pull requests of a document move are not ordered; merge the add first.
- Exact vector scans take time in proportion to the number of hidden rows; this timing is the accepted residual of the no-existence-leak rule.
- Push access to a tech repo puts text in front of every caller (audience `all`); `kh setup` reports its collaborators and cannot restrict them.
- The `http` embedder receives T0a text: `KH_EMBED_HTTP_ALLOWED_AUDIENCES` lists audiences, and T0a rows have audience `all`.
- Two replicas can duplicate chunks of one tech repo; this is the same race as for group repos.
- A T3 record may be marked `replaced_by` a T0a document: it is a pointer, not a promotion.
- Stored query text can hold sensitive words. Readers are limited by the subset rule, retention is `KH_USAGE_RETENTION_DAYS`, and `KH_USAGE_LOG=false` switches logging off.

## Roles and RLS matrix

- Three roles, none superuser, none BYPASSRLS: `kh_migrator` (owns schema `kh`, migrations and backups), `kh_app` (request path) and `kh_indexer` (worker, rebuild, daily, requeue).
- Roles are created outside the migrations (tests: `tests/support/pg.py`; production: `kh setup apply` with an admin DSN, or the operator). The migrations `0001_baseline.sql` and `0002_usage_events.sql` only grant and create tables; `0003_supersede.sql` also adds columns and the definer function `kh.mark_record_outdated`. `kh migrate` refuses a database whose `kh.schema_migrations` holds a name the package does not ship: a fresh install is required.
- Every table has `ENABLE` and `FORCE ROW LEVEL SECURITY` and an explicit `kh_migrator_all` policy (`USING (true)`), because FORCE also binds the owner.
- Per request `caller_tx` opens a transaction on the `kh_app` pool and sets the audiences, principal type and `sub` with `set_config(..., true)` plus `statement_timeout = 5s`.

| Table | kh_app | kh_indexer |
|---|---|---|
| `schema_migrations` | none | none |
| `claim_jti` | through `kh.claim_seen` only | SELECT and DELETE of expired rows |
| `groups`, `labels` | SELECT | SELECT |
| `documents` | SELECT where `can_read(audience)` | ALL |
| `records` | SELECT where `can_read(audience)`; INSERT with check `can_read(audience) AND created_by_sub = kh.caller_sub()`; UPDATE only through the definer functions `kh.mark_record_outdated` and `kh.move_record`; no DELETE | SELECT |
| `chunks` | SELECT where `can_read(audience)` | ALL |
| `index_jobs`, `repo_state`, `index_alerts` | none | ALL |
| `record_moves` | none (read only through the definer function `kh.record_moved`, which answers only for a group the caller holds) | none |
| `usage_events` | INSERT for the caller's own group set (people only); SELECT where every group of the searcher is held | SELECT, DELETE |

- `usage_events` is read under the subset rule: an event is visible when `audiences <@ kh.caller_audiences()`, that is, when the reader holds every group of the searcher. This leaks nothing, because each key in an event's `owner_keys` is a hit the searcher could read, so its audience is one of the searcher's groups, and the searcher's groups are a subset of the reader's. A key whose record or document left the reader's groups is dropped by the row-level join in `usage`, and a moved record keeps its id, so no reader learns of an item they cannot read. The searcher's hash (`who`) and `audiences` are never returned by any tool.

- Triggers: `chunks_inherit_owner` copies tier, audience, labels, stale, owner key and created time from the owner row; `owner_propagate_to_chunks` pushes later changes of a document down; `records_enqueue_embed` is a definer function (`kh_app` has no grant on `index_jobs`) that queues an `embed_record` job; `kh.assert_labels_exist` raises SQLSTATE `P0K05` for an unknown label; a BEFORE DELETE trigger on `labels` raises `P0K06` while a document or record uses the label.
- Dedupe: re-ingesting the same `(audience, source, source_ref)` returns the existing row's id through a SELECT under RLS. This is safe because inserting already requires the audience, so it never reveals a record of another audience. A concurrent duplicate is caught as a unique violation and answered by the same SELECT.
- Caller-path inserts use application-generated UUIDs.
- No existence leaks: `get_doc` runs one RLS query and answers `not found` for a missing and a hidden row alike.

## Claim middleware and call binding

- `ClaimMiddleware` is pure ASGI around `/mcp` only. It buffers the body (413 past `KH_MAX_BODY_BYTES`), reads the `X-PrivacyFence-Claim` header and verifies it with PrivacyFence's vendored verifier (`auth/pf_claim_verify.py`, source commit and sha256 in `auth/VENDORED.md`). Any failure is 401 with an empty body and only the error code is logged.
- The replay cache is `kh.claim_seen(jti, expires_at)`, so a claim id works once across processes. The daily job purges expired ids.
- `human` is always refused, `service` is refused on `tools/call` except `list_audiences` with `sub` `privacyfence`. `email` is lower-cased and kept only for `claude_client`. Audiences must match the group regex; a claim with a bad name is refused for every principal type. The email then selects the person's groups through `people`, and the claim's `audiences` are used only for n8n workflows.
- The verifier checks the `req` field, a hash of the tool name and arguments. The middleware stores the parsed name and arguments as `kh_call`. `tool_guard` refuses a dispatch whose tool name differs from `kh_call`, or whose keyword arguments are not exactly the stored arguments over the declared defaults, so the call that runs is the call that was hashed. Any refusal answers `internal error (ref ...)`.
- `GET` and `DELETE` on `/mcp` are verified and answered 405: the server is stateless.
- Errors are fixed texts (`mcp/errors.py`). Any other exception becomes `internal error (ref <uuid>)` with the detail logged server-side only.
- Tool schemas are flat (`str`, bounded `int`, `bool`, `list[str]`, `Literal`), no argument is called `reason`, and a unit test applies PrivacyFence's skip rules to every registered schema.
- `KH_DEV_IDENTITY` is refused when `KH_ENV=prod`, and is used only for requests with no claim header. It follows the rules above: its `audiences` are ignored for a `claude_client`, whose groups come from its `email`.
- `/metrics` exists only when `KH_METRICS_TOKEN_FILE` is set. The rate limiter (`KH_RATE_LIMIT_PER_MIN` per caller) is per process, so a second replica doubles the allowance.

## Indexer

- `repo` is the bare repository name everywhere. A webhook's `full_name` is split at `/` and its owner must equal `org`.
- `sync_repo` takes `pg_advisory_xact_lock(hashtext(repo))`, lists the tree of `main`, and diffs it against `kh.documents` by `(path, blob_sha)`. Files rejected earlier are not stored, so they are retried on every push.
- Included files: `*.md`, no dot-prefixed path segment, not the root `README.md`, at most 1 MiB. Changed blobs are parsed, chunked and embedded outside the transaction; then one transaction deletes vanished rows, upserts documents (`audience` is the repo's group, always) and chunks, and records alerts.
- Frontmatter: required `id`, `title`, `review_by` (`YYYY-MM-DD`); optional `labels`, each of which must exist in `kh.labels` (otherwise the file is rejected `unknown label <x>`); optional `supersedes`, a list of document ids (a single string counts as a list of one; at most 20, no duplicates, not the document's own id; otherwise the file is rejected `invalid supersedes`); other keys are ignored. A supersede link stays inside one repo, so inside one group. A duplicate `id` in one repo rejects every file that holds it. `stale` is `review_by < today`, recomputed by `kh daily`.
- Supersede resolver (`indexer/supersede.py`, run in every sync of a repo over all its stored documents; it sets `kh.documents.superseded_by`, which the upsert never writes). Rules: (1) a key that names no document alerts `supersedes unknown id <key>`; a document naming itself is skipped; (2) a document named by several others takes the successor with the smallest `doc_key` and alerts `superseded by <n> documents; using <doc_key>`; (3) a document on a cycle gets no successor and each member alerts `supersedes cycle: <keys>`; (4) every other target maps to its direct successor, so in a chain A to B to C both A and B are superseded; (5) every document gets a value, none when nothing replaces it. Removing the line or deleting the successor makes the old document current again on the next sync.
- Tech repos (tier T0a) take another path through `sync_repo`; see "Technical documents (T0a)". The group rules above do not apply to them.
- Embedding providers are `ollama`, `http` and `fake`; the `http` provider embeds only audiences in `KH_EMBED_HTTP_ALLOWED_AUDIENCES`. T3 bodies are read from S3 by `embed_record` jobs. Failed jobs retry five times with a growing delay. An `embed_record` job removes NUL characters from a stored body before chunking and logs a warning with the record id; the blob in S3 is not changed.

## Search

One statement inside `caller_tx`: an exact vector order (no ANN index) fused by RRF with a full-text list, each limited to `KH_SEARCH_POOL`, weighted by tier and by the stale flag, one best chunk per owner. With `KH_SEARCH_MIN_SIMILARITY` above 0, vector candidates whose cosine similarity to the query is below it are dropped after the pool is cut, in both the requested-tier and the lower-tier lists, so a query with no real match returns no hits and `min_tier=T0` can answer the normative text; full-text matches are kept. The floor is server configuration and not part of a cursor's scope. At 0 (the default) the floor is off. Each hit carries `similarity`, the cosine similarity of its chunk to the query (null for a chunk without a vector), next to the RRF `score`. A `hidden` CTE lists superseded documents and outdated records (under the caller's row-level security); the filter drops their chunks before the pool is cut, so recall is kept. `include_outdated=true` keeps them, marks each hit `outdated` with `replaced_by`, and weights them by `KH_STALE_WEIGHT` like a stale hit. With `min_tier=T0`, a query whose only T0 matches are hidden answers the `only T3 records match` text when T3 hits exist. By default (`min_tier=T3`) T0, T0a and T3 are searched together, T0a weighted below T0 and records below T0a. `min_tier=T0a` searches T0 and T0a and fails with `only T3 records match; repeat with min_tier T3 to see them` when only records match. With `min_tier=T0` only T0 is searched; when no T0 hit exists but T3 hits do, the call fails with that text, and when T0a hits exist it fails with `only T0a documents or T3 records match; repeat with min_tier T0a or T3 to see them` (`NORMATIVE_T0A`). Cursors are HMAC-bound to the caller, the query and the filters, with a bounded offset.

## Usage log

`kh.usage_events` records what people search and open, so an n8n promotion agent can find T3 records worth promoting to T0 and queries no T0 document answers.

- **What is stored.** One row per event: the time, the kind (`search` or `open`), the searcher as a keyed hash, the searcher's group set, and the ids (`doc:` or `rec:`) of the hits returned or the one document opened. A search row also holds the query text (at most 500 characters, NUL removed; a normalised form is generated from it), the `min_tier` asked for and two counts of documents whose chunks match the full-text query, one for T0 and one for T3. An open row holds no query and no tier.
- **Who is logged.** Only `claude_client` callers, so n8n workflows and the promotion agent's own calls stay out of the signal and out of the table. Only searches at offset 0 are logged, because paging would inflate counts, and `get_doc` is logged only when it finds the row. A `min_tier=T0` search that fails with `only T3 records match` is logged too. Nothing is logged when `KH_USAGE_LOG` is `false` or `KH_CURSOR_KEY_FILE` is unset.
- **Searcher hash.** HMAC-SHA256 of the `sub`, keyed with a sub-key derived from the content of `KH_CURSOR_KEY_FILE`; no name, email or `sub` is stored. Rotating that file resets the distinct-people counts.
- **Write after the transaction.** `tool_guard` writes the events after the tool's own transaction has closed, on success and on a refused call, never on another exception or on cancellation. The write takes one pooled connection and waits at most 0.25 s for it. A failure of any kind, a timeout included, drops the events, logs a warning and adds to `kh_usage_events_dropped_total`; it never fails the tool. A second connection is not opened inside the tool's transaction, which could exhaust the pool of 10.
- **Two signals.** A record is worth promoting when many distinct people find or open it (`usage` with `view=records`, ranked by distinct people because searches and opens inflate when Claude searches on its own), and `outranked_t0` counts the searches in which it ranked above every T0 hit. A query is a gap when no T0 document matches its full-text query (`view=queries`); `t3_matches` above 0 means records exist, 0 means nothing exists. Both views list only what at least two distinct people did, so what one person alone did never produces an item.
- **Why not pool counts.** The search statement's `t0_hits` and `t3_hits` count owners in the fused pool, whose vector list returns the nearest chunks however unrelated while `KH_SEARCH_MIN_SIMILARITY` is 0, so they are above 0 whenever the caller sees any embedded document. The log stores counts of documents that match the full-text query (all terms), whatever labels or group filter the search carried, and compares the rank of a record with the first T0 key.
- **Retention.** `kh daily` deletes events older than `KH_USAGE_RETENTION_DAYS` (default 180), and `usage` never reads past that window.

## Tools

Ten tools, all with flat arguments: `search`, `get_doc`, `list_stale`, `list_labels`, `usage` (reads, `auto`) and `ingest_record`, `mark_record_outdated`, `move_item`, `propose_doc` (writes, `popup`) and `list_audiences` (PrivacyFence's service claim only, not under `tools`). The declaration in `deploy/privacyfence-upstream.example.yaml` allows `claude_client` and `n8n_workflow`.

- `ingest_record` refuses unless the caller holds `audience` and it is a configured group, when the body exceeds `KH_MAX_RECORD_BYTES`, or when a label is unknown, or when a text argument (`source`, `source_ref`, `title`, `body`, `source_url`, `content_type` or a label) contains a NUL character, which Postgres text cannot store (`argument <name> is invalid`); other control characters are kept. It stores the body at `records/<uuid>`, keeps the first 2000 characters as excerpt and stamps the config's `bot_login` as `written_by`.
- `mark_record_outdated(id, outdated=true, replaced_by="", note="")` takes record ids only and calls the definer function `kh.mark_record_outdated`, which checks the caller can read the record and that `replaced_by` (a `doc:` or `rec:` id) belongs to the same group; a missing or unreadable record answers `NOT_FOUND`. `outdated=false` clears the mark, and then `replaced_by` and `note` must be empty. The table keeps only the last actor (`outdated_by`, never returned); the call is logged at INFO.
- `move_item(id, from_group, to_group)` moves a record (`rec:`) or a document (`doc:`) between two groups the caller holds. Only a `claude_client` may call it; a workflow is refused (`this tool is not available to workflows`). The two groups must differ, both must be active groups the caller holds (`AUDIENCE_NOT_HELD`), and `from_group` must be the group the item is in now (`the item is not in from_group`). A record moves at once through the definer function `kh.move_record`: audience, labels, blob, `written_by` and any outdated mark and note stay, `replaced_by` is cleared, and the id stays. A document moves with two pull requests, opened by the bot: an add in the target group's repo and a removal in the source group's repo; each group's approvers merge their own, add first (the order is not enforced). Refusals: the target group already holds the same `(source, source_ref)`, path or id (`MOVE_CONFLICT`); another item names this one as its `replaced_by`, or the document is `superseded_by` another or named as a record's `replaced_by` (`MOVE_LINKED`); a document whose text the tool cannot render back unchanged (`MOVE_TEXT`, move it by hand); a file that changed since it was indexed (`BLOB_CHANGED`). A document that itself `supersedes` others is moved without the link, because a link stays inside one repo: the result lists these in `dropped_supersedes` and lists in `revived` the documents of the source group that become current again once the removal is merged. Calling the tool again returns the open pull requests, or opens only the removal when the add is open or already merged. If the removal cannot be opened, the result has `remove_pr_url ""` and a `note`. The call is logged at INFO.
- Similar items (`search/similar.py`): after a new record is stored and after a pull request is opened, `ingest_record` and `propose_doc` return `similar`, up to `KH_SIMILAR_LIMIT` items of the same group (never another group) whose best chunk has `similarity` of at least `KH_SIMILAR_MIN_SCORE`, with `id`, `tier`, `title` and `similarity` (not the RRF score). Superseded and outdated items are left out, and `propose_doc` leaves out the document being edited. The lookup embeds the text with a 5 second limit and runs in a savepoint; any failure gives `[]` and never loses the record or the pull request. `KH_SIMILAR_LIMIT=0` switches it off.
- `propose_doc` requires the caller to hold `group`. The path matches `^[a-z0-9][a-z0-9/_-]*\.md$` with no `..`, `//` or dot segment and is not `README.md`; `base_blob` empty means the path must not exist, otherwise Forgejo enforces the race through `sha`. Text with control characters other than tab and newline, bidirectional controls, HTML comments or tags is refused, as is a body over `KH_PROPOSE_MAX_BYTES`. The hub renders the file itself, parses it back with the indexer's parser, creates the branch `bot/propose/<uuid4>` from the head of `main`, puts the one file and opens a pull request to `main`. The bot is refused any branch outside `bot/` and any pull request whose base is not `main`; it stops at `KH_PROPOSE_MAX_OPEN` open pull requests. The pull-request body lists `Requested-by`, `Principal` and `Written-by` as information, not enforced. Its optional `supersedes` list (existing ids of the repo, rules as in the frontmatter) is written into the file and named in a last body line `Supersedes:`; to change a document keep the `supersedes` that `get_doc` returned, or the old one becomes current again.

## Moving an item

`move_item` moves a record or a document from one group to another, both held by the caller. A moved record keeps its `rec:` id; a moved document gets a new `doc:` id, because the copy indexed from the target repo is a new row.

- **Tombstone.** `kh.record_moves` stores `(audience, source, source_ref)` of every record moved out of a group. After its dedupe lookup finds nothing, `ingest_record` asks `kh.record_moved`; a true answer refuses with `a record with this source and source_ref was moved out of this audience`, so a workflow that re-sends by design cannot put the moved information back. Only a caller who holds the group learns of a tombstone. Moving a record back into a group deletes that group's tombstone.
- **Worker lock and re-embed policy.** The embed worker (`_embed_record`) takes `pg_advisory_xact_lock` on `rec:<id>` at the start of its write transaction and re-reads the audience; if the audience differs from the one it used for `may_embed`, it fails and the job retries. The move queues its own `embed_record` job, which embeds under the target group's policy. Two effects follow: a move into a group that may embed sends the body to the embedder for the first time; a move into a group that may not embed drops the vectors, so the record is then found by full-text search only.
- **A move is not a purge.** The old repo's history, past usage queries and backups keep the old copy.

## Deviation from the PrivacyFence contract

`docs/identity-claim.md` in PrivacyFence says only `sub`, `principal_type`, `audiences` and `req` may change what a query returns, and calls `email` audit-only. The hub uses `email` for access: it selects a person's groups through `people` in `kh.yaml`. The owner decided this on 2026-11-15 for attendee-only records and extended it on 2026-11-17 (`group-source: khyaml-email`): the email is now the only thing that gives a person any group, and the claim's `audiences` are used only for n8n workflows.

PrivacyFence 8.0.0a3 ships the opt-in key `email_for_visibility` (PrivacyFence ADR 0310). The declaration sets it, so PrivacyFence puts the verified email in the claim for this upstream. Without it no person's claim carries an email, so no person holds any group.

What the hub relies on: `email` is lower-cased; it is ASCII only; it is present only for `claude_client` (the hub also drops it for every other principal); a change to any of these is a breaking contract change in PrivacyFence.

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
| A formal-review trust walk | Closest to the old design, but much more code than letting Forgejo branch protection decide. |
| A trust walk by merger | Still needs a walk and verdict storage. |
| A `sync_teams` job copying Forgejo team members into Postgres | A second store and a freshness rule for a list the hub already loads. |
| Read-only `readers:` per group | Owner decision: every member reads and writes, approvers merge. |
| A `repos:` map with a separate approve group | Lets an approve group become an audience with no repo. |
| Label join tables | Arrays plus the two triggers give the same guard with far less SQL. |
| A global unique `(source, source_ref)` | It leaks the existence of records of other audiences. |
| Building the new modules beside the old ones | Two schemas and double test-support work; the old world was deleted instead. |
| `get_doc` fetching from Forgejo | Slow and unfiltered; `get_doc` is an indexed lookup under RLS. |
| Rank-by-pool hit counts (`t0_hits`) as the "T0 answers it" signal | Always above 0 while `KH_SEARCH_MIN_SIMILARITY` is 0; replaced by full-text match counts and the rank of the record against the first T0 key. |
| Requiring a full-text match for every search hit | `websearch_to_tsquery('english')` ANDs every term and the corpus has Hungarian text, so recall would drop; the similarity floor is used instead. |
| A "no confident match" flag in the `search` answer | An empty `hits` list already says it once the similarity floor is set. |
| Writing the usage event inside the tool transaction | The `normative` error rolls it back, and a failed insert would fail the tool. |
| Writing the usage event in a `finally` | Runs on cancellation; awaiting the pool there is unsafe. |
| A second connection opened inside the tool's transaction | Can exhaust the pool of 10 under load. |
| Daily rollup tables for usage | Need one rollup per audience set to keep the subset rule; more code; lose the open-to-query link. |
| Metrics or log export of usage | No row-level security; the design rejects filtering outside the database. |
| `outcome`, `page_offset`, `labels`, `audience_filter` and a stored `query_norm` column in the usage log | Derivable or unused by either view (`query_norm` is a generated column). |
| A separate usage key file | One more secret; the sub-key derived from the cursor key is enough. |
| Logging n8n workflows | They are not people, and the agent's own searches would feed back into its signal. |
| Parsing frontmatter in technical files | Rejects files nobody reviewed and needs one rule per file kind; a synthesised header covers every file. |
| A tier named `T1` | The owner chose `T0a`: it is a variant of T0, not a step between T0 and T3. |
| Counting T0a in `t0_fts_hits` | A technical document is not a normative answer, so the promotion agent would stop seeing the gap. |
| `kh setup` creating the clones | The clone or fork is a decision about upstream and credentials; the operator makes it and `kh setup` only checks it. |
| PrivacyFence's group map and a membership source for n8n tokens | A second copy of kh.yaml kept in step by hand; PrivacyFence ignores it once audiences_tool is set. |
