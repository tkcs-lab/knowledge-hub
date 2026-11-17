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

- pending

## p7-search-tool

- pending

## p8-doc-tools

- pending

## p9-ingest-record

- pending

## p10-propose-doc

- pending

## p11-setup-core

- pending

## p12-setup-ollama-s3

- pending

## p13-forgejo-admin

- pending

## p14-forgejo-step

- pending

## p15-setup-cli

- pending

## p16-release-deploy

- pending

## p17-docs-retirement

- pending

## Final review

- pending

## Finishing

- pending
