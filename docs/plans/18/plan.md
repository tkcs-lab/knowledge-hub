# Plan: CI checks on every pull request and on main

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** Every pull request and every push to `main` runs the new `ci` workflow. It shows four checks: `lint` (ruff), `test (3.12)` and `test (3.13)` (the full pytest suite, unit and integration, against a throwaway PostgreSQL 16 with pgvector) and `image` (the Dockerfile builds and `kh --help` runs in it). A red check shows on the pull request; once the owner makes them required on `main`, a red pull request cannot merge. A release dry run now fails unless all four checks are green on the commit, instead of passing when no check exists, so a release waits for `ci` to finish on `main` (the runbook says so).

**Scope:** A new workflow `.github/workflows/ci.yml` and one new step in `release.yml` that requires the four checks by name. Both are protected paths, so each reaches `main` through its own protected-change pull request after the owner approves it on the board. The feature pull request carries the ruff pin in `pyproject.toml`, the docs (README "CI" section, two runbook sentences) and the changelog line. The tests, the Dockerfile and the profile do not change. No coverage threshold, type checker or dependency audit.

**Security:** The workflow has `contents: read` only, uses no secret, checks out without persisting the token and runs pull requests from forks with GitHub's read-only token. The test job runs `sudo apt-get` through the existing `scripts/ensure_test_pg.sh`. The release gate can no longer pass on a commit whose checks are missing.

**Performance:** About two to four minutes per push on GitHub-hosted runners (the suite takes 80 s locally, plus the PostgreSQL install), four jobs in parallel (`lint`, two `test`, `image`).

**Review focus:** the named-check gate in `release.yml` and the job names it depends on (3.2, 3.3); the concurrency group that never cancels a run on `main` (3.2); installing PostgreSQL with `sudo` on the runner (3.4); two protected pull requests and the order they must merge in, the `ci` one running the new workflow as soon as it is opened (3.6); `image` on an arm64 runner for every pull request (3.1).

## 1. Goal

The repository has no CI on pull requests or on `main`. Lint and tests run only when a person or a worker runs them, and `release.yml`'s step "Require green checks on this commit" passes whatever the code does, because no other check exists on the commit. After this change every pull request and every push to `main` is linted, tested against a real PostgreSQL and built as an image, and the owner can make those checks required. Tracking issue: [tkcs-lab/knowledge-hub#18](https://github.com/tkcs-lab/knowledge-hub/issues/18).

## 2. Current state

- `.github/workflows/` holds only `release.yml` (`workflow_dispatch`, `runs-on: ubuntu-24.04-arm`). Its step "Require green checks on this commit" (`release.yml:47-62`) lists the check runs of the commit and fails on any that is not completed with `success`, `skipped` or `neutral`, except those named like its own workflow (`release`) or its jobs (`build`, `publish`). With no other workflow the list is empty and the step passes.
- `tests/unit/test_release_files.py` pins the shape of `release.yml` (top-level `permissions: {contents: read}`, every `run:` starts with `set -euo pipefail`).
- `.tld/toolkit.yaml`: `verify.fast` is `ruff check .` and `pytest -q -m unit`; `verify.dod` is `ruff check .` and `pytest -q`, both through `.venv/bin/python`.
- `pyproject.toml`: `requires-python = ">=3.12"`, extra `dev = [pytest, pytest-asyncio, ruff]`, ruff rules `E F I B UP`, line length 110. The `Dockerfile` builds a wheel on `python:3.13-slim` and installs it; `migrations/*.sql` and `governance/templates/*` reach the wheel only through `[tool.setuptools.package-data]`.
- `tests/conftest.py:12-19` marks `tests/integration/**` `integration` and `tests/unit/**` `unit`. Integration fixtures (`tests/support/fixtures_db.py`) start one cluster per session with `tests/support/pg.py`: binaries from `/usr/lib/postgresql/16/bin`, data in `/var/tmp/kh-pg-<pid>`, unix socket only (`listen_addresses=` empty), trust auth, `su postgres` only when running as root. When `initdb` is missing it runs `scripts/ensure_test_pg.sh`, which `apt-get install`s `postgresql-16 postgresql-16-pgvector` and needs root.
- Measured in this session: `ruff check .` passes; `pytest -q` gives 1118 passed, 1 skipped in 80 s; `pytest -q -m unit` 416 passed in 1.6 s.
- `.github/workflows/**` is a protected path of the toolkit (default `hooks.protect_paths`; the profile has no `hooks` key). A worker never writes it; it reaches `main` as a one-file pull request opened after the owner's `apply` (playbooks/policies/protected-changes.md).
- `docs/runbook.md:141-155` (Releasing) step 2 says a dry run checks "that every other check on the commit is green".

## 3. Design

### 3.1 What runs, and what was rejected

| Job | Runs | Why |
|---|---|---|
| `lint` | `ruff check .` on Python 3.12 | the profile's first verify command |
| `test` (matrix 3.12, 3.13) | `pytest -q`, the whole suite with integration | 3.12 is `requires-python`'s minimum, 3.13 is what the image runs |
| `image` (on `ubuntu-24.04-arm`) | `docker build`, then `docker run --rm knowledge-hub:ci --help` | catches a Dockerfile, package-data (a missing `migrations/*.sql`) or arm64-only break before release day, on the architecture `release.yml` ships |

Rejected:

- **Unit tests only in CI**: the integration tests are where row-level security is proved, and they take 80 s. They run on every push.
- **A Postgres service container** (`services: postgres` with a pgvector image): the tests build their own cluster on a unix socket from `/usr/lib/postgresql/16/bin`; a service would need a second code path in `tests/support/pg.py`. Installing the packages keeps CI identical to a developer machine.
- **arm64 for every job**: `lint` and `test` do not depend on the architecture, and `ubuntu-24.04` is the standard hosted image where PostgreSQL packages are known to install. Only `image` runs on `ubuntu-24.04-arm`, the runner `release.yml` already uses in this repository.
- **Keeping `release.yml`'s gate as it is** ("every check present is green"): it passes when the `ci` runs of the commit have not been created yet (a dispatch seconds after a merge) or when `ci.yml` is broken or renamed. Rejected after the architect role flagged it; 3.3 adds a named list.
- **Calling `ci.yml` from `release.yml` with `workflow_call`**: removes the race too, but runs the whole suite again on every release and changes both files more. Rejected.
- **A unit test pinning `ci.yml` and the new gate step** like `test_release_files.py`: both reach `main` in separate pull requests (3.6), so a test in the feature pull request would fail until they merge. The workflow proves itself by running on its own pull request; the invariants are listed in 3.2 and 3.3 for review. A follow-up issue, opened by p1 (step 8), adds `tests/unit/test_ci_files.py` once both are on `main`.
- **Coverage thresholds, mypy, `pip-audit`, a 3.14 job**: not asked for; each is its own decision.
- **Pinning actions by commit SHA**: `release.yml` uses major tags (`actions/checkout@v4`); this file follows the repository's convention. Ruff is pinned (3.5) because a new ruff release can flag existing code under the same rule selection and turn `lint` red with no change in the repository.

### 3.2 The workflow file `.github/workflows/ci.yml`

Exact content (the protected-change diff of phase p1 creates this file, byte for byte):

```yaml
name: ci

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

concurrency:
  group: ci-${{ github.event.pull_request.number || github.sha }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

jobs:
  lint:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: pyproject.toml

      - name: Install
        run: |
          set -euo pipefail
          python -m venv .venv
          .venv/bin/python -m pip install --upgrade pip
          .venv/bin/python -m pip install -e ".[dev]"

      - name: Ruff
        run: |
          set -euo pipefail
          .venv/bin/python -m ruff check .

  test:
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    strategy:
      fail-fast: false
      matrix:
        python: ["3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false

      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}
          cache: pip
          cache-dependency-path: pyproject.toml

      - name: PostgreSQL 16 with pgvector
        run: |
          set -euo pipefail
          sudo bash scripts/ensure_test_pg.sh

      - name: Install
        run: |
          set -euo pipefail
          python -m venv .venv
          .venv/bin/python -m pip install --upgrade pip
          .venv/bin/python -m pip install -e ".[dev]"

      - name: Tests
        run: |
          set -euo pipefail
          .venv/bin/python -m pytest -q

  image:
    runs-on: ubuntu-24.04-arm
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false

      - name: Build image
        run: |
          set -euo pipefail
          docker build -t knowledge-hub:ci .

      - name: Smoke test
        run: |
          set -euo pipefail
          docker run --rm knowledge-hub:ci --help
```

Invariants a reviewer checks (and a later unit test can pin):

- Workflow name `ci`; job ids `lint`, `test`, `image`, no `name:` on any job. None is `build`, `publish` or `release`, which `release.yml` ignores. The check runs are named `lint`, `test (3.12)`, `test (3.13)`, `image`: exactly the names of 3.3 and of the ruleset (ma1).
- Top-level `permissions: {contents: read}`, no job-level `permissions`, no `secrets.` reference, no `pull_request_target`.
- Every `run:` starts with `set -euo pipefail`, as `test_release_files.py` requires of `release.yml`.
- No `paths` or `branches` filter on `pull_request` (a required check filtered out of a pull request waits forever).
- A run on `main` is never cancelled or replaced: its group is the commit SHA, unique per push, and `cancel-in-progress` is true only for `pull_request`, whose group is the pull request number. A group shared by all pushes to `main` would let a newer pending run replace an older one, leaving that commit without checks.
- The commands are the profile's `verify.dod` commands, unchanged.

### 3.3 The named-check gate in `release.yml`

A new step directly after "Require green checks on this commit" (`release.yml:47-62`), before `docker/setup-buildx-action`. The existing step stays: it still fails on any red check of another app or workflow. Exact text (the second protected-change diff of p1 inserts it, indented as the other steps of `build`):

```yaml
      - name: Require the ci checks on this commit
        env:
          GH_TOKEN: ${{ github.token }}
          REPO: ${{ github.repository }}
          SHA: ${{ github.sha }}
        run: |
          set -euo pipefail
          gh api --paginate "repos/$REPO/commits/$SHA/check-runs?per_page=100" \
            --jq '.check_runs[] | select(.status == "completed" and .conclusion == "success") | .name' > "$RUNNER_TEMP/ci-green.txt"
          missing=""
          for name in "lint" "test (3.12)" "test (3.13)" "image"; do
            grep -Fxq "$name" "$RUNNER_TEMP/ci-green.txt" || missing="$missing [$name]"
          done
          if [ -n "$missing" ]; then
            echo "Required ci checks not green on this commit:$missing"
            exit 1
          fi
```

- The list goes to `$RUNNER_TEMP`, not the workspace, so it never enters the image build context of the next steps.
- It needs `checks: read`, which the `build` job already has. It adds no permission and no secret.
- The four names are the check runs of 3.2. Renaming a job or a matrix value in `ci.yml` needs the same change here and in the ruleset; the README section (3.5) says so.
- `tests/unit/test_release_files.py` keeps passing: the step starts with `set -euo pipefail`, adds no job, no `skip_checks` and no `:latest`.

### 3.4 PostgreSQL on the runner

`sudo bash scripts/ensure_test_pg.sh` installs `postgresql-16` and `postgresql-16-pgvector` with `apt-get` when `initdb` or `vector.control` is missing, and exits 0 at once when both exist. Ubuntu 24.04 ships PostgreSQL 16 and `postgresql-16-pgvector` in its archive. The tests then run as the unprivileged `runner` user, so `tests/support/pg.py` calls `initdb` and `pg_ctl` directly (no `su`), with data and socket under `/var/tmp`. Any PostgreSQL the image runs listens on TCP, and the test cluster has no TCP listener, so the two cannot clash. The script and `pg.py` do not change.

### 3.5 Ruff pin, docs and changelog (the feature pull request)

- `pyproject.toml`: in `[project.optional-dependencies]` change `dev = ["pytest", "pytest-asyncio", "ruff"]` to `dev = ["pytest", "pytest-asyncio", "ruff==0.17.5"]` (the version this plan was checked with; `ruff check .` passes with it on `main`). There is no lockfile.
- `README.md`: a new section between "Quick start" and "The `kh` commands":

  ```markdown
  ## CI

  The `ci` workflow (`.github/workflows/ci.yml`) runs on every pull request and every push to `main`: `lint` (ruff), `test (3.12)` and `test (3.13)` (the whole pytest suite against a throwaway PostgreSQL 16 with pgvector) and `image` (builds the Dockerfile on arm64 and runs `kh --help` in it). The commands are the ones in Quick start. A release dry run requires these four checks green on its commit by name (`.github/workflows/release.yml`), so a job renamed in `ci.yml` must be renamed there and in the `main` ruleset too.
  ```

- `docs/runbook.md`, line 5: replace "Not run in the cloud, so yours to do and check by hand: the Dockerfile build, and the backup script's `pg_dump` against the protected tables (`deploy/backup-kh.sh`). Check both on the first deploy." with "Not run in the cloud, so yours to do and check by hand: the backup script's `pg_dump` against the protected tables (`deploy/backup-kh.sh`). Check it on the first deploy. The Dockerfile build runs in the `ci` workflow on every pull request."
- `docs/runbook.md`, Releasing step 2: replace the sentence "A dry run checks the changelog heading, that every other check on the commit is green, builds the arm64 image and runs `docker run --rm <image> --help`." with "The skill dispatches only once the `ci` workflow has finished on the merge commit; a dry run started earlier fails at **Require the ci checks on this commit**. A dry run checks the changelog heading, that every other check on the commit is green, that `lint`, `test (3.12)`, `test (3.13)` and `image` are among them, builds the arm64 image and runs `docker run --rm <image> --help`."
- `CHANGELOG.md`, under `## [Unreleased]` → `### Added`, last bullet: "- CI: the `ci` workflow lints, runs the whole test suite on Python 3.12 and 3.13 against PostgreSQL 16 with pgvector, and builds the image on every pull request and every push to `main`."

### 3.6 Delivery and order

1. Phase p1 writes the ruff pin, the docs and the changelog on the feature branch and reports two protected changes, each with its own diff: `protected-change: .github/workflows/ci.yml - add the ci workflow (plan 18, 3.2)` (creates the file) and `protected-change: .github/workflows/release.yml - require the ci checks by name (plan 18, 3.3)` (inserts the step).
2. The implement orchestrator puts each diff to the owner (`apply`, `skip`, `stop`; `apply` is deliberately not marked recommended for a workflow; the `ci.yml` ask says that applying it runs the new workflow at once on its pull request). After `apply` it opens one draft pull request per path, `Protected change: .github/workflows/ci.yml` and `Protected change: .github/workflows/release.yml`, to `main`. The `ci` pull request runs `ci` on itself: the first live test of the workflow.
3. **Order:** the owner merges the `ci.yml` pull request first, when its four checks are green, and the `release.yml` one after it (the release pull request gets the `ci` checks once updated from `main`). Merged the other way round, every release dry run fails until `ci.yml` is on `main`; nothing breaks, but no release can run in between.
4. The feature pull request gets the checks once it is updated from `main`. Then ma1 and ma2 below.

A red check on the `ci` protected pull request means the content of 3.2 is wrong (for example the runner image lacks the packages of 3.3). The fix is a new protected-change diff from a follow-up, never an edit by hand on that branch; see Risks.

## 4. Manual steps

### Before implementation

None.

### After implementation

ma1 and ma2 need both protected-change pull requests of 3.6 merged to `main` first.

**ma1. Require the four checks on `main`** (about 5 minutes)

1. Open [Settings → Rules → Rulesets](https://github.com/tkcs-lab/knowledge-hub/settings/rules) of the repository. If a ruleset for `main` already exists, open it; otherwise click **New ruleset → New branch ruleset**, name it `main`, set **Enforcement status** to **Active** and under **Target branches** add **Include default branch**.
2. Tick **Require status checks to pass**, click **Add checks** and add `lint`, `test (3.12)`, `test (3.13)` and `image` (they are offered once the protected-change pull request has run them).
3. Click **Create** (or **Save changes**). The ruleset page lists the four checks.
4. Report in the pull request: `ma1: pass (4 required checks on main)` or `ma1: fail (<what the page shows>)`.

**ma2. A release dry run counts the CI checks** (about 10 minutes)

1. Wait until the `ci` run on `main`'s newest commit is green: [Actions → ci](https://github.com/tkcs-lab/knowledge-hub/actions/workflows/ci.yml).
2. Open [Actions → release](https://github.com/tkcs-lab/knowledge-hub/actions/workflows/release.yml), click **Run workflow**, keep branch `main` and `dry_run` ticked, click **Run workflow**.
3. Open the run, then the `build` job: the steps **Require green checks on this commit** and **Require the ci checks on this commit** pass. (The step **Check changelog heading** before it fails if `main` has no released version heading for the current `__version__`; then report that and stop, it is not a CI problem.)
4. Report in the pull request: `ma2: pass (dry run gate green with ci checks)` or `ma2: fail (<step and message>)`.

## 5. Risks and open questions

- **Runner packages.** If `sudo bash scripts/ensure_test_pg.sh` fails on `ubuntu-24.04` (package not found, or `initdb` present but `vector.control` in another major version's directory), the protected pull request goes red at "PostgreSQL 16 with pgvector". The fix is a new protected-change diff (for example adding the PostgreSQL apt repository); the feature branch needs no change.
- **Order of the pull requests.** The feature pull request's docs describe a workflow and a gate that exist only after the protected pull requests merge. If the owner skips one, the orchestrator records `protected-change skipped: <path>` and the docs of 3.5 would be wrong for it: the feature pull request must not merge until both are on `main` or the docs are corrected.
- **Release gate names.** If a `ci` check run on GitHub is not named exactly `test (3.12)` (for example a renamed matrix value), ma2 fails at **Require the ci checks on this commit** with the missing name in brackets. The fix is a new protected-change diff, never a weaker gate.
- **arm64 runner for `image`.** `release.yml` already runs on `ubuntu-24.04-arm` here. If the `image` job stays queued with no runner on pull requests, report it; moving `image` to `ubuntu-24.04` is the fallback, by a new protected-change diff.
- **Job-name drift.** Until the follow-up test (p1 step 8) exists, only review and the gate of 3.3 (which fails loudly) keep the four names in step.
- **Flaky integration tests on shared runners.** None seen locally; a red run is real until its log proves otherwise (pr-steward policy). Never re-run it away.
- **A brief that is wrong.** If the worker's scratch copy of 3.2 or the patched scratch copy of `release.yml` does not parse with `yaml.safe_load`, the step of 3.3 has no anchor (the step name "Require green checks on this commit" or `uses: docker/setup-buildx-action@v3` is missing), or a sentence of 3.5 is not found verbatim, the plan is out of date: stop with `status=blocked`.

## Implementation manifest

```yaml
plan_slug: ci-checks
feature_branch: feat/ci-checks
tracking_issue: 18
max_parallel: 1
manual_before: []
manual_after:
  - id: ma1-require-checks
    title: Require lint, test (3.12), test (3.13) and image on main with a ruleset
    why: only a repository admin can change the rulesets; without it a red pull request can still merge
  - id: ma2-release-dry-run
    title: Run a release dry run and see both green-check steps pass with the ci checks on the commit
    why: proves the named-check gate finds the real check runs; the profile has no ci.dispatchable, so implement cannot dispatch release.yml itself
verify_after_merge:
  - .venv/bin/python -m pytest -q -m unit
final_checks:
  - "`tld plan deviations check --file docs/plans/18/deviations.md --plan docs/plans/18/plan.md --all` exits 0"
  - the changelog has entries under "## [Unreleased]" and no version heading
  - each of .github/workflows/ci.yml and .github/workflows/release.yml has a protected-change pull request or a `protected-change skipped` deviation
phases:
  - id: p1-ci-workflow
    title: Report ci.yml and the release gate as protected changes; ruff pin, README, runbook, changelog
    depends_on: []
    complexity: M
    touches:
      - pyproject.toml
      - README.md
      - docs/runbook.md
      - CHANGELOG.md
    brief: |
      Read first: docs/plans/18/plan.md sections 3.2, 3.3, 3.5, 3.6; README.md; docs/runbook.md; .github/workflows/release.yml.
      Do not edit anything under .github/ (protected). Build both diffs in a scratch directory outside the repository.
      1. ci.yml: copy the YAML block of plan 3.2 byte for byte into <scratch>/ci.yml. Check it with `.venv/bin/python -c 'import sys, yaml; d = yaml.safe_load(open(sys.argv[1])); assert set(d["jobs"]) == {"lint", "test", "image"}' <scratch>/ci.yml`. Make the diff with `git diff --no-index /dev/null <scratch>/ci.yml` and rewrite its header so the diff starts with exactly the two lines `--- /dev/null` and `+++ b/.github/workflows/ci.yml`: drop the `diff --git`, `new file mode` and `index` lines (the protected-changes policy rejects extended headers).
      2. release.yml: copy .github/workflows/release.yml to <scratch>/release.yml. Insert the YAML block of plan 3.3 (its lines as shown, which carry the 6-space step indent) followed by one blank line, directly before the line `      - uses: docker/setup-buildx-action@v3`, i.e. after the step "Require green checks on this commit" and its blank line. Check it with `.venv/bin/python -c 'import sys, yaml; s = [x.get("name") for x in yaml.safe_load(open(sys.argv[1]))["jobs"]["build"]["steps"]]; i = s.index("Require green checks on this commit"); assert s[i + 1] == "Require the ci checks on this commit"' <scratch>/release.yml`. Make the diff with `git diff --no-index .github/workflows/release.yml <scratch>/release.yml` and rewrite its header so the diff starts with exactly `--- a/.github/workflows/release.yml` and `+++ b/.github/workflows/release.yml`: drop the `diff --git` and `index` lines.
      3. In your report put, for each file, one line `protected-change: .github/workflows/ci.yml - add the ci workflow (plan 18, 3.2)` and `protected-change: .github/workflows/release.yml - require the ci checks by name (plan 18, 3.3)`, each followed by its diff in a fenced block whose fence is longer than any backtick run in it. Verify each with `patch --dry-run -p1` on a scratch copy before reporting.
      4. pyproject.toml: change `"ruff"` in the `dev` extra to `"ruff==0.17.5"`, nothing else. Reinstall with `.venv/bin/python -m pip install -e ".[dev]"`; if pip cannot install ruff 0.17.5, stop with status=blocked.
      5. README.md: insert the "## CI" section of plan 3.5 verbatim between the "## Quick start" section and "## The `kh` commands", one blank line before and after.
      6. docs/runbook.md: make the two replacements of plan 3.5 (line 5, and Releasing step 2), verbatim. If an old sentence is not found verbatim, stop with status=blocked.
      7. CHANGELOG.md: add the bullet of plan 3.5 as the last bullet of "### Added" under "## [Unreleased]". Open no version heading.
      8. Open one follow-up issue in tkcs-lab/knowledge-hub with the GitHub issue tool: title `Unit test for ci.yml and the release named-check gate`, body `Add tests/unit/test_ci_files.py like tests/unit/test_release_files.py, pinning the invariants of docs/plans/18/plan.md sections 3.2 and 3.3 (job and check names, permissions, strict run steps, no path filters, the four names in release.yml). Do it after both protected-change pull requests of #18 are merged.` Put its URL in your report as `follow-up: <url>`. If issue creation is refused, put `follow-up: not created` and go on.
      9. `.venv/bin/python -m ruff check .` and `.venv/bin/python -m pytest -q -m unit` pass (tests/unit/test_docs_links.py checks README links). Also check the patched copy against the release test's rules: `.venv/bin/python -c 'import sys, yaml; d = yaml.safe_load(open(sys.argv[1])); assert all(s["run"].lstrip().startswith("set -euo pipefail") for j in d["jobs"].values() for s in j["steps"] if "run" in s)' <scratch>/release.yml`. Do not edit tests/unit/test_release_files.py.
    acceptance:
      - grep -n '"ruff==0.17.5"' pyproject.toml matches
      - grep -n "^## CI$" README.md matches once
      - grep -c "are among them" docs/runbook.md prints 1
      - grep -c "The Dockerfile build runs in the \`ci\` workflow" docs/runbook.md prints 1
      - grep -n "CI: the \`ci\` workflow lints" CHANGELOG.md matches a line above the first "## [0." heading
      - git diff --name-only origin/main...HEAD lists only pyproject.toml, README.md, docs/runbook.md, CHANGELOG.md and files under docs/plans/18/
      - the report has the two protected-change lines of step 3, each followed by a diff that `patch --dry-run -p1` applies cleanly to the file at origin/main (or to /dev/null for ci.yml)
      - .venv/bin/python -m ruff check . and .venv/bin/python -m pytest -q -m unit pass
      - the report has a line that starts with the word follow-up and a colon
```
