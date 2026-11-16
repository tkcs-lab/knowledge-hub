# Deviations: agent-writes

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-forgejo-client

- review: Worker brief pointed to the plan manifest entry instead of pasting it; worker had no tld CLI, so roles were checked by the orchestrator (none required).
- worker: none reported; worker suggested a CHANGELOG line, not added (outside touches).

## p2-proposals-migration

- worker: tests/integration/test_any_of_migration.py line 90 changed to stop_after="0007_any_of_audiences.sql" (outside touches): main gained 0007_any_of_audiences.sql after the plan was written, so the plan migration 0007_proposals.sql shares the number 0007 and sorts after it.
- retry: First report was blocked on that test; orchestrator approved the one-line edit and kept the plan file name.
- scope: tests/integration/test_any_of_migration.py accepted as out of scope (see worker entry).

## p3-agent-rules

- None.

## p4-agent-store

- None.

## p5-write-note

- pending

## p6-propose-doc

- pending

## p7-propose-edit

- pending

## p8-governance

- None.

## p9-status-tools

- pending

## p10-proposal-events

- None.

## p11-search-marking

- pending

## p12-register-tools

- pending

## p13-docs

- pending

## Final review

- pending

## Finishing

- pending
