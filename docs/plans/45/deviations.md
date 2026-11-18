# Deviations: admin-commands

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-yamledit

- pending

## p3a-steps-db

- worker: remove_group also deletes the group's kh.record_moves rows, as plan section 3.6 says and the brief does not
- review: workers had no tld; the orchestrator ran roles required (none required) and scope check (no out-of-scope paths); integration tests could not run here (no database), the worker ran them against a local Postgres

## p3b-steps-forgejo

- None.

## p2-ops

- pending

## p4-apply

- pending

## p5-cli-labels-persons

- pending

## p6-cli-groups

- pending

## p7-retire

- pending

## Final review

- pending

## Finishing

- pending
