# Deviations: admin-panel

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-settings-admins

- worker: Created the local venv to run checks; tld was unavailable in the worker so the orchestrator ran the roles step (none required).

## p2-config-watch

- worker: tld was unavailable in the worker, so the orchestrator ran the roles step (none required); no other deviation.

## p3-directory-watch

- worker: tld was unavailable in the worker, so the orchestrator ran the roles step (none required); no other deviation.

## p4-admin-if-sha256

- worker: tld was unavailable in the worker, so the orchestrator ran the roles step (none required); no other deviation.

## p5-job-queue-ops

- worker: tld was unavailable in the worker; the orchestrator ran the roles step (none required). The new integration tests could not run in this environment (the database fixture fails with FeatureNotSupported, apparently no pgvector); only unit tests and lint ran, so CI has to run them.

## p6-panel-session-auth

- worker: tld was unavailable in the worker, so the orchestrator ran the roles step (none required); no other deviation.

## p7-panel-app-shell

- worker: tld was unavailable in the worker, so the orchestrator ran the roles step (none required); no other deviation.

## p8-panel-read-api

- worker: tld was unavailable in the worker, so the orchestrator ran the roles step (none required). The new integration test file could not run here (no pgvector); CI has to run it.
- scope: tests/unit/test_panel_app.py (owned by p7) was extended for the new route builder; small and expected.

## p9-panel-ops-args

- worker: tld was unavailable in the worker, so the orchestrator ran the roles step (none required); no other deviation.

## p10-panel-health

- worker: tld was unavailable in the worker, so the orchestrator ran the roles step (none required). The new integration test file could not run here (no pgvector); CI has to run it.

## p11-panel-ops-run

- worker: tld was unavailable in the worker, so the orchestrator ran the roles step (none required); no other deviation.

## p11b-panel-ops-routes

- worker: tld was unavailable in the worker, so the orchestrator ran the roles step (none required). The integration test file could not run here (no pgvector); CI has to run it. The worker first sent a doorbell with a non-SHA head, then corrected it; the branch tip matched.

## p12-ui-core-labels

- worker: tld was unavailable in the worker, so the orchestrator ran the roles step (none required); no other deviation. The UI was not exercised in a browser by the orchestrator; ma2-click-through covers it.

## p13-ui-groups-repos

- worker: tld was unavailable in the worker, so the orchestrator ran the roles step (none required); no other deviation. The UI was not exercised in a browser by the orchestrator; ma2-click-through covers it.

## p14-ui-pipeline-health

- worker: tld was unavailable in the worker, so the orchestrator ran the roles step (none required); no other deviation. The UI was not exercised in a browser by the orchestrator; ma2-click-through covers it.

## p15-deploy-example

- worker: tld was unavailable in the worker, so the orchestrator ran the roles step (none required); no other deviation.

## p16-docs-retirement

- worker: tld was unavailable in the worker, so the orchestrator ran the roles step (none required); no other deviation.

## Final review

- final: Blocking finding 1 (p13): Groups and Repos actions applied at once without Preview, against Design 3.8; being fixed in fix-1.
- final: Blocking finding 2 (p14): Pipeline buttons used a copy of the op flow from groups.js instead of opForm previewless, and was not recorded; being fixed in fix-1.
- final: Non-blocking findings 4, 5, 7, 8 and 9 (directory watch switch, reclaim attempts, health reads, config error text, plural job output) were fixed in fix-2; its integration test change could not run here (no pgvector).
- final: Blocking findings 1 and 2 and non-blocking findings 3 and 6 (Preview-then-Apply for every Groups and Repos action, one shared op flow in ops.js, busy message on 409, no empty dirs on repo save) were fixed in fix-1. The UI was not exercised in a browser by the orchestrator; ma2-click-through covers it.
- final: Second review approved; its one non-blocking finding (Pipeline action output cleared by the tab reload) is fixed in fix-3.
- final: fix-3 merged: the Pipeline tab keeps the output of the last action after the reload.

## Finishing

- scope: Scope disposition: the one out-of-scope path (tests/unit/test_panel_app.py, p8) is accepted and still in the final diff.
- worker: The fix branches fix-1, fix-2 and fix-3 were not scope-checked: each spans files of several phases and their diffs were reviewed by hand and by the second final review.
- worker: The integration tests added by p5, p8, p10, p11b and fix-2 could not run in the session environment (no pgvector); only unit tests and ruff ran. CI has to run them. The UI was not exercised in a real browser by the orchestrator; manual step ma2-click-through covers it.
