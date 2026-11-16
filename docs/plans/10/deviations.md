# Deviations: canonical-validator

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-shared-core

- worker: none
- review: worker ran no tld step (tld unavailable); orchestrator ran roles required (none required) and the scope check
- review: full pytest -q cannot run here: integration tests error because the pgvector extension is missing on this machine; unit tests (244) and ruff pass

## p2-config-owners

- worker: none
- review: worker ran no tld step (tld unavailable); orchestrator ran roles required (none required) and the scope check; full pytest -q not runnable here (no pgvector), unit tests and ruff pass
- retry: final review fix-1: added reference_audiences to the cus entry of deploy/kh.example.yaml and docs/configuration.md, a config-table row for it, and a test that the example entities match the defaults

## p4-forgejo-client

- worker: none
- review: worker ran no tld step (tld unavailable); orchestrator ran roles required (none required) and the scope check; no doorbell arrived, found by the timer
- conflict: CHANGELOG.md: both sides added entries under Unreleased; kept both

## p9-privacyfence-docs

- worker: none
- review: worker ran no tld step (tld unavailable); orchestrator ran roles required (none required) and the scope check

## p3-schema-render

- worker: none
- review: worker ran no tld step (tld unavailable); orchestrator ran roles required (none required) and the scope check

## p5-validate-rules

- worker: none
- review: worker ran no tld step (tld unavailable); orchestrator ran roles required (none required) and the scope check

## p6-validate-command

- worker: none
- review: worker ran no tld step (tld unavailable); orchestrator ran roles required (none required) and the scope check

## p7-domain-owners

- worker: none
- review: worker ran no tld step (tld unavailable); orchestrator ran roles required (none required) and the scope check

## p10-retire-docs

- worker: none
- review: worker ran no tld step (tld unavailable); orchestrator ran roles required (none required) and the scope check; full pytest -q not runnable here (no pgvector), unit tests and ruff pass

## Final review

- final: p2-config-owners: deploy/kh.example.yaml and the docs/configuration.md example gave cus no reference_audiences, so an install from the example would lose the customer-reference restriction; fixed in a fix worker
- final: p10-retire-docs: docs/configuration.md has no reference_audiences row in the config table (described in prose only); fixed in a fix worker

## Finishing

- pending
