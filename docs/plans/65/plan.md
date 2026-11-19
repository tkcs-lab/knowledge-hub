# Plan: docs refresh, deployment guide and runbook corrections

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** An operator who sets up the hub opens the new `docs/deployment.md`. It lists every component the hub needs with its version and how that was verified, which secret goes into which container, where each `kh` command must run, which jobs the operator has to schedule, and the ordered first deploy, verification and upgrade. The two runbooks then only describe day-to-day work, and every command, message and default they quote matches the code.

**Scope:** Documentation and two example files only. New: `docs/deployment.md`. Corrected: `README.md`, `docs/runbook.md`, `docs/runbook-identity.md`, `docs/configuration.md`, `docs/design.md`, the comments and image tags of `deploy/compose.example.yaml`, one comment of `deploy/kh.example.yaml` and the `[Unreleased]` structure of `CHANGELOG.md`. The three upgrade-note sections of the old runbook are removed (their facts move to the guide). No code, no backup-script change, no workflow change. The backup gap (hub keys, OAuth state, move tombstones) is documented, not fixed.

**Review focus:** the first-deploy order for a directory-backed install (section 3.2, p1), the restore section that now states what the dump loses (section 3.3, p2), the facts about which database role each command uses (section 3.1, p1 and p3) and the compose image tag decision (section 3.0 item 6, p1b), because a wrong sentence there misleads an operator.

## 1. Goal

Issue [#65](https://github.com/tkcs-lab/knowledge-hub/issues/65): make the documentation valid after the identity change (OIDC sign-in, directory sync, hub keys, PrivacyFence removal, migration 0006) and the admin panel, add the missing deployment guide with its dependencies, and decide whether the two runbooks (`docs/runbook.md`, `docs/runbook-identity.md`) are still correct. They are not: an audit of both against the code at `ac81c9d` found the wrong or outdated statements listed in section 3.3 to 3.5.

## 2. Current state

- No deployment guide. The install order is step 1 to 9 of "Install order" in `docs/runbook.md`; the dependencies are scattered over `README.md`, `deploy/compose.example.yaml`, `scripts/ensure_test_pg.sh` and `.github/workflows/ci.yml`.
- `deploy/compose.example.yaml` has four services (`knowledge-hub`, `knowledge-hub-directory`, `knowledge-hub-panel`, `ollama`). Three of them pin `ghcr.io/tkcs-lab/knowledge-hub:0.1.0` (the hub, the directory sync and the panel). The latest release is 0.5.1 (`src/knowledge_hub/__init__.py`), and no tag contains `kh panel`, `kh directory`, `kh key` or the `admins` key (everything is under `[Unreleased]` in `CHANGELOG.md`).
- `docs/runbook.md` is 268 lines, `docs/configuration.md` 324, `docs/design.md` 262, `docs/runbook-identity.md` 94, `README.md` 67, `CHANGELOG.md` 211. `tests/unit/test_docs_links.py` allows 400 lines per non-plan `.md` and requires relative links to resolve (it does not check `#anchors`).
- `tests/unit/test_deploy_files.py` pins the compose example (section 3.6).

## 3. Design

Every fact below was read from the code at `ac81c9d`; the file in brackets is where a worker can check it in one grep.

### 3.0 Decisions and what was rejected

1. **A new `docs/deployment.md`** is the one owner of: components and dependencies, networks, secrets mapping, where each command runs, scheduled jobs, the first-deploy order, verification, upgrade. The runbooks link to it and repeat none of it. The guide is a **contract with the stack the operator brings** (Postgres, Forgejo, S3, a proxy, timers), not a recipe: this repository cannot test how anyone runs them. A dependency version appears only where this repository pins or tests it; otherwise the cell says `not pinned` and how it is verified.
2. **Rejected: growing `docs/runbook.md` into "install and operate".** It would approach the 400-line limit and keep mixing first install with day-two work, which is how the stale rollout notes accumulated.
3. **Rejected: a full reference stack in `deploy/`** (Postgres, Forgejo, Garage, proxy, timers). It is a code and configuration change outside a docs ask, `test_compose_example_networks` pins the network set, and CI could only run `docker compose config`.
4. **Remove, do not rewrite, the historical sections** of `docs/runbook.md`: "Live probe of ma1" (its checks move into the guide's "Verify"), "Rollout of move_item" (0.5.0) and "Rollout of supersede and outdated" (0.4.0). Keep "Moving from PrivacyFence" in `docs/runbook-identity.md`: that change is unreleased.
5. **Backup stays as it is.** `deploy/backup-kh.sh` dumps `kh.records` only (a test pins that). The restore section states what is lost; a script change is a follow-up (Risks).
6. **Compose image tag:** all three services pin `0.5.1` (the latest release; the test regex needs a semver) with a comment that the directory, panel and OAuth features need the first release after 0.5.1. A follow-up issue bumps it after that release (the release can only be cut after this change has merged, so it cannot be a manual step of this plan). The example cannot run on 0.5.1 anyway: `deploy/kh.example.yaml` has `admins`, which 0.5.1 refuses, and the comment says so.
7. **Open research, not blocking:** the minimum Forgejo version for the branch-protection and team fields `kh setup` writes (`docs/plans/26/plan.md` marks it unconfirmed) and which Garage versions accept the hub's path-style requests. The guide says `not pinned, minimum not confirmed` and that `kh setup plan` is the compatibility check.

### 3.1 Facts for `docs/deployment.md` (p1) and the reference docs (p3)

**Database role per command** (`settings.dsn(...)`, `make_*_pool`):

| Command | Role | Note |
|---|---|---|
| `kh serve`, `kh rebuild` (also `--queue`), `kh check`, `kh daily`, `kh requeue-embeddings` | app and indexer pools (`deps.py` `build_deps`) | `build_deps` needs `KH_OLLAMA_URL` set (with `KH_EMBED_PROVIDER=ollama`) and, with `KH_FORGEJO_URL`, `KH_FORGEJO_INDEXER_TOKEN_FILE`; no server has to answer for `--queue` |
| `kh migrate`, `kh setup`, `kh label\|group\|person\|repo` | migrator | `kh setup` also needs `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE` (required by the forgejo step, `setup/cli.py`) and optionally `KH_SETUP_PG_ADMIN_DSN_FILE` (without it the postgres step only checks) |
| `kh key create\|list\|revoke`, `kh directory plan\|people\|show` | migrator (`auth/keys_cli.py`, `directory/store.py` `load_membership`) | run in a container that has `KH_DB_MIGRATOR_PASSWORD_FILE` (the `knowledge-hub` service in the example) |
| `kh directory sync` | indexer (`directory/cli.py`) | runs in `knowledge-hub-directory`; needs migration 0006 and role `kh_indexer` to exist |
| `kh panel serve` | indexer (stats, health reads, pipeline operations) and migrator (the `migrations` health check, `panel/health.py`); its subprocesses use migrator plus the Forgejo admin token (admin commands, `setup plan`) and app plus indexer (`kh check`) | the example mounts both setup secrets; it does not mount `KH_SETUP_SLACK_WEBHOOKS_FILE` |
| `kh lint` | none | |

**Components and dependencies** (rows of the guide's table; columns: component, version, where pinned, how verified):

- Python: `>=3.12` (`pyproject.toml` `requires-python`); image base `python:3.13-slim` (`Dockerfile`); CI runs 3.12 and 3.13. The release image is `linux/arm64` only (`release.yml` `platforms`).
- Python libraries: `mcp==2.3.0` and `ruamel.yaml>=0.18,<0.19` are pinned; the others are not, and there is no lockfile. The image is the supported artefact; a wheel install takes current dependency versions.
- PostgreSQL 16 with pgvector, extension `vector` (`scripts/ensure_test_pg.sh`, `ci.yml`); vector search is exact, no index. Roles `kh_migrator`, `kh_app`, `kh_indexer`, none superuser, none `BYPASSRLS`. Other majors: not verified.
- Forgejo: no version pinned or checked. Verified by `kh setup plan` (teams, protection, webhook, token identity read back).
- Ollama `0.40.16` with model `bge-m3`, 1024 dimensions, `mem_limit: 3g` (compose example; `KH_EMBED_PROVIDER=http` is the alternative). Verified by the setup `ollama` step.
- S3-compatible store, path-style, region default `garage` (`KH_S3_REGION`); Garage in the reference deployment, version not pinned. Verified by the setup `s3` step (put, get, delete probe).
- OIDC provider: Google (`KH_OIDC_HOSTED_DOMAINS` mandatory; directory needs the Admin SDK and domain-wide delegation) or Keycloak; ID tokens must be RS256 or ES256 (`oauth/upstream.py`). By hand.
- A reverse proxy or edge in front of the public paths, and a private network or access proxy for the panel. Operator-provided. By hand.
- n8n (optional client): sends `Authorization: Bearer khk_<id>.<secret>`; version not pinned; support for the hub's streamable HTTP endpoint is unconfirmed.
- Slack incoming webhooks (optional), `pg_dump`/`pg_restore`, the `aws` CLI and restic (backup, `deploy/backup-kh.sh`, not run in CI), GHCR (`ghcr.io/tkcs-lab/knowledge-hub`).
- The admin panel adds no new component and no new Python dependency (`pyproject.toml` only gained `panel/static/*` as package data). Its container needs Postgres, the hub at `KH_PANEL_HUB_URL` (`/healthz`), Forgejo, Ollama and S3 (for `setup plan` and `kh check`) and egress to the OIDC issuer.

**Scheduled jobs** (this repository ships no timers; the operator's stack provides them): `kh daily` once a day; `kh setup plan` as a drift alarm (alert on exit 1 or 2); `kh setup apply --only forgejo` every 15 minutes so directory joiners and leavers reach the Forgejo teams; the `knowledge-hub-directory` service (`kh directory sync --every 300`); `deploy/backup-kh.sh`; and an operator job that creates the Forgejo account of every new directory person.

**Forgejo accounts of directory people.** The login is the lower-case local part of the email, matching `[a-z0-9][a-z0-9_.-]{0,38}` (`directory/rules.py` `login_from_email`). `kh setup` never creates users. Until the account exists `kh setup plan` prints `i account <login>: no Forgejo account yet; not added to its teams`; an account whose email differs from the directory email prints `! account <login>: email differs from directory; not added` (a `!` line, so apply exits 2) (`setup/forgejo.py`).

**Restarts.** `kh serve` exits with code 75 within `KH_CONFIG_WATCH_SECONDS` plus a debounce after a valid change of `kh.yaml` (not of `admins` or comments); the compose `restart: unless-stopped` brings it back. `kh directory sync --every` exits 75 as well but polls the watcher once per sleep and debounces over two polls, so it restarts one to two sync intervals (5 to 10 minutes at 300) after the change (`directory/sync.py`, `config_watch.py`); `KH_CONFIG_WATCH_SECONDS` only switches that watch on (above 0). With `directory.provider: none` the sync exits 0 with a note and `unless-stopped` restarts it in a loop: start the service only when a provider is set.

### 3.2 First-deploy order for a directory-backed install

On an empty database `kh directory sync --once` cannot run first: the role `kh_indexer`, the database and `kh.directory_snapshot_put` (migration 0006) do not exist yet. The order the guide documents:

1. Components running (Postgres, Forgejo, S3, Ollama), Forgejo organisation and the two accounts, secret files, `kh.yaml`, and **the Ollama model pulled by the one-off container of the guide**. Ollama sits on the internal `kh` network and cannot download the model; without it the setup `ollama` step raises, the later steps are skipped and `kh setup apply` exits 2 (`setup/ollama.py`, `setup/steps.py`).
2. `kh setup plan --only postgres` (expect `+` lines for the roles and the database, exit 1). A full `kh setup plan` on an empty server exits 2: the labels step cannot connect to the missing database and prints `setup: labels: <error>` (`setup/labels.py`).
3. With a directory provider: `kh setup apply --only postgres`, then `kh directory sync --once`, then `kh setup apply`. Without a provider: `kh setup apply`.
4. `kh setup plan` again: only `i` lines, exit 0.

A plain `kh setup apply` with a provider set and no snapshot still runs every later step (a `!` line does not stop them, `setup/steps.py`) and ends with `! directory: snapshot missing` and exit 2; directory-only members are then left out of the Forgejo teams until the next apply. So the guide recommends the three-command order and states what the plain order does.

### 3.3 Corrections in `docs/runbook.md` (p2)

1. Delete the sentence "This is a breaking release for a fresh install only ..." (line 5); the guide's Upgrade section keeps one line: a database that holds a migration this image does not ship is refused by `kh migrate` and the postgres step with `database predates the baseline; a fresh install is required` (`db/migrate.py`; `kh serve` itself checks nothing), which is also what an older image says after migration 0006 (hence `deploy/rollback-0006.sql`).
2. "Install order" (steps 1 to 9) becomes a two-line pointer to the guide's "First deploy". The wrong sentence about `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE` goes with it.
3. Delete "Live probe of ma1", "Rollout of move_item" and "Rollout of supersede and outdated".
4. "Backup and restore": `deploy/backup-kh.sh` dumps `kh.records` and mirrors the blob bucket. **Not dumped and not rebuildable:** `kh.api_keys` (every hub key; create them again and switch each n8n credential), `kh.oauth_*` (every Claude connector signs in again), `kh.record_moves` (a re-sent `ingest_record` of a moved record is accepted again), `kh.usage_events` (lost; "Promotion agent" already says so). Rebuildable: documents, chunks, groups, labels, jobs, `kh.repo_state` and `kh.directory_snapshot` (next sync); `kh.index_alerts` are lost and reappear when the files are read again. Restore step 1 becomes `kh setup apply --only postgres` when a directory provider is set (a plain apply ends with `! directory: snapshot missing` and exit 2), and restore step 4 becomes: if a directory provider is set run `kh directory sync --once` before `kh setup apply`.
5. "Joiners and leavers": say the Forgejo login of a directory person is the email local part and link to the guide's "Scheduled jobs" instead of "the stack's converge" and "a 15-minute timer".
6. "Releasing" step 6 (roll out) becomes a link to the guide's "Upgrade".
7. "Accepted risks" gains: backups omit hub keys, OAuth state and move tombstones.
8. "Admin panel": add that the panel does not mount `KH_SETUP_SLACK_WEBHOOKS_FILE`, so a panel change on a group that has `slack_channel` ends with `! slack hook <repo>: no URL for group <g> in KH_SETUP_SLACK_WEBHOOKS_FILE` and exit 2 unless the file is mounted (`setup/forgejo.py`); that the host directory of `./config` must be writable by the container's user because the panel writes a lock file and a temporary file next to `kh.yaml` (`admin/yamledit.py`); and that the directory sync service restarts too, later (section 3.1, Restarts).
9. Line 3: add that where each command runs is in the guide's "Where each command runs".

### 3.4 Corrections in `docs/runbook-identity.md` (p2)

1. "What to set": the paragraph "OAuth is on only when ..." says: all four of `KH_PUBLIC_URL`, `KH_OIDC_ISSUER`, `KH_OIDC_CLIENT_ID`, `KH_OIDC_CLIENT_SECRET_FILE` set means on, none means off, some but not all exits 2 with `KH_OIDC_* and KH_PUBLIC_URL must be set together`; `KH_OIDC_ALLOWED_DOMAINS` is then required too and `KH_PUBLIC_URL` is `https://host` without a path. The claim that `kh serve` logs `auth paths: oauth=on keys=on dev_identity=off` is deleted: the line is an INFO message on a logger no code configures for `kh serve`, so it is not printed. The check is the `/.well-known/oauth-authorization-server` document (already step 1 of "Check it").
2. Directory, Google step 4: `KH_DIRECTORY_GOOGLE_SUBJECT` is required, not optional (`directory/google.py`). Step 5: an approver who is not a member is skipped with an INFO log line `directory: skip ...` and is listed by `kh directory plan`, not "with a warning".
3. Keycloak step 1: also set `KH_DIRECTORY_KEYCLOAK_REALM` and `KH_DIRECTORY_KEYCLOAK_CLIENT_ID` (`kh-directory`); the URL is `https` unless `KH_ENV` is `dev` or `test` or the host is a single-label name on the internal admin network (`directory/keycloak.py`).
4. "The sync service": on a first deploy the order is `kh setup apply --only postgres`, `kh directory sync --once`, `kh setup apply` (link to the guide); the plain order ends with `! directory: snapshot missing` and exit 2. The sync's egress: Google `oauth2.googleapis.com` and `admin.googleapis.com`; Keycloak only the internal admin URL. `--every N` is the sync interval (60 to 3600) and does not read `directory.refresh_seconds`; the stale alert `2 * refresh_seconds` holds only while `--every` equals `refresh_seconds` (compose uses 300). The sync restarts itself with exit code 75 after a valid `kh.yaml` change (section 3.1, Restarts).
5. "Check it": `kh directory plan|people|show` and `kh key` need the migrator password, so they run in the `knowledge-hub` container; `kh directory sync` runs in the sync container.
6. "Stale snapshot": the gauge `kh_directory_snapshot_age_seconds` has no series while no snapshot exists or the provider is `none`, so alert on the gauge above `2 * refresh_seconds` **or absent**.
7. "Joiners and leavers": Forgejo login rule, the two `account` lines of section 3.1, and a link to the guide's "Scheduled jobs" for the 15-minute timer.
8. "Hub keys", Create: `--out FILE` writes the line `key: khk_<id>.<secret>`; the bearer value is the part after `key: `, so strip the prefix before putting it in an n8n header credential.
9. "Moving from PrivacyFence", step 1 rollback: the command assumes a Postgres service named `postgres`; say that the compose example has none and the name is the operator's.

### 3.5 Corrections in the reference docs (p3)

`docs/configuration.md` (locate each by its quoted text):

1. Section "Sign-in and OAuth": delete the sentence that `kh serve` logs `auth paths: ...`; say it is not printed and the check is the metadata document.
2. Table "Database": `KH_DB_MIGRATOR_PASSWORD_FILE` is used by `kh migrate`, `kh setup`, the admin commands, `kh key` and `kh directory plan|people|show`, the panel health check and `deploy/backup-kh.sh`; `KH_DB_INDEXER_PASSWORD_FILE` also by `kh directory sync` and the panel.
3. `KH_CONFIG_WATCH_SECONDS`: for `kh directory sync --every` the value only switches the watch on (above 0); the restart follows one to two sync intervals later, not after this many seconds.
4. `kh key create`: add that `--out FILE` writes `key: khk_<id>.<secret>`.
5. `kh rebuild`: `--queue` needs no answering Ollama or Forgejo server, but `KH_OLLAMA_URL` must be set when `KH_EMBED_PROVIDER` is `ollama` (and `KH_FORGEJO_INDEXER_TOKEN_FILE` when `KH_FORGEJO_URL` is set), because the commands build the same dependencies as `kh serve`.
6. `kh daily` row: it prints `stale flags changed: N; identity rows purged: N; usage events purged: N`.
7. `directory.provider`: an unknown name is refused by `kh directory sync` (exit 2, `directory: unknown provider <x>`); `plan`, `people` and `show` print normally and `kh serve` treats a snapshot of another provider as missing (static membership only).
8. `directory.refresh_seconds`: it does not set the sync interval (`--every N` does); it only bounds `max_stale_seconds` and feeds the panel health check.
9. The `kh.yaml` key table has a blank line before the `tech_org` row that detaches `tech_org` and `tech_repos` from the table: remove that blank line.
10. The sentence that the hub restarts after "a valid change to `people` or a group": any valid change except `admins` and comments restarts it.

`docs/design.md`: the "Rejected alternatives" row "The SDK's `auth=` hook" loses the claim-header reason and says: the hub mounts the SDK's OAuth routes and verifies bearers in its own middleware, so the SDK hook adds nothing. The role bullet "`kh_indexer` (worker, rebuild, daily, requeue)" gains "the directory sync and the panel's pipeline operations". The "Public surface" bullet says `kh daily` purges expired OAuth rows and recomputes stale flags. The admin-panel sentence "All SQL runs as `kh_indexer` in a read-only transaction" becomes: the panel reads and writes as `kh_indexer` (stats, health reads, pipeline operations) and connects as `kh_migrator` only for the `migrations` health check; add that the statistics queries run in read-only transactions with a 5 s statement timeout (`panel/stats.py`). The per-operation audit line is printed to stdout (`panel/runner.py`); the per-request line goes to the logger `knowledge_hub.panel.audit` (`panel/app.py`), no handler is installed for it, so it is not printed by default.

`CHANGELOG.md`: `[Unreleased]` has two `### Added` blocks and two bullets after `### Security`. Merge them into one `### Added` (all bullets kept verbatim, in order), keep the bullets under `### Removed` and `### Security` as they are. Duplicate bullets stay (the release playbook rewrites the section; deleting statements here risks losing facts). Add one `### Changed` bullet for this change.

### 3.6 What the tests pin (a worker must not break them)

`tests/unit/test_deploy_files.py`: in the three kh services every environment key containing `PASSWORD`, `TOKEN`, `KEY`, `HMAC`, `SECRET` or `CREDENTIALS` ends in `_FILE` with a value under `/run/secrets/`; no text anywhere in the file (comments included) matches `(password|token|secret)\s*[:=]\s*["']?[A-Za-z0-9+/]{16,}`; the networks are exactly `db`, `kh`, `egress` and `ollama` has no `egress`; the directory service has the command `["directory","sync","--every","300"]`, networks `db` and `egress`, `read_only`, `cap_drop: [ALL]` and exactly the secrets `pg_kh_indexer`, `kh_directory_google_key`, `kh_directory_keycloak_secret`; the literal text `KH_OIDC_CLIENT_SECRET_FILE: /run/secrets/kh_oidc_client_secret` appears in the file (today only in a comment: do not edit that comment line); the hub image matches `ghcr.io/tkcs-lab/knowledge-hub:\d+\.\d+\.\d+`; the panel has the command `["panel","serve","--port","8081"]`, no `ports`, the single volume `./config:/etc/kh` (no `:ro`), no `KH_OIDC_*` or `KH_PUBLIC_URL`, and the seven `KH_PANEL_*` keys; `kh.example.yaml` loads with groups `all` and `mgmt`, two labels and a non-empty `admins`. `tests/unit/test_docs_links.py`: relative links resolve and every non-plan `.md` has at most 400 lines.

## 4. Manual steps

Nothing is needed before implementation. The item below happens after the last merge.

### Before implementation

None.

### After implementation

**ma1-walk-the-guide: follow `docs/deployment.md` on a real environment (about 2 hours).** Needs Postgres 16 with pgvector, Forgejo, an S3 store, Ollama and (for the directory part) a Google or Keycloak test setup.

1. The features the guide describes are unreleased: in a checkout of the branch `docs/docs-refresh` build an image with `docker build -t knowledge-hub:test .` and point the three `image:` lines of `deploy/compose.example.yaml` at it.
2. Open `https://github.com/tkcs-lab/knowledge-hub/blob/docs/docs-refresh/docs/deployment.md`.
3. Follow "First deploy" step by step on an empty database; at each step compare the output with the guide.
4. Do "Verify" and "Upgrade" once each.
Report back: pass/fail per section, and every step whose output differs from the guide.

## 5. Risks and open questions

1. **Facts stated without a check.** Open research (not asked, not blocking): the minimum Forgejo version for the fields `kh setup` writes, and which Garage versions accept the hub's path-style requests. The guide says `not pinned` for both and names `kh setup plan` and the setup `s3` step as the checks. A worker who is tempted to write a version number that appears in none of `ci.yml`, `Dockerfile`, `pyproject.toml` or the compose example stops with `status=blocked`.
2. **A cited code fact differs from the plan** (the code changed since `ac81c9d`). Each brief says: grep the cited symbol first; if it does not say what the plan says, stop with `status=blocked` and name the file and symbol.
3. **Docs that drift again.** The guide points to headings by name, never to step numbers (the old "install step 5" was already wrong), and each secret's meaning stays in `docs/configuration.md`.
4. **Backup gap.** `deploy/backup-kh.sh` and `tests/unit/test_deploy_files.py` (which pins `records` only) leave hub keys, OAuth state and move tombstones out. Follow-up for the owner: decide whether to dump `kh.api_keys` (key hashes are sensitive), `kh.oauth_*` and `kh.record_moves`.
5. **`kh serve` prints no INFO log.** Fixing the docs removes a false claim; the real fix (configure logging in `kh serve`) is a code change and a follow-up. The same gap hides the INFO line `exiting with code 75` and the per-request panel audit line.
6. **Compose image tag** stays behind the code until the next release. After that release open a follow-up issue to bump the tag on the three services; it cannot be a manual step here because the release is cut after this change merges. The example file says so in a comment.
7. **Duplicate bullets in `[Unreleased]`** stay; the release step rewrites the section.

## Implementation manifest

```yaml
plan_slug: docs-refresh
feature_branch: docs/docs-refresh
tracking_issue: 65
max_parallel: 2
manual_after:
  - id: ma1-walk-the-guide
    title: Follow docs/deployment.md on a real environment
    why: Only a real Postgres, Forgejo, S3, Ollama and identity provider show whether the documented order and the quoted outputs are right; CI cannot run them.
verify_after_merge:
  - ".venv/bin/python -m pytest -q -k docs_links"
  - ".venv/bin/python -m pytest -q -k deploy_files"
final_checks:
  - "`tld plan deviations check --file docs/plans/65/deviations.md --plan docs/plans/65/plan.md --all` exits 0"
  - "the changelog has entries under ## [Unreleased] and no new version heading"
phases:
  - id: p1-deployment-guide
    title: New deployment guide, README links, compose and example comments
    depends_on: []
    complexity: M
    touches:
      - docs/deployment.md
      - README.md
      - deploy/compose.example.yaml
      - deploy/kh.example.yaml
    brief: |
      Goal: write the deployment guide and link it from the README. Docs only: change no code, no test, no script, and do not edit deploy/ (the next phase owns the example files).
      Rules: read plan sections 3.0, 3.1 and 3.2 of docs/plans/65/plan.md first. Every fact there cites a symbol; before you write a fact, grep that symbol in src/ (for example `grep -n "login_from_email" src/knowledge_hub/directory/rules.py`). If the code says something different, stop with status=blocked and name the file and symbol. Never write a version number that is not in ci.yml, Dockerfile, pyproject.toml or deploy/compose.example.yaml. Point to other docs by file link and heading name, never by step number. A doc starts with `# Title` and a summary of at most 3 lines. No real secret, host or address (use example.com). Do not edit docs/runbook.md, docs/runbook-identity.md, docs/configuration.md or docs/design.md (later phases own them); do not edit docs/plans/.

      1. Create docs/deployment.md with exactly these `##` headings in this order and no other `##` heading: `## What you deploy and what you bring`, `## Dependencies`, `## Networks, hosts and public paths`, `## Secrets`, `## Where each command runs`, `## Scheduled jobs`, `## First deploy`, `## Verify`, `## Upgrade`, `## Backup and restore`. Keep it at most 330 lines.
      2. `## What you deploy and what you bring`: the four services of deploy/compose.example.yaml (knowledge-hub, knowledge-hub-directory, knowledge-hub-panel, ollama) and what the operator brings (Postgres, Forgejo, S3 store, a proxy, timers, Forgejo accounts). State that the guide is a contract with that stack: this repository cannot test how the operator runs it. One ASCII component picture of at most 15 lines.
      3. `## Dependencies`: one table with the columns component, version, where pinned, how verified, with the rows of plan section 3.1 "Components and dependencies" (Python, Python libraries, PostgreSQL, Forgejo, Ollama, S3, OIDC provider, reverse proxy, n8n, Slack, backup tools, GHCR) and the admin panel sentence under the table. Use the literal text `not pinned` and `not verified` where the plan says so. State that the image is the supported artefact and a wheel install takes current dependency versions.
      4. `## Networks, hosts and public paths`: the three networks (`db` and `kh` internal, `egress` not internal) and which service sits on which (read deploy/compose.example.yaml); the egress hosts: hub sign-in for Google `accounts.google.com`, `oauth2.googleapis.com`, `www.googleapis.com`; directory sync for Google `oauth2.googleapis.com` and `admin.googleapis.com`, for Keycloak only the internal admin URL; the panel needs egress to the OIDC issuer. Public paths: link to docs/runbook-identity.md "Public paths" (do not copy the list). The panel is never published on the internet: private network or access proxy.
      5. `## Secrets`: one table, columns secret file, variable, mounted by, needed for. Rows from deploy/compose.example.yaml `secrets:` and each service's `secrets:` list (the three database passwords, bot token, indexer token, webhook HMAC, S3 access and secret key, cursor key, metrics token, OIDC client secret, Google directory key, Keycloak directory secret, setup Postgres admin DSN, setup Forgejo admin token, panel OIDC client secret, panel session key) plus `KH_SETUP_SLACK_WEBHOOKS_FILE` (not mounted by any example service; needed by `kh setup` and by the panel when a group has `slack_channel`). State: a mounted secret file must exist or compose fails to start the service; for the directory provider you do not use, an empty file is enough because only `directory.provider` decides which credential is read (only the provider named by `directory.provider` is built, `build_provider` in `directory/provider.py`; check with `grep -n "def build_provider" src/knowledge_hub/directory/provider.py`; if it builds both providers, stop blocked). What each variable means stays in docs/configuration.md: link, do not copy.
      6. `## Where each command runs`: one table, columns command, container, database role, extra secrets, from plan section 3.1 "Database role per command". Add the sentence that `kh setup` needs secrets the `knowledge-hub` service never mounts (the setup Postgres admin DSN and the setup Forgejo admin token), so it runs in a one-off container or in the panel container.
      7. `## Scheduled jobs`: the jobs of plan section 3.1 "Scheduled jobs", the Forgejo-account paragraph (login rule and the two `account` lines, quoted exactly), and the Restarts paragraph (exit code 75 for `kh serve` and for the sync, the two-intervals delay, the `provider: none` loop). State that this repository ships none of these timers.
      8. `## First deploy`: numbered steps in the order of plan section 3.2. Step 1: the components running, the Forgejo organisation `org` and the two accounts `bot_login` and `indexer_login` (neither an org owner nor a site admin), the secret files, `kh.yaml` from deploy/kh.example.yaml placed in `./config/`, and the Ollama pull one-off container (copy the `docker run` command from the "Install order" step "Ollama" of docs/runbook.md; say that it runs before the first setup because Ollama sits on the internal `kh` network and cannot download the model, and that otherwise the setup `ollama` step fails and `kh setup apply` exits 2; add that until the model is available embedding fails and `kh rebuild` and `kh requeue-embeddings` follow, and the `bench_embed.py` line). Steps 2 to 4: as plan section 3.2 (`kh setup plan --only postgres`; apply in the directory or plain order; `kh setup plan` again). Then, in order: start `kh serve` (and the sync service when a provider is set, and the panel when wanted: link to docs/runbook.md "Admin panel" for its first start), `kh rebuild` then `kh check` expecting exit 0, then identity: link to docs/runbook-identity.md. Name each command's expected result (`+` lines and exit 1 for `kh setup plan --only postgres`, while a full `kh setup plan` on an empty server exits 2 with `setup: labels: <error>`; `i` lines and exit 0; `/healthz` answers `ok`; the worker starts only when `KH_FORGEJO_URL` is set).
      9. `## Verify`: the checks of the old "Live probe of ma1" section of docs/runbook.md, without the id `ma1`: second `kh setup plan` shows only `i` lines; in Forgejo Settings, Branches of a repo: direct push off, merge whitelist exactly `<group>-approve`, admins bound; merge a `propose_doc` pull request as an approver and find the document with `search`; a listed person reads only their groups; a person whose sign-in email is in neither `people` nor the directory reads nothing; add a third group, run apply, the first two groups are untouched; any `+`, `~` or `!` line after apply is a bug. Add the search-floor calibration pointer: link to docs/runbook.md "Calibrating the search similarity floor".
      10. `## Upgrade`: (a) all kh services run the same image tag; (b) `kh serve` does not check the schema, so run `kh setup apply --only postgres` (or `kh migrate`) with the new image before restarting; (c) restart serve, the sync service and the panel; (d) `kh setup plan` expects exit 0, then `kh check`; (e) one line: `kh migrate` and the postgres step refuse a database that holds a migration the image does not ship with `database predates the baseline; a fresh install is required` (`kh serve` itself checks nothing), which is also what an older image says after migration 0006 (rollback: docs/runbook-identity.md "Moving from PrivacyFence"); (f) the identity release: link to docs/runbook-identity.md "Moving from PrivacyFence"; (g) without Docker: `pip install knowledge_hub-X.Y.Z-py3-none-any.whl` then `kh setup plan --config kh.yaml`; the wheel does not pin its dependencies. Pin the digest: `docker buildx imagetools inspect ghcr.io/tkcs-lab/knowledge-hub:X.Y.Z`; there is no `latest` tag.
      11. `## Backup and restore`: two sentences: the backup is `deploy/backup-kh.sh` and the restore, with what the dump does not contain, is in docs/runbook.md "Backup and restore" (link to the file, not an anchor).
      12. README.md: in "Documentation" add as the first bullet `- [Deployment guide](docs/deployment.md): components and dependencies, secrets, where each command runs, scheduled jobs, first deploy, verify and upgrade.`; change the Runbook bullet text to `routine tasks, alerts, moves, restore, rotation and releasing.`; in the `kh serve` command row list the routes as `/mcp`, the OAuth paths, `/webhooks/forgejo`, `/healthz` and `/metrics` when `KH_METRICS_TOKEN_FILE` is set; after the first paragraph add one sentence: `It needs PostgreSQL 16 with pgvector, Forgejo, an S3-compatible store, Ollama and an OIDC provider; see the [deployment guide](docs/deployment.md).`
      13. Run `.venv/bin/python -m pytest tests/unit/test_docs_links.py -q` and `.venv/bin/python -m ruff check .`. Report `docs-changed: added docs/deployment.md` in your report.
      Stop conditions: a cited symbol does not say what the plan says (blocked, name file and symbol); docs/deployment.md would exceed 330 lines (blocked, say which section is too long).
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_docs_links.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
      - "grep -c '^## ' docs/deployment.md prints 10"
      - "grep -n '^## First deploy$' docs/deployment.md matches one line"
      - "wc -l docs/deployment.md prints a number of at most 330"
      - "grep -n 'deployment.md' README.md matches at least two lines"
  - id: p1b-example-files
    title: Correct the compose example and kh.example.yaml comments and tags
    depends_on: [p1-deployment-guide]
    complexity: S
    touches:
      - deploy/compose.example.yaml
      - deploy/kh.example.yaml
    brief: |
      Goal: make the two example files agree with docs/deployment.md (written by the previous phase). Comments and the three image tags only: change no key, no secret name, no network, no command, no volume, no test, no script.
      Rules: read plan sections 3.0 item 6 and 3.6 of docs/plans/65/plan.md first. docs/deployment.md has the heading `## First deploy` (check with `grep -n '^## First deploy$' docs/deployment.md`). Never edit the commented line `# KH_OIDC_CLIENT_SECRET_FILE: /run/secrets/kh_oidc_client_secret`: a test needs that text. No comment may contain the word `password`, `token` or `secret` followed by `:` or `=` and 16 or more letters or digits (a test scans the whole file).

      deploy/compose.example.yaml:
      1. Change `ghcr.io/tkcs-lab/knowledge-hub:0.1.0` to `ghcr.io/tkcs-lab/knowledge-hub:0.5.1` on the three `image:` lines (services `knowledge-hub`, `knowledge-hub-directory`, `knowledge-hub-panel`) and in the comment line `#   docker buildx imagetools inspect ghcr.io/tkcs-lab/knowledge-hub:0.1.0`.
      2. Directly above the comment line `    # The release image (linux/arm64). Pin the digest in the stack:` insert, with the same indent, the line `    # The directory sync, the panel, OAuth and the admins and directory keys of kh.yaml need the first release after 0.5.1 (CHANGELOG [Unreleased]); 0.5.1 refuses this example's kh.yaml.`
      3. Replace the text `(docs/runbook.md, install step 5)` by `(docs/deployment.md, section "First deploy")`.
      4. In the `knowledge-hub-directory` service environment, change the comment line `      # KH_DIRECTORY_GOOGLE_SUBJECT: "kh-directory-reader@example.com"` to `      # KH_DIRECTORY_GOOGLE_SUBJECT: "kh-directory-reader@example.com"   # required for the google provider`, and after the line `      KH_DIRECTORY_KEYCLOAK_CLIENT_SECRET_FILE: /run/secrets/kh_directory_keycloak_secret` add the three lines `      # KH_DIRECTORY_KEYCLOAK_URL: "http://keycloak:8080"`, `      # KH_DIRECTORY_KEYCLOAK_REALM: "example"` and `      # KH_DIRECTORY_KEYCLOAK_CLIENT_ID: "kh-directory"`.
      5. Directly above the line `  knowledge-hub-directory:` (after the existing comment block of that service) add the comment line `  # With directory.provider: none the command exits 0 and restart: unless-stopped restarts it in a loop, so start it only when a provider is set.`
      6. In the `knowledge-hub-panel` environment, directly above the line `      KH_SETUP_PG_ADMIN_DSN_FILE: /run/secrets/kh_setup_pg_admin_dsn` add the comment line `      # Set KH_SETUP_SLACK_WEBHOOKS_FILE and mount it here too when a group has slack_channel; without it a panel change on such a group ends with a ! slack hook line and exit 2.`
      7. Replace the comment line `      # Read-write and a directory: the commit renames a file and keeps a lock file next to it.` by `      # Read-write and a directory (the panel writes a lock file and a temporary file next to kh.yaml): the host directory must be writable by the container user, uid 10001.`

      deploy/kh.example.yaml:
      8. Change the comment `# non-empty subset of members` of `approvers` to `# subset of members; may be empty only when the group has directory approvers`.

      9. Run `.venv/bin/python -m pytest tests/unit/test_deploy_files.py -q`.
      Stop conditions: a quoted line is not found (blocked, name it); `test_deploy_files.py` fails after your edits (revert the edit that broke it; do not edit the test; if you cannot, blocked).
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_deploy_files.py -q passes"
      - "grep -c 'knowledge-hub:0.1.0' deploy/compose.example.yaml prints 0"
      - "grep -c 'knowledge-hub:0.5.1' deploy/compose.example.yaml prints 4"
      - "grep -c 'install step 5' deploy/compose.example.yaml prints 0"
      - "grep -n 'section \"First deploy\"' deploy/compose.example.yaml matches one line"
      - "grep -n 'may be empty only when the group has directory approvers' deploy/kh.example.yaml matches one line"
  - id: p2-runbooks
    title: Correct the operations runbook and the identity runbook
    depends_on: [p1-deployment-guide]
    complexity: M
    touches:
      - docs/runbook.md
      - docs/runbook-identity.md
    brief: |
      Goal: make both runbooks true and short, with the install and upgrade material living only in docs/deployment.md (already written by the previous phase; read its headings first with `grep -n '^## ' docs/deployment.md`).
      Rules: read plan sections 3.1, 3.3 and 3.4 of docs/plans/65/plan.md. Locate every edit by the quoted text or heading, not by line number. Before you write a fact, grep its cited symbol in src/; if the code says something different, stop with status=blocked and name file and symbol. Link to docs/deployment.md by file and heading name, never by step number. Do not edit anything but the two runbooks.

      docs/runbook.md:
      1. Delete the paragraph that starts `This is a breaking release for a fresh install only`.
      2. Replace the whole `## Install order` section (steps 1 to 9, including the Ollama `docker run` block) by one paragraph: the install is in [deployment guide](deployment.md), section "First deploy"; day-two work starts here. Keep the `## Sign-in, directory and keys` section after it.
      3. Delete the sections `### Live probe of ma1`, `### Rollout of move_item` and `### Rollout of supersede and outdated` completely. Keep `### Calibrating the search similarity floor`.
      4. Intro paragraph (line 3): after the sentence that commands run as the `kh` user inside the container, add `Which container runs which command, and with which secrets, is in the [deployment guide](deployment.md), section "Where each command runs".`
      5. `### Joiners and leavers`: replace `(the next `kh setup apply --only forgejo`, normally a 15-minute timer)` by `(the next `kh setup apply --only forgejo`, an operator-provided timer, see the [deployment guide](deployment.md), section "Scheduled jobs")` and add the sentence: `A directory person needs a Forgejo account whose login is the lower-case local part of their email and whose email equals the directory email; until then `kh setup plan` prints an `account` line (see [runbook-identity](runbook-identity.md)).`
      6. `## Backup and restore`: rewrite the first paragraph to: documents and chunks are rebuilt from Forgejo, groups and labels from `kh.yaml`, the directory snapshot by the next sync; `deploy/backup-kh.sh` writes `kh-t2t3-<date>.dump` (custom format, INSERT statements) of `kh.records` and mirrors the blob bucket into `<BACKUP_DIR>/blobs/`; back both up off the box (restic). Then add a bold paragraph `**Not in the backup and not rebuildable.**` with the four losses of plan section 3.3 item 4, each naming its table, and what to do for each (`kh.api_keys`: create the keys again with `kh key create` and switch every n8n credential; `kh.oauth_*`: every Claude connector signs in again; `kh.record_moves`: do not re-send a record that was moved, a re-sent record is accepted again; `kh.usage_events`: lost). In the restore steps change step 1 to `kh setup apply --only postgres` when `directory.provider` is set (a plain apply ends with `! directory: snapshot missing` and exit 2), and change step 4 to: if `directory.provider` is set run `kh directory sync --once`, then `kh setup apply`, then `kh rebuild`. Keep the other steps and the Garage sentence.
      7. `## Releasing`: replace step 6 (`To roll out, ...`) by `To roll out, follow [deployment guide](deployment.md), section "Upgrade".`
      8. `## Accepted risks`: add the bullet `- **Backups omit hub keys, OAuth state and move tombstones.** See "Backup and restore"; the dump holds `kh.records` only.`
      9. `## Admin panel`: add three sentences at the end of "How a change reaches the hub": `The directory sync service restarts itself the same way, but later: it polls the file once per sync interval and waits two polls, so one to two intervals (5 to 10 minutes at `--every 300`) after a valid change.`; `The panel container does not mount `KH_SETUP_SLACK_WEBHOOKS_FILE` in the example: a change on a group that has `slack_channel` then ends with `! slack hook <repo>: no URL for group <g> in KH_SETUP_SLACK_WEBHOOKS_FILE` and exit 2 until you mount the file.`; `The host directory of `./config` must be writable by the container user, because the panel writes a lock file and a temporary file next to `kh.yaml`.`

      docs/runbook-identity.md:
      10. `## Sign-in and OAuth`, paragraph starting `OAuth is on only when`: replace it by: `OAuth is on when all four of `KH_PUBLIC_URL`, `KH_OIDC_ISSUER`, `KH_OIDC_CLIENT_ID` and `KH_OIDC_CLIENT_SECRET_FILE` are set and off when none is; some but not all is a startup error (`KH_OIDC_* and KH_PUBLIC_URL must be set together`, exit 2). `KH_OIDC_ALLOWED_DOMAINS` is then required as well, and `KH_PUBLIC_URL` is `https://<host>` with no path. `kh serve` does not print a line that tells you which paths are on (that message is logged at INFO level and no logging is configured for `kh serve`); check the two metadata documents instead (see "Check it"). Leave `KH_DEV_IDENTITY` unset in production.`
      11. `## Directory`, Google step 4 (`Add domain-wide delegation`): after `Set `KH_DIRECTORY_GOOGLE_SUBJECT` to the reader address` add `(it is required for the Google provider)`. Step 5: replace `the approver is skipped with a warning` by `the approver is skipped (an INFO log line `directory: skip ...`, and `kh directory plan` lists it)`.
      12. Keycloak step 1: after `Put its secret in the file named by `KH_DIRECTORY_KEYCLOAK_CLIENT_SECRET_FILE`` add `, set `KH_DIRECTORY_KEYCLOAK_CLIENT_ID` to `kh-directory` and `KH_DIRECTORY_KEYCLOAK_REALM` to the realm name`; and after `set `KH_DIRECTORY_KEYCLOAK_URL` to the internal admin URL` add `(`https`, unless `KH_ENV` is `dev` or `test` or the host is a single-label name such as `keycloak` on the internal admin network)`.
      13. `**The sync service.**` paragraph: replace the last sentence (`On a first deploy run ...`) by: `On a first deploy the order is `kh setup apply --only postgres`, `kh directory sync --once`, then `kh setup apply` (the sync needs the roles and migration 0006 that the postgres step creates; see the [deployment guide](deployment.md), section "First deploy"). A plain `kh setup apply` with a provider set and no snapshot still runs every step but ends with `! directory: snapshot missing` and exit 2.` Then add: `The sync needs egress to `oauth2.googleapis.com` and `admin.googleapis.com` (Google) or only the internal admin URL (Keycloak). `--every N` (60 to 3600) is the sync interval; it does not read `directory.refresh_seconds`. After a valid change of `kh.yaml` the sync exits with code 75 and `restart: unless-stopped` restarts it, one to two intervals later (see [runbook](runbook.md), "Admin panel").`
      14. `**Check it.**` (the directory one): add as a first bullet: `Run `kh directory plan`, `people` and `show` in the `knowledge-hub` container (they read the snapshot with the migrator password); `kh directory sync` runs in the sync container. `kh key` also needs the migrator password.`
      15. `**Stale snapshot.**`: replace `Alert when the gauge `kh_directory_snapshot_age_seconds` is above `2 * refresh_seconds`.` by `Alert when the gauge `kh_directory_snapshot_age_seconds` is above `2 * refresh_seconds` (assuming `--every` equals `refresh_seconds`) or absent: the series has no value while no snapshot exists or the provider is `none`.`
      16. `**Joiners and leavers.**`: replace `when `kh setup apply --only forgejo` next runs (a 15-minute timer in the stack)` by `when `kh setup apply --only forgejo` next runs (an operator-provided timer, see the [deployment guide](deployment.md), section "Scheduled jobs")` and replace the last sentence by: `A directory person needs a Forgejo account whose login is the lower-case local part of the email and whose email equals the directory email; `kh setup` never creates it. Until it exists `kh setup plan` prints `i account <login>: no Forgejo account yet; not added to its teams`; an account with another email prints `! account <login>: email differs from directory; not added` (apply exits 2).`
      17. `## Hub keys`, Create bullet: after `so with `--out FILE` it goes to the file (mode 0600) instead` add `. The file holds the line `key: khk_<id>.<secret>`; the bearer value is the part after `key: `, so remove that prefix before you store it as the header credential`.
      18. `## Moving from PrivacyFence`, step 1 Rollback: after the `docker compose exec -T postgres psql ...` command add the sentence `The command assumes a Postgres service named `postgres`; the compose example has none, so use the name of your own.`
      19. Run `.venv/bin/python -m pytest tests/unit/test_docs_links.py -q`.
      Stop conditions: a cited symbol does not match (blocked); a quoted text to replace is not found (blocked, name it: the file changed since the plan); a doc would exceed 400 lines (blocked).
      Report `docs-changed: deleted-section Live probe of ma1` style lines only for files added, moved or deleted; this phase adds none.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_docs_links.py -q passes"
      - "grep -c 'Live probe\\|Rollout of' docs/runbook.md prints 0"
      - "grep -c 'breaking release' docs/runbook.md prints 0"
      - "grep -n 'section \"First deploy\"' docs/runbook.md matches"
      - "grep -n 'kh.api_keys' docs/runbook.md matches"
      - "grep -n 'kh setup apply --only postgres' docs/runbook-identity.md matches"
      - "grep -n 'KH_DIRECTORY_KEYCLOAK_REALM' docs/runbook-identity.md matches"
      - "grep -n 'no logging is configured' docs/runbook-identity.md matches"
      - "grep -n 'email differs from directory' docs/runbook-identity.md matches"
      - "grep -n 'after `key: `' docs/runbook-identity.md matches"
      - "wc -l docs/runbook.md prints a number of at most 400"
      - "wc -l docs/runbook-identity.md prints a number of at most 400"
  - id: p3-reference-retire
    title: Correct configuration, design and the changelog (retirement phase)
    depends_on: [p2-runbooks, p1b-example-files]
    complexity: M
    touches:
      - docs/configuration.md
      - docs/design.md
      - CHANGELOG.md
    brief: |
      Goal: bring the reference docs in line with the code and tidy the unreleased changelog. This is the retirement phase: it adds the changelog entry.
      Rules: read plan sections 3.1, 3.5 and 3.6 of docs/plans/65/plan.md. Locate every edit by quoted text. Before you write a fact, grep the symbol cited in the plan; if the code differs, stop with status=blocked and name file and symbol. Do not edit other files. docs/configuration.md has 76 lines of headroom under the 400-line limit.

      docs/configuration.md:
      1. In `### Sign-in and OAuth`, delete the sentence ``kh serve` logs `auth paths: oauth=<on|off> keys=on dev_identity=<on|off>`.` and put in its place: ``kh serve` does not print which paths are on (the message is logged at INFO level and no logging is configured); check `/.well-known/oauth-authorization-server`.`
      2. In `### Database`: `KH_DB_MIGRATOR_PASSWORD_FILE` meaning becomes `Password file for `kh_migrator` (`kh migrate`, `kh setup`, the admin commands, `kh key`, `kh directory plan|people|show`, the panel's `migrations` health check and `deploy/backup-kh.sh`). No file means no password.`; `KH_DB_INDEXER_PASSWORD_FILE` meaning becomes `Password file for `kh_indexer` (worker, rebuild, check, daily, requeue, `kh directory sync` and the panel).`
      3. In the `KH_CONFIG_WATCH_SECONDS` row replace the meaning by: `Seconds between two checks of `kh.yaml` by `kh serve` (0 to 3600; 0 switches the watch off). `kh serve` exits with code 75 after a valid change that is still the same at the next check, and the container's restart policy brings it back. For `kh directory sync --every` the value only switches the watch on (above 0): the sync checks once per interval and restarts one to two intervals after the change.` Keep the row's default and range as they are in the file, and check them with `grep -n "config_watch_seconds" src/knowledge_hub/settings.py`; if the default or range there differs from the row, stop blocked.
      4. In the `kh key create ...` command row add: `With `--out FILE` the file holds the line `key: khk_<id>.<secret>`; the bearer value is the part after `key: `.`
      5. In the `kh rebuild` command row (it mentions `--queue`): replace the claim that `--queue` needs neither Ollama nor the Forgejo token by: `--queue` needs no answering Ollama or Forgejo server, but `KH_OLLAMA_URL` must be set when `KH_EMBED_PROVIDER` is `ollama` (and `KH_FORGEJO_INDEXER_TOKEN_FILE` when `KH_FORGEJO_URL` is set), as for `kh serve`.
      6. In the `kh daily` row replace the description by: `Recompute the `stale` flags, purge expired OAuth rows and delete usage events past `KH_USAGE_RETENTION_DAYS`; it prints `stale flags changed: N; identity rows purged: N; usage events purged: N`.`
      7. In the `directory.provider` row replace the clause about `kh serve` refusing an unknown name by: `an unknown name is refused by `kh directory sync` with `directory: unknown provider <x>` and exit 2; `plan`, `people` and `show` print normally, and `kh serve` treats a snapshot of another provider as missing (static membership only).`
      8. In the `directory.refresh_seconds` row replace the meaning by: `60 to 3600. It does not set the sync interval (the argument of `kh directory sync --every N` does); it bounds `max_stale_seconds` and feeds the panel's `directory` health check.`
      9. In the `kh.yaml` key table remove the blank line that precedes the `tech_org` row, so that the `tech_org` and `tech_repos` rows belong to the table again.
      10. Replace the phrase `a valid change to `people` or a group` (end of the section before `kh setup`) by `a valid change to any key except `admins` (comments and formatting restart nothing)`.

      docs/design.md:
      11. In `## Rejected alternatives`, the row `The SDK's `auth=` hook`: replace the second cell by `Verifying the bearer needs the hub's own middleware (two credential types, the live membership); the hub mounts the SDK's OAuth routes and uses no `auth=` hook.`
      12. In `## Roles and RLS matrix`, the first bullet: replace `` `kh_indexer` (worker, rebuild, daily, requeue)`` by `` `kh_indexer` (worker, rebuild, daily, requeue, `kh directory sync` and the panel's pipeline operations)``.
      13. In `## Sign-in, OAuth and keys`, bullet `**Public surface.**`: the sentence about `kh daily` becomes `` `kh daily` purges expired OAuth rows and recomputes the stale flags.``
      14. In the admin-panel section replace the sentence `All SQL runs as `kh_indexer` in a read-only transaction` (find it with `grep -n "All SQL runs as" docs/design.md`) by: `The panel reads and writes as `kh_indexer` (statistics, health reads and the pipeline operations) and connects as `kh_migrator` only for the `migrations` health check.` Then append ` The statistics queries run in read-only transactions with a 5 s statement timeout.` (check with `grep -n "READ ONLY" src/knowledge_hub/panel/stats.py`; if absent, stop blocked). In the same section, where the audit line is described, say: ` The per-operation line is printed to stdout; the per-request line goes to the logger `knowledge_hub.panel.audit`, for which no handler is installed, so it is not printed by default.` (check with `grep -n "audit" src/knowledge_hub/panel/app.py src/knowledge_hub/panel/runner.py` and `grep -rn "basicConfig\|addHandler" src/knowledge_hub/panel`; if a handler exists, stop blocked).

      CHANGELOG.md, section `## [Unreleased]` only:
      15. Count the bullets before: `awk '/^## \[Unreleased\]/{f=1;next} /^## \[/{f=0} f&&/^- /' CHANGELOG.md | wc -l` (32 today). Move the single bullet of the second `### Added` and the two bullets after `### Security` (`kh repo list|add|set|remove` and `tech_repos.<r>.dirs`) to the end of the first `### Added` list, verbatim, then delete the second `### Added` heading and the now empty lines. Keep every other bullet and heading where it is.
      16. At the end of `### Changed` add the bullet: `- Documentation: new `docs/deployment.md` (components and dependencies, secrets, where each command runs, scheduled jobs, first deploy, verify and upgrade). The runbooks, the configuration reference, the design notes and the compose example are corrected against the code, and the one-off rollout notes of 0.4.0 and 0.5.0 are removed from the runbook.`
      17. The count after must be 33. Run `.venv/bin/python -m pytest tests/unit/test_docs_links.py -q`.
      Stop conditions: a quoted text is not found (blocked, name it); the bullet count is not 33; a doc would exceed 400 lines.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_docs_links.py -q passes"
      - ".venv/bin/python -m pytest -q -m unit passes"
      - "awk '/^## \\[Unreleased\\]/{f=1;next} /^## \\[/{f=0} f&&/^### Added/' CHANGELOG.md | wc -l prints 1"
      - "awk '/^## \\[Unreleased\\]/{f=1;next} /^## \\[/{f=0} f&&/^- /' CHANGELOG.md | wc -l prints 33"
      - "grep -c 'auth paths:' docs/configuration.md prints 0"
      - "grep -n 'stale flags changed: N; identity rows purged: N; usage events purged: N' docs/configuration.md matches"
      - "grep -n 'does not set the sync interval' docs/configuration.md matches"
      - "grep -c 'The claim is in its own header' docs/design.md prints 0"
      - "grep -n 'uses no `auth=` hook' docs/design.md matches"
      - "wc -l docs/configuration.md prints a number of at most 400"
      - "wc -l docs/design.md prints a number of at most 400"
      - "wc -l CHANGELOG.md prints a number of at most 400"
```
