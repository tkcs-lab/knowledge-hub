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

- pending

## p5-worker-webhook

- pending

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
