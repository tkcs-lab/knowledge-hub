# Plan: owner self-merge as a T0 stamp

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** An operator lists teams in `self_merge_teams` in `kh.yaml`. A merged pull request is then trusted in the T0 walk either as before (an approval by someone else, stamp `reviewed`) or because its author merged it themselves and belongs to a self-merge team that owns every path it touched (stamp `owner_merge`). `search` and `get_doc` return `stamp` next to `approved_by`. `kh rebuild --rejudge` judges the history afresh; plain `kh rebuild` and every sync reuse stored verdicts. `kh domain-owners` passes an open PR when the owner approvals are in place or self-merge would apply, and its output says which.

**Scope:** New `self_merge_teams` and `bot_logins` config keys, a pure rule module shared by the trust walk and `kh domain-owners`, a team directory and tree-diff helper, table `kh.merge_verdicts`, `stamp` columns on `kh.documents` and `kh.document_versions`, the `--rejudge` flag, docs and the 0.2.0 release. Squash and rebase merges stay untrusted as today. With an empty `self_merge_teams` results are identical to today's.

**Security:** A bot never qualifies, the owner rules are read at the merge commit's first parent, the touched paths come from a tree comparison (not the compare endpoint, which may list fewer files), and a team read that fails stops the sync instead of silently disputing. The indexer token needs `read:organization`.

**Review focus:** the rule and its order (3.1); stored verdicts, including disputed ones, and when they are reused (3.5); how `kh domain-owners` combines owner approvals with self-merge and the widen rules (3.7); the touched-paths source (3.3).

## 1. Goal

Let an owner such as a CEO or COO change canonical content in their own domain alone, with history, CI and accountability intact, and tell agents which kind of approval a T0 document carries. Tracking issue: [tkcs-lab/knowledge-hub#12](https://github.com/tkcs-lab/knowledge-hub/issues/12). Owner decisions taken on the board (2026-11-16): the plan waits for #10 and #14, which are merged; `kh domain-owners` keeps the owner-team approval rule for path (a) and adds self-merge as a second way to pass; the bot list becomes the `bot_logins` config key.

## 2. Current state

- `src/knowledge_hub/approvals.py`: `BOT_LOGINS = ("kh-hub-bot",)` and `current_approvals(pr, reviews, bot_logins)`, the one definition of a counted approval (APPROVED, official, not dismissed, on the PR head, not the author, not a bot).
- `src/knowledge_hub/indexer/trust.py`: `Verdict` (status, approved_by, approved_at, merge_pr, pr_url, reason), `_merge_verdict` (`:92`) accepts only (a), `evaluate_chain` (`:123`) is pure and walks the first-parent chain after the anchor.
- `src/knowledge_hub/indexer/sync_plan.py`: `_gather_trust` (`:108`) re-fetches the PR and reviews of every merge commit after the anchor on every sync and passes `BOT_LOGINS`; `DocUpsert`, `DocVersion` and `_doc_changed` carry no stamp.
- `src/knowledge_hub/indexer/sync.py`: `_load` reads `repo_state` and `documents`; `sync_repo` re-plans under an advisory lock when the stored state changed; `_write` writes documents, versions, alerts and `repo_state`. `src/knowledge_hub/indexer/rebuild.py:82` `rebuild(deps, repo)` and `cli.py:61` `_rebuild`.
- `src/knowledge_hub/governance/ci.py:232` `_domain_owners` builds `counted` with `BOT_LOGINS`, resolves team members and calls `owners_check.check_owners`; a pass prints nothing. `governance/owners.py` has `OwnersFile.rule_for` (first matching rule) and `build_owners` (governance paths map to the admin team).
- `src/knowledge_hub/forgejo/client.py` has `team_id`, `is_team_member`, `compare_files`, `list_tree` (entries with `path`, `type`, `sha`), `get_raw`, `pull_for_commit`. `tests/support/forgejo_fake.py` has none of the team methods; `pr()` has no `merged_by` or `merged_at`.
- `src/knowledge_hub/tools/search.py:50` and `tools/docs.py:98` return `approved_by` and `approved_at`; `search/sql.py:120` selects the document columns.
- `docs/design.md` "Trust walk and anchor rules" describes only the approval rule. No docs index exists (the profile has no `docs.index`).
- Issue #11 (merged as #14) records an open question: whether a CI user that is not in a team can list teams and read membership. The indexer token is not that user, but the same endpoints are used.

## 3. Design

### 3.1 The rule

For a merge commit `M` of a PR into `main`, with first parent `F`, the verdict is, in this order:

1. No merged PR for `M`: disputed, reason `no merged pull request for merge commit` (unchanged).
2. Path (a): `current_approvals(pr, reviews, bots)` is not empty: trusted, stamp `reviewed`, `approved_by` the sorted approvers, `approved_at` the latest review time (unchanged, `BAD_TIMESTAMP` handling unchanged).
3. Path (b): `self_merge_check(...)` (3.2) is ok: trusted, stamp `owner_merge`, `approved_by` is `(merger,)`, `approved_at` is the PR's `merged_at`, else the merge commit's committer date; if neither parses the verdict is disputed with reason `unparsable timestamp`.
4. Otherwise disputed, reason `no approval of the final head` (the text stays the same whether or not self-merge was tried, so an empty `self_merge_teams` changes nothing).

Rejected: letting (b) win over (a) (a document with a real second reviewer should say so); a vacuous (b) for a merge with no changed paths (an empty diff proves no ownership, so it is refused).

### 3.2 `knowledge_hub/merge_rule.py` (pure)

```python
REVIEWED = "reviewed"; OWNER_MERGE = "owner_merge"
NOT_ENABLED = "self-merge is not enabled"
NO_MERGER = "the merger is unknown"
OTHER_MERGER = "merged by someone other than the author"
BOT_AUTHOR = "the author is a bot"
NO_OWNERS = "owners.yml is not available at the parent commit"
NO_PATHS = "no changed paths"
NOT_OWNER = "the author does not own every changed path through a self-merge team"

@dataclass(frozen=True)
class SelfMerge: ok: bool; reason: str | None; uncovered: tuple[str, ...] = ()

def touched_paths(changes: Iterable[Mapping[str, Any]]) -> tuple[str, ...]   # sorted unique filename and previous_filename
def teams_to_check(owners: OwnersFile | None, paths: Sequence[str], self_merge_teams: Collection[str]) -> frozenset[str]
def self_merge_check(*, author: str | None, merger: str | None, bots: Collection[str],
                     owners: OwnersFile | None, paths: Sequence[str],
                     self_merge_teams: Collection[str], author_teams: Collection[str]) -> SelfMerge
```

`self_merge_check` tests in this order: empty `self_merge_teams` gives `NOT_ENABLED`; `author` or `merger` missing gives `NO_MERGER`; different logins give `OTHER_MERGER`; author in `bots` gives `BOT_AUTHOR`; `owners is None` gives `NO_OWNERS`; no paths gives `NO_PATHS`. Then for every path `rule = owners.rule_for(path)`; the path is uncovered when `rule is None`, or `rule.team` is not in `self_merge_teams`, or `rule.team` is not in `author_teams`. Any uncovered path gives `NOT_OWNER` with the sorted uncovered paths; otherwise ok. "Owns" means the first matching rule, the same rule `kh domain-owners` uses, so governance paths (`owners.yml`, `schema/**`, `.forgejo/**`) belong to `kh-admins` and need `kh-admins` in `self_merge_teams`. `teams_to_check` returns the `rule.team` of every path that has a rule and is in `self_merge_teams`.

### 3.3 `knowledge_hub/merge_io.py` (reads Forgejo)

- `class TeamDirectory(forgejo)` caches for one run: `async is_member(team, login) -> bool` (team id through `team_id`, an unknown team gives `False`; membership through `is_team_member`) and `async teams_of(login, teams) -> frozenset[str]`. `ForgejoError` (including 403 when the token lacks `read:organization`) propagates: a sync that needs a membership and cannot read it fails and is retried, it never disputes quietly.
- `async owners_at(forgejo, repo, sha) -> OwnersFile | None`: `get_raw(repo, "owners.yml", sha)`; an error whose text contains `HTTP 404` or an `OwnersError` gives `None`; other errors propagate.
- `async tree_paths_changed(forgejo, repo, base, head) -> tuple[str, ...]`: `list_tree` at both commits, entries of type other than `tree`, the sorted paths whose blob sha differs or that exist on one side only. A rename shows up as a removed and an added path, so both are checked. Rejected: the compare endpoint, because a truncated file list would hide a path outside the author's domain; and a diff per commit through `changed_files`, for the same reason.

### 3.4 Config

`Config.self_merge_teams: list[str] = []`, each name must fullmatch `[a-z0-9][a-z0-9_-]*` (error text `config: self_merge_teams team 'X' is not a valid team name`); duplicates are removed keeping order. `Config.bot_logins: list[str] = ["kh-hub-bot"]`, each name must fullmatch `[A-Za-z0-9][A-Za-z0-9_.-]*`. `Config.bots` is a property returning `tuple(dict.fromkeys(("kh-hub-bot", *self.bot_logins)))`, so `kh-hub-bot` is always a bot. `approvals.BOT_LOGINS` stays as the base constant. `config_hash` is unchanged (visibility only). Both keys are read at judging time and stored verdicts are not re-judged by a config change; see 3.5.

### 3.5 Stored verdicts

Migration `0007_merge_verdicts.sql`:

```sql
CREATE TABLE kh.merge_verdicts (
    repo text NOT NULL, merge_sha text NOT NULL, pr_number int,
    status text NOT NULL CHECK (status IN ('trusted', 'disputed')),
    stamp text CHECK (stamp IN ('reviewed', 'owner_merge')),
    reason text, approved_by text[] NOT NULL DEFAULT '{}', approved_at timestamptz, pr_url text,
    judged_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, merge_sha),
    CONSTRAINT merge_verdicts_stamp CHECK ((status = 'trusted') = (stamp IS NOT NULL)));
ALTER TABLE kh.documents ADD COLUMN stamp text CHECK (stamp IN ('reviewed', 'owner_merge'));
ALTER TABLE kh.document_versions ADD COLUMN stamp text CHECK (stamp IN ('reviewed', 'owner_merge'));
```

Row-level security `ENABLE` and `FORCE`, policy `kh_migrator_all`, `GRANT SELECT, INSERT, UPDATE, DELETE` and policy `kh_indexer_all` to `kh_indexer` only: `kh_app` and `kh_provenance` get nothing, so the table is readable by the indexer role only. The `kh_app` grants on `documents` and `document_versions` are table-level, so `stamp` is readable there at once and RLS is unchanged.

Every merge commit after the anchor is judged once and its verdict stored, trusted or disputed. A stored verdict is reused by every later sync and rebuild, and the PR, reviews, owners file and team reads are skipped for it (a performance gain: today every sync re-reads every PR). The consequences, to be written into the runbook:

- A person leaving a team no longer turns their earlier self-merges disputed.
- Enabling `self_merge_teams` does not bless history that was already judged; `kh rebuild --rejudge` does that on purpose.
- Changing `bot_logins` or `self_merge_teams`, an account compromise, a reviewer later found to be unfit: run `kh rebuild --rejudge [--repo R]`.
- The first sync after upgrading judges the whole history once, as a rebuild would; run `kh rebuild` right after `kh migrate`.
- A disputed merge stays disputed until `--rejudge`, even if an approval is added afterwards.

Anchor-covered commits and non-merge commits are not stored (their verdict needs no PR). `--rejudge` deletes the repo's rows inside the sync transaction and writes the fresh ones. Rejected: storing only trusted verdicts (a later membership change could then bless an old disputed merge retroactively), and recording membership snapshots (verdict plus judged time is the audit record).

### 3.6 Trust walk plumbing

`trust.py`: `Verdict` gains `stamp: str | None = None`. New `@dataclass(frozen=True) class MergeInput: owners: OwnersFile | None; paths: tuple[str, ...]; author_teams: frozenset[str]`. `evaluate_chain(repo, commits, prs, reviews, changed_files, bot_logins, anchor, *, stored=None, inputs=None, self_merge_teams=())` stays pure; for a merge commit with a stored verdict it uses it, else it calls `_merge_verdict(sha, prs, reviews, bots, inputs.get(sha), self_merge_teams, commit_date)`. `TrustResult.judged: dict[str, Verdict]` holds the merge verdicts judged in this run (not the reused ones).

`sync_plan._gather_trust(forgejo, repo, chain, anchor, config, stored)` skips the PR and review reads for stored shas. For a new merge commit it reads PR and reviews as today and, when `config.self_merge_teams` is not empty, the PR author is the merger, the author is not in `config.bots` and no counted approval exists, it builds `MergeInput`: `owners_at(F)`, `tree_paths_changed(F, M)`, `TeamDirectory.teams_of(author, teams_to_check(...))`. One `TeamDirectory` serves the whole call. `build_sync_plan(..., stored_verdicts=None, rejudge=False)` (`rejudge` ignores the stored verdicts), `SyncPlan.verdicts` is `TrustResult.judged` and `SyncPlan.rejudge` the flag. `DocUpsert.stamp` and `DocVersion.stamp` are `v.stamp` for a trusted file, else `None`; `_doc_changed` compares `stamp`. Documents covered by the anchor have `stamp None`: their `approved_by` is `['trust-anchor']`, and calling that `reviewed` would promise a review nobody recorded.

`sync.py`: `_load` also returns the stored verdicts of the repo as `Verdict` objects; `_write` inserts `plan.verdicts` (`ON CONFLICT (repo, merge_sha) DO NOTHING`), and with `plan.rejudge` first deletes the repo's rows; the documents `INSERT` and versions `INSERT` write `stamp`. `sync_repo(..., rejudge=False)` passes the flag to both `build_sync_plan` calls and re-plans under the lock also when the set of stored shas differs from the one read before. `rebuild(deps, repo=None, rejudge=False)` passes it to both `_sync` calls (including the retry after a refused sync); `cli.py` adds `kh rebuild --rejudge`.

### 3.7 Output and `kh domain-owners`

`search` hits and `get_doc` return `"stamp"`: the document's stamp, `""` when none and for raw and derived rows; `get_doc(version=...)` returns the version row's stamp. `search/sql.py` selects `d.stamp AS doc_stamp`.

`kh domain-owners` (open PR, author as would-be merger) passes when the owner-team approvals of `check_owners` are satisfied or `self_merge_check` is ok, and the widen-audience requirements hold. `check_owners` gains `owner_approval_waived: bool = False`, which skips the per-path approval lines (the widen lines stay). In `_domain_owners`: compute the approval lines as today; if none, the message is `approved by <sorted counted reviewers who are members of a needed team>` (or `no owner approval needed` when no path changed); else, when `config.self_merge_teams` is not empty, build the inputs with `owners` (read at the PR base), `touched_paths(compare) ∪ tree_paths_changed(merge_base or base, head)`, `TeamDirectory.teams_of(author, ...)` and call the same `self_merge_check` with the author as merger; if ok recompute the lines with the approval requirement waived and the message is `self-merge by owner <author>`. A passing result prints that one message as its only line and the commit status description is the message (at most 140 characters, else `all checks passed`); a failing result prints the unsatisfied lines as today. The self-merge branch is not tried when approvals already satisfy the owners, so a CI user that cannot read teams is unaffected in that case. A failed read still exits 2.

Known gap, to be stated in `docs/governance.md`: the check judges the author as merger; if someone else merges the PR the indexer disputes the merge although CI was green.

### 3.8 Shared cases

`tests/data/self_merge/cases.yaml` lists the rule cases once (author, merger, teams, `self_merge_teams`, files, `owners.yml` change, approvals, bot logins) with the expected stamp of the trust walk and the expected pass of `kh domain-owners`. `tests/support/self_merge_cases.py` loads it and builds both worlds. One test per consumer, plus an agreement test: where the trust walk is `owner_merge` and there is no approval, `kh domain-owners` passes; `kh domain-owners` never passes where the trust walk is disputed. They differ only where an approval exists from a non-owner (trust walk `reviewed`, `kh domain-owners` fails), which is deliberate: CI may be stricter than the indexer, never looser.

## 4. ADRs

None (the repository has no ADR directory); the decisions live in sections 3.1, 3.3 and 3.5 and in `docs/design.md`.

## 5. Manual steps

### Before implementation

None.

### After implementation (before the feature pull request merges)

**1. ma1-live-probe: check the live Forgejo (about 30 minutes).**
1. Open `https://<your Forgejo host>/user/settings/applications`, create a token for the indexer user with `read:repository` and `read:organization` (the compose repo creates the real one; this is for the probe), and store it in a shell variable only, not in a file.
2. Pick a merged pull request of a test repo and request `GET https://<your Forgejo host>/api/v1/repos/<org>/<repo>/commits/<merge sha>/pull`. Confirm the answer has `merged_by.login` and `merged_at`.
3. Request `GET https://<your Forgejo host>/api/v1/orgs/<org>/teams` and `GET https://<your Forgejo host>/api/v1/teams/<id>/members/<login>` and confirm the token gets 204 for a member and 404 for a non-member.
4. On a test deployment set `self_merge_teams: [<owner team>]` in `kh.yaml`, let a member of that team merge their own pull request that changes one document in their domain, run `kh rebuild --repo <repo>`, and confirm `get_doc` shows `stamp: owner_merge` and `approved_by` with the merger. Then remove them from the team, run `kh rebuild` and confirm the stamp stays, run `kh rebuild --rejudge` and confirm the document becomes `disputed`.
Report back: pass or fail for each of the four steps.

**2. ma2-token-scope: give the production indexer token `read:organization` (about 10 minutes).** The compose repo creates the token and this session cannot read that repository.
1. In the compose repo find where the indexer token (`kh_indexer_token`) is created and add the scope `read:organization` next to `read:repository`.
2. Deploy, and only then add teams to `self_merge_teams`. With the default empty list the hub makes no team call.
Report back: the scope list of the token and pass or fail.

### After the feature pull request merges (outside the manifest)

**Release 0.2.0 (about 10 minutes).** Version 0.2.0 has not been released yet; it contains #10, #14 and this change.
1. In a Claude Code session on this repository run `/tld:release` and give version `0.2.0`; approve the one board question it asks.
2. When the release workflow ends, read the image's `sha256:` digest from the workflow summary.
3. Edit the description of the merged feature pull request: add the line `Image digest: <digest>`.
4. On the host run `kh migrate`, then `kh rebuild` once, as the runbook says.
Report back: pass or fail, and the digest.

## 6. Risks and open questions

- **PR fields (high).** The recorded fixtures have no `merged_by` or `merged_at`. If Forgejo omits either on `commits/<sha>/pull`, self-merge never applies (fail closed) and the timestamp falls back to the commit date. Probe: ma1 steps 2 and 4. A phase that finds a different field name stops with `status=blocked`.
- **Token scope (high).** Without `read:organization` a sync that needs a membership fails (403) until the token is fixed; with `self_merge_teams` empty nothing is read. Probe and scope: ma1, ma2.
- **CI user visibility (medium).** `kh domain-owners` already needs team reads (plan 10); the self-merge branch reads the author's membership in the same way.
- **Gap between CI and merge (medium).** CI judges the author as merger; another merger gives a disputed merge after a green check. Documented in `docs/governance.md`.
- **Stored disputed verdicts (medium).** A late approval or a Forgejo gap at first judgement is not picked up until `--rejudge`. Documented in the runbook.
- **Compare truncation (medium, existing).** File verdicts still use the compare file list. Self-merge does not: it uses the tree comparison.
- **Integration tests need PostgreSQL with pgvector** (`scripts/ensure_test_pg.sh`); a worker that cannot start it runs the unit tests and says which integration tests did not run; CI runs them.

## Implementation manifest

```yaml
plan_slug: owner-self-merge
feature_branch: feature/owner-self-merge
tracking_issue: 12
max_parallel: 3
manual_before: []
manual_after:
  - id: ma1-live-probe
    title: Check the merged-by fields, team reads and the stamp on the live Forgejo
    why: CI cannot reach the live Forgejo, and the PR and team endpoints are assumed from recorded fixtures.
  - id: ma2-token-scope
    title: Give the production indexer token read:organization
    why: The compose repo owns the token; without the scope a sync that needs a team lookup fails.
verify_after_merge:
  - .venv/bin/python -m pytest -q
final_checks:
  - "`tld plan deviations check --file docs/plans/12/deviations.md --plan docs/plans/12/plan.md --all` exits 0"
  - "the changelog has entries under docs.changelog.section and no version heading"
phases:
  - id: p1-rule-and-config
    title: Config keys, the pure self-merge rule and the Forgejo helpers
    depends_on: []
    complexity: M
    touches:
      - src/knowledge_hub/config.py
      - src/knowledge_hub/merge_rule.py
      - src/knowledge_hub/merge_io.py
      - tests/unit/test_config.py
      - tests/unit/test_merge_rule.py
      - tests/unit/test_merge_io.py
    brief: |
      Implement 3.2, 3.3 and 3.4. Existing helpers to copy: `tests/unit/test_forgejo_status.py` (httpx mock transport style) and `tests/unit/test_governance_owners.py`.
      1. `config.py`: add `TEAM_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")` and `LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")`; add to `Config` the fields `self_merge_teams: list[str] = []` and `bot_logins: list[str] = ["kh-hub-bot"]`; in `Config._validate` raise `ValueError(f"self_merge_teams team {t!r} is not a valid team name")` and `ValueError(f"bot_logins login {b!r} is not a valid login")`; add a `field_validator("self_merge_teams")` that returns the list with duplicates removed in order; add `@property def bots(self) -> tuple[str, ...]` returning `tuple(dict.fromkeys((BOT_LOGINS[0], *self.bot_logins)))` with `from knowledge_hub.approvals import BOT_LOGINS` at the top. Do not touch `config_hash`.
      2. Create `merge_rule.py` exactly as the signatures and the check order of 3.2 (the module constants are the reason texts quoted there). `touched_paths` takes dicts with `filename` and optional `previous_filename`. `teams_to_check` returns `frozenset({rule.team for path in paths if (rule := owners.rule_for(path)) is not None and rule.team in self_merge_teams})`, empty when `owners is None`.
      3. Create `merge_io.py` with `TeamDirectory`, `owners_at` and `tree_paths_changed` exactly as 3.3 (`forgejo` is any object with `team_id`, `is_team_member`, `get_raw`, `list_tree`; `owners_at` catches `ForgejoError` only to test `"HTTP 404" in str(exc)` and re-raises otherwise; import `OwnersError`, `load_owners`, `OwnersFile` from `knowledge_hub.governance.owners`).
      4. Tests. `test_config.py`: defaults, a bad team name, a bad login, de-duplication, `Config().bots == ("kh-hub-bot",)`, `bot_logins: [x]` gives `("kh-hub-bot", "x")`, `config_hash(Config(self_merge_teams=["a"])) == config_hash(Config())`. `test_merge_rule.py`, one test per behaviour: qualifies (author owns every path through a self-merge team, owners from `build_owners("all", ...)`); one path of another team fails and is listed in `uncovered`; team not in `self_merge_teams` fails; other merger fails; author `kh-hub-bot` fails and a configured bot fails even when it is in the team; `owners=None` fails; empty `self_merge_teams` fails with `NOT_ENABLED`; no paths fails; governance paths (`owners.yml`, `schema/x.json`, `.forgejo/workflows/kh.yml`, `glossary.md`) need `kh-admins` in `self_merge_teams` and in `author_teams`; a rename passes only when `previous_filename` is also owned (use `touched_paths`); `teams_to_check` results. `test_merge_io.py` with `ForgejoFake`-free tiny fakes (a class with async methods): `TeamDirectory` caches (count calls), an unknown team is `False`, a `ForgejoError` propagates; `owners_at` returns `None` for a 404 error and for invalid YAML and propagates a 500; `tree_paths_changed` reports a changed blob, an added path, a removed path and a rename as two paths and ignores `tree` entries.
      Do not edit other source files. Stop with `status=blocked` if `Config()` equality or `test_config.py::test_config_hash...` fails after step 1.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_config.py tests/unit/test_merge_rule.py tests/unit/test_merge_io.py -q passes"
      - "grep -n \"self_merge_teams\" src/knowledge_hub/config.py matches"
      - ".venv/bin/python -m ruff check . passes"
  - id: p2-schema-and-fake
    title: Migration 0007 and the Forgejo fake with teams and merger fields
    depends_on: []
    complexity: M
    touches:
      - src/knowledge_hub/migrations/0007_merge_verdicts.sql
      - tests/support/forgejo_fake.py
      - tests/integration/test_schema_tables.py
      - tests/integration/test_rls_tables.py
      - tests/integration/test_rls_policies.py
      - tests/unit/test_forgejo_fake.py
    brief: |
      Implement the SQL of 3.5 and the fake.
      1. Create `0007_merge_verdicts.sql`: the `CREATE TABLE` and the two `ALTER TABLE ... ADD COLUMN stamp` statements of 3.5 verbatim; then `ALTER TABLE kh.merge_verdicts ENABLE ROW LEVEL SECURITY; ... FORCE ROW LEVEL SECURITY;`, `CREATE POLICY kh_migrator_all ON kh.merge_verdicts FOR ALL TO kh_migrator USING (true) WITH CHECK (true);`, `GRANT SELECT, INSERT, UPDATE, DELETE ON kh.merge_verdicts TO kh_indexer;` and `CREATE POLICY kh_indexer_all ON kh.merge_verdicts FOR ALL TO kh_indexer USING (true) WITH CHECK (true);` (copy the `repo_state` lines of `0003_rls.sql:306` and its policy lines). No grant to `kh_app` or `kh_provenance`. Start the file with a two-line comment saying what it adds.
      2. `tests/support/forgejo_fake.py`: `pr(...)` gains keyword arguments `merged_by: str | None = None` and `merged_at: str | None = None` that add `"merged_by": {"login": ...}` and `"merged_at": ...` only when given; `ForgejoFake.__init__` gains `teams: dict[str, set[str]] | None = None`; add `async def team_id(self, name) -> int | None` (1-based position in `teams`, `None` when absent) and `async def is_team_member(self, team_id, login) -> bool`, each appending `"team_id"` / `"is_team_member"` to `self.calls`; give `commits_first_parent` nothing new. `FakeCommit.files` already builds trees, so `get_raw` of `owners.yml` at a parent works.
      3. `test_schema_tables.py`: add `"stamp"` to the `documents` and `document_versions` expected lists and a `merge_verdicts` entry with the column order of the `CREATE TABLE`. `test_rls_tables.py` and `test_rls_policies.py`: extend the existing per-table matrices with `merge_verdicts` following the `repo_state` rows (indexer reads and writes, migrator all; `kh_app` gets `permission denied`; `kh_provenance` gets `permission denied`); read both files first and copy the closest case.
      4. `tests/unit/test_forgejo_fake.py`: `pr()` without the new arguments is unchanged (compare with a literal dict), with them it has `merged_by`; `team_id` and `is_team_member` answers and call recording.
      Stop with `status=blocked` if an existing test of the three integration files fails after step 1 for a reason other than the new column or table. If PostgreSQL cannot be started (`scripts/ensure_test_pg.sh`), run the unit file and `.venv/bin/python -m pytest tests/integration -q --collect-only` and say that the integration tests were not run.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_forgejo_fake.py -q passes"
      - ".venv/bin/python -m pytest tests/integration/test_schema_tables.py tests/integration/test_rls_tables.py tests/integration/test_rls_policies.py tests/integration/test_migrate_runner.py -q passes"
      - "grep -c \"merge_verdicts\" src/knowledge_hub/migrations/0007_merge_verdicts.sql prints a number above 5"
      - ".venv/bin/python -m ruff check . passes"
  - id: p3-trust-walk
    title: Judge self-merge in the trust walk and plan the stamp and the verdicts
    depends_on: [p1-rule-and-config, p2-schema-and-fake]
    complexity: M
    touches:
      - src/knowledge_hub/indexer/trust.py
      - src/knowledge_hub/indexer/sync_plan.py
      - tests/data/self_merge/cases.yaml
      - tests/support/self_merge_cases.py
      - tests/unit/test_trust.py
      - tests/unit/test_sync_plan.py
      - tests/unit/test_self_merge_trust.py
    brief: |
      Implement 3.1, 3.6 (`trust.py` and `sync_plan.py`) and 3.8 (the trust side). Existing helpers: `tests/unit/test_sync_plan.py` (`doc`, `merged`, `repo_of`, `plan`) and `tests/unit/test_trust.py::run`.
      1. `trust.py`: add `stamp: str | None = None` to `Verdict`; add `MergeInput` and `TrustResult.judged` as in 3.6; replace `_merge_verdict` by the 3.1 order using `merge_rule.self_merge_check` (author `pr["user"]["login"]`, merger `(pr.get("merged_by") or {}).get("login")`, `approved_at` from `parse_forgejo_ts(pr.get("merged_at"))` else the commit date passed in); path (a) sets `stamp=REVIEWED`; keep every existing reason text. `evaluate_chain` gets the keyword parameters `stored`, `inputs`, `self_merge_teams` (defaults keep today's behaviour), uses a stored verdict when its sha has one, and records every verdict it computed for a merge commit in `result.judged`. The anchor verdict keeps `stamp None`.
      2. `sync_plan.py`: `_gather_trust(forgejo, repo, chain, anchor, config, stored)` and `build_sync_plan(..., stored_verdicts=None, rejudge=False)` as 3.6 says (skip PR and review reads for stored shas; build `MergeInput` only for a new merge commit whose PR author equals `merged_by`, who is not in `config.bots`, when `config.self_merge_teams` is set and `current_approvals` is empty; use `config.bots` instead of `BOT_LOGINS`, and drop the `BOT_LOGINS` import if unused). Add `stamp: str | None` to `DocUpsert` (after `pr_url`) and `DocVersion`, set as in 3.6, add `("stamp", up.stamp)` to `_doc_changed`, add `verdicts: dict[str, Verdict]` and `rejudge: bool = False` to `SyncPlan` and fill them.
      3. `tests/data/self_merge/cases.yaml` and `tests/support/self_merge_cases.py`: each case has `name`, `author`, `merger`, `teams` (team to members), `self_merge_teams`, `bots` (extra bot logins), `files` (paths the PR changes, each with a valid document body from a helper), `owners_change` (optional new `owners.yml` text the PR commits), `approvals` (logins), `trust` (expected `reviewed`, `owner_merge` or `disputed`) and `domain_owners` (expected `pass` or `fail`). Required cases: owner self-merge in own domain (`trust: owner_merge`, `domain_owners: pass`); one path of another team; team not in `self_merge_teams`; someone else merged; author `kh-hub-bot` merged by `kh-hub-bot` (even listed in a self-merge team); a configured bot; `owners.yml` missing at the parent; a PR that edits `owners.yml` to give itself `kh-admins` self-merge is judged by the old file; governance path with and without `kh-admins` in `self_merge_teams`; non-owner approval without self-merge (`trust: reviewed`, `domain_owners: fail`); bot promotion PR with a human approval (`reviewed`) and without (`disputed`); empty `self_merge_teams` with a self-merge shape (`disputed`); a rename whose old path belongs to another team. The helper `build_trust_world(case) -> (repos, config)` builds a `FakeRepo` with `owners.yml` in the root tree and the PR as merge commit `m1` with `merged_by`, `merged_at` and the reviews, and `ForgejoFake(repos, teams=...)`. (The `domain_owners` world builder comes with p6 and goes in the same file then.)
      4. Tests: `test_self_merge_trust.py` runs `build_sync_plan` for every case and asserts the stamp of the document and its `approved_by` (`[merger]` for `owner_merge`) and that a bot case makes no `is_team_member` call (`fake.calls`); stored verdicts: a second `build_sync_plan` with `stored_verdicts` from the first plan's `verdicts` and a fake in which the author has left the team gives the same stamp and the fake records no `pull_for_commit`, no `team_id` and no `is_team_member`; `rejudge=True` with the same fake gives `disputed`; with an empty `self_merge_teams` the plan equals the plan of an equal world without the new arguments (compare `upserts` and `repo_state`) and the fake has no `team_id` call. `test_trust.py`: add a test that `Verdict.stamp` is `reviewed` for the existing fixtures and `result.judged` holds the merge shas but not the anchor ones; every existing test must pass unchanged. `test_sync_plan.py`: add `stamp` expectations to one existing approval test only.
      Stop with `status=blocked` if a case of the table cannot be built with `ForgejoFake`, or if an existing `test_trust.py` or `test_sync_plan.py` test fails after step 1 or 2.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_self_merge_trust.py tests/unit/test_trust.py tests/unit/test_sync_plan.py -q passes"
      - "grep -n \"BOT_LOGINS\" src/knowledge_hub/indexer/sync_plan.py returns nothing"
      - ".venv/bin/python -m ruff check . passes"
  - id: p5-stamp-output
    title: Return the stamp from search and get_doc
    depends_on: [p2-schema-and-fake]
    complexity: S
    touches:
      - src/knowledge_hub/search/sql.py
      - src/knowledge_hub/tools/search.py
      - src/knowledge_hub/tools/docs.py
      - tests/support/seed.py
      - tests/integration/test_tool_get_doc.py
      - tests/integration/test_get_doc_version.py
      - tests/integration/test_search_ranking.py
    brief: |
      Implement the output half of 3.7.
      1. `search/sql.py:120`: add `d.stamp AS doc_stamp,` after `d.approved_at AS doc_approved_at,`. `tools/search.py` `_hit`: add `"stamp": (row["doc_stamp"] or "") if kind == "doc" else "",` after `approved_at`. `tools/docs.py`: add `d.stamp` to `_DOC_SQL` after `d.approved_at`, `"stamp": row["stamp"] or "",` after `approved_at` in the `doc` output and `"stamp": "",` in the raw and derived output; add `v.stamp` to `_VERSION_SQL` and `stamp=ver["stamp"] or "",` in the `out.update` of `_at_version`.
      2. `tests/support/seed.py`: where it inserts a document, set `stamp` to `reviewed` (find the `approved_by=["x"]` line at about `:98` and add the field to the document and version rows the way the neighbouring columns are written).
      3. Tests: in `test_tool_get_doc.py` assert `res.data["stamp"] == "reviewed"` next to the `approved_by == ["x"]` check and `""` for a raw and a derived id; in `test_get_doc_version.py` assert the version call returns the version row's `stamp`; in `test_search_ranking.py` assert the document hit has `"stamp": "reviewed"` and a raw hit has `""`. Do not change `tests/integration/test_leak_matrix.py`; it must still pass.
      Stop with `status=blocked` if a leak-matrix case fails.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_tool_get_doc.py tests/integration/test_get_doc_version.py tests/integration/test_search_ranking.py tests/integration/test_leak_matrix.py tests/unit/test_tool_schemas.py -q passes"
      - "grep -n \"stamp\" src/knowledge_hub/tools/search.py src/knowledge_hub/tools/docs.py src/knowledge_hub/search/sql.py matches in all three files"
      - ".venv/bin/python -m ruff check . passes"
  - id: p4-store-and-rebuild
    title: Store and reuse verdicts, and kh rebuild --rejudge
    depends_on: [p3-trust-walk]
    complexity: M
    touches:
      - src/knowledge_hub/indexer/sync.py
      - src/knowledge_hub/indexer/rebuild.py
      - src/knowledge_hub/cli.py
      - tests/integration/test_sync.py
      - tests/integration/test_rebuild.py
      - tests/unit/test_cli_rebuild.py
    brief: |
      Implement 3.5 (storage) and the `sync.py` part of 3.6. Existing helpers: `tests/integration/test_rebuild.py` (`make_deps`, `two_docs`, `rows`) and `tests/unit/test_sync_plan.py` (`merged`, `repo_of`).
      1. `sync.py`: `_load` returns `(state, existing, stored)` where `stored` is `{merge_sha: Verdict}` from `SELECT merge_sha, status, stamp, reason, approved_by, approved_at, pr_number, pr_url FROM kh.merge_verdicts WHERE repo = %s` (`approved_by` as a tuple, `approved_at` formatted with `_fmt_ts`); update the three call sites. `_write` gets the plan's verdicts: when `plan.rejudge`, first `DELETE FROM kh.merge_verdicts WHERE repo = %s`; then for each item of `plan.verdicts` `INSERT INTO kh.merge_verdicts (repo, merge_sha, pr_number, status, stamp, reason, approved_by, approved_at, pr_url) VALUES (...) ON CONFLICT (repo, merge_sha) DO NOTHING`. Add `stamp` to the documents `INSERT` column list, the `ON CONFLICT ... DO UPDATE SET` list and the values (`u.stamp`), and to the `document_versions` `INSERT` (`v.stamp`). `sync_repo(..., rejudge: bool = False)` passes `stored_verdicts=stored` and `rejudge` to both `build_sync_plan` calls (the stored verdicts of the locked view in the second call) and also re-plans when `set(locked_stored) != set(stored)`; a `rejudge` run passes `stored_verdicts=None`.
      2. `rebuild.py`: `_sync(deps, name, rejudge=False)` and `rebuild(deps, repo=None, rejudge=False)` pass the flag to both `_sync` calls. `cli.py`: `rebuild` gets `p.add_argument("--rejudge", action="store_true")`; `_rebuild(settings, config, repo, rejudge)` passes it; output lines stay `rebuilt <repo>`.
      3. Integration tests (build the world with `repo_of`, `merged`, the fake with `teams`, a config with `self_merge_teams`): the first sync of a self-merge PR stores one `merge_verdicts` row with `stamp = owner_merge` and `approved_by = {author}`, the document row has `stamp = owner_merge`; remove the author from the fake team and sync again: the row, the document status `current` and `stamp` stay; `rebuild(deps)` keeps them; `rebuild(deps, rejudge=True)` makes the document `disputed`, replaces the row (`status = disputed`, `stamp` null) and a second plain rebuild leaves it; a plain approved PR stores `reviewed`; a disputed merge is stored with `stamp` null; `judged_at` of an existing row does not change on a plain rebuild. A test that a row for a sha no longer on the chain disappears after `rejudge`. `tests/unit/test_cli_rebuild.py`: `main(["rebuild", "--rejudge", "--repo", "x"])` calls the rebuild function with `rejudge=True` (monkeypatch `knowledge_hub.cli._with_deps` or `_rebuild`) and without the flag with `False`.
      Stop with `status=blocked` if the locked re-plan test in `tests/integration/test_sync.py` fails after step 1. If PostgreSQL cannot be started (`scripts/ensure_test_pg.sh`), run the unit file and `--collect-only` on the integration files and say which tests did not run.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_cli_rebuild.py -q passes"
      - ".venv/bin/python -m pytest tests/integration/test_sync.py tests/integration/test_rebuild.py tests/integration/test_sync_chunks.py tests/integration/test_get_doc_version.py -q passes"
      - ".venv/bin/python -m knowledge_hub.cli rebuild --help exits 0 and lists --rejudge"
      - ".venv/bin/python -m ruff check . passes"
  - id: p6-domain-owners
    title: kh domain-owners passes on self-merge and says why
    depends_on: [p1-rule-and-config, p2-schema-and-fake, p3-trust-walk]
    complexity: M
    touches:
      - src/knowledge_hub/governance/ci.py
      - src/knowledge_hub/governance/owners_check.py
      - tests/support/self_merge_cases.py
      - tests/support/forgejo_http.py
      - tests/unit/test_domain_owners.py
      - tests/unit/test_self_merge_agreement.py
    brief: |
      Implement 3.7 and the agreement half of 3.8. Read `tests/unit/test_domain_owners.py` first (`make`, `TEAMS`, `OWNERS_YML`) and copy its style.
      1. `owners_check.py`: `check_owners(..., owner_approval_waived: bool = False)`: when true skip the per-path approval requirement (the `matched` loop that adds `approval from <team> required`) but keep `no owner in owners.yml` lines, the widen lines and the sorting.
      2. `ci.py`: replace `BOT_LOGINS` by `config.bots` in `_domain_owners`; after the existing `check_owners` call, `message` is built as 3.7 says: when `lines` is empty set `message` to `approved by <comma-joined sorted counted reviewers who are in a needed team's members>` (or `no owner approval needed` when `paths` is empty); otherwise, when `config.self_merge_teams` is not empty, `author = (pr.get("user") or {}).get("login")`, `touched = sorted(set(paths) | set(await tree_paths_changed(read, repo, pr.get("merge_base") or base_sha, pr["head"]["sha"])))`, `directory = TeamDirectory(read)`, `author_teams = await directory.teams_of(author, teams_to_check(owners, touched, config.self_merge_teams))` (only when `author` is not None), then `sm = self_merge_check(author=author, merger=author, bots=config.bots, owners=owners, paths=touched, self_merge_teams=config.self_merge_teams, author_teams=author_teams)`; when `sm.ok` recompute `lines = check_owners(..., owner_approval_waived=True)` and set `message = f"self-merge by owner {author}"`. Return `CheckResult(lines or [message], not lines)` where `lines` is empty exactly when the check passes. In `run_domain_owners` the success description is `message` cut to 140 characters when it is not empty, else `all checks passed`; to carry it make `CheckResult` have an optional field `message: str = ""` (keep the existing two fields and order). The CLI prints `lines` as today, so a pass prints the one message line. Import `TeamDirectory`, `tree_paths_changed` from `knowledge_hub.merge_io` and `self_merge_check`, `teams_to_check` from `knowledge_hub.merge_rule`.
      3. `tests/support/forgejo_http.py`: in the `git/trees/` branch of `_handle`, when the repo's world has a key `trees` (a dict `{sha: {path: blob_sha}}`) and the requested sha is in it, serve that tree (`{"path": p, "type": "blob", "sha": blob}` per path, sorted, `total_count` set) instead of `world["tree"]`; otherwise keep today's answer. `tests/support/self_merge_cases.py`: add `build_domain_owners_world(case) -> (FakeForgejoHttp, Config)`: repo `canonical-all` with `owners.yml` in `tree`, an open PR 7 (author `case.author`, `base.sha` `b1`, `merge_base` `m1`, `head.sha` `h1`, no labels), `compare` listing the case files (with `previous_filename` for a rename case), `trees` for `m1` (old paths) and `h1` (new paths and the changed blob shas), `reviews` from the case approvals, `teams` from the case.
      4. Tests. `test_domain_owners.py`: update the assertions that expect an empty pass output (`lines == []` and description `all checks passed`) to the new message lines; add: self-merge by an owner passes with `["self-merge by owner alice"]` and status description `self-merge by owner alice`; approvals by owners still pass with `approved by bob` and the self-merge branch makes no team call for the author (`http.paths("members")` count); a widen PR with self-merge still needs the destination owner (`widen-audience needs approval from one of: owners-hr`); self-merge disabled gives today's `approval from <team> required` lines; the author a bot fails. `test_self_merge_agreement.py`: for every case of `cases.yaml` compare `trust` and `domain_owners`; assert `domain_owners == pass` for each case with `trust == owner_merge` and no approvals, and that no case has `domain_owners == pass` with `trust == disputed`.
      Stop with `status=blocked` if an existing test of `test_domain_owners.py` other than the pass-output ones fails after step 2.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_domain_owners.py tests/unit/test_self_merge_agreement.py tests/unit/test_cli_validate.py -q passes"
      - "grep -n \"self_merge_check\" src/knowledge_hub/governance/ci.py src/knowledge_hub/indexer/trust.py matches both files"
      - ".venv/bin/python -m ruff check . passes"
  - id: p7-retire-docs
    title: Docs, example config and changelog
    depends_on: [p4-store-and-rebuild, p5-stamp-output, p6-domain-owners]
    complexity: S
    touches:
      - docs/design.md
      - docs/configuration.md
      - docs/runbook.md
      - docs/governance.md
      - deploy/kh.example.yaml
      - CHANGELOG.md
    brief: |
      This is the retirement phase; it only writes documentation and the example. Do not edit `docs/plans/**`.
      1. `docs/design.md`, section "Trust walk and anchor rules": change the third bullet to say a merge passes by (a) an approval as before (stamp `reviewed`) or (b) self-merge (stamp `owner_merge`): author equals `merged_by`, author not a bot (`bot_logins`, `kh-hub-bot` always), and for every path the merge touched (tree comparison against the first parent, renames count both paths) the first matching rule of `owners.yml` at the first parent names a team in `self_merge_teams` that the author belongs to; missing or invalid `owners.yml` means no self-merge; the PR cannot change the rules that judge it. Add a section "Provenance stamp" under it: `stamp` is `reviewed`, `owner_merge` or empty; `owner_merge` is an accountable decision by an owner and not a second-person review, the tier is still T0 and `approved_by` is the merger; trust-anchor documents have an empty stamp and `approved_by` `['trust-anchor']`; `search` and `get_doc` return it next to `approved_by`. Add a bullet that verdicts are stored in `kh.merge_verdicts` at first judgement (readable by the indexer role only), reused by every sync and rebuild, and replaced only by `kh rebuild --rejudge`.
      2. `docs/configuration.md`: add rows `self_merge_teams` (default `[]`, names `^[a-z0-9][a-z0-9_-]*$`, teams whose members may merge their own PRs on paths the team owns; needs `read:organization` on the indexer token; governance paths need `kh-admins`) and `bot_logins` (default `[kh-hub-bot]`, `kh-hub-bot` is always included, bots never self-merge and their approvals never count); change the `kh rebuild` row to `kh rebuild [--repo R] [--rejudge]` with `--rejudge` described as a fresh judgement of every merge (see the runbook); in the `KH_FORGEJO_INDEXER_TOKEN_FILE` row add that it needs `read:organization` when `self_merge_teams` is not empty.
      3. `docs/runbook.md`: add a section "Merge verdicts and `--rejudge`" with the five consequences listed in plan section 3.5 of `docs/plans/12/plan.md` in your own words, the commands (`kh rebuild --repo R --rejudge`) and these use cases: after an account compromise, after changing `bot_logins` or `self_merge_teams`, after a team was cleaned up and an operator wants history judged by today's membership, and after upgrading to 0.2.0 run `kh rebuild` once right after `kh migrate`. Add to the alert table nothing.
      4. `docs/governance.md`: update the `kh domain-owners` row and the paragraph on counted approvals: the pass message (`approved by …`, `self-merge by owner …`), that self-merge replaces only the owner-approval requirement and not the widen rules, and the known gap (another merger gives a disputed merge after green CI).
      5. `deploy/kh.example.yaml`: add the lines `self_merge_teams: []` and `bot_logins: [kh-hub-bot]` after `restricted_audiences_imply_all`.
      6. `CHANGELOG.md` under `## [Unreleased]`: `### Added` bullets for `self_merge_teams` and the `owner_merge` stamp, `stamp` in `search` and `get_doc`, `kh rebuild --rejudge` and stored merge verdicts, `bot_logins`, and the self-merge pass of `kh domain-owners`; one `### Changed` bullet that `kh domain-owners` prints the approval or self-merge that applied when it passes. No version heading.
      Stop with `status=blocked` if a statement contradicts the merged code, and name the file.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_docs_links.py tests/unit/test_deploy_files.py -q passes"
      - "grep -n \"Unreleased\" -A16 CHANGELOG.md shows the new Added entries and no heading with a version number between Unreleased and 0.1.1"
      - "grep -n \"rejudge\" docs/runbook.md docs/configuration.md matches in both files"
      - ".venv/bin/python -m pytest -q passes"
```
