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

- worker: set_flags and set_choices validate through check_new_column on a probe column
- worker: CLI prints "dataset: audience <g> is not an active group" for an inactive audience

## p4-row-mirror

- None.

## p5-row-writes

- None.

## p6-query-builder

- retry: Worker ended its first turn without a report and was nudged once; it then reported done
- worker: Worker reported an accidental unpushed ruff format in its own tree; the pushed commit is unaffected

## p7-query-tools

- worker: With saved, limit overrides the stored limit only when it differs from the default 100 (plan does not say how to tell an explicit limit)

## p8-get-doc-rows

- worker: Two existing rec: assertions in test_tool_get_doc.py now expect a row key, since rec: answers gain row (file is in touches)
- worker: Malformed doc:, rec: and row: ids give BAD_GET_ID; BAD_ID and do_move unchanged

## p9-history

- worker: test_as_of_is_parsed_but_ignored in tests/unit/test_dataset_query_build.py rewritten as test_as_of_needs_history_and_reads_the_twin (as_of now needs history)
- scope: scope: tests/unit/test_dataset_query_build.py
- worker: A plain date as_of means midnight UTC; _rebuild_mirror_trigger also rebuilds history_upd when history is on

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
