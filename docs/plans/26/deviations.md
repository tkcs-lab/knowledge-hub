# Deviations: two-tier-rewrite

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-demolish-and-config

- None.

## p1b-claim-resolution

- worker: principals.py keeps a legacy hand-listed PRINCIPALS dict for test_rls_tables.py and seed.py; p2 should remove it

## p2-new-schema

- None.
- review: dead legacy PRINCIPALS dict left in tests/support/principals.py (outside p2 touches); to delete in a later phase that owns the file

## p3-frontmatter-forgejo

- None.
- review: is_ancestor uses the compare API as head...base and answers True when no commits come back; not verified against a real Forgejo (ma1 confirms)
- review: an unquoted integer id in frontmatter is rejected as invalid id (the old parser accepted it)
- review: FakeForgejo gained rewrite, head, files and fail_next helpers beyond the brief, for later phases

## p4-sync-core

- worker: sync_repo returns the SyncPlan; plan_chunks(embedder, repo, docs, *, chunker_version) takes ChunkDoc and no config; apply_chunks(conn, plan) reads the repo from the plan
- worker: sync_plan also exports changed_paths and is_included so only changed blobs are fetched
- worker: a stored row is deleted when its path now holds a different id; a rename keeps row and chunks, paths are parked as moving:<id> so two files can swap
- worker: documents.stale is set at upsert as review_by < today; excluded or oversize files raise no alert, and a stored file that becomes excluded is deleted
- worker: chunk planning reads stored chunks outside the write lock, so concurrent syncs of one repo could plan from stale state; p5 must run one sync per repo at a time

## p5-worker-webhook

- worker: Worker._git_push holds a per-repo asyncio.Lock on top of the one-job-at-a-time loop (p4 rule); not in the brief
- worker: worker raises ValueError on any job kind other than git_push and embed_record; webhook drops pr_event unlike the old file
- worker: test_metrics covers job-state and webhook counters only; tool-call and rate-limit paths covered by a direct METRICS.tool_call render test because no tools are registered yet
- review: metrics.agent_write_failure and its Prometheus counter are leftovers of the old agent writes; metrics.py is outside p5 touches, to be removed by a later phase or the final review

## p6-rebuild-jobs-cli

- worker: cli.COMMANDS stays (serve, migrate) and a separate JOB_COMMANDS tuple was added, because test_settings.py asserts the old tuple and is outside touches; lint runs before settings load so it cannot exit 2
- worker: rebuild deletes the repo_state row (last_indexed_sha is NOT NULL) and raises ValueError (CLI exit 2) for a repo in no group or when deps.reader is None
- worker: check prints indexed nothing for never-indexed repos, skips blank-body documents for missing chunks, prints config says no group; requeue_embeddings skips records with a queued or running embed_record job
- review: test_cli_has_only_serve_and_migrate in test_settings.py has a now-misleading name; rename in a later phase that owns the file

## p7-search-tool

- worker: tools/search.py omits from __future__ import annotations because pydantic could not resolve Literal with it
- worker: min_tier T3 searches T0 and T3 together; min_tier T0 searches T0 with T3 as a second list used only for the NORMATIVE check; the cursor binds to a hash of query, min_tier, labels and audience
- worker: settings validates KH_TIER_WEIGHTS with a field_validator; seed.py gained vector_literal, add_chunked_doc (embed, vector_of options) and add_chunked_record
- worker: the vector list has no similarity threshold, so a query with no text match still returns the nearest embedded chunks (as the old design)

## p8-doc-tools

- worker: list_labels returns a bare list that the MCP SDK wraps as {"result": [...]}; list_stale returns {documents, next_cursor} with entries id, title, audience, labels, repo, path, review_by, stale (3.8 does not fix these fields)
- worker: get_doc falls back to the record excerpt when deps.blobs is None or the blob is missing

## p9-ingest-record

- worker: ingest_record wraps the INSERT in a savepoint so a unique violation does not abort the caller transaction; a duplicate or failed INSERT deletes the orphan S3 blob best effort; labels are de-duplicated for the existence check but stored as given

## p10-propose-doc

- worker: render_file always emits a labels flow list (labels: [] when empty) with JSON-quoted labels; title must be non-empty single-line else BAD_ARG; check_text also runs on title and summary; a tag is < or </ followed by a letter
- worker: a label failing LABEL_RE answers BAD_LABEL without a query; TOO_LARGE counts the rendered file plus summary in bytes; a parse-back mismatch answers BAD_TEXT; ForgejoError, KeyError or TypeError from the Forgejo section become PROPOSE_FAILED
- worker: error order: not configured, audience, path, id, review_by, title, text, labels, size, parse-back, open-PR cap, base_blob/path, ID_IN_USE, then branch, put, pull; deps.close_deps also closes forgejo_bot; tools/propose.py has no from __future__ annotations
- review: known gaps: concurrent proposals of one id on different paths can open two PRs (the indexer duplicate-id rejection covers it after merge); a put_file race on a stale prev_blob leaves an orphan bot/propose branch with no cleanup

## p11-setup-core

- worker: the runner stops at the first failing step and skips later ones, and redacts password=, token= and secret= values in error text
- worker: without an admin DSN the checks connect as kh_migrator to the postgres database; on failure it prints ! lines for role, database and extension; wrong role attributes or database owner print ~ lines (! without an admin DSN); an unset role password file means no password is set
- worker: group removal also deletes the group repo_state row; queued retry jobs print as + job git_push: <repo>; the test Cluster() root directory is renamed (-setup) to avoid a per-pid collision with the session cluster
- review: CLI wiring of kh setup and reading KH_SETUP_PG_ADMIN_DSN_FILE are left to p15

## p12-setup-ollama-s3

- worker: ollama: model name matches <name> or <name>:latest in /api/tags; after an apply pull the dimension is re-checked
- worker: S3Step also needs KH_S3_BUCKET (else raises) and, without an injected client, endpoint and key files; head_bucket errors other than 404/NoSuchBucket/NotFound print ! probe failed <code>; failed creation prints ! create failed <code>; a created bucket is probed at once

## p13-forgejo-admin

- worker: create_team takes extra fields (units_map, includes_all_repositories, can_create_org_repo); org_owners reads the members of the org team named Owners; whoami(token) builds its own request to override Authorization because reader.py is outside touches; the fake lets only site admins call anything except GET /user (non-admin gets 403)
- review: endpoint paths written from the worker knowledge of Forgejo, not from the plan; to confirm in ma1: PUT /teams/{id}/repos/{org}/{repo}, GET /repos/{o}/{r}/collaborators/{login}/permission, /orgs/{org}/hooks; fake units_map accepts any values

## p14-forgejo-step

- worker: ForgejoAdmin has no team-delete or user-lookup method (files outside touches), so the step calls the private _obj(GET /users/{login}) and _request(DELETE /teams/{id}) and the test subclasses FakeForgejoAdmin to add both routes; both endpoints unconfirmed, for ma1
- worker: followed 3.7 where the brief is shorter: team units_map (repo.code and repo.pulls set to the team permission) is checked and repaired, a site-admin bot or indexer is a ! line, and units missing from a units_map count as none
- worker: org-owner, site-admin and effective-permission checks run with or without tokens (only the whoami login match needs a token); a collaborator with wrong effective permission after put_collaborator prints ! after apply
- worker: account existence is checked for every member and approver as well as bot and indexer; a missing login is ! and skipped; a team repo other than its group repo prints ! and is never removed; webhook without a secret prints ! and writes nothing, a same-URL hook of another type is !, and each apply prints an i line for the rewritten secret
- review: follow-up: add ForgejoAdmin.get_user and delete_team with fake routes so the step can drop private client calls; ma1 confirms GET /users/{login}, DELETE /teams/{id}, the units_map shape and the webhook content_type under config

## p15-setup-cli

- worker: kh setup is intercepted in cli.main when argv[0] is setup; a setup subparser exists only so it shows in kh --help; --only builds just the selected steps
- worker: missing KH_FORGEJO_URL or KH_OLLAMA_URL, or an unreadable credential file, prints setup: <ENV NAME> is not set and exits 2; a failing load_settings or load_config prints the error to stderr and exits 2

## p16-release-deploy

- pending

## p17-docs-retirement

- pending

## Final review

- pending

## Finishing

- pending
