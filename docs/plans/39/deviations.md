# Deviations: archive-groups-labels

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-config

- None.

## p2-tools

- worker: integration test uses the fresh_db fixture for document inserts instead of seeded_db.admin_dsn, which does not exist
- review: tools/usage.py (added on main after the plan) returns record labels unfiltered; archived labels would show there; fixed by a follow-up fix worker

## p3-indexer

- pending

## p4-setup

- None.

## p5-retire

- pending

## Final review

- pending

## Finishing

- pending
