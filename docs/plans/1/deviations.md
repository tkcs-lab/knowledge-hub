# Deviations: knowledge-hub-service

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-skeleton

- worker: No deviation in the code; the worker could not run tld guards, dod or role checks because its container has no tld toolkit.
- review: Owner chose to waive worker-side tld steps and role reviews; orchestrator ran profile validation, ruff and unit and integration tests after the merge.
- worker: config.py checks unknown audiences in audiences, raw_audience_map and entities but has no domain check, since section 3.1 does not place domains.
- retry: Phase marked blocked once for the missing toolkit, then merged after the owner waived the toolkit steps.

## p2-claims

- scope: scope: src/knowledge_hub/auth/ruff.toml
- review: Added auth/ruff.toml, a small file that only excludes the vendored verifier from linting; accepted. Vendored file sha256 matches VENDORED.md. Worker tld steps and roles waived by the owner; orchestrator ran ruff, unit and integration tests.

## p3a-runner-fixture

- review: Diff matched the touches list exactly; worker tld steps and roles were waived by the owner, orchestrator ran ruff, unit and integration tests after the merge.

## p3b-tables

- review: Diff matched the touches list; worker tld steps and roles waived by the owner, orchestrator ran ruff, unit and integration tests.
- scope: scope: tests/integration/test_migrate_runner.py
- review: The worker loosened one assertion in test_migrate_runner.py from an exact list to the first entry, because it breaks once 0002 exists; accepted.
- worker: Worker chose column types the plan left open: conflicts.closed_by_pr is text; candidates.derived_id and target_doc_id are foreign keys ON DELETE SET NULL; index_jobs.id and index_alerts.id are bigint identity; composite primary keys on document_versions, derived_sources, candidate_sources and conflict_items. Owner may want to review.

## p3c-rls

- review: The worker hit the account usage limit and its first report was lost; the orchestrator asked it to resume and report. Orchestrator ran ruff and the full suite (130 passed) on the merge. Security-critical SQL: the final review should read 0003_rls.sql against the matrix.
- worker: kh_indexer can UPDATE only src_required_audiences and src_allowed_emails on derived_sources, candidate_sources and conflict_items (the 3.3 text, not the matrix row, says so); ALL for kh_indexer means SELECT, INSERT, UPDATE, DELETE without TRUNCATE, REFERENCES or TRIGGER.
- worker: Decision and closing columns were not listed in the plan: indexer UPDATE on candidates is state, decided_at, required_audiences, allowed_emails; on conflicts state, closed_at, closed_by_pr, required_audiences, allowed_emails.
- worker: kh.visible wraps the plan expression in coalesce(..., false); kh.candidate_mark keeps stored branch, pr_number and pr_url when given null and always replaces error; the sql-language definer functions use a CTE with RETURNING 1; caller_* helpers and visible have no SET search_path so they can be inlined in policies.
- review: Worker ran 10 mutation checks on the migration and each broke exactly one test. kh_indexer has no EXECUTE on kh.caller_* or kh.visible; phase p3d must account for that in its triggers.

## p3d-triggers

- scope: scope: tests/integration/test_rls_policies.py
- scope: scope: tests/integration/test_schema_tables.py
- review: The worker switched off user triggers in the seed fixtures of the two earlier test files, because the new triggers overwrite or reject their hand-written rows; accepted, trigger behaviour is covered by test_triggers.py.
- worker: Choices the plan left open: owner created time is documents.indexed_at, raw_records.ingested_at or derived_records.created_at; raw_audience_map is matched on separate kind (series, folder) and key columns; one shared sources_snapshot_visibility function serves the three source tables and raises P0K03 for an unknown kind or a non-uuid id; chunks_inherit_owner and owner_propagate_to_chunks are SECURITY INVOKER and call none of the caller helpers; the propagate trigger does a no-op UPDATE chunks SE

## p4-rls-access

- worker: Choices the plan left open: pools are created with open=False and the caller awaits pool.open(); PgReplayCache.add_if_new is synchronous and runs the query on the pool loop, raising RuntimeError if called on that loop thread; pool sizes are min 1 and max 10; the chunk drift query of 3.3 did not exist and is written as DRIFT_SQL in tests/support/seed.py, for the later check phase to reuse or move.
- review: Diff matched touches; worker tld steps and roles waived by the owner, orchestrator ran ruff and the full suite (300 passed).

## p5-mcp-get-doc

- worker: get_doc returns owner empty, approved_by empty, no review_by, successor or commit_sha, status current and stale false for raw and derived records, because those tables have no such columns and a raw ingester sub would leak; indexed_at is ingested_at or created_at. A refused dispatch (no caller, name mismatch, changed arguments) returns the INTERNAL text with a ref because 3.9 has no code for it.
- worker: tool_guard(name, deps) wraps fn(call, **args) and rewrites the signature to (ctx, **args); later tool modules must not use from __future__ import annotations. get_doc returns a dict; source_link is empty without forgejo_web_url. The mcp_client fixture runs the app lifespan in its own task. Deps.indexer_pool is required, not defaulted. The SDK prefixes tool errors with Error executing tool <name>:.
- review: Diff matched touches; worker tld steps and roles waived by the owner, orchestrator ran ruff and the full suite (327 passed).

## p6a-search-core

- worker: decode_cursor has an extra keyword-only max_offset defaulting to 100 (the settings default) and callers must pass settings.search_max_offset; an offset above it raises CURSOR_FOREIGN. Token layout: 32-byte HMAC then an 8-byte big-endian offset, MAC over sub, hash of sorted audiences, hash of query and offset; the key is bytes; may_embed takes visibility typed as object.
- review: Diff matched touches; worker tld steps and roles waived by the owner, orchestrator ran ruff and the full suite (372 passed).

## p6b-search-tool

- worker: Open choices: lower_tier_hits comes from a second pair of vector and full-text lists over the lower tiers; newest_at is one value across visible T2 and T3 owners; NORMATIVE fires when min_tier is T0 or T1, no non-disputed T0 or T1 owner is in the fused set and T2 plus T3 is above zero; the cursor binds the query plus domains, min_tier and entities; limit is clamped to 1-50; result shape is hits, lower_tier_hits, next_cursor; search fails with INTERNAL if cursor_key_file is unset; build_search_qu
- worker: Test seeding: the indexer role has no INSERT on raw_records or derived_records, so raw and derived rows are inserted as wf_token through the app pool and then the indexer sets visibility.
- review: Behaviour risk for the owner: lower_tier_hits has no relevance cutoff on the vector side, so a T0 or T1 query with no current T0/T1 hit will almost always return NORMATIVE when any lower-tier content is visible. A similarity threshold would fix it but the plan specifies none.

## p7-http-e2e

- worker: ToolCase has more fields than the four in the brief (args_for_missing, cited, citation, cases, label); the args_* fields are callables taking ids and the case, and expect_hidden is a callable that asserts on the response. search is not run for the seed case all, because all is an English stop word; get_doc covers it. Both test files reuse the app_settings and deps fixtures from test_search_ranking.py.
- review: Milestone 1 closed. Diff matched touches; worker tld steps and roles waived by the owner, orchestrator ran ruff and the full suite (464 passed).

## p8-frontmatter-chunker

- worker: Judgement calls the plan leaves open: a small piece merges into the previous one only if the result stays within 1200 characters; the 150 overlap characters are prepended after splitting and merging, so a piece can reach about 1352 characters; heading lines inside code fences are not headings; text before the first heading has an empty heading_path; the chunker version is passed in by the caller.
- worker: Frontmatter: review_by accepts a YAML date or an ISO string; id may be an integer and is stored as a string; status defaults to current; impossible dates give Rejected with frontmatter: invalid YAML or date.
- review: Diff matched touches exactly; worker tld steps and roles waived by the owner, orchestrator ran ruff, unit and integration tests.

## p9a-trust

- worker: Choices the plan left open: commits is the first-parent chain newest first; reviews map PR number to review list; changed_files map commit sha to paths; Verdict is a frozen dataclass; TrustResult has an extra anchor_verdict field and verdict_for(path); an untrusted repo downgrades every file verdict to disputed; a merge commit counts only if its PR base is main.
- review: Diff matched touches; worker tld steps and roles waived by the owner, orchestrator ran ruff and the full test suite.

## p9b-forgejo-client

- review: Diff matched touches; worker tld steps and roles waived by the owner, orchestrator ran ruff and the full test suite (117 passed).

## p10-embed-providers

- worker: EmbeddingError did not exist and base.py is outside touches, so it is defined in embed/ollama.py with a shared check_vectors helper and BATCH_SIZE; embed/http.py imports from there and it may move to base.py later. The factory raises ValueError when a provider URL or token file setting is missing and splits the http allowed audiences on commas; both providers batch by 32 and take an optional transport argument; bench_embed.py builds its embedder from Settings and --url overrides ollama_url.
- review: Diff matched touches; the benchmark ran only with the fake provider, not against a real Ollama (manual step ma8). Orchestrator ran ruff and the full suite (432 passed).

## p11a1-sync-plan

- worker: "Descends from last_indexed_sha" is implemented as the sha being on the first-parent chain of main, which is stricter than ancestry; if the head moves while planning the plan raises ForgejoError and the run retries.
- worker: Changed files are fetched per first-parent commit; T0 repos only after the anchor and T1 repos only after last_indexed_sha. document_versions rows are planned only for documents whose last-changing commit differs from the existing row.
- worker: Duplicate id: all files with the id are rejected except exactly one last changed by an approved merge; T1 repos have no approvals so all duplicates are rejected. A document is superseded only by a current same-repo document naming it or via its own superseded_by naming a current one.
- worker: The commit-to-PR fallback of listing closed PRs is not implemented because the client has no such method; a missing PR means disputed. Every .md file in the tree is parsed, so a README.md without frontmatter gets a rejection alert. The writer phase must resolve superseded_by to a uuid, parse dates and dedupe alerts across syncs.
- review: The worker commit trailer says Plan-Phase: knowledge-hub/p11a1-sync-plan (wrong slug); accepted because the merge commit carries the correct trailer. The worker hit the account usage limit mid-run and finished after a resume.

## p11a2-sync-write

- worker: Choices left open: after taking the advisory lock sync_repo reloads repo_state and the existing documents and re-plans if they changed; changed documents get a temporary path before upsert so renames and swaps cannot hit UNIQUE (repo, path); a refused plan writes only its alerts; alerts are deduped on (repo, path, message); superseded_by is resolved in a second pass after the upserts.
- review: Known risk: if Forgejo submitted_at carries fractional seconds or a non-Z offset, the planner compares it as a raw string and may mark a document changed on every sync; the fix belongs in sync_plan.py. The # fmt: skip hits in the diff are formatter hints, not skipped tests. Orchestrator ran ruff and the full suite (471 passed).

## p11b-sync-chunks

- scope: scope: tests/integration/test_sync.py
- review: One line changed in test_sync.py because sync_repo now takes a required embedder; accepted. The worker also reformatted unrelated files by mistake and reverted them; the commit touches only the four listed files.
- worker: Choices left open: plan_chunks takes chunker_version as a keyword because Config does not carry KH_CHUNKER_VERSION, and sync_repo has a matching chunker_version keyword defaulting to 1; unchanged chunks are matched by content_sha, not ordinal, so ids survive an inserted section; a chunk is also rewritten when the embedding model changes or its embedded state flips; if sync_repo re-plans after taking the lock, the chunk plan is recomputed inside the transaction so embedding runs while the lock is
- review: Callers of sync_repo (rebuild, worker) must pass an embedder and chunker_version=settings.chunker_version. Orchestrator ran ruff and the full suite (477 passed).

## p12-webhook-worker

- worker: Choices left open: a missing or empty webhook secret gives 401 (fails closed); an oversize body gives 413; invalid JSON or a non-object gives 400 after the signature check; the event type is read from X-Forgejo-Event with X-Gitea-Event as fallback; the repo comes from repository.full_name with owner equal to KH_FORGEJO_ORG and the name a key of config.repos.
- worker: Retry model: a failed job with fewer than 5 attempts is claimed again once run_after has passed because the state constraint has no retry state, so the claim query accepts queued and failed; after the fifth attempt it stays failed. Worker has extra optional keywords chunker_version and poll_seconds and a stop() method; callers should pass settings.chunker_version.
- review: Open item: embed_raw and embed_derived jobs are marked done without being processed, because no phase handles them yet, and the worker is not started in the app lifespan. A later phase must process those kinds or raw and derived chunks stay unembedded.

## p14-blobs

- worker: make_s3_store_with_stubber() returns a (S3BlobStore, Stubber) tuple because tests need the stubber to queue responses; BlobNotFound subclasses KeyError; delete is idempotent for a missing key, matching S3.
- review: Diff matched touches; worker tld steps and roles waived by the owner, orchestrator ran ruff and the full suite (151 passed).

## p13a-wiring

- worker: Choices left open: the Worker is built only when KH_FORGEJO_URL is set, so deps.worker is None without Forgejo; ForgejoClient uses KH_FORGEJO_INDEXER_TOKEN_FILE and build_deps raises ValueError if the URL is set without it; the production ClaimVerifier gets replay_cache None because ClaimMiddleware already applies PgReplayCache; build_deps opens both pools and close_deps closes them; build_deps raises ValueError for a partial S3 setting; mcp/server.py is unchanged and Deps.indexer_pool stays typ
- review: Gaps for the final review: starting and stopping the worker in the app lifespan and uvicorn startup in kh serve are not covered by tests; with a dev identity and no KH_PF_* settings deps.verifier is None, so a request that carries a claim header would fail inside ClaimMiddleware instead of being refused cleanly. Orchestrator ran ruff and the full suite (494 passed).

## p13b-rebuild-check

- scope: scope: tests/unit/test_settings.py
- review: One test in test_settings.py called kh migrate, which is now implemented, so it was switched to the daily stub; accepted.
- worker: Choices left open: rebuild retries a refused sync (main rewritten) by setting repo_state.last_indexed_sha to NULL and syncing again; check re-fetches each indexed document at HEAD and hashes it, counts a document missing at HEAD as drift and does not flag never-indexed files; a versioned get_doc reads the current row first for every id kind so a hidden id gives not found, and returns the whole file with frontmatter while tier, status, approval fields, commit_sha and source_link come from the doc
- review: Known defect risk for the final review: sync_plan._doc_changed compares approved_at as a string and sync._fmt_ts truncates to whole seconds, so Forgejo timestamps with fractional seconds make every document look changed on each sync and break rebuild-twice idempotence; normalise approved_at in trust.py or sync_plan.py. Orchestrator ran ruff and the full suite (518 passed).

## p15-ingest-raw

- worker: Choices left open: a bad domain or occurred_at raises a local KhError with fixed texts (domain must be one of the domains; occurred_at must be ISO 8601 with a UTC offset) because the catalogue has none and errors.py was closed to additions; ALREADY_INGESTED fills the id as raw:<uuid>; generated_by is stored as jsonb {label: string} or NULL; get_doc on a raw row without a blob falls back to the excerpt; embed_raw deletes the record existing chunks before inserting so a retry is safe; unembeddable
- review: The ingest_raw leak-matrix case is registered with no seed cases because a write tool cannot single out a seed row; the matrix calls it once per principal with a fresh ingest. Orchestrator ran ruff and the full suite (537 passed).

## p16a-derived-sql

- scope: scope: tests/integration/test_triggers.py
- review: The worker changed test_triggers.py because a derived row with no sources can no longer commit: bare derived rows are inserted with session_replication_role replica and the embed-job test inserts a source row too; accepted.
- worker: The deferred trigger raises SQLSTATE P0K04 for both no sources and differing visibility (the plan names no code; P0K04 is next free); it compares sets, re-reads the stored visibility at commit and skips a row deleted in the same transaction. Malformed source ids count as unresolved instead of raising; an empty input returns (0, {}, NULL, {}); derive_visibility([]) raises ValueError; Visibility is a frozen dataclass.
- review: Worker ran 12 mutation checks on 0005 and each broke a test. Open for p16b: map P0K04 at commit to an internal error, and consider refusing a derived row whose allowed_emails is an empty list (visible to nobody).

## p16b-write-derived

- worker: Choices left open: zero cited sources return DERIVED_SOURCES; citing the same source twice counts once and the same doc with two different @version values is refused with BAD_ID; an @version must match [0-9A-Za-z._-]{1,64}; an unknown domain raises a local KhError; claim source_ids must be non-empty and name cited sources with @version stripped; write_derived is not limited to workflow principals because the plan is silent.
- worker: An empty allowed_emails (cited sources share no attendee) is refused with a local KhError coded derived_nobody and a fixed text because no catalogue entry fits; the tool runs SET CONSTRAINTS kh.derived_visibility_matches IMMEDIATE after inserting sources so a P0K04 becomes INTERNAL with the reason logged; the embed_raw handler became a shared _embed_owner in worker.py that also serves embed_derived (tier T2, NULL embedding when may_embed is false).
- review: Worker ran 15 mutation checks; all but two equivalent mutants broke a test. The leak-matrix case for write_derived has no seed cases; one parametrization of the hidden-source test is skipped because that source is visible to its caller. Orchestrator ran ruff and the full suite (593 passed, 1 skipped).

## p17-recompute-requeue

- worker: Design change: when no __config__ hash is stored (new install or restore) load_audience_map only loads the map and neither recomputes nor records a hash, because recomputing rewrote the visibility that about 43 existing tests insert directly; kh check keeps reporting config drift until the operator runs kh recompute-visibility. The brief said recompute when the stored hash differs.
- worker: Choices left open: recompute_visibility rewrites kh.raw_audience_map from config first so the command works standalone, taking LOCK TABLE ACCESS EXCLUSIVE on it so no raw row is stored under the old map after the recompute; raw rows with neither mapping nor participants keep their visibility; documents in repos not in config.repos are untouched; citation cycles raise ValueError and roll back; requeue_embeddings skips records with a queued or running job and returns the number inserted; both new 
- review: Open items: config_hash covers config.entities but no step recomputes entities_cache visibility, so the recorded hash covers entity visibility that is not recomputed (a later phase owning entities_cache should add that); the runbook must tell the operator to run kh recompute-visibility once after install. Worker ran 10 mutation checks, each broke a test except one equivalent mutant. Orchestrator ran ruff and the full suite (620 passed, 1 skipped).

## p18-routing

- worker: Choices left open: an unknown explicit_repo raises NO_REPO with the repo name; SPAN names the first two audiences in sorted order; WIDER shows the audiences as a sorted comma-joined list; visibility.py now imports KhError from mcp/errors.py at load time (no circular import).
- review: Worker ran 9 mutation checks; 2 survived the first tests (the flag threshold and dropping the T0 filter), so two tests were added and both now fail the mutation. Orchestrator ran ruff and the full suite (608 passed, 1 skipped).

## p19a-candidates-sql

- scope: scope: tests/integration/test_triggers.py
- scope: scope: tests/integration/test_schema_tables.py
- scope: scope: tests/integration/test_rls_policies.py
- review: Three earlier test files changed because bare candidates and conflicts with no sources now fail at commit: seeds use replica role or disable user triggers, and the policy matrix gains four kh_provenance grants; accepted.
- worker: Both triggers raise SQLSTATE P0K04 and compare as sets, as 0005. The brief omitted kh_provenance grants that the definer triggers need: column SELECT (id, required_audiences, allowed_emails) on candidates and conflicts, whole-table SELECT on candidate_sources and conflict_items, each with a USING true policy. Error texts: candidate has no sources, conflict has no sources, and <candidate|conflict> visibility does not match its sources.
- review: Worker ran 7 mutation checks; 6 broke tests and one survived (a provenance branch in the SECURITY INVOKER function, which has no effect because the caller RLS already hides those rows).

## p19b1-forgejo-write

- worker: The plan left the Forgejo wire format open: open_pull turns label names into label ids by reading repo labels then org labels and raises ForgejoError for an unknown name (an unreadable org label list is skipped); put_file looks for the file first and sends POST to create or PUT with the blob sha to update so a retry works; create_branch sends the sha as old_ref_name; the fake records refs but does not guard them. These wire formats are unverified against a live Forgejo (manual step ma3).
- review: Guard tests try every bad ref against every write method and require ForgejoRefused with no request sent. Orchestrator ran ruff and the full suite (510 passed).

## p19b2-propose-change

- worker: Persistence: tool_guard rolls the caller transaction back when a tool raises, which would delete the failed candidate, so the candidate insert and every kh.candidate_mark call run in their own committed caller_tx on the app pool (a second pool connection per call, pool max 10); the guard transaction is used for reads only.
- worker: Choices left open: unresolved sources and target_id answer NOT_FOUND; local fixed texts for exactly one of target_id and new_path, at least one source id, and promotion unavailable without a Forgejo connection; an @version on a source id gives BAD_VERSION and target_id must be a plain doc: id; a candidate_id retry must resend the same proposal (recomputed dedupe key must equal the stored one, else BAD_CANDIDATE), skips the dedupe, daily-limit and insert steps, tolerates create_branch failing bec
- review: Worker ran 10 mutation checks and 9 were caught; the survivor is equivalent because the unique-violation fallback returns the same DUPLICATE. Open item: tool_guard rolls back on any KhError, so later tools that must keep a write after an error (flag_conflict) hit the same problem; letting the guard commit before raising would be cleaner. Orchestrator ran ruff and the full suite (657 passed, 1 skipped).

## p20-conflicts-stale

- worker: Choices left open: keyset cursors are base64url(json position) plus an HMAC bound to sub, audiences, tool name and filters, with the helpers cursor_key, keyset_scope, encode_keyset, decode_keyset and MAX_LIST_LIMIT living in tools/docs.py because search/cursor.py is outside touches; any bad or foreign cursor gives CURSOR_FOREIGN.
- worker: flag_conflict returns conflict_id, takes doc:, raw: or derived: ids (versioned, uppercase or malformed ids give NOT_FOUND), uses the domain of item a, allows flagging the same pair twice, and adds local fixed texts for a equal to b and for a bad note length. list_conflicts is newest first with equality filters on domain and status (an unknown value gives an empty page) and limit clamped to 1-50. list_stale orders by review_by ascending with NULLs last, then id, with equality filters on domain an
- review: Gap: the P0K04 mapping to INTERNAL around SET CONSTRAINTS kh.conflict_visibility_matches IMMEDIATE is wired but no test forces that trigger to fail. Worker mutation checks all broke a test. Orchestrator ran ruff and the full suite (699 passed, 1 skipped).

## p21-pr-outcomes

- worker: Choices left open: conflicts.closed_by_pr stores <repo>#<number>; the PR repo comes from the job repo and is compared with candidates.target_repo and pr_number; a PR that is neither merged nor closed is ignored; a merge promotes a candidate in pr_open or rejected but a close without merge rejects only from pr_open, so a stale event cannot undo a promotion; conflicts close only when the candidate is promoted, only if open, matched on (kind, id); the mismatch alert goes to index_alerts with the PR
- review: Worker ran 7 mutation checks; two first survived and tests were added so all now fail. Orchestrator ran ruff and the full suite (668 passed, 1 skipped).

## p22a-grist-daily

- scope: scope: tests/unit/test_settings.py
- review: test_cli_stub_exits_3 asserted that daily was a stub and was replaced by test_cli_daily_exits_2_without_grist_url; the unused _not_implemented helper in cli.py was left; accepted.
- worker: Entity visibility is set from config.entities on every run (allowed_emails NULL) because recompute does not touch entities_cache, so a config change takes effect at the next daily run; an empty audience list for a kind raises ValueError before any write and the CLI exits 2; a Grist row is skipped and counted if its key is missing, not kind:slug, or its prefix does not match its table (prd, cmp, ver, cus); stale is set both ways (review_by before today) for every document with a review_by, which 
- worker: All four Grist tables are fetched before any write and one transaction does the stale update, upsert, delete and jti purge; upserts write only when a value differs so a rerun leaves synced_at alone; GristClient raises GristError and kh daily exits 1 on it, and exits 2 unless KH_GRIST_URL, KH_GRIST_DOC_ID and KH_GRIST_KEY_FILE are set. No migration or grant changes were needed.
- review: Worker ran 4 mutation checks; each broke a test. Orchestrator ran ruff and the full suite (713 passed, 1 skipped).

## p22b-entities-tools

- worker: Choices left open: list_entities returns entities (id, kind, name, owner_team, status, attrs, synced_at) and next_cursor; get_entity returns the entity and its visible documents ordered by id; a malformed or unknown id gives NOT_FOUND like a hidden one; the bad-type text is a local constant BAD_TYPE raised as KhError bad_type; the keyset cursor has width 1 and its scope covers type, status and owner_team; limit is clamped to 1-50 and the type check runs before limit and cursor handling; GET_ENTI
- review: Worker ran 6 mutation checks; each broke a test. Orchestrator ran ruff and the full suite (731 passed, 1 skipped).

## p23-hardening

- worker: Choices left open: metrics.py uses one module-level METRICS registry because claims.py and webhook.py have no Deps; the bucket uses time.monotonic and prunes idle buckets above 10000; retry_seconds is a whole number of at least 1 and the rate-limit check runs after dispatch and argument checks and before caller_tx, skipped when the limiter is None; kh_tool_calls_total outcomes are ok, error, internal and rate_limited and tool_guard refusals are not counted; claim rejection codes use a closed set
- worker: /metrics re-reads its token file on every request so the token can be rotated; a missing or empty file, a non-Bearer scheme or a wrong token gives 401 using hmac.compare_digest; make_metrics_endpoint takes settings from build_app; kh_index_jobs is emitted for all four states when the indexer pool exists and omitted otherwise or when the query fails.
- review: Worker ran 14 mutation checks; the empty-token mutation first survived because the endpoint read deps.settings, which the worker fixed so it now fails. The in-process rate limiter is per process only. Orchestrator ran ruff and the full suite (748 passed, 1 skipped).

## p24-deploy-backup

- worker: Choices left open: the Dockerfile build stage installs build with pip and builds the wheel, the final stage installs only the wheel; the kh user has no home directory and a nologin shell with WORKDIR /home/kh; the compose healthcheck uses python urllib against /healthz because the slim image has no curl; the compose example pins ollama/ollama:0.12.0 (a placeholder version) and knowledge-hub:0.1.0 and uses .invalid PrivacyFence URLs and in-stack hostnames; the backup script reads KH_DB_HOST, KH_D
- review: Not verified here: the image build and the pg_dump behaviour for a FORCE-RLS owner (manual step ma5); the compose read_only root filesystem with a /tmp tmpfs may need more writable paths. The orchestrator ran ruff, bash -n on the backup script and the full suite (753 passed, 1 skipped).

## p25a-docs-design

- worker: Choices left open: design.md is 113 lines; the three asks for PrivacyFence (bless the email claim for visibility, promise it is set only for a verified person, announce any change to it) are the worker wording; configuration.md adds a commands table and a restricted_audiences_imply_all section; the kh.yaml example uses placeholder anchor shas and forgejo.example.invalid. Built behaviours that differ from the plan text are documented as built.
- review: Docs only; the PrivacyFence asks in design.md are for the owner to confirm before sending. Orchestrator ran ruff and the full suite (753 passed, 1 skipped).

## p25b-docs-ops

- worker: Choices left open: the README quick start uses python3 -m venv with pip install -e .[dev] because no separate stack guide exists in the repo; the runbook reads index_alerts with SQL since no kh command shows alerts; the runbook adds alert rows for the pull-request mismatch text and the supersedes same-repo text, and rotation rows for the metrics token and database passwords; the restore command is pg_restore --data-only --disable-triggers --no-owner; the link test skips fenced and inline code, i
- review: Open for the final review: the restore step in the deploy/backup-kh.sh header comment lacks --data-only --disable-triggers and should match the runbook; whether embedding fails or is skipped when Ollama is down was not verified. README is 43 lines and the runbook 93. Orchestrator ran ruff and the full suite (756 passed, 1 skipped).

## p26-release-workflow

- worker: Choices left open: no tld-release.yml template exists in the repo, so the green-checks gate excludes check runs named build, publish and the workflow name and uses gh api on commits/$SHA/check-runs accepting success, skipped and neutral; the build step tags the image knowledge-hub:release-test and the push step rebuilds with push-by-digest taking the digest from that step; the tag step uses -f object=$SHA -f type=commit with the message Release <version>; the release title is v<version>; the run
- review: The worker could not run the tld acceptance commands; the orchestrator ran tld profile validate (exit 0) and tld ops release-prepare (current 0.1.0). tld guard allow-profile was not used for the profile edit. The workflow has never run on GitHub; the release environment and the TLD_RELEASE_TOKEN secret are manual steps for the owner. Orchestrator ran ruff and the full suite (762 passed, 1 skipped).

## Final review

- final: Blocking finding 1: propose_change wrote to Forgejo with the indexer token because nothing read KH_FORGEJO_BOT_TOKEN_FILE; fix-1 builds a separate bot client used only for promotion writes.
- final: Blocking finding 2: sync compared approved_at as a raw Forgejo string, so a committer date with a local offset or fractional seconds made every anchor-trusted document look changed on each sync; fix-2 normalises timestamps to UTC instants.

## Finishing

- pending
