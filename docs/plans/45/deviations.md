# Deviations: admin-commands

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-yamledit

- gate: plan section 3.4 settings did not round-trip kh.example.yaml (null written as empty); the owner allowed a None-as-null representer in _yaml() via board ask a-06ecdb19b75b93c3
- retry: first worker stopped on the brief stop condition; a follow-up worker added the representer on the same branch

## p3a-steps-db

- worker: remove_group also deletes the group's kh.record_moves rows, as plan section 3.6 says and the brief does not
- review: workers had no tld; the orchestrator ran roles required (none required) and scope check (no out-of-scope paths); integration tests could not run here (no database), the worker ran them against a local Postgres

## p3b-steps-forgejo

- None.

## p2-ops

- review: no deviation found in the diff review; workers had no tld, so the orchestrator ran roles required (none) and the scope check (clean)

## p4-apply

- review: the closing restart line also carries the PrivacyFence refresh note, longer than the brief's text; workers had no tld, so the orchestrator ran roles required (none) and the scope check

## p5-cli-labels-persons

- review: no deviation found in the diff review; workers had no tld, so the orchestrator ran roles required (none) and the scope check (clean); the worker ran the full suite including integration tests (1229 passed)

## p6-cli-groups

- pending

## p7-retire

- pending

## Final review

- pending

## Finishing

- pending
