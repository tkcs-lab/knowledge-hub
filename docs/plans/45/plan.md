# Plan: Admin commands for labels, groups and people

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** `kh label add|remove|archive|unarchive|rename|list`, `kh group add|remove|archive|unarchive|list`, `kh group member|approver add|remove` and `kh person add|remove|list` change `kh.yaml` and, at once, the database and Forgejo. A command edits the file (comments kept), runs the matching `kh setup` step and prints the usual `+ ~ - ! ? i` lines. `--dry-run` prints the file diff and the plan and writes nothing; `--no-apply` only edits the file. `kh label rename NAME TEXT` changes the description only; the key never changes. There is no group rename. The hub still reads `kh.yaml` at start, so each change ends with `restart kh serve`.

**Scope:** a new package `src/knowledge_hub/admin/`, public single-object methods on the labels, postgres and forgejo setup steps, the dependency `ruamel.yaml`, docs, the changelog and the compose example (the config file is mounted as a directory). `kh setup` is unchanged except that an archived group's existing Forgejo teams lose members the config no longer lists. No schema change, no migration, and the serving app never imports the new code.

**Security:** the commands decide who can read what (people, groups, approvers), so they need the same admin credentials as `kh setup` and print no secret. The file is validated before it is written and replaced atomically under a lock. A removal that the database refuses (a label in use, a group with records) leaves `kh.yaml` as it was; any other failure after the edit keeps the new file and says so.

**Review focus:** the stage, apply, commit order and when the file is committed after a failure (3.3); the ruamel round trip and its comment loss (3.4); trimming members from an archived group's teams (3.6); the directory mount of `kh.yaml` (3.9).

## 1. Goal

Today `kh setup plan|apply --config kh.yaml` only makes the environment follow a hand-edited file. After this plan an operator manages labels, groups, members, approvers and people with commands instead of editing YAML, and each command also performs its effect on Postgres and Forgejo. kh.yaml stays the single source of truth. Tracking issue: [#45](https://github.com/tkcs-lab/knowledge-hub/issues/45).

Owner decisions (issue #45): edit `kh.yaml` then apply at once; keep YAML comments with `ruamel.yaml`; `label rename` edits the description only; no `group rename`; membership commands (`group member`, `group approver`, `person`) are included.

## 2. Current state

- `src/knowledge_hub/cli.py:114-117` sends `kh setup ...` to `setup/cli.py` before argparse; every other command goes through one argparse tree (`:118-130`).
- `src/knowledge_hub/setup/cli.py:23-98` builds one step per name in `_builders(settings, config, create_bucket)` and runs them with `setup/steps.py:run`. The helper `_NotSet` (`:17-21`) and `_read` read credential files named by `Settings`.
- `src/knowledge_hub/setup/labels.py`: `LabelsStep.apply(prune)` inserts and updates labels from the config; with `prune` it deletes every label the config no longer lists (`_remove`, `:76-90`, refuses a label in use with `! label <x>: in use`).
- `src/knowledge_hub/setup/postgres.py:121-168`: `_groups(dsn, prune)` inserts missing groups; with `prune` it removes every group the config no longer lists (`_remove_group`, refused with `! group <x>: still has <n> records`, deletes the group's documents and `repo_state`).
- `src/knowledge_hub/setup/forgejo.py:117-157`: `_sync` walks every group, then the webhook, unmanaged teams and repos. An archived group only has its repo archived (`_archive`, `:262-270`); its teams are not touched. `--prune` deletes every unmanaged team.
- `src/knowledge_hub/config.py:44-232`: `KhConfig` and `parse_config` validate the file; every error text starts with `config:`. `load_config` uses PyYAML, which drops comments on a rewrite.
- `deploy/compose.example.yaml:61` mounts the single file `./kh.yaml:/etc/kh/kh.yaml:ro`. A single-file bind mount does not follow a file that is replaced by a rename.
- `tests/unit/test_import_boundary.py` forbids `knowledge_hub.forgejo.admin` and `knowledge_hub.setup` in the serving app.
- Tests to extend: `tests/unit/test_setup_cli.py`, `tests/unit/test_setup_forgejo.py` (fixtures `fake`, `admin`, `applied`; the helper function `archived_config`), `tests/integration/test_setup_postgres.py` (fixtures `empty`, `settings`, helpers `cfg`, `step`, `lines`).

## 3. Design

### 3.1 Commands

All commands are dispatched in `knowledge_hub/cli.py` `main` like `setup`: `if argv and argv[0] in ("label", "group", "person"): from knowledge_hub.admin.cli import main as admin_main; return admin_main(argv)`. They need no entry in `COMMANDS` and the serving app never imports `knowledge_hub.admin`.

| Command | Edit of `kh.yaml` | Effect applied |
|---|---|---|
| `kh label add NAME [-d TEXT]` | `labels[NAME] = TEXT` (default `""`); error if it exists | labels step |
| `kh label rename NAME TEXT` | `labels[NAME] = TEXT` (the key never changes) | labels step |
| `kh label archive NAME` / `unarchive NAME` | add to / remove from `archived_labels` | none (restart `kh serve`) |
| `kh label remove NAME` | delete from `labels` and from `archived_labels` | labels step, then `remove_label(NAME)` |
| `kh label list [--json]` | none | none |
| `kh group add NAME --members A,B --approvers A [--repo R]` | new group | postgres `sync_groups`, forgejo `sync_group(NAME)` |
| `kh group remove NAME --yes` | delete the group | postgres `remove_group(NAME)`, forgejo `remove_group(NAME)` |
| `kh group archive NAME` / `unarchive NAME` | set / delete `archived` | forgejo `sync_group(NAME)` |
| `kh group member add\|remove GROUP LOGIN` | edit `members` | forgejo `sync_group(GROUP)` |
| `kh group approver add\|remove GROUP LOGIN` | edit `approvers` | forgejo `sync_group(GROUP)` |
| `kh group list [--json]` | none | none |
| `kh person add LOGIN EMAIL` | `people[LOGIN] = EMAIL.lower()` | none |
| `kh person remove LOGIN [--from-groups]` | delete from `people`; with the flag also from every group's `members` and `approvers` | forgejo `sync_group(G)` for each group the login was in |
| `kh person list [--json]` | none | none |

There is no `group rename` and no `person` email change; `--help` of `kh group` says `a group cannot be renamed: its name is the audience of every document and record`.

Common flags on every mutating command: `--config FILE` (default: the environment variable `KH_CONFIG_FILE`; neither set gives `admin: no config file: pass --config or set KH_CONFIG_FILE`, exit 2), `--dry-run`, `--no-apply`. `list` commands take `--config` and `--json` and read only the file (no settings, no credentials). A command whose edit changes nothing (archive of an archived label, member add of an existing member, and so on) prints `nothing to change` and exits 0 without writing or applying.

### 3.2 Modules

- `admin/yamledit.py`: `load`, `render`, `validate`, `locked`, `commit`, `unified_diff` (3.4).
- `admin/ops.py`: pure edits of the loaded document, one function per row of 3.1 (3.5).
- `admin/apply.py`: `Steps` and `execute` (3.3).
- `admin/cli.py`: argument parsing, common flags, dispatch to `labels_cmd.py`, `persons_cmd.py`, `groups_cmd.py`; each of those exposes `register(sub)` and uses `apply.execute`.
- Setup steps gain public methods (3.6); `setup/cli.py` renames `_builders` to `build_steps` and `_NotSet` to `NotSet` so `admin/apply.py` can use them.

### 3.3 `apply.execute`: stage, apply, commit

```python
Edit = Callable[[Doc], bool]                       # mutates the doc, True if anything changed
Check = Callable[[Steps, KhConfig], Awaitable[str | None]]   # read only; a refusal text or None
After = Callable[[Steps, bool], Awaitable[list[Change]]]     # bool = write; False in --dry-run
async def execute(path: Path | None, edit: Edit, *, check: Check | None = None, after: After | None = None,
                  target: str | None = None, notes: Sequence[str] = (), dry_run: bool, no_apply: bool,
                  steps_factory: StepsFactory | None = None) -> int
```

`Steps(settings, config)` builds the real steps lazily through `setup.cli.build_steps` (`labels()`, `postgres()`, `forgejo()`, each returning the concrete class `LabelsStep`, `PostgresStep` or `ForgejoStep` via `typing.cast`); a `NotSet` or `ValueError` becomes `admin: <name> is not set`, exit 2, and nothing is written. `steps_factory` lets tests inject fakes; when it is `None`, `execute` uses the module function `apply.default_steps_factory(config)` (loads `load_settings()` and returns `Steps(settings, config)`), which CLI tests monkeypatch.

Order, all under `yamledit.locked(path)`:

1. `load` the file, `edit` the document; `False` prints `nothing to change`, exit 0.
2. `render` the new text and `validate` it (`yaml.safe_load` then `parse_config`); a `ConfigError` prints its `config: ...` text, exit 2, nothing written.
3. Print the unified diff. With `--dry-run`: unless `--no-apply`, run `check` (a refusal prints `admin: <text>` and exits 2 as in step 4) and then `after(steps, False)` against the new `KhConfig` and print the lines, then exit 0 (a `NotSet` prints `plan skipped: <name> is not set` and still exits 0). Writes nothing.
4. Unless `--no-apply`: run `check`; a refusal text prints `admin: <text>`, exit 2, nothing written.
5. Unless `--no-apply`: run `after(steps, True)` with the new `KhConfig` while the old file is still in place. `target` is the line prefix of the object the command deletes or creates, for example `label x` or `group g`. A returned `!` change whose text starts with `target` means the command's own object was refused: print the lines and `kh.yaml unchanged`, exit 2, nothing written.
6. Otherwise `commit` the new text. If `after` raised, print `setup: <message>` (secrets scrubbed with `setup.steps._message`'s rule) and `kh.yaml changed; apply incomplete: run kh setup apply`, exit 2. If other `!` lines were returned, print `kh.yaml changed; <n> lines need your attention`, exit 2. With `--no-apply` print `kh.yaml changed; run kh setup apply`.
7. Print each of `notes`, then `restart kh serve: the hub reads kh.yaml at start`.

Why: a deletion refused at apply time (a record appeared after a pre-check) must not leave `kh.yaml` ahead of the database; setup is idempotent, so any other failure after the edit is repaired by the next `kh setup apply`. Rejected: edit first and apply after (leaves the file ahead on a refusal); a `prune_only` argument on the steps (changes what `prune=True` means; targeted public methods instead).

Exit codes follow `kh setup`: 0 done, 2 error, refusal or remaining `!` line.

### 3.4 `yamledit`

- Library `ruamel.yaml`, `pyproject.toml` dependency `ruamel.yaml>=0.18,<0.19`. Configure `YAML()` with `preserve_quotes = True`, `width = 4096`, `indent(mapping=2, sequence=4, offset=2)`.
- `load(path) -> Doc` (`Doc` has `.data` (the round-trip mapping), `.text` (the original text)); a read error raises `ConfigError("config: cannot read <path>: <reason>")`; an empty or comment-only file loads as an empty mapping.
- `render(doc) -> str`; `validate(text) -> KhConfig` is `parse_config(yaml.safe_load(text))`, so what is written is exactly what is validated.
- `locked(path)` is a context manager: an exclusive `fcntl.flock` on `<dir>/.<filename>.lock` (created if missing); failure raises `ConfigError("admin: cannot lock <path>: <reason>")`. It protects only against two admin commands at once.
- `commit(path, text)`: resolve symlinks, write a temp file in the same directory, `fsync`, copy mode and owner (`os.chown` ignored when not permitted), `os.replace`, `fsync` the directory. An unwritable directory raises `ConfigError("admin: cannot write <path>: <reason>")`.
- `unified_diff(old, new, name) -> str` with `difflib.unified_diff`, three lines of context, file labels `kh.yaml` and `kh.yaml (new)`.
- Comment loss: ruamel attaches a comment to the key before it. Removing the last entry of a block or a group drops comments attached to it (for example `# archived_labels: ...` after the last label); the diff of step 3 shows it before anything is written. Golden tests on `deploy/kh.example.yaml` pin which lines change for each operation.

### 3.5 `ops`

All functions take the `Doc`, return `bool` (changed) and raise `OpError`; every function that takes a group also raises `unknown group <g>`; each of them (a `ValueError`, text printed as `admin: <text>`, exit 2). Logins match case-insensitively; the stored spelling is the one in `people`; emails are written lower-case. New keys go at the end of their mapping; new lists are written in flow style (`[a, b]`).

| Function | Errors (exact text) |
|---|---|
| `label_add(doc, name, description)` | `label <n> already exists` |
| `label_describe(doc, name, description)` | `unknown label <n>`; same text: returns `False` |
| `label_remove(doc, name)` (also deletes it from `archived_labels`) | `unknown label <n>` |
| `label_archive` / `label_unarchive` | `unknown label <n>`; already in that state: `False` |
| `group_add(doc, name, members, approvers, repo)` | `group <n> already exists` |
| `group_remove(doc, name)` | `unknown group <n>` |
| `group_set_archived(doc, name, flag)` (`True` writes `archived: true`, `False` deletes the key) | `unknown group <n>`; same state: `False` |
| `member_add(doc, group, login)` | `unknown group <g>`; `login <l> is not in people; run kh person add first`; already a member: `False` |
| `member_remove(doc, group, login, with_approver)` | `<l> is not a member of <g>`; `<l> is an approver of <g>; remove the approver first or pass --with-approver` |
| `approver_add(doc, group, login)` | `<l> is not a member of <g>`; already: `False` |
| `approver_remove(doc, group, login)` | `<l> is not an approver of <g>`; `group <g> needs at least one approver` |
| `person_add(doc, login, email)` | `person <l> already exists` |
| `person_remove(doc, login, from_groups)` | `unknown person <l>`; `person <l> is a member of <a>, <b>; pass --from-groups to remove them from every group` |

`group_add` takes `members` and `approvers` as lists of logins; the config validation (`config: group <g> approver <l> is not a member`, and so on) reports the rest.

### 3.6 Setup step changes (public single-object methods)

- `LabelsStep.remove_label(name: str, *, write: bool) -> Change`: the body of `_remove` made public; with `write=False` it only reads and returns the `-` or `!` line.
- `PostgresStep.sync_groups(*, write: bool) -> list[Change]`: `_groups(dsn, None if not write else False)` without roles, database or migrations. `PostgresStep.remove_group(name, *, write: bool) -> Change` (it reads the group's repo from `kh.groups`) (the same refusals as `_remove_group`). `PostgresStep.group_usage(name) -> tuple[int, int]`: documents and records of the group, read only.
- `ForgejoStep.sync_group(name, *, write: bool) -> list[Change]`: for one group, the account check of its members, approvers and both bots, then teams, repo, team repos, collaborators, branch protection (for an archived group the `_archive` step and the trim below); no webhook, no unmanaged-object lines, no owners line. Internally a private `_group(out, name, teams, repos, missing, write)` holds the per-group calls; `_sync` keeps its single account check, identity check and shared `teams`/`repos` state and calls `_group` per group (so `kh setup` prints the same lines in the same order), while `sync_group` computes that state for one group and calls `_group`. `ForgejoStep.remove_group(name, *, repo: str, write: bool) -> list[Change]`: deletes the teams `<name>` and `<name>-approve` when present (`- team <t>`), never the repo (`i repo <r>: kept`). `ForgejoStep.missing_accounts(logins: list[str]) -> list[str]`: the logins that have no Forgejo account (read only).
- Archived groups: for an archived group whose teams already exist, `sync_group` removes team members that the config no longer lists (`~ team <t>: members -<login>`); it never adds members and never creates a team. Without this a leaver stays in an archived group's team with read access. The runbook sentence "leaves the teams and members as they are" changes accordingly.
- `setup/cli.py`: rename `_builders` to `build_steps` and `_NotSet` to `NotSet`; `tests/unit/test_setup_cli.py` follows the rename.

### 3.7 Checks before the edit (read only, skipped with `--no-apply`)

- `group add` and `group member add` and `group approver add`: `Steps.forgejo().missing_accounts(new logins)` must be empty, else `admin: account <l> does not exist in Forgejo; create it first`.
- `group remove` without `--yes`: the `Check` returns `group <g>: removing it deletes <n> documents from the index and keeps the Forgejo repo <r>; pass --yes to continue` (from `group_usage`; when records exist it returns `group <g> still has <n> records` instead), printed as `admin: <text>`, exit 2, also with `--dry-run`. The group's repo name is read from the old config inside the `edit` closure and kept for `after`, the same way `person remove` keeps the groups it left.
- `group remove` in `after`: run postgres `remove_group` first; when its change is `!`, return it and skip the forgejo call, so no team is deleted for a refused removal.

### 3.8 Output notes

`group add` ends with `PrivacyFence: map a Workspace group to <name> in admin_config.json if n8n workflows should hold it (docs/runbook.md)`. `group unarchive` ends with `run kh rebuild --repo <repo>` (pushes while archived were ignored). `group remove` ends with `the Forgejo repo <repo> is kept; delete it in Forgejo if you want it gone`.

### 3.9 Deploy

`deploy/compose.example.yaml:61` becomes `- ./config:/etc/kh:ro` (the directory) so a replaced `kh.yaml` is seen by path after a restart; `KH_CONFIG_FILE` stays `/etc/kh/kh.yaml`. The operator runs the admin commands where `kh setup` runs today, with `KH_CONFIG_FILE` pointing at the writable file and the `KH_SETUP_*` credential files set. The runbook says to move `kh.yaml` into `./config/`.

### 3.10 Rejected

- `label rename` that rewrites records, documents or frontmatter (the owner chose description only).
- `group rename` (the name is the audience in every row, Forgejo team and PrivacyFence entry).
- A `kh group remove` that deletes the Forgejo repo (setup never deletes repos).
- Direct database writes that bypass `kh.yaml` (the file would drift; the next `kh setup apply` would undo them).
- Taking the setup lock in `kh setup` (it only reads the file).

## 4. Manual steps

No `manual_before`. One `manual_after`, because the first admin commands against a real Forgejo cannot run in CI.

### ma1-live-admin-roundtrip: run the commands against the real stack (about 15 minutes)

**After implementation**

1. Move `kh.yaml` into a new directory `./config/` next to your compose file and change the volume to `./config:/etc/kh:ro` as in [`deploy/compose.example.yaml`](https://github.com/tkcs-lab/knowledge-hub/blob/feat/admin-commands/deploy/compose.example.yaml). Recreate the stack with `docker compose up -d --force-recreate`. You see `kh serve` healthy.
2. On the machine where you run `kh setup` (see [the runbook](https://github.com/tkcs-lab/knowledge-hub/blob/feat/admin-commands/docs/runbook.md)), set `KH_CONFIG_FILE` to the writable `kh.yaml` and run `kh label add smoke:admin -d "temporary"`. You see a diff, `+ label smoke:admin`, one `+ job git_push: <repo>` line per active group, `kh.yaml updated` and `restart kh serve: ...`.
3. Run `kh label remove smoke:admin`. You see `- label smoke:admin`.
4. Pick a test group and a person who is in `people`: run `kh group member add <group> <login>`, then open the team page of the group in your Forgejo (`<forgejo_web_url>/org/<org>/teams/<group>`) and see the login as a member; run `kh group member remove <group> <login>` and see them gone.
5. Run `kh setup plan --config <file>`; you see no `+`, `~`, `-` or `!` line and exit 0 (`?` and `i` lines may remain).
6. Report back in the pull request: `ma1 pass` or `ma1 fail: <step and what you saw>`.

## 5. Risks and open questions

- The ruamel API (`YAML()`, `preserve_quotes`, `indent(mapping, sequence, offset)`, `CommentedSeq.fa.set_flow_style()`) is written from knowledge, not checked online. If a call differs, `p1-yamledit` stops with `status=blocked` and a `Research:` line.
- Round trip output must equal the input for untouched lines. If a golden test of p1 cannot be made to pass with the settings in 3.4, stop with `status=blocked`.
- The postgres `sync_groups` and forgejo `sync_group` split must leave `kh setup` output unchanged. If an existing test in `test_setup_forgejo.py` other than the archived-group teams test changes, stop with `status=blocked`.
- The permissions of the real Forgejo admin token for team member removal on archived orgs are not tested in CI (ma1 covers it).
- Unrelated drift: `sync_group` looks only at one group, so drift elsewhere does not turn a member change into exit 2.
- A changed mount is a breaking deploy change for operators who copy the compose example; the changelog says so.

## Implementation manifest

```yaml
plan_slug: admin-commands
feature_branch: feat/admin-commands
tracking_issue: 45
max_parallel: 3
manual_before: []
manual_after:
  - id: ma1-live-admin-roundtrip
    title: Run the label, group member and setup plan commands against the real stack
    why: A real Forgejo and Postgres are not available to CI, so team changes and the directory mount are checked by hand.
verify_after_merge:
  - .venv/bin/python -m pytest -q -m unit -k admin
  - .venv/bin/python -m pytest -q -m unit -k setup
  - .venv/bin/python -m pytest -q -m integration -k admin
final_checks:
  - "`tld plan deviations check --file docs/plans/45/deviations.md --plan docs/plans/45/plan.md --all` exits 0"
  - "the changelog has entries under `## [Unreleased]` and no version heading"
  - "`.venv/bin/python -m pytest -q` passes"
phases:
  - id: p1-yamledit
    title: ruamel round-trip editor for kh.yaml
    depends_on: []
    complexity: M
    touches:
      - pyproject.toml
      - src/knowledge_hub/admin/__init__.py
      - src/knowledge_hub/admin/yamledit.py
      - tests/unit/test_admin_yamledit.py
      - tests/unit/test_import_boundary.py
    brief: |
      Read first: docs/plans/45/plan.md (sections 3.2, 3.4), src/knowledge_hub/config.py, deploy/kh.example.yaml, tests/unit/test_import_boundary.py.
      1. In `pyproject.toml` add `"ruamel.yaml>=0.18,<0.19"` to `dependencies`; install it with `.venv/bin/python -m pip install -e ".[dev]"`.
      2. Create `src/knowledge_hub/admin/__init__.py` with only the docstring `"""Admin commands: edit kh.yaml and apply the matching setup steps (plan 45). Never imported by the serving app."""`.
      3. Create `src/knowledge_hub/admin/yamledit.py` with exactly the functions of plan section 3.4: `Doc` (a dataclass with `data` and `text`), `load`, `render`, `validate`, `locked`, `commit`, `unified_diff`, using `knowledge_hub.config.ConfigError` and `parse_config`. `load` of a file whose top level is not a mapping raises `ConfigError("config: top level must be a mapping")`.
      4. In `tests/unit/test_import_boundary.py` extend the `bad` list so that `knowledge_hub.admin` (and its submodules) and `ruamel` modules also fail the check.
      5. Create `tests/unit/test_admin_yamledit.py` with these tests (all use `tmp_path`): `test_roundtrip_of_example_file_is_identical` (load then render `deploy/kh.example.yaml` equals the text), `test_validate_rejects_invalid_text_with_config_prefix`, `test_commit_replaces_atomically_and_keeps_mode`, `test_commit_follows_a_symlink`, `test_commit_reports_unwritable_directory`, `test_locked_blocks_a_second_holder` (use a second `os.open` + `fcntl.flock(LOCK_EX | LOCK_NB)` inside the context and expect `BlockingIOError`), `test_load_empty_file_gives_empty_mapping`, and golden tests on the example file, each asserting that `unified_diff` of the edit contains no changed line other than the expected ones: `test_golden_add_label_changes_only_its_line`, `test_golden_remove_label_keeps_other_comments`, `test_golden_add_group_appends_a_block`, `test_golden_add_person_adds_one_line`, `test_golden_set_archived_on_group`. The golden edits are made directly on `doc.data` here (the ops module comes in p2).
      6. Stop condition: if the round trip of the example file is not identical with the settings of section 3.4, or a ruamel call named there does not exist, stop with `status=blocked` and a `Research:` line.
      7. Run `.venv/bin/python -m ruff check .` and the tests of this phase.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_admin_yamledit.py -q passes"
      - ".venv/bin/python -m pytest tests/unit/test_import_boundary.py -q passes"
      - "grep -n 'ruamel.yaml' pyproject.toml matches"
      - ".venv/bin/python -m ruff check . passes"
  - id: p3a-steps-db
    title: Single-object removal and group sync on the labels and postgres steps
    depends_on: []
    complexity: M
    touches:
      - src/knowledge_hub/setup/labels.py
      - src/knowledge_hub/setup/postgres.py
      - src/knowledge_hub/setup/cli.py
      - tests/unit/test_setup_cli.py
      - tests/integration/test_setup_postgres.py
    brief: |
      Read first: docs/plans/45/plan.md (section 3.6), src/knowledge_hub/setup/labels.py, src/knowledge_hub/setup/postgres.py, src/knowledge_hub/setup/cli.py, tests/integration/test_setup_postgres.py.
      1. `labels.py`: add `async def remove_label(self, name: str, *, write: bool) -> Change` to `LabelsStep`. It opens the connection like `plan`, counts the use as `_remove` does, returns `Change("!", f"label {name}: in use")` when used, returns `Change("-", f"label {name}")` when `write` is false and the label exists (or `Change("i", f"label {name}: not in the database")` when it does not exist), and performs the delete exactly as `_remove` does when `write` is true (a label absent from the database with `write=True` returns the same `i` line and deletes nothing). Make `_remove` call the shared code; its behaviour for `--prune` stays identical.
      2. `postgres.py`: add `async def sync_groups(self, *, write: bool) -> list[Change]` returning `await self._groups(self._settings.dsn("migrator"), False if write else None)` (no roles, database or migrations); add `async def remove_group(self, name: str, *, write: bool) -> Change` (the check and the delete of `_remove_group`, with `write=False` returning `Change("-", f"group {name}")` without deleting, or the `!` refusal; `Change("i", f"group {name}: not in the database")` when absent) and `async def group_usage(self, name: str) -> tuple[int, int]` returning the count of documents and of records with that audience. Reuse `_connect`.
      3. `setup/cli.py`: rename `_builders` to `build_steps` and `_NotSet` to `NotSet` (all uses, the call in `main`).
      4. `tests/unit/test_setup_cli.py`: follow the rename (`monkeypatch.setattr(setup_cli, "build_steps", builders)`).
      5. Extend `tests/integration/test_setup_postgres.py` with: `test_remove_label_deletes_unused_label_and_refuses_used` (use `add_record(conn, audience=..., labels=[...])` from `tests/support/seed.py`), `test_remove_label_plan_only_writes_nothing`, `test_sync_groups_adds_only_groups` (no role or migration change is reported), `test_remove_group_refuses_records`, `test_remove_group_deletes_documents`, `test_group_usage_counts`.
      6. Stop condition: if `kh setup plan` output of an existing test changes, stop with `status=blocked`.
      7. Run ruff and the tests of this phase.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_setup_postgres.py -q -k 'remove_label or sync_groups or remove_group or group_usage' passes"
      - ".venv/bin/python -m pytest tests/unit/test_setup_cli.py tests/integration/test_setup_postgres.py -q passes"
      - "grep -rn '_builders\\|_NotSet' src tests returns nothing"
      - ".venv/bin/python -m ruff check . passes"
  - id: p3b-steps-forgejo
    title: Single-group sync, removal and account check on the forgejo step
    depends_on: []
    complexity: M
    touches:
      - src/knowledge_hub/setup/forgejo.py
      - tests/unit/test_setup_forgejo.py
    brief: |
      Read first: docs/plans/45/plan.md (sections 3.6, 3.7), src/knowledge_hub/setup/forgejo.py, tests/unit/test_setup_forgejo.py, tests/support/forgejo_admin_fake.py.
      1. In `forgejo.py` move the per-group body of the loop in `_sync` into a private `async def _group(self, out, name, teams, repos, missing, write)` (the existing team, repo, team-repo, collaborator and protection calls for an active group, `_archive` and the trim of step 2 for an archived group). `_sync` keeps its single `_accounts` call, `_identity`, the shared `teams`/`repos`, the webhook, the unmanaged lines and the owners line, and calls `_group` for each group; it must print the same lines in the same order as before. Split `_accounts` so its core is `async def _check_accounts(self, out, logins, owners) -> set[str]` (returns the missing logins); `_accounts` calls it with every login. Add the public `async def sync_group(self, name: str, *, write: bool) -> list[Change]`: it loads the owners, checks only the accounts of that group's members and approvers and both bots, fetches `teams` and `repos`, calls `_group` and returns the changes.
      2. Archived group trim: in `sync_group`, for an archived group, after `_archive`, for each of the teams `<g>` and `<g>-approve` that already exists, list its members; for every member not in the configured `members` (write team) or `approvers` (approve team) append `Change("~", f"team {team}: members -{login}")` and, when `write`, call `self._admin.remove_team_member(tid, login)`. Never add a member and never create a team.
      3. Add `async def remove_group(self, name: str, *, repo: str, write: bool) -> list[Change]` (the group is no longer in `self._config`, so the caller passes its repo name): for `<name>` and `<name>-approve` that exist, `Change("-", f"team {t}")` and, when `write`, `await self._delete_team(int(team["id"]))`; then `Change("i", f"repo {repo}: kept")` when that repo exists.
      4. Add `async def missing_accounts(self, logins: list[str]) -> list[str]` returning the logins for which `self._account(login)` is `None`, in the given order.
      5. Tests in `tests/unit/test_setup_forgejo.py`: `test_sync_group_touches_only_that_group`, `test_sync_group_has_no_webhook_or_unmanaged_lines`, `test_sync_all_output_is_unchanged` (run `plan` and `apply` and compare with the lines the existing tests expect; reuse their config), `test_archived_group_removes_members_the_config_dropped`, `test_archived_group_never_adds_members_or_creates_teams`, `test_remove_group_deletes_both_teams_and_keeps_repo`, `test_remove_group_plan_deletes_nothing`, `test_missing_accounts_method`. The existing `test_archived_group_repo_is_archived_and_teams_untouched` must still pass unchanged (its teams hold exactly the configured members).
      6. Stop condition: if any existing test needs a change, stop with `status=blocked` and say which.
      7. Run ruff and the tests of this phase.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_setup_forgejo.py -q passes"
      - ".venv/bin/python -m pytest tests/unit/test_setup_forgejo.py -q -k 'sync_group or remove_group or missing_accounts or archived_group' passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p2-ops
    title: Pure edit functions for labels, groups, members and people
    depends_on: [p1-yamledit]
    complexity: M
    touches:
      - src/knowledge_hub/admin/ops.py
      - tests/unit/test_admin_ops.py
    brief: |
      Read first: docs/plans/45/plan.md (sections 3.4, 3.5), src/knowledge_hub/admin/yamledit.py, deploy/kh.example.yaml.
      1. Create `src/knowledge_hub/admin/ops.py` with `class OpError(ValueError)` and one function per row of the table in plan section 3.5, with exactly those names, signatures and error texts. Each takes `doc: Doc` (from `yamledit`) and edits `doc.data` in place.
      2. Rules: logins compare with `casefold()`; the stored login spelling is the one found in `people`; emails are `.lower()`; a missing top-level key (`labels`, `archived_labels`, `people`, `groups`) is created as an empty mapping or list; new lists are `CommentedSeq` set to flow style; new mapping keys are appended at the end; `group_add` writes the keys in the order `repo` (only when given), `members`, `approvers`; `person_remove` with `from_groups` removes the login from every group's `members` and `approvers`.
      3. Tests in `tests/unit/test_admin_ops.py`, using `yamledit.load` on a `tmp_path` file built from a small text with comments; every function gets a success case and each error text of the table gets a test named `test_<function>_<error words>`. Also: `test_label_remove_also_drops_archived_entry`, `test_member_remove_with_approver_flag_removes_both`, `test_person_remove_from_groups_cleans_every_group`, `test_ops_keep_untouched_comments`, `test_group_add_writes_flow_lists`.
      4. Every test validates the result with `yamledit.validate(yamledit.render(doc))`, except the tests of errors raised by the config validation itself (`approver_remove` of the last approver is an `OpError`, not a config error).
      5. Run ruff and the tests of this phase.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_admin_ops.py -q passes"
      - "grep -c '^def ' src/knowledge_hub/admin/ops.py reports at least 14"
      - ".venv/bin/python -m ruff check . passes"
  - id: p4-apply
    title: "The execute function: stage, apply, commit"
    depends_on: [p1-yamledit, p3a-steps-db, p3b-steps-forgejo]
    complexity: M
    touches:
      - src/knowledge_hub/admin/apply.py
      - tests/unit/test_admin_apply.py
    brief: |
      Read first: docs/plans/45/plan.md (sections 3.3, 3.7), src/knowledge_hub/admin/yamledit.py, src/knowledge_hub/setup/cli.py (`build_steps`, `NotSet`), src/knowledge_hub/setup/steps.py.
      1. Create `src/knowledge_hub/admin/apply.py` with the type aliases `Edit`, `Check`, `After`, the class `Steps` (constructor `Steps(settings, config)`; methods `labels()`, `postgres()`, `forgejo()` that call `build_steps(settings, config, False)[name]()` once each and cache the result) and `async def execute(...)` with the exact signature of section 3.3. `StepsFactory = Callable[[KhConfig], Steps]`; `default_steps_factory(config)` loads `load_settings()` and returns `Steps(settings, config)`; `execute` uses it when `steps_factory` is `None`.
      2. Implement the seven numbered stages of section 3.3 exactly, with these output strings: `nothing to change`; the diff from `unified_diff`; `kh.yaml updated` after a commit; `kh.yaml unchanged` on a refusal; `kh.yaml changed; apply incomplete: run kh setup apply`; `kh.yaml changed; <n> lines need your attention`; `kh.yaml changed; run kh setup apply` (no-apply); `plan skipped: <name> is not set`; `admin: <name> is not set` (a `NotSet`); the closing line `restart kh serve: the hub reads kh.yaml at start`. Print every `Change` with `Change.render()`. Return 0 or 2 as in section 3.3. `path is None` prints `admin: no config file: pass --config or set KH_CONFIG_FILE` and returns 2 before anything else.
      3. Scrub exceptions from `after` with the same regex as `setup.steps._message` (import `_message`; it is a function of the same package family, so import is allowed from `admin`).
      4. Tests in `tests/unit/test_admin_apply.py` with a fake `Steps`-like object and a `tmp_path` file: `test_no_change_writes_nothing`, `test_invalid_edit_is_not_written`, `test_dry_run_writes_nothing_and_runs_after_with_write_false`, `test_no_apply_commits_without_steps`, `test_check_refusal_writes_nothing`, `test_target_refusal_keeps_the_old_file`, `test_other_bang_commits_and_exits_two`, `test_after_exception_commits_and_says_incomplete`, `test_not_set_writes_nothing`, `test_secrets_in_errors_are_hidden`, `test_success_prints_restart_note_and_commits`, `test_lock_is_held_during_after` (assert a second `flock` fails inside `after`), `test_notes_are_printed_after_success`.
      5. Run ruff and the tests of this phase.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_admin_apply.py -q passes"
      - "grep -n 'async def execute' src/knowledge_hub/admin/apply.py matches"
      - ".venv/bin/python -m ruff check . passes"
  - id: p5-cli-labels-persons
    title: kh label and kh person commands and the dispatcher
    depends_on: [p2-ops, p4-apply]
    complexity: M
    touches:
      - src/knowledge_hub/admin/cli.py
      - src/knowledge_hub/admin/labels_cmd.py
      - src/knowledge_hub/admin/persons_cmd.py
      - src/knowledge_hub/cli.py
      - tests/unit/test_admin_cli.py
      - tests/integration/test_admin_labels.py
    brief: |
      Read first: docs/plans/45/plan.md (sections 3.1, 3.3, 3.5, 3.8), src/knowledge_hub/admin/apply.py, src/knowledge_hub/admin/ops.py, src/knowledge_hub/cli.py, tests/unit/test_setup_cli.py, tests/integration/test_setup_postgres.py.
      1. Create `admin/cli.py`: `main(argv: list[str]) -> int` building `argparse.ArgumentParser(prog="kh")` with a required subparser `command` for `label`, `group`, `person`; for each module name in `COMMAND_MODULES = ("labels_cmd", "persons_cmd")` import `knowledge_hub.admin.<name>` with `importlib` and call its `register(sub)`. Helpers in the same file: `add_common(parser, *, mutating: bool)` (adds `--config` (type `Path`, default from `os.environ.get("KH_CONFIG_FILE")`), and for mutating also `--dry-run` and `--no-apply`; read-only commands add `--json`), and `def run(args, edit, **kw) -> int` that calls `asyncio.run(apply.execute(args.config, edit, dry_run=args.dry_run, no_apply=args.no_apply, **kw))` and turns an `OpError` raised inside `edit` into `admin: <text>`, exit 2. Argparse errors exit 2.
      2. Create `admin/labels_cmd.py` with `register(sub)` adding `label` with the subcommands of plan 3.1 (`add NAME -d/--description`, `rename NAME TEXT`, `archive NAME`, `unarchive NAME`, `remove NAME`, `list`). Edits call the `ops.label_*` functions. After functions: add and rename: `lambda s, w: s.labels().apply(False) if w else s.labels().plan()`; remove: `apply(False)` then `remove_label(name, write=w)`, with `target=f"label {name}"`; archive, unarchive: no `after`. `list` prints `NAME  STATE  DESCRIPTION` (STATE is `active` or `archived`), or a JSON list of `{"name","description","archived"}` with `--json`, read from `yamledit.load` + `validate` only.
      3. Create `admin/persons_cmd.py` with `register(sub)` for `person add LOGIN EMAIL`, `person remove LOGIN [--from-groups]`, `person list [--json]` (columns LOGIN, EMAIL, GROUPS). `person remove --from-groups` needs the groups the login was in before the edit: capture them in the `edit` closure and use them in `after` as `forgejo().sync_group(g, write=w)` for each; `person add` and plain `person remove` have no `after`.
      4. In `src/knowledge_hub/cli.py` `main`, after the `setup` dispatch add the `label`, `group`, `person` dispatch of plan 3.1.
      5. `tests/unit/test_admin_cli.py` (monkeypatch `knowledge_hub.admin.apply.default_steps_factory` to return a fake `Steps` with recording fake steps): `test_label_add_calls_edit_and_after`, `test_label_remove_targets_the_label`, `test_label_rename_changes_only_description` (end to end on a tmp file with `--no-apply`), `test_label_list_output_and_json`, `test_person_add_lowercases_email`, `test_person_remove_needs_from_groups_when_member`, `test_person_remove_from_groups_syncs_each_group`, `test_missing_config_is_exit_two`, `test_dispatcher_routes_label_group_person` (via `knowledge_hub.cli.main`).
      6. `tests/integration/test_admin_labels.py`: the real Postgres cluster like `test_setup_postgres.py`: `test_label_add_then_rename_then_remove_roundtrip` (file, database and exit codes), `test_label_remove_in_use_keeps_the_file`.
      7. Run ruff and the tests of this phase.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_admin_cli.py -q passes"
      - ".venv/bin/python -m pytest tests/integration/test_admin_labels.py -q passes"
      - ".venv/bin/python -m pytest tests/unit/test_import_boundary.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
  - id: p6-cli-groups
    title: kh group commands
    depends_on: [p5-cli-labels-persons]
    complexity: M
    touches:
      - src/knowledge_hub/admin/groups_cmd.py
      - src/knowledge_hub/admin/cli.py
      - tests/unit/test_admin_groups_cmd.py
      - tests/integration/test_admin_groups.py
    brief: |
      Read first: docs/plans/45/plan.md (sections 3.1, 3.5, 3.7, 3.8), src/knowledge_hub/admin/cli.py, src/knowledge_hub/admin/labels_cmd.py, src/knowledge_hub/setup/forgejo.py, tests/unit/test_setup_forgejo.py.
      1. In `admin/cli.py` change `COMMAND_MODULES` to `("labels_cmd", "persons_cmd", "groups_cmd")`.
      2. Create `admin/groups_cmd.py` with `register(sub)` for `group add NAME --members A,B --approvers A [--repo R]` (comma separated, both required), `group remove NAME --yes`, `group archive NAME`, `group unarchive NAME`, `group member add|remove GROUP LOGIN [--with-approver on remove]`, `group approver add|remove GROUP LOGIN`, `group list [--json]` (columns NAME, REPO, STATE (`active` or `archived`), MEMBERS, APPROVERS). The parser's `description` for `group` contains `A group cannot be renamed: its name is the audience of every document and record.`
      3. Edits call the `ops.group_*`, `ops.member_*`, `ops.approver_*` functions. After functions per plan 3.1: add: `postgres().sync_groups(write=w)` then `forgejo().sync_group(name, write=w)`, `target=f"group {name}"`, notes from 3.8; remove: postgres `remove_group(name, write=w)`, and only when that change is not `!` forgejo `remove_group(name, repo=<repo>, write=w)` (the repo name is captured from the old config in the `edit` closure), `target=f"group {name}"`, notes from 3.8; archive/unarchive: `forgejo().sync_group(name, write=w)` (unarchive adds the rebuild note); member and approver commands: `forgejo().sync_group(group, write=w)`.
      4. `check` functions (plan 3.7): add, member add, approver add: `missing_accounts` of the new logins; remove without `--yes`: the usage message from `postgres().group_usage(name)`. A `NotSet` there is `admin: <name> is not set`, exit 2.
      5. `tests/unit/test_admin_groups_cmd.py` (fake steps, `--no-apply` for pure edits): `test_group_add_edits_and_syncs`, `test_group_add_refuses_missing_account`, `test_group_remove_needs_yes_and_shows_counts`, `test_group_remove_refused_keeps_the_file_and_the_teams` (the fake Forgejo still has both teams), `test_group_archive_and_unarchive_toggle_the_key`, `test_unarchive_prints_rebuild_note`, `test_member_add_unknown_person`, `test_member_remove_of_approver_needs_flag`, `test_approver_remove_last_approver`, `test_group_list_output_and_json`, `test_group_help_says_no_rename`.
      6. `tests/integration/test_admin_groups.py`: Postgres cluster plus `FakeForgejoAdmin` like the other setup tests (see how `tests/unit/test_setup_forgejo.py` builds `Fake`): `test_group_add_member_remove_roundtrip` (file, database rows, Forgejo teams), `test_group_remove_with_records_is_refused_and_file_unchanged`, `test_leaver_removed_from_archived_group_team`.
      7. Run ruff and the tests of this phase.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_admin_groups_cmd.py -q passes"
      - ".venv/bin/python -m pytest tests/integration/test_admin_groups.py -q passes"
      - "grep -n 'cannot be renamed' src/knowledge_hub/admin/groups_cmd.py matches"
      - ".venv/bin/python -m ruff check . passes"
  - id: p7-retire
    title: Docs, changelog, example files
    depends_on: [p6-cli-groups]
    complexity: S
    touches:
      - docs/configuration.md
      - docs/runbook.md
      - README.md
      - CHANGELOG.md
      - deploy/compose.example.yaml
      - deploy/kh.example.yaml
    brief: |
      Read first: docs/plans/45/plan.md (sections 3.1, 3.6, 3.8, 3.9), docs/configuration.md (Commands, kh setup), docs/runbook.md (Adding a group, Archiving and unarchiving, Joiners and leavers), tests/unit/test_deploy_files.py, tests/unit/test_docs_links.py.
      1. `docs/configuration.md`: add the rows for `kh label ...`, `kh group ...`, `kh person ...` to the Commands table; add a section `## kh admin commands` after `kh setup` with the table of plan 3.1 (without the Effect column wording that names step methods: say `runs the labels step`, `runs the postgres and forgejo steps for that group`), the common flags, the exit codes, the stage, apply, commit rule in three sentences, and the sentence that `kh.yaml` must sit in a writable directory for the admin commands; append to the Forgejo step text: `An archived group keeps its teams; members the config no longer lists are removed from them, none are added.` In the Lines table change the `-` row to `remove (with --prune, or by kh label remove and kh group remove)`.
      2. `docs/runbook.md`: in `Adding a group`, `Archiving and unarchiving` and `Joiners and leavers` add the command form first (`kh group add ...`, `kh group archive ...`, `kh group member remove ...`, `kh person remove --from-groups`) and keep the file-edit form as the second way; change the sentence that says setup leaves archived teams as they are; in the first-deploy list say to place `kh.yaml` in `./config/`.
      3. `README.md`: add one row to the commands table: `kh label`, `kh group`, `kh person`: manage `kh.yaml` and apply the change.
      4. `CHANGELOG.md` under `## [Unreleased]`: `### Added` with the commands (one bullet per family), `### Changed` with the compose mount change (`./config:/etc/kh:ro`; move `kh.yaml` into `./config/` and recreate the stack) and the archived-team trim.
      5. `deploy/compose.example.yaml`: line 61 becomes `- ./config:/etc/kh:ro`; `deploy/kh.example.yaml`: first comment line says `mounted from a directory at the path in KH_CONFIG_FILE`.
      6. Do not add a docs index row (the repository has no docs index).
      7. Stop condition: if `tests/unit/test_deploy_files.py` or `tests/unit/test_docs_links.py` fails for a reason other than your edits, stop with `status=blocked`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_deploy_files.py tests/unit/test_docs_links.py -q passes"
      - "grep -n 'config:/etc/kh' deploy/compose.example.yaml matches"
      - "grep -n 'kh admin commands' docs/configuration.md matches"
      - "grep -c 'kh label' CHANGELOG.md reports at least 1"
```
