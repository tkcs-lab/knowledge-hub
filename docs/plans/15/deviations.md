# Deviations: any-of-audiences

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-sql-clauses

- worker: test_any_of_migration.py seeds as the superuser admin_dsn instead of kh_migrator, because SET LOCAL session_replication_role = replica needs superuser (as tests/support/seed.py does)
- worker: worker had no tld CLI, so the orchestrator ran the role check (no roles required at implement_phase) and the verification itself

## p2-python-rules

- worker: test_recompute_matches_migration.py seeds its document in canonical-hr (not canonical-all) and its own fixed-id rows, so the second recompute changes nothing
- worker: worker had no tld CLI; orchestrator found no roles required and ran the verification

## p3-leak-matrix

- worker: touched tests/support/tool_cases.py: per-case single-token search slugs (case name without underscores) in the seed chunk text and the search query
- scope: scope: tests/support/tool_cases.py (outside touches, shared fixture)
- retry: phase was blocked on search cases matching other cases through shared words; orchestrator sent the slug fix to the same worker
- worker: worker had no tld CLI; orchestrator found no roles required and ran the verification

## p4-docs-retire

- worker: worker had no tld CLI; orchestrator found no roles required and ran the verification

## Final review

- None.

## Finishing

- review: scope disposition: tests/support/tool_cases.py accepted; two non-blocking review notes (may_embed splits on a literal pipe instead of clause_members; configuration.md upgrade heading sits above the general recompute paragraph) are left for the pull request
