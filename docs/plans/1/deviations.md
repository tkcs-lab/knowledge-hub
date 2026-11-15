# Deviations: knowledge-hub-service

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-skeleton

- worker: No deviation in the code; the worker could not run tld guards, dod or role checks because its container has no tld toolkit.
- review: Owner chose to waive worker-side tld steps and role reviews; orchestrator ran profile validation, ruff and unit and integration tests after the merge.
- worker: config.py checks unknown audiences in audiences, raw_audience_map and entities but has no domain check, since section 3.1 does not place domains.
- retry: Phase marked blocked once for the missing toolkit, then merged after the owner waived the toolkit steps.

## p2-claims

- pending

## p3a-runner-fixture

- review: Diff matched the touches list exactly; worker tld steps and roles were waived by the owner, orchestrator ran ruff, unit and integration tests after the merge.

## p3b-tables

- pending

## p3c-rls

- pending

## p3d-triggers

- pending

## p4-rls-access

- pending

## p5-mcp-get-doc

- pending

## p6a-search-core

- pending

## p6b-search-tool

- pending

## p7-http-e2e

- pending

## p8-frontmatter-chunker

- pending

## p9a-trust

- pending

## p9b-forgejo-client

- pending

## p10-embed-providers

- pending

## p11a1-sync-plan

- pending

## p11a2-sync-write

- pending

## p11b-sync-chunks

- pending

## p12-webhook-worker

- pending

## p14-blobs

- pending

## p13a-wiring

- pending

## p13b-rebuild-check

- pending

## p15-ingest-raw

- pending

## p16a-derived-sql

- pending

## p16b-write-derived

- pending

## p17-recompute-requeue

- pending

## p18-routing

- pending

## p19a-candidates-sql

- pending

## p19b1-forgejo-write

- pending

## p19b2-propose-change

- pending

## p20-conflicts-stale

- pending

## p21-pr-outcomes

- pending

## p22a-grist-daily

- pending

## p22b-entities-tools

- pending

## p23-hardening

- pending

## p24-deploy-backup

- pending

## p25a-docs-design

- pending

## p25b-docs-ops

- pending

## p26-release-workflow

- pending

## Final review

- pending

## Finishing

- pending
