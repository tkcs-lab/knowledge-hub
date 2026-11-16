# Deviations: owner-self-merge

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-rule-and-config

- None.

## p2-schema-and-fake

- None.

## p2b-forbid-stamp

- None.

## p3-trust-walk

- None.

## p4-store-and-rebuild

- None.
- final: The worker ran the document_versions UPDATE on every sync, not only on --rejudge, and wrote the merge verdict onto disputed version rows (final review, blocking); fixed by fix-1.
- review: The DELETE of kh.merge_verdicts on --rejudge is also guarded by not plan.refused, which the plan did not say (final review, non-blocking).

## p5-stamp-output

- None.

## p6-domain-owners

- None.

## p7-retire-docs

- None.

## Final review

- pending

## Finishing

- pending
