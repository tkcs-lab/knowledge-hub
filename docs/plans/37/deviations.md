# Deviations: usage-log

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-schema-and-settings

- final: tests/integration/test_migrate_runner.py: the worker renamed test_schema_migrations_lists_the_baseline to test_schema_migrations_lists_both_migrations; the plan kept the old name. The rename is kept because the test now lists both migrations.

## p2-event-logging

- worker: usage.py reads the cursor key file inside the try block, so an unreadable key counts as a dropped event and never raises; the plan code did not say where the read goes.

## p3-usage-tool

- None.

## p4-retention

- None.

## p5-docs-and-retirement

- None.

## Final review

- final: review 1: one blocking finding (unrecorded test rename in p1, recorded above); non-blocking: opens tie-break test and rec: open-event test missing.

## Finishing

- final: Scope: no out-of-scope paths in any phase; the p1 scope check was skipped because its merge came before the check.
