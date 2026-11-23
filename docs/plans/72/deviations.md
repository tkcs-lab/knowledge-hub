# Deviations: structured-datasets

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-schema-catalog

- worker: Worker also updated tests/integration/test_migration_0002.py (one line: expected migration list now includes 0007)
- scope: scope: tests/integration/test_migration_0002.py

## p2-dataset-create

- gate: Policy D uses the ::text[] cast form (plan 3.2/decision 15 text fails in Postgres); owner chose it on board ask a-721c15dc6236a47d
- worker: model.py adds one refusal text not listed in 3.2: column type <t> is not valid
- retry: Worker stopped blocked once on the policy defect and was resumed after the owner answer

## p3-dataset-alter-cli

- pending

## p4-row-mirror

- pending

## p5-row-writes

- pending

## p6-query-builder

- pending

## p7-query-tools

- pending

## p8-get-doc-rows

- pending

## p9-history

- pending

## p10-csv-import

- pending

## p11-bi-roles

- pending

## p12-panel-datasets

- pending

## p13-docs-retirement

- pending

## Final review

- pending

## Finishing

- pending
