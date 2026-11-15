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

- worker: Judgement calls the plan leaves open: a small piece merges into the previous one only if the result stays within 1200 characters; the 150 overlap characters are prepended after splitting and merging, so a piece can reach about 1352 characters; heading lines inside code fences are not headings; text before the first heading has an empty heading_path; the chunker version is passed in by the caller.
- worker: Frontmatter: review_by accepts a YAML date or an ISO string; id may be an integer and is stored as a string; status defaults to current; impossible dates give Rejected with frontmatter: invalid YAML or date.
- review: Diff matched touches exactly; worker tld steps and roles waived by the owner, orchestrator ran ruff, unit and integration tests.

## p9a-trust

- worker: Choices the plan left open: commits is the first-parent chain newest first; reviews map PR number to review list; changed_files map commit sha to paths; Verdict is a frozen dataclass; TrustResult has an extra anchor_verdict field and verdict_for(path); an untrusted repo downgrades every file verdict to disputed; a merge commit counts only if its PR base is main.
- review: Diff matched touches; worker tld steps and roles waived by the owner, orchestrator ran ruff and the full test suite.

## p9b-forgejo-client

- review: Diff matched touches; worker tld steps and roles waived by the owner, orchestrator ran ruff and the full test suite (117 passed).

## p10-embed-providers

- pending

## p11a1-sync-plan

- worker: "Descends from last_indexed_sha" is implemented as the sha being on the first-parent chain of main, which is stricter than ancestry; if the head moves while planning the plan raises ForgejoError and the run retries.
- worker: Changed files are fetched per first-parent commit; T0 repos only after the anchor and T1 repos only after last_indexed_sha. document_versions rows are planned only for documents whose last-changing commit differs from the existing row.
- worker: Duplicate id: all files with the id are rejected except exactly one last changed by an approved merge; T1 repos have no approvals so all duplicates are rejected. A document is superseded only by a current same-repo document naming it or via its own superseded_by naming a current one.
- worker: The commit-to-PR fallback of listing closed PRs is not implemented because the client has no such method; a missing PR means disputed. Every .md file in the tree is parsed, so a README.md without frontmatter gets a rejection alert. The writer phase must resolve superseded_by to a uuid, parse dates and dedupe alerts across syncs.
- review: The worker commit trailer says Plan-Phase: knowledge-hub/p11a1-sync-plan (wrong slug); accepted because the merge commit carries the correct trailer. The worker hit the account usage limit mid-run and finished after a resume.

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
