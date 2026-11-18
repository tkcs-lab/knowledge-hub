# Deviations: supersede-and-outdated

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-migration-0002

- worker: Migration renumbered to 0003_supersede.sql (header comment -- 0003); tests list three migrations; test_migrate_runner shipped-list test renamed test_three_migrations_are_shipped.
- review: Owner decision (board ask a-2053584aacfcb343): main gained 0002_usage_events.sql after the plan was written, so the plan 0002_supersede.sql became 0003_supersede.sql; the plan text was left unchanged and the override went into the worker brief.
- retry: First worker blocked on the name clash and was archived; a second worker did the phase.
- review: Worker brief referenced the plan manifest entry instead of copying it; worker ran no tld steps (tld unavailable); no roles are required at this point; integration tests not runnable on the orchestrator machine (worker reported 829 passed).

## p2-t0-supersede-indexer

- None.

## p3-search-and-get-doc

- None.

## p4-mark-record-outdated-tool

- pending

## p5-propose-supersedes

- None.

## p6-similar-items

- pending

## p7-retire-docs

- pending

## Final review

- pending

## Finishing

- pending
