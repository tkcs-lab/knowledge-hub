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

## p4-forgejo-client

- pending

## p9-privacyfence-docs

- worker: none
- review: worker ran no tld step (tld unavailable); orchestrator ran roles required (none required) and the scope check

## p3-schema-render

- worker: none
- review: worker ran no tld step (tld unavailable); orchestrator ran roles required (none required) and the scope check

## p5-validate-rules

- pending

## p6-validate-command

- pending

## p7-domain-owners

- pending

## p10-retire-docs

- pending

## Final review

- pending

## Finishing

- pending
