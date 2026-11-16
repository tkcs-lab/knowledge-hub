# Deviations: owner-self-merge

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-rule-and-config

- None.

## p2-schema-and-fake

- None.
- conflict: main gained migration 0007_any_of_audiences.sql (PR 17) while the plan ran; the plan migration was renumbered to 0008_merge_verdicts.sql and the migration tests adjusted (fix-3).

## p2b-forbid-stamp

- None.

## p3-trust-walk

- None.

## p4-store-and-rebuild

- None.
- final: The worker ran the document_versions UPDATE on every sync, not only on --rejudge, and wrote the merge verdict onto disputed version rows (final review, blocking); fixed by fix-1.
- review: The DELETE of kh.merge_verdicts on --rejudge is also guarded by not plan.refused, which the plan did not say (final review, non-blocking).
- worker: fix-1: the rejudge of old version rows takes values from the verdict only when the verdict and the repo are trusted; versions at the current head follow the plan's per-file outcome, because the plan's DocVersion list holds only new versions.
- worker: fix-2: a rejudge leaves old version rows that are disputed untouched; the runbook says older disputed versions stay disputed.

## p5-stamp-output

- None.

## p6-domain-owners

- None.

## p7-retire-docs

- None.

## Final review

- final: Review 1 (changes, 1 blocking): the version-row UPDATE in sync.py ran on every sync and ignored per-file disputes (p4); fix-1 started.
- final: Review 2 (changes, 1 blocking): fix-1 writes a trusted stamp onto old version rows that stay disputed when a rejudge turns their merge trusted (sync.py _rejudge_versions); the owner was asked how to proceed.
- final: Review 3 (approve, 0 blocking): fix-2 resolves the review 2 finding; non-blocking points go to the pull request body.

## Finishing

- pending
