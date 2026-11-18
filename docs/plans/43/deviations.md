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

- pending

## p4-tech-docs

- pending

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
