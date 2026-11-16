# Deviations: any-of-audiences

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-sql-clauses

- worker: test_any_of_migration.py seeds as the superuser admin_dsn instead of kh_migrator, because SET LOCAL session_replication_role = replica needs superuser (as tests/support/seed.py does)
- worker: worker had no tld CLI, so the orchestrator ran the role check (no roles required at implement_phase) and the verification itself

## p2-python-rules

- pending

## p3-leak-matrix

- pending

## p4-docs-retire

- pending

## Final review

- pending

## Finishing

- pending
