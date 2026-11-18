# Deviations: t0a-technical-docs

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-config

- review: none beyond a CHANGELOG entry under Unreleased (allowed by the changelog rule)
- None.
- final: fix-1: an empty tech_org with tech_repos is now refused (non-blocking review finding, fixed at the owner's request); new test test_tech_repos_need_a_non_empty_tech_org

## p2-tier-schema

- worker: two existing tests outside touches changed: test_migration_0002 now expects the 0004 migration, and test_search_ranking uses equal tier weights because T0 >= T0a >= T3 forbids flipping them
- conflict: CHANGELOG.md conflicted with p1 (both added entries); resolved by keeping both
- review: orchestrator ran the full suite including integration on a local Postgres 16: 950 passed

## p3-get-doc-usage

- retry: first attempt stopped: the plan's usage SQL broke test_outranked_t0_needs_no_doc_before_the_record, whose fixture used a doc key with no document row; the orchestrator told the worker to repair the fixture with a real T0 document
- worker: existing test test_outranked_t0_needs_no_doc_before_the_record now seeds a real T0 document (add_doc); assertion unchanged
- final: fix-1 removed the unused tier column from list_stale (final review, blocking)

## p4-tech-docs

- worker: one extra test (test_changed_text_missing_raises); one label alert per configured label missing from kh.labels, in config order

## p5-tech-sync

- None.

## p6-worker-webhook

- worker: rebuild and check raise the KH_FORGEJO_URL-required error when a tech repo is selected or configured and the tech reader is missing (plan stated it only for check); one extra test for it

## p7-setup

- None.
- final: fix-1: _effective takes admin keyword-only (non-blocking review finding, fixed at the owner's request)

## p8-retire-docs

- None.
- final: fix-1: runbook step 4 now says the first sync is queued when kh serve starts (non-blocking review finding, fixed at the owner's request)

## Final review

- final: blocking: list_stale query selected an unused tier column (p3-get-doc-usage), against plan 3.9; fix worker fix-1 reverts it

## Finishing

- pending
