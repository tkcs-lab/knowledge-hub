# Plan: Archived state for groups and labels

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** In `kh.yaml` an operator sets `archived: true` on a group, or lists a label under the new top-level key `archived_labels`, then runs `kh setup apply` and restarts `kh serve`. An archived group is held by nobody: its documents and records vanish from `search`, `get_doc` and `list_stale`, `ingest_record` and `propose_doc` refuse it, and nothing is indexed for it. An archived label vanishes from `list_labels`, from the `labels` of every result, and from search filters, and new writes using it fail with `unknown label`. Nothing is deleted: removing the flag restores everything, and `kh setup apply --prune` never deletes an archived item.

**Scope:** Changes `src/knowledge_hub/config.py`, `auth/claims.py`, the tools `ingest_record`, `propose_doc`, `list_labels`, `search`, `get_doc`, `list_stale`, the indexer (webhook, worker, `sync_repo`, rebuild) and the setup labels and forgejo steps, plus docs and the changelog. Does not change the database schema (no migration), the Postgres setup step, PrivacyFence, or what `--prune` removes for groups and labels that are simply absent from `kh.yaml`.

**Review focus:** Archive state lives only in `kh.yaml`, not in the database (section 3.1). The hiding of an archived group rests on the existing row-level security, because nobody holds the group (3.2). Archived labels stay in `kh.labels` so indexing a document that names one is accepted (3.4). A real Forgejo may refuse the repo edit that unarchives (section 4, manual step `ma1-forgejo-archive-roundtrip`).

## 1. Goal

Issue [#39](https://github.com/tkcs-lab/knowledge-hub/issues/39): groups and labels get an archived state that acts like removed without pruning data. Today a group or label can only disappear through `kh setup apply --prune`, which deletes rows (labels) or refuses while records exist (groups), and a mere absence from `kh.yaml` only reports `? unmanaged`.

## 2. Current state

- `src/knowledge_hub/config.py:48-58` `GroupConfig` (`repo`, `members`, `approvers`); `:61-69` `KhConfig` (`groups`, `labels: dict[str, str]`); `:174-182` `audiences_for_email`; `:164-169` `group_for_repo`.
- `src/knowledge_hub/auth/claims.py:107-114` `resolve_audiences`: a person gets `config.audiences_for_email(email)`, an n8n workflow gets the claim audiences that are `in config.groups`.
- `src/knowledge_hub/tools/records.py:52`, `tools/propose.py:45`: `audience`/`group` must be held and `in deps.config.groups`; both check labels against `kh.labels` (`records.py:71-76`, `propose.py:65-70`).
- `src/knowledge_hub/tools/labels.py:12-15`: `list_labels` returns every `kh.labels` row.
- `src/knowledge_hub/tools/search.py:30-48` (`_hit`) and `tools/docs.py:93,107,209`: results copy `labels` verbatim.
- `src/knowledge_hub/indexer/webhook.py:81`, `indexer/worker.py:109-122` (`_git_push`), `indexer/sync.py:98-101`, `indexer/rebuild.py:14-16,30-45`: a repo in any configured group is indexed, rebuilt and checked.
- `src/knowledge_hub/setup/labels.py:64-71`: after a new label, a `git_push` job is queued for every group's repo.
- `src/knowledge_hub/setup/forgejo.py:117-150`: every configured group gets teams, repo, collaborators and protection; `REPO_SETTINGS` (`:21-26`).
- Row-level security: `kh.can_read(audience)` compares a row's audience with the caller's audiences (`0001_baseline.sql`); a group nobody holds is unreadable.
- The hub reads `kh.yaml` once at `kh serve` start (`docs/design.md`, "Resolution").

## 3. Design

### 3.1 Where the state lives

In `kh.yaml` only. No migration, no column on `kh.groups` or `kh.labels`; `kh.groups` and `kh.labels` keep every configured row, archived or not, so foreign keys, the in-use label trigger and rows stay valid. The hub applies the state when it reads the config, so archiving or unarchiving needs `kh serve` to restart, exactly as a change to a group already does. Rejected: a database column (needs a migration, setup writes, RLS grants and a join on every search to strip labels), and a second config file.

### 3.2 Config keys (p1-config)

```yaml
groups:
  hr:
    archived: true          # default false
labels:
  topic:old: Old topic
archived_labels: [topic:old]  # default []; every entry must be a key of labels
```

- `GroupConfig.archived: bool = False`. An archived group keeps every other validation (members in `people`, non-empty approvers, unique repo), so unarchiving never meets a new error.
- `KhConfig.archived_labels: list[str]`, default empty. Errors, both starting `config:` through the existing wrapper: `archived_labels entry '<x>' is not in labels` and `archived_labels lists '<x>' twice`.
- `KhConfig` gains: property `active_groups` (list of group names with `archived` false, in config order), method `is_active(name: str) -> bool` (the group exists and is not archived), property `archived_label_set` (frozenset of `archived_labels`), method `visible_labels(labels: Iterable[str]) -> list[str]` (the input without archived labels, order kept).
- `audiences_for_email` returns only active groups.
- `resolve_audiences` for an `n8n_workflow` keeps the claim audiences for which `config.is_active(a)` is true.

### 3.3 Hub behaviour for an archived group (p2-tools, p3-indexer)

Nobody holds an archived group, so row-level security already hides its documents, records and chunks from every tool; no SQL changes. Added on top, as defence in depth and for clear errors:

- `ingest_record` and `propose_doc` use `deps.config.is_active(audience)` in place of `audience in deps.config.groups`; the failure text stays `audience is not held by this caller or not configured`.
- The webhook answers 202 `ignored` for a push to an archived group's repo and queues nothing.
- The worker finishes a queued `git_push` job of an archived group's repo as `done` without syncing, so jobs queued before archiving do not retry five times and fail.
- `sync_repo` raises `ValueError("repo <repo> is archived")` for an archived group's repo.
- `kh rebuild` and `kh check` cover active groups' repos only; `kh rebuild --repo <archived repo>` raises `ValueError("repo <repo> is archived")`.
- Rows, `kh.repo_state` and the Forgejo repo stay. Unarchiving needs the operator to run `kh rebuild --repo <repo>` once, because pushes while archived were ignored; the runbook says so.

### 3.4 Hub behaviour for an archived label (p2-tools)

The label stays in `kh.labels` with its description, so the database triggers keep accepting it. Owner answer: documents whose frontmatter names an archived label are indexed normally (no change to `sync_plan.py`).

- `list_labels` omits archived labels.
- `ingest_record` and `propose_doc` refuse an archived label with the existing `unknown label` text (`BAD_LABEL`).
- `search` returns `{"hits": [], "next_cursor": ""}` at once when any requested label filter is archived. Every hit's `labels` goes through `config.visible_labels`.
- `get_doc` (both `doc:` and `rec:` results) and `list_stale` documents pass `labels` through `config.visible_labels`.
- Rows keep the label in the database; unarchiving brings it back with no reindex.
- `kh setup apply --prune` never removes an archived label, because it stays in `labels` in `kh.yaml`.

### 3.5 Setup for archived items (p4-setup)

Owner answer for the Forgejo side: mark the repo archived, keep teams and members.

- Labels step: the `git_push` jobs queued after a new label go to `config.active_groups` only.
- Postgres step: unchanged; archived groups are still configured, so they are neither pruned nor reported `? unmanaged`.
- Forgejo step, for a group with `archived: true`: skip teams, team repos, collaborators and protection entirely (they stay as they are). If the repo does not exist, report `i group <name>: archived, repo <repo> not created`. If it exists and is not archived, report `~ repo <repo>: archived` and on apply `edit_repo(repo, archived=True)`.
- Forgejo step, for an active group whose repo is archived in Forgejo (an unarchive): `_repo` reports `~ repo <repo>: archived` and its one `edit_repo` call sends `archived=False` together with any other setting drift, before the protection step runs.
- `_accounts` checks logins of active groups only. The managed-team and unmanaged-repo sets keep counting archived groups, so their teams and repo are never reported or pruned.
- `REPO_SETTINGS` does not gain an `archived` key; the archived flag is handled in `_repo` and `_archive` so repos from the fake that lack the key still read as not archived (`bool(have.get("archived"))`).

### 3.6 Rejected alternatives

- Treat an archived group as removed from `kh.yaml`: `kh.groups` has foreign keys from documents and records, and `--prune` refuses or deletes.
- Archive label state in `kh.labels`: see 3.1.
- Reject documents that name an archived label: owner chose to accept them.
- Empty the Forgejo teams of an archived group: owner chose to keep them.

## 4. Manual steps

### Before implementation

None.

### After implementation

1. `ma1-forgejo-archive-roundtrip` (about 15 minutes). A real Forgejo may refuse the repository edit that turns `archived` off, which the fake in the tests cannot show.
   1. On a test deployment copy of `kh.yaml`, add a group `archtest` with one member and one approver from `people`, as in the [runbook](https://github.com/tkcs-lab/knowledge-hub/blob/feat/archive-groups-labels/docs/runbook.md) section "Adding a group". Run `kh setup apply --config kh.yaml`. You see `+ group archtest`, `+ repo kb-archtest` and the team lines.
   2. Set `archived: true` on `archtest`. Run `kh setup plan --config kh.yaml`. You see `~ repo kb-archtest: archived`. Run `kh setup apply --config kh.yaml`; open the repo page in Forgejo and see the archived banner.
   3. Run `kh setup plan` again: no `~` or `+` lines.
   4. Set `archived: false`. Run `kh setup apply --config kh.yaml`. You see `~ repo kb-archtest: archived` and the banner is gone.
   5. Report back one line for the pull request: `ma1: pass` or `ma1: fail - <the Forgejo status and message from step 4>`.

## 5. Risks and open questions

- Forgejo may answer 403 or 422 to `PATCH` of an archived repo, including the one that sets `archived` to false. Detection: step 4 of `ma1`. Workers cannot see this; no phase depends on it. If `ma1` fails, a follow-up plan unarchives through the dedicated route Forgejo offers.
- `kh serve` must restart after the flags change. A person who forgets still sees the old state; the runbook and configuration docs say so.
- p4's fake edit adds an `archived` key to every fake repo dict; an exact-equality test on a repo dict would fail. A worker that sees this updates the expectation and does not weaken any other assertion; if more than the repo-dict tests break, it stops with `status=blocked`.
- p3: a unit test that builds a `SimpleNamespace` config would break on `active_groups`. A worker switches that test to a real `KhConfig`; if more than one test needs this, it stops with `status=blocked`.

## Implementation manifest

```yaml
plan_slug: archive-groups-labels
feature_branch: feat/archive-groups-labels
tracking_issue: 39
max_parallel: 3
manual_before: []
manual_after:
  - id: ma1-forgejo-archive-roundtrip
    title: Archive and unarchive a test group against the real Forgejo
    why: The fake Forgejo accepts any repo edit; a real Forgejo may refuse the edit that sets archived to false.
verify_after_merge:
  - .venv/bin/python -m pytest -q -k archive
final_checks:
  - "`tld plan deviations check --file docs/plans/39/deviations.md --plan docs/plans/39/plan.md --all` exits 0"
  - "the changelog has entries under `## [Unreleased]` and no version heading"
phases:
  - id: p1-config
    title: Archived flags in the config model and claim resolution
    depends_on: []
    complexity: S
    touches:
      - src/knowledge_hub/config.py
      - src/knowledge_hub/auth/claims.py
      - tests/unit/test_config.py
      - tests/unit/test_claims.py
    brief: |
      Implement plan section 3.2. Read `src/knowledge_hub/config.py` and `src/knowledge_hub/auth/claims.py:100-145` first.
      1. In `config.py` add `archived: bool = False` to `GroupConfig` and `archived_labels: list[str] = Field(default_factory=list)` to `KhConfig`.
      2. In `KhConfig._validate`, after the labels loop, refuse an `archived_labels` entry that is not a key of `labels` with `ValueError(f"archived_labels entry {name!r} is not in labels")` and a repeated entry with `ValueError(f"archived_labels lists {name!r} twice")`.
      3. Add to `KhConfig`: property `active_groups -> list[str]` (names whose `archived` is false, in `groups` order); `is_active(self, name: str) -> bool` (`name in self.groups and not self.groups[name].archived`); property `archived_label_set -> frozenset[str]`; `visible_labels(self, labels: Iterable[str]) -> list[str]` (drop members of `archived_label_set`, keep order). Import `Iterable` from `collections.abc`.
      4. In `audiences_for_email` keep only groups for which `not group.archived`.
      5. In `auth/claims.py` `resolve_audiences`, replace `a in config.groups` with `config.is_active(a)`.
      6. Tests, copying the style of the neighbouring tests: in `tests/unit/test_config.py` add `test_archived_group_flag_defaults_false_and_is_read`, `test_archived_labels_must_be_defined_and_unique`, `test_active_groups_and_visible_labels`, `test_audiences_for_email_skips_archived_groups`; in `tests/unit/test_claims.py` add `test_archived_group_is_not_held_by_person_or_workflow` using `resolve_audiences` for `claude_client` and `n8n_workflow`.
      Do not edit `tests/support/principals.py`. Stop with `status=blocked` if `resolve_audiences` has a caller outside `auth/claims.py` that relies on archived groups being returned.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_config.py tests/unit/test_claims.py -q passes"
      - "grep -n 'config.is_active' src/knowledge_hub/auth/claims.py matches"
      - ".venv/bin/python -m ruff check src tests passes"
  - id: p2-tools
    title: Archived groups and labels in the six tools
    depends_on: [p1-config]
    complexity: M
    touches:
      - src/knowledge_hub/tools/records.py
      - src/knowledge_hub/tools/propose.py
      - src/knowledge_hub/tools/labels.py
      - src/knowledge_hub/tools/search.py
      - src/knowledge_hub/tools/docs.py
      - tests/integration/test_archive_tools.py
    brief: |
      Implement plan sections 3.3 (first bullet) and 3.4. Read the five tool files and `tests/integration/test_list_labels.py`, `tests/support/seed.py`, `tests/support/principals.py`.
      1. `records.py:52` and `propose.py:45`: replace `audience not in deps.config.groups` and `group not in deps.config.groups` by `not deps.config.is_active(...)`; keep the error text.
      2. `records.py` label check (the `known != set(wanted)` branch) and `propose.py` equivalent: also raise `bad_label` when `set(wanted) & deps.config.archived_label_set` is not empty.
      3. `labels.py`: `list_labels` filters out rows whose `name` is in `call.deps.config.archived_label_set`.
      4. `search.py`: at the start of `search` after `limit` is clamped, `if set(labels) & deps.config.archived_label_set: return {"hits": [], "next_cursor": ""}`. Change `_hit(row)` to `_hit(row, config)` and wrap the labels value in `config.visible_labels(...)`; update the one call site.
      5. `docs.py`: wrap the three `list(row["labels"] or [])` expressions (get_doc doc result, get_doc record result, list_stale documents) in `call.deps.config.visible_labels(...)`; keep the result a list.
      6. New `tests/integration/test_archive_tools.py`: override the `kh_config` fixture with `KH_CONFIG.model_copy(update={"labels": {"ember": "Ember product", "other": "Another label"}, "archived_labels": ["other"], "groups": {**KH_CONFIG.groups, "hr": GroupConfig(repo="kb-hr", members=["carol"], approvers=["carol"], archived=True)}})`. Tests (all use `mcp_client` and `seeded_db`): `test_archived_label_is_not_listed`; `test_ingest_with_archived_label_is_unknown_label` (alice_all, audience `all`, labels `["other"]`, expects error text `unknown label`); `test_ingest_into_archived_group_is_refused` (carol_hr, audience `hr`, expects `audience is not held by this caller or not configured`); `test_search_filtered_by_archived_label_finds_nothing` (insert a document with labels `["ember", "other"]` in group `all` through `add_doc` and an `embed`-free full-text hit on its body, expect `hits == []` for `labels=["other"]` and one hit whose `labels == ["ember"]` for `labels=["ember"]`); `test_get_doc_hides_archived_label` (same document, `labels == ["ember"]`); `test_propose_with_archived_label_is_unknown_label` is skipped unless `deps.forgejo_bot` can be faked in an existing test: copy the setup of `tests/integration/test_propose_doc.py` and add it only if that file builds the bot fake in a reusable fixture, otherwise cover the propose change with the `grep` acceptance below.
      Stop with `status=blocked` if a result path other than the three in step 5 returns `labels` to the caller (grep `"labels"` in `src/knowledge_hub/tools`).
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_archive_tools.py -q passes"
      - ".venv/bin/python -m pytest tests/integration/test_list_labels.py tests/integration/test_ingest_record.py tests/integration/test_propose_doc.py tests/integration/test_search_leaks.py tests/integration/test_tool_get_doc.py tests/integration/test_list_stale.py -q passes"
      - "grep -n 'archived_label_set' src/knowledge_hub/tools/records.py src/knowledge_hub/tools/propose.py src/knowledge_hub/tools/labels.py src/knowledge_hub/tools/search.py matches in all four files"
      - ".venv/bin/python -m ruff check src tests passes"
  - id: p3-indexer
    title: Skip archived groups in webhook, worker, sync and rebuild
    depends_on: [p1-config]
    complexity: S
    touches:
      - src/knowledge_hub/indexer/webhook.py
      - src/knowledge_hub/indexer/worker.py
      - src/knowledge_hub/indexer/sync.py
      - src/knowledge_hub/indexer/rebuild.py
      - tests/integration/test_archive_indexing.py
    brief: |
      Implement plan section 3.3 (bullets 2 to 5). Read the four indexer files and `tests/integration/test_webhook_worker.py:1-100` and `tests/integration/test_rebuild.py`.
      1. `webhook.py` (`forgejo_webhook`): replace the `group_for_repo(repo) is None` test by `group = deps.config.group_for_repo(repo)` then `if group is None or not deps.config.is_active(group): return _empty(202, "ignored")`; keep `repo is None` first.
      2. `worker.py` `_git_push`: after the `repo is None` check, `group = self.config.group_for_repo(repo)`; `if group is not None and not self.config.is_active(group): log.info("git_push skipped, group %s is archived", group); return`.
      3. `sync.py` `sync_repo`: after the `audience is None` check add `if not config.is_active(audience): raise ValueError(f"repo {repo} is archived")`.
      4. `rebuild.py`: `configured_repos` returns `sorted(config.repo_for_group(name) for name in config.active_groups)`. In `rebuild`, when `repo` is not in `repos` and `deps.config.group_for_repo(repo)` is not `None`, raise `ValueError(f"repo {repo} is archived")`; otherwise keep the existing `repo {repo} is in no group` error.
      5. New `tests/integration/test_archive_indexing.py`, reusing the `world`, `http`, `post`, `push`, `jobs` helpers by copying them (do not import from another test module) and overriding `kh_config` with `KH_CONFIG.model_copy(update={"groups": {**KH_CONFIG.groups, "hr": GroupConfig(repo="kb-hr", members=["carol"], approvers=["carol"], archived=True)}})`. Tests: `test_push_to_archived_repo_is_ignored` (202, `jobs(world) == []`); `test_worker_finishes_archived_git_push_job_without_syncing` (insert a `git_push` job for `kb-hr` with `INSERT INTO kh.index_jobs`, run `Worker(...).run_once()` with a reader that raises on any call, expect state `done`); `test_sync_repo_refuses_archived_repo`; `test_rebuild_skips_archived_and_refuses_it_by_name` (`configured_repos` has no `kb-hr`, `rebuild(deps, "kb-hr")` raises `ValueError` with `archived`).
      If `tests/unit/test_cli_rebuild.py` or an existing test fails only because a `SimpleNamespace` config lacks `active_groups`, replace that fake with `KH_CONFIG` in that test (add the test file to your changes); stop with `status=blocked` if more than one existing test needs this.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_archive_indexing.py -q passes"
      - ".venv/bin/python -m pytest tests/integration/test_webhook_worker.py tests/integration/test_rebuild.py tests/integration/test_sync.py tests/unit/test_cli_rebuild.py -q passes"
      - "grep -n 'is_active' src/knowledge_hub/indexer/webhook.py src/knowledge_hub/indexer/worker.py src/knowledge_hub/indexer/sync.py matches in all three files"
      - ".venv/bin/python -m ruff check src tests passes"
  - id: p4-setup
    title: Setup steps skip and archive archived groups
    depends_on: [p1-config]
    complexity: M
    touches:
      - src/knowledge_hub/setup/labels.py
      - src/knowledge_hub/setup/forgejo.py
      - tests/support/forgejo_admin_fake.py
      - tests/unit/test_setup_forgejo.py
      - tests/integration/test_setup_postgres.py
    brief: |
      Implement plan section 3.5. Read `src/knowledge_hub/setup/forgejo.py:117-215`, `src/knowledge_hub/setup/labels.py`, `tests/support/forgejo_admin_fake.py:86-100,235-260`, `tests/unit/test_setup_forgejo.py:1-130,339-end` and `tests/integration/test_setup_postgres.py:100-135,167-end`.
      1. `labels.py` `apply`: iterate `self._config.active_groups` instead of `self._config.groups` when queueing the `git_push` jobs.
      2. `forgejo.py` `_sync` loop over `cfg.groups.items()`: after `repo = cfg.repo_for_group(group)` add `if gc.archived: await self._archive(out, repos, group, repo, write); continue`. Keep the rest of the loop body as it is.
      3. Add `async def _archive(self, out, repos, group, repo, write)`: `have = repos.get(repo)`; if `have is None` append `Change("i", f"group {group}: archived, repo {repo} not created")`; elif `not have.get("archived")`: append `Change("~", f"repo {repo}: archived")` and, when `write`, `repos[repo] = await self._admin.edit_repo(repo, archived=True)`.
      4. In `_repo`, in the branch where the repo exists, after the `drift` dict is built from `REPO_SETTINGS`, add `if have.get("archived"): drift["archived"] = False`, so the one `edit_repo(repo, **drift)` call unarchives and the printed line is `~ repo <repo>: archived`.
      5. In `_accounts`, iterate only groups with `not gc.archived` when collecting member and approver logins. Do not change the `managed` and `wanted` sets in `_sync`.
      6. Fake: in `tests/support/forgejo_admin_fake.py` `_new_repo` add `"archived": False` to the repo dict so `_repo_edit` (which updates only existing keys) stores it.
      7. Tests in `tests/unit/test_setup_forgejo.py`: `test_archived_group_repo_is_archived_and_teams_untouched`; `test_archived_group_without_repo_creates_nothing` (expects the `i` line, `fake.repos` has no `kb-<group>`); `test_archived_group_is_converged_on_second_plan` (second plan has no `~` or `+` lines); `test_unarchived_group_unarchives_repo_before_protection` (apply with the group archived, then apply with it active, expect `~ repo kb-...: archived` and `fake.repos[...]["archived"] is False`); `test_archived_group_members_are_not_account_checked` (a member missing from `fake.users` raises no `!` line). In `tests/integration/test_setup_postgres.py` add `test_archived_group_is_kept_by_prune` (config with `hr` archived, `apply(True)` leaves the `kh.groups` row) and `test_labels_jobs_skip_archived_groups` (new label queues only the active group's `git_push` job).
      Stop with `status=blocked` if an existing test in `tests/unit/test_forgejo_admin.py` or `tests/unit/test_setup_forgejo.py` fails because of the new `archived` key in more than the repo-dict equality expectations.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_setup_forgejo.py tests/unit/test_forgejo_admin.py -q passes"
      - ".venv/bin/python -m pytest tests/integration/test_setup_postgres.py -q passes"
      - "grep -n 'def _archive' src/knowledge_hub/setup/forgejo.py matches"
      - ".venv/bin/python -m ruff check src tests passes"
  - id: p5-retire
    title: Docs, example config and changelog
    depends_on: [p2-tools, p3-indexer, p4-setup]
    complexity: S
    touches:
      - docs/configuration.md
      - docs/design.md
      - docs/runbook.md
      - deploy/kh.example.yaml
      - CHANGELOG.md
    brief: |
      Bring the docs in line with plan section 3. Read `docs/configuration.md:115-160`, `docs/design.md:1-60,85-110`, `docs/runbook.md:88-110`, `deploy/kh.example.yaml` and the top of `CHANGELOG.md`.
      1. `docs/configuration.md`: in the `kh.yaml` key table add a row `groups.<g>.archived` (default `false`; an archived group is held by nobody, its documents and records are hidden, writes are refused, nothing is indexed; nothing is deleted) and a row `archived_labels` (default `[]`; label names, each a key of `labels`; hidden from `list_labels`, from result labels and from search filters; writes using them fail with `unknown label`). Add the two error texts from plan 3.2. State that the hub reads both when `kh serve` starts.
      2. `docs/design.md`: in "Tiers and groups" add one bullet each for an archived group and an archived label, as in plan 3.3 and 3.4, naming that the database is unchanged and the state lives in `kh.yaml`.
      3. `docs/runbook.md`: add a section `### Archiving and unarchiving` after "Adding a group": set the flag or add the label to `archived_labels`, run `kh setup plan` then `kh setup apply`, restart `kh serve`; for a group `kh setup` also marks the Forgejo repo archived and leaves teams and members; to unarchive remove the flag, run `kh setup apply`, restart `kh serve` and run `kh rebuild --repo <repo>` once; `--prune` never deletes an archived group or label; to delete for good remove it from `kh.yaml` and use `--prune` as before.
      4. `deploy/kh.example.yaml`: add commented lines only (`#     archived: true` under the `mgmt` group and `# archived_labels: [topic:onboarding]` at the end), because `tests/unit/test_deploy_files.py::test_example_config_loads` asserts the loaded groups and labels.
      5. `CHANGELOG.md`: under `## [Unreleased]` add `### Added` with one bullet: `` `archived: true` on a group and `archived_labels` in `kh.yaml` hide a group or label as if removed, without deleting any data; `kh setup` archives the group's Forgejo repo. Unarchiving needs a `kh serve` restart and `kh rebuild --repo <repo>`. ``
      Do not edit any file under `src` or `tests`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_deploy_files.py tests/unit/test_docs_links.py -q passes"
      - "grep -n 'archived_labels' docs/configuration.md docs/design.md docs/runbook.md CHANGELOG.md matches in all four files"
      - "grep -c '^## \\[' CHANGELOG.md is unchanged from before the phase (no new version heading)"
```
