# Deviations: record-removal

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-removal-sql

- worker: none; worker ran without tld, orchestrator ran the role check (no roles required)
- review: none

## p2-removal-tools

- worker: worker reported none; ran without tld, orchestrator checked roles (none required)
- review: none

## p3-removal-purge-cli

- worker: worker reported none; ran without tld, orchestrator checked roles (none required)
- review: none
- scope: scope: tests/integration/test_directory_sync_db.py (stub deps got blobs=None because the daily line now reads deps.blobs)

## p4-removal-docs

- worker: worker reported none; ran without tld, orchestrator checked roles (none required)
- review: none

## Final review

- None.

## Finishing

- pending
