# Deviations: agent-writes

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-forgejo-client

- review: Worker brief pointed to the plan manifest entry instead of pasting it; worker had no tld CLI, so roles were checked by the orchestrator (none required).
- worker: none reported; worker suggested a CHANGELOG line, not added (outside touches).

## p2-proposals-migration

- worker: tests/integration/test_any_of_migration.py line 90 changed to stop_after="0007_any_of_audiences.sql" (outside touches): main gained 0007_any_of_audiences.sql after the plan was written, so the plan migration 0007_proposals.sql shares the number 0007 and sorts after it.
- retry: First report was blocked on that test; orchestrator approved the one-line edit and kept the plan file name.
- scope: tests/integration/test_any_of_migration.py accepted as out of scope (see worker entry).

## p3-agent-rules

- None.
- final: Final review: a rationale line "Reason for widening: x" could satisfy the widening check through the PR body; fixed by fix-2 (rationale lines made inert, test added).

## p4-agent-store

- None.

## p5-write-note

- None.

## p6-propose-doc

- None.

## p7-propose-edit

- worker: A T1 or glossary target is refused with NOT_EDITABLE by mapping the bad_path error of promote._target_doc to it.

## p8-governance

- None.

## p9-status-tools

- None.

## p10-proposal-events

- None.

## p11-search-marking

- worker: tests/integration/test_any_of_migration.py changed (outside touches): calls migrate(dsn) before the drift check, since the check now reads chunks.agent_written from 0007_proposals.
- retry: First report blocked on that test; orchestrator approved the one-line edit.
- scope: tests/integration/test_any_of_migration.py accepted as out of scope (see worker entry).

## p12-register-tools

- worker: Removed from __future__ import annotations from src/knowledge_hub/tools/agent_proposals.py (p6 file, outside touches): pydantic could not resolve the Literal of the kind argument when registering propose_doc.
- retry: First report blocked on that import; orchestrator approved deleting the one line.
- scope: src/knowledge_hub/tools/agent_proposals.py accepted as out of scope and a collision with p6-propose-doc (see worker entry).

## p13-docs

- None.
- level: Main gained plans 12, 18 and owner-self-merge while this ran; merging it into the feature branch conflicts in 10 files, so a strong-level conflict worker (fix-1) did the merge instead of the standard worker level.
- conflict: fix-1 merged main (plans 12 and 18, owner-self-merge) into the branch: the requester exclusion also applies under main merge rule (a requester named by a record never passes as an owner self-merge; a proposal without a valid record fails closed even when self-merge would pass).
- conflict: fix-1 renamed migration 0007_proposals.sql to 0009_proposals.sql because main now has 0008_merge_verdicts.sql; references updated.
- level: The fix-1 conflict worker ran at the strong level.

## Final review

- pending

## Finishing

- pending
