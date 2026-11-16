# Deviations: ci-checks

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-ci-workflow

- worker: CHANGELOG had no "### Added" under Unreleased, so the worker added that subheading for the bullet.
- worker: Worker could not run tld (dod, roles); the orchestrator ran roles required/verify: no roles required.

## Final review

- None.

## Finishing

- scope: No out-of-scope paths. Workflow files are delivered as protected-change pull requests 22 (ci.yml) and 23 (release.yml), approved by the owner on the board.
