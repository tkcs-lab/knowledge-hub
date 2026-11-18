# Deviations: admin-commands

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-yamledit

- gate: plan section 3.4 settings did not round-trip kh.example.yaml (null written as empty); the owner allowed a None-as-null representer in _yaml() via board ask a-06ecdb19b75b93c3
- retry: first worker stopped on the brief stop condition; a follow-up worker added the representer on the same branch
- retry: fix-4 merged: commit creates its temp file with tempfile.mkstemp so a stale temp file cannot block it (final review finding 6)

## p3a-steps-db

- worker: remove_group also deletes the group's kh.record_moves rows, as plan section 3.6 says and the brief does not
- review: workers had no tld; the orchestrator ran roles required (none required) and scope check (no out-of-scope paths); integration tests could not run here (no database), the worker ran them against a local Postgres
- final: the kh.record_moves delete sat in the shared _remove_group, so kh setup apply --prune also deleted those rows, which the plan says stays unchanged (fix-2)
- retry: fix-2 merged: the record_moves delete now runs only for kh group remove, so kh setup apply --prune behaves as before (final review finding 4)

## p3b-steps-forgejo

- None.

## p2-ops

- review: no deviation found in the diff review; workers had no tld, so the orchestrator ran roles required (none) and the scope check (clean)

## p4-apply

- review: the closing restart line also carries the PrivacyFence refresh note, longer than the brief's text; workers had no tld, so the orchestrator ran roles required (none) and the scope check
- final: a build_steps ValueError raised lazily inside after was treated as an after failure and the file was committed; plan 3.3 says it writes nothing (fix-1)
- retry: fix-1 merged: check exceptions and lazy step build errors now write nothing (final review findings 1 and 2)

## p5-cli-labels-persons

- review: no deviation found in the diff review; workers had no tld, so the orchestrator ran roles required (none) and the scope check (clean); the worker ran the full suite including integration tests (1229 passed)

## p6-cli-groups

- review: no deviation found in the diff review; workers had no tld, so the orchestrator ran roles required (none) and the scope check (clean)
- review: fix-4 merged: kh group remove now requires --yes also with --no-apply, which plan 3.7 did not (final review finding 5, owner asked to fix all findings)

## p7-retire

- review: no deviation found in the diff review; workers had no tld, so the orchestrator ran roles required (none) and the scope check (clean); no docs index in this repository
- retry: fix-3 merged: changelog entries moved under Unreleased after the main merge; runbook config paths and the group remove doc row corrected (final review findings 3, 7, 8)

## Final review

- final: blocking: an exception inside check committed kh.yaml without the confirmation gate (p4-apply, fix-1)
- final: blocking: lazy build_steps ValueError committed the file (p4-apply, fix-1)
- final: blocking: after the main merge the changelog entries sat under 0.5.0 and Unreleased was empty (p7-retire, fix-3)
- final: blocking: setup apply --prune also deleted record_moves rows (p3a-steps-db, fix-2)
- final: second review: approve; all 8 findings of the first review confirmed fixed (4 blocking, 4 non-blocking)
- scope: src/knowledge_hub/admin/yamledit.py: fix-4 edited this p1 file for the temp-file finding; accepted
- scope: tests/unit/test_admin_yamledit.py: fix-4 added the stale-temp-file test here; accepted

## Finishing

- final: scope disposition: the only drift is fix-4 editing yamledit.py and its test (p1 files), accepted; dod gate (ruff, full pytest 1255 passed against a local Postgres) green
