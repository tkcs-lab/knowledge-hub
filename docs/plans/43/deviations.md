# Deviations: t0a-technical-docs

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-config

- review: none beyond a CHANGELOG entry under Unreleased (allowed by the changelog rule)
- None.

## p2-tier-schema

- worker: two existing tests outside touches changed: test_migration_0002 now expects the 0004 migration, and test_search_ranking uses equal tier weights because T0 >= T0a >= T3 forbids flipping them
- conflict: CHANGELOG.md conflicted with p1 (both added entries); resolved by keeping both
- review: orchestrator ran the full suite including integration on a local Postgres 16: 950 passed

## p3-get-doc-usage

- retry: first attempt stopped: the plan's usage SQL broke test_outranked_t0_needs_no_doc_before_the_record, whose fixture used a doc key with no document row; the orchestrator told the worker to repair the fixture with a real T0 document
- worker: existing test test_outranked_t0_needs_no_doc_before_the_record now seeds a real T0 document (add_doc); assertion unchanged

## p4-tech-docs

- worker: one extra test (test_changed_text_missing_raises); one label alert per configured label missing from kh.labels, in config order

## p5-tech-sync

- pending

## p6-worker-webhook

- pending

## p7-setup

- pending

## p8-retire-docs

- pending

## Final review

- pending

## Finishing

- pending
