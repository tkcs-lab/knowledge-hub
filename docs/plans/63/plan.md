# Admin panel in knowledge-hub

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** An administrator opens the panel's own address, signs in with the company account (the sign-in provider the hub already uses) and sees five tabs: Groups, Labels, Repos, Pipeline and Health. Every change to labels, groups, people and technical repos has Preview (a dry run that shows the `kh.yaml` diff) and Apply; the hub restarts itself within about 20 seconds. The Groups tab shows, per group, how much information it holds: documents by tier, records, chunks, stale and outdated counts and the last indexing time. The Pipeline tab can rebuild a repo, retry dead jobs and requeue embeddings.

**Scope:** Adds `kh panel serve`, a separate process in the same image, plus an `admins` key in `kh.yaml`, a `kh.yaml` watcher: `kh serve` and `kh directory sync --every` now exit with code 75 on a valid change (`KH_CONFIG_WATCH_SECONDS=0` turns it off) and `/healthz` gains a digest header; `kh rebuild --queue`; `--if-sha256` on the admin commands, whose restart note changes; and a `knowledge-hub-panel` compose service. It does not change the MCP tools, the schema (no migration) or tkcs-work-compose; that repository's kh tab is retired by a follow-up plan.

**Security:** The panel holds the setup admin credentials, so it is its own container and never part of `kh serve`. Sign-in is the upstream OIDC flow plus the `admins` list, re-read on every request. Writes run the existing `kh label|group|person|repo` commands as subprocesses with a strict allowlist; every free-text value is validated and passed after `--`.

**Performance:** Stats are a few `GROUP BY` queries in read-only transactions with a 5 s limit; nothing runs on the request path of `/mcp`.

**Review focus:** Running the panel next to the compose repo's kh tab loses `admins` and `tech_repos` (5); the sign-in cookies and the admin check (3.3); the subprocess write path and its argument hygiene (3.6); `kh serve` exiting with code 75 on a config change, with the debounce and drain (3.2); the rebuild-as-queued-job decision (3.7).

## 1. Goal

Tracking issue: https://github.com/tkcs-lab/knowledge-hub/issues/63.

The kh part of tkcs-work-compose (Knowledge hub and Groups tabs, a spool, a root applier that runs `docker compose run kh-admin` and restarts the hub) moves into knowledge-hub itself. The owner's scope is labels, groups, repos, health check, indexing pipeline and per-group information about the amount of information. The panel works wherever the kh image runs, with no host applier and no Docker socket.

## 2. Current state

- `kh label|group|person|repo` (`src/knowledge_hub/admin/*`) already edit `kh.yaml` with ruamel under `yamledit.locked()` (`admin/yamledit.py:79`), validate it, run the setup steps, print the diff, honour `--dry-run` and exit 2 on a refusal (`admin/apply.py:73-211`).
- `kh serve` reads `kh.yaml` once at start (`cli.py:148`, `app.py`), so every change needs a restart. `main` always returns 0 after `_serve` (`cli.py:167-173`).
- `kh rebuild` syncs and embeds in its own process (`indexer/rebuild.py:24-49`); the worker's claim query takes only `queued` and `failed` jobs under `MAX_ATTEMPTS` 5 (`indexer/worker.py:20-28`, `_finish` at 176-183); a job cut off while `running` is never reclaimed.
- `kh directory sync --every` loads the config once and loops (`directory/sync.py:106-125`, `directory/cli.py:58`).
- The OAuth settings validator needs `KH_PUBLIC_URL`, issuer, client id and secret file together (`settings.py:130-132`); `UpstreamClient` takes its redirect URI from `UpstreamConfig` (`oauth/upstream.py:44-52`).
- `kh_indexer` can read `groups`, `labels`, `records` and `directory_snapshot_get()` and can write `documents`, `chunks`, `index_jobs`, `repo_state`, `index_alerts` (`docs/design.md`, "Roles and RLS matrix").
- tkcs-work-compose: `roles/admin` (aiohttp, Cloudflare Access and password, `kh_spool.py`), `roles/kh_apply` (root applier). It is not changed here.

## 3. Design

### 3.1 Shape

New package `src/knowledge_hub/panel/`, new command `kh panel serve [--port 8081]` (dispatched lazily from `cli.main` like `setup`). One Starlette app on one uvicorn worker (`workers=1`: the job table and the lock are in memory). The panel never imports `knowledge_hub.app`, `knowledge_hub.mcp.*`, `knowledge_hub.admin.*` or `ruamel`; edits happen in subprocesses. A unit test enforces this (p7).

The panel container holds: the `kh_indexer` password (stats and pipeline), the migrator password (migration check), the Forgejo and Postgres admin credentials for `kh setup plan` and the admin commands, the OIDC client secret and a session key. `kh serve` keeps none of the admin credentials.

Names (`src/knowledge_hub/panel/`): `settings.py`, `context.py`, `session.py`, `auth.py`, `app.py`, `cli.py`, `stats.py`, `api_read.py`, `health.py`, `api_health.py`, `ops.py` (validation and argv), `pipeline_ops.py` (in-process ops), `runner.py` (subprocess, lock, job table), `api_ops.py`, `static/`.

### 3.2 Settings, `admins`, config watch

- `KhConfig.admins: list[str]`, lower-cased emails, one `@`, at most 254 characters, no duplicates, at most 50. Not editable through the panel and not in the op allowlist.
- `Settings.config_watch_seconds` (`KH_CONFIG_WATCH_SECONDS`, default 10, 0 to 3600, 0 switches the watch off).
- `PanelSettings` (`panel/settings.py`, class `PanelSettings(BaseSettings)`, `extra="ignore"`, `populate_by_name=True`) so the panel container leaves every `KH_OIDC_*` and `KH_PUBLIC_URL` unset and `Settings` still loads (finding of the architect review):

| Field | Variable | Rule |
|---|---|---|
| `env` | `KH_ENV` | `dev`, `test` or `prod`, default `prod` |
| `config_file` | `KH_CONFIG_FILE` | required |
| `public_url` | `KH_PANEL_PUBLIC_URL` | required unless `dev_admin` (the redirect URI needs it); `check_public_url(url, env, "KH_PANEL_PUBLIC_URL")` (moved out of `Settings`) |
| `oidc_issuer`, `oidc_client_id`, `oidc_client_secret_file` | `KH_PANEL_OIDC_ISSUER`, `KH_PANEL_OIDC_CLIENT_ID`, `KH_PANEL_OIDC_CLIENT_SECRET_FILE` | required unless `dev_admin` |
| `oidc_allowed_domains`, `oidc_hosted_domains` | `KH_PANEL_OIDC_ALLOWED_DOMAINS`, `KH_PANEL_OIDC_HOSTED_DOMAINS` | comma lists; allowed required unless `dev_admin`; hosted required for the issuer host `accounts.google.com` |
| `session_key_file` | `KH_PANEL_SESSION_KEY_FILE` | required unless `dev_admin`; `read_bytes().strip()` has at least 32 bytes |
| `session_hours` | `KH_PANEL_SESSION_HOURS` | 1 to 24, default 8 |
| `hub_url` | `KH_PANEL_HUB_URL` | default `http://knowledge-hub:8080` |
| `dev_admin` | `KH_PANEL_DEV_ADMIN` | an email; only when `env` is `dev` or `test` |

Error strings: `KH_PANEL_DEV_ADMIN is only allowed when KH_ENV is dev or test`; `<VARIABLE> is required` (first missing in the table order, `KH_CONFIG_FILE` first); `KH_PANEL_OIDC_HOSTED_DOMAINS is required for a Google issuer`; `KH_PANEL_PUBLIC_URL must be an https URL without a path`.

`config_watch.py`: `EXIT_RESTART = 75`; `config_digest(config) -> str` is the SHA-256 of `json.dumps(config.model_dump(mode="json", exclude={"admins"}), sort_keys=True)`, so comment-only, formatting-only and `admins`-only edits never restart anything; `ConfigWatcher(path, loaded, load=load_config)` with `poll() -> bool` that is True only when a valid config whose digest differs from the loaded one is seen on two consecutive polls (debounce), resets when the digest returns to the loaded one, and logs `config on disk is invalid; keeping the loaded config: <error>` once per distinct error text; `async watch(path, loaded, interval, on_change, *, sleep, load)` loops `poll`. `kh serve` runs `watch` when `config_file` is set and `config_watch_seconds > 0`; `on_change` sets `server.should_exit = True`; `_serve` returns 75; `main` returns it. The log line is `config changed on disk; exiting with code 75 so the container restarts`. The lifespan drain `wait_for(task, 10)` becomes `DRAIN_SECONDS = 60` so a running job finishes. `/healthz` keeps the body `ok` and gains the header `X-KH-Config-Digest: <digest of the config the hub loaded>`. `kh directory sync --every` polls the same watcher after each sleep and returns 75.

Restart policy: the compose example already uses `restart: unless-stopped`, which restarts after any exit. Every kh container must run the same image tag: an older image rejects `admins` (`extra="forbid"`).

### 3.3 Sign-in, session, request rules

- A second `UpstreamClient` with `UpstreamConfig(issuer, client_id, client_secret, allowed_domains, hosted_domains, redirect_uri=f"{public_url}/auth/callback", allow_http=env in (dev, test))`. `allow_http` follows the existing rule.
- `session.py`: `Signer(key: bytes)`; `sign(purpose, payload, ttl_seconds, now) -> str` = `b64url(json) + "." + b64url(HMAC-SHA256(subkey, b64url(json)))`; `verify(purpose, token, now) -> dict | None` (constant-time compare, checks `exp`); `subkey = HMAC-SHA256(key, b"kh-panel/" + purpose)`; purposes `flow` and `session`. The key is the content of `KH_PANEL_SESSION_KEY_FILE`, not the client secret.
- `GET /auth/login`: new `state`, `nonce` (`secrets.token_urlsafe(32)`), PKCE from `upstream.new_pkce()`; sets the flow cookie (`__Host-kh_panel_flow`, or `kh_panel_flow` when the public URL is not https; signed `{state, nonce, verifier}`; Max-Age 600; Path `/`; HttpOnly; Secure when https; SameSite Lax because the IdP redirect is a cross-site navigation); answers 302 to `upstream.authorization_url`. There is no `next` parameter (no open redirect).
- `GET /auth/callback`: verify the flow cookie, compare `state` with `hmac.compare_digest`, clear the flow cookie; on `error`, missing code, a bad cookie or any `UpstreamError` answer 403 with the fixed text `sign-in refused` (400 `invalid request` for a bad cookie or state); otherwise `tokens = await upstream.exchange(code, verifier)`, `id_token = tokens.get("id_token")` (a non-string raises `UpstreamError("id_token_missing")`), then `await upstream.verify_id_token(id_token, nonce)` (the pattern of `oauth/flow.py:97-101`; `authorization_url` is async too); load the config fresh (`ConfigError` gives 403 `panel unavailable: kh.yaml is invalid`); the lower-cased email must be in `config.admins` else 403 `not an administrator`. Success: a new session cookie (`__Host-kh_panel`, signed `{email, iat, exp}`, SameSite Strict, HttpOnly, Secure when https, Path `/`, Max-Age `session_hours * 3600`) and a 200 HTML page with `<meta http-equiv="refresh" content="0;url=/">` and a link `Continue` to `/` (a Strict cookie is not reliably sent on a redirect that continues a cross-site navigation, so no 302 here).
- `current_admin(request, ctx) -> str | None`: with `dev_admin` set returns it; else verifies the session cookie and re-checks `email in config.admins` against a fresh `load_config` (fail closed: an invalid `kh.yaml` or an empty `admins` is no admin).
- Middlewares (outermost first): (1) errors become `{"error": "<code>", "detail": "<text>"}` JSON, unknown exceptions `internal` with no detail and a log line; headers `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Cache-Control: no-store` on `/api/*`; (2) `Host` must equal the host of `public_url`, except for `/healthz` (the container healthcheck calls it with Host `127.0.0.1`); the CSRF origin is `public_url`, and in dev with `dev_admin` and no `public_url` it is `http://` plus the request `Host`; (3) auth gate: `/healthz`, `/auth/login`, `/auth/callback` and `/static/*` need no session, `GET /` redirects to `/auth/login` without one, every `/api/*` answers 401 `unauthorized` without one; (4) CSRF on POST, PUT, PATCH, DELETE: `Origin` equals the panel origin, `Sec-Fetch-Site` is `same-origin` when sent, `Content-Type` is `application/json`, else 403 `csrf`. State-changing requests log one JSON audit line `{ts, email, method, path, status}`.

### 3.4 Read API (all GET, JSON, behind the gate)

All SQL runs as `kh_indexer` inside `BEGIN READ ONLY` with `SET LOCAL statement_timeout = '5s'`.

- `/api/me`: `{"email", "config_sha256"}` where `config_sha256` is `sha256(Path(config_file).read_text().encode()).hexdigest()` (null when unreadable).
- `/api/labels`: `{"labels": [{"name", "description", "archived", "documents", "records", "tech_repos": [names]}], "config_sha256"}`; counts from `unnest(labels)` over `kh.documents` and `kh.records`.
- `/api/groups`: `{"groups": [{"name", "repo", "archived", "slack_channel", "members": [login], "approvers": [login], "directory": {"<provider>": {"members": [ref], "approvers": [ref]}}, "stats": {...}}], "people": [{"login", "email"}], "config_sha256"}`. `stats` is `{"documents": {"T0", "T0a"}, "stale_documents", "records", "outdated_records", "chunks": {"T0", "T0a", "T3"}, "embedded_chunks", "document_chars", "last_indexed_at", "last_ingested_at"}`; T0a rows have audience `all`, so they appear under the group `all`; `document_chars` is `sum(length(documents.body))` and records carry only their excerpt in the database, so there is no size for records.
- `/api/repos` (`dirs` and `labels` are `null` for group repos): `{"tech_org", "repos": [{"name", "kind": "group"|"tech", "group", "dirs", "labels", "archived", "documents", "last_indexed_sha", "indexed_at"}], "config_sha256"}`.
- `/api/pipeline`: `{"jobs": {"queued", "running", "done", "failed_retrying", "failed_dead"}, "oldest_queued_age_s", "stuck_running", "failed": [{"id", "kind", "repo", "attempts", "last_error" (300 characters), "created_at", "run_after", "dead"}] (newest 50), "alerts": [{"id", "repo", "path", "message", "created_at"}] (newest 50), "records_without_chunks", "repos": [{"repo", "last_indexed_sha", "indexed_at"}]}`. `failed_dead` is `state='failed' AND attempts >= MAX_ATTEMPTS`; `failed_retrying` is the other failed rows; `stuck_running` is `running` with `created_at` older than 60 minutes; `records_without_chunks` counts records ingested more than 5 minutes ago.
- `/api/health` (3.5).
- `/api/ops/{id}` (3.6).

### 3.5 Health checks

`GET /api/health` returns `{"generated_at", "checks": [{"id", "status", "detail"}]}`, `status` one of `ok`, `warn`, `fail`, `n/a`:

| id | Rule |
|---|---|
| `config` | `load_config` succeeds: ok `kh.yaml is valid`; else fail with the error |
| `database` | `SELECT 1` as indexer: ok or fail |
| `migrations` | `asyncio.to_thread(db.migrate.pending, settings.dsn("migrator"))`: none ok; some fail `N pending: <names>`; no migrator password file `n/a`; an error warn |
| `hub` | GET `<hub_url>/healthz` (3 s): connection error fail; 200 ok; header `X-KH-Config-Digest` different from `config_digest` of the file on disk: warn `the hub runs an older kh.yaml than the one on disk` |
| `queue` | `failed_dead > 0` fail; `stuck_running > 0` or `oldest_queued_age_s > 600` warn; else ok |
| `alerts` | alerts created in the last 24 h: warn with the count; else ok |
| `indexed` | an active group repo or tech repo with no `repo_state` row: warn listing them |
| `embeddings` | `records_without_chunks > 0`: warn |
| `directory` | provider `none`: `n/a`; no snapshot or age over `max_stale_seconds`: fail; age over `2 * refresh_seconds`: warn; else ok (`directory.store.load_snapshot` on an indexer connection) |

Two slow checks are ops, not GETs: `health.setup_plan` (`kh setup plan`) and `health.index_check` (`kh check`).

### 3.6 Write path

`POST /api/ops` body `{"op", "args", "dry_run", "config_sha256"}` (`dry_run` default false, `config_sha256` optional 64 lowercase hex); answer 202 `{"id", "state": "running"}`; `GET /api/ops/{id}` answers `{"id", "op", "state": "running"|"done", "ok", "exit_code", "dry_run", "output", "restart_expected", "started_at", "finished_at"}`. One op at a time (`asyncio.Lock`; a second submit gets 409 `busy`); the job table keeps the last 50 in memory (uuid4 hex ids). An op whose output contains `admin: cannot lock` ends with `ok: false`; the UI shows `Another change is running; try again in a minute` for it as for a 409.

Ops (arguments are exactly the listed keys; optional keys marked `?`):

| op | args | argv after `kh` |
|---|---|---|
| `label.add` | name, description | `label add --description=D -- NAME` |
| `label.describe` | name, description | `label rename -- NAME TEXT` |
| `label.archive`, `label.unarchive`, `label.remove` | name | `label <verb> -- NAME` |
| `person.add` | login, email | `person add -- LOGIN EMAIL` |
| `person.remove` | login, from_groups (bool) | `person remove [--from-groups] -- LOGIN` |
| `group.add` | name, members [login], approvers [login] | `group add --members=a,b --approvers=a -- NAME` |
| `group.archive`, `group.unarchive` | name | `group <verb> -- NAME` |
| `group.remove` | name, confirm (bool) | `group remove [--yes] -- NAME` |
| `member.add`, `approver.add`, `approver.remove` | group, login | `group member add -- GROUP LOGIN` (and `approver add|remove`) |
| `member.remove` | group, login, with_approver (bool) | `group member remove [--with-approver] -- GROUP LOGIN` |
| `repo.add` | name, dirs? [str], labels? [str] | `repo add [--dirs=a,b] [--labels=x] -- NAME` |
| `repo.set` | name, dirs?, labels? (at least one; labels `[]` clears and is sent as `--labels=`) | `repo set [--dirs=a,b] [--labels=x] -- NAME` |
| `repo.remove` | name, confirm (bool) | `repo remove [--yes] -- NAME` |
| `health.setup_plan` | none | `setup plan --config=PATH` |
| `health.index_check` | none | `check` |
| `repo.rebuild` | repo | in process: `queue_rebuild` |
| `jobs.retry` | ids? [int] | in process: `retry_jobs` |
| `jobs.reclaim` | none | in process: `reclaim_stuck` |
| `embeddings.requeue` | none | in process: `requeue_embeddings` |

Rules: the config ops get `--config=<config_file>`, `--dry-run` when `dry_run` is true and `--if-sha256=<hex>` when `config_sha256` is given, all placed before the `--` that precedes every positional (so no value is ever read as an option); `dry_run: true` is a 400 `bad_args` for every other op; validation uses `fullmatch` with `config.LABEL_RE`, `GROUP_RE`, `LOGIN_RE`, `REPO_RE`, `DIR_RE`; descriptions at most 200 characters with no control character (Unicode category `Cc`); emails by the `people` rule; lists at most 50 items, each validated; ids at most 100 positive integers; `repo.rebuild` takes a repo from the active group repos or tech repos of the freshly loaded config. A test feeds the argv of every op to the real `admin.cli` parser and checks the parsed namespace.

Subprocess: `[sys.executable, "-m", "knowledge_hub.cli", *argv]`, an explicit environment (`PATH`, `HOME`, `PYTHONUNBUFFERED=1` and every `KH_*` variable except `KH_PANEL_*`), `stdin=DEVNULL`, `stderr=STDOUT`, `start_new_session=True`, `os.killpg` on a 300 s timeout (exit code `-9`, output ends `timed out after 300 s`). Output is read as it arrives, capped at 8192 characters (the rest is drained and dropped), and every value of a `KH_*_FILE` file of at most 4096 bytes and at least 8 characters is replaced by `***`. `restart_expected` is true for a successful, non-dry config op. One JSON audit line per op: `{ts, email, op, args, dry_run, exit_code, id}`.

Write-race protection: the admin commands gain `--if-sha256 HEX` (p4): checked right after load (`admin: kh.yaml changed since the preview; preview again`, exit 2) and, always, right before commit the file is read again and compared with the loaded text (`admin: kh.yaml changed while the command ran; run it again`, exit 2). The panel sends the `config_sha256` it displayed.

### 3.7 Pipeline operations

- `jobs/retry.py`: `retry_jobs(pool, ids: list[int] | None = None) -> int` sets `state='queued', attempts=0, run_after=now()` for rows with `state='failed' AND attempts >= MAX_ATTEMPTS` (and `id = ANY(ids)` when given), keeping `last_error`; `reclaim_stuck(pool, older_than_minutes: int = 60) -> int` sets `running` rows whose `created_at` is older back to `queued`. The retrying failed jobs (attempts under 5) are picked up by the worker on their own.
- `jobs/rebuild_queue.py`: `queue_rebuild(pool, repo) -> bool` always deletes the `repo_state` row, then inserts one `git_push` job unless a queued job, or a running one younger than an hour, exists for the repo (the `_POLL_TECH` guard of `indexer/worker.py:31-38`); False when a job already exists. `kh rebuild --repo R --queue` does the same from the command line. Rejected: running `kh rebuild` in the panel (it needs Ollama and the app pool and cannot finish in the timeout on CPU embeddings).
- The queued job is run by the hub's own worker, so the result is asynchronous; the Pipeline tab shows the queue and the repo head. A rebuild queued while the hub runs an older `kh.yaml` than the disk says is warned about in the UI from the `hub` health check.

### 3.8 UI

Static, no build step, vanilla JS modules served from `/static/`: `index.html`, `panel.css`, `app.js` (tabs, `api()` helper, redirect to `/auth/login` on 401), `dom.js` (`h(tag, attrs, ...children)` using `createElement` and `textContent` only), `ops.js` (the Preview, Apply and poll widget), `labels.js`, `groups.js`, `repos.js`, `pipeline.js`, `health.js`. Apply stays disabled until a Preview of the same arguments succeeded; the Apply request carries the `config_sha256` of that Preview. After a successful Apply with `restart_expected` the page says `kh serve restarts within about 20 seconds; Health shows when it is back`. A unit test fails on `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `eval`, `new Function`, `document.write` and inline `on...=` attributes, and checks that every `"/api/..."` string literal in the scripts matches a registered route.

### 3.9 Rejected alternatives

| Alternative | Why not |
|---|---|
| Keep the spool and the root applier | Needs a host unit and Docker access; the ask is a panel inside kh |
| Call `admin.apply.execute` in process | `asyncio.run` inside the CLI cannot nest, output goes through `print`, and every `*_cmd` module would need a refactor |
| Mount the panel inside `kh serve` | The public process would hold the setup admin credentials |
| Cloudflare Access plus a shared password (the compose model) | The owner chose the hub's own OIDC sign-in |
| Session table in Postgres | Needs a migration; the 8 h window of a stolen cookie is accepted, and admin removal is immediate because `admins` is re-read |
| Hot reload of `kh.yaml` in `kh serve` | Many modules hold the config; the owner chose exit and restart |
| Session key derived from the OIDC client secret | The IdP operators can read that secret and its rotation would log everyone out |
| Panel editing Keycloak group members | Gives the panel a Keycloak `manage-users` credential; out of scope (owner decision) |

## 4. Manual steps

Nothing manual is needed before the phases: every phase builds against fakes. The two items below need a real deployment and happen after the last merge. Together they take about 35 minutes.

### Before implementation

None.

### After implementation

**ma1-oidc-client: sign-in client for the panel (about 15 minutes).**
1. Pick the panel's public address, for example `https://kh-admin.<your domain>`, reachable only from a private network or behind an access proxy.
2. Google issuer: open https://console.cloud.google.com/apis/credentials, create an OAuth client of type Web application (or edit the hub's existing one), and add the authorised redirect URI `https://kh-admin.<your domain>/auth/callback`. Keycloak issuer: open `https://<keycloak host>/admin/master/console/#/<realm>/clients`, create a confidential client with the standard flow and the same redirect URI. You see a client id and a client secret.
3. Store the client secret as the file `/etc/stack-secrets/kh_panel_oidc_client_secret` (mounted at `/run/secrets/kh_panel_oidc_client_secret`) and 48 random bytes as `/etc/stack-secrets/kh_panel_session_key` (`head -c 48 /dev/urandom | base64 > <file>`). Secrets never go through chat or the repository.
4. Add your email, in lower case, under `admins:` in `kh.yaml` (the example is in https://github.com/tkcs-lab/knowledge-hub/blob/feat/admin-panel/deploy/kh.example.yaml).
Report back: pass or fail of "I can sign in and a non-admin account gets `not an administrator`".

**ma2-click-through: use the panel against the real stack (about 20 minutes).**
1. Start the `knowledge-hub-panel` service from `deploy/compose.example.yaml` (all kh containers on the same image tag; the steps are in https://github.com/tkcs-lab/knowledge-hub/blob/feat/admin-panel/docs/runbook.md#admin-panel) and open the panel address.
2. Groups: the figures match `kh check` and `SELECT audience, count(*) FROM kh.documents GROUP BY 1`.
3. Labels: add `zz-test`, press Preview, then Apply, see `kh serve` restart (Health shows `hub` ok again within a minute), then remove `zz-test`.
4. Pipeline: press Rebuild on one repo and see the job go from queued to done.
5. Health: press `setup plan`; the output matches `kh setup plan` run by hand.
Report back: a pass or fail line per step for the pull request.

## 5. Risks and open questions

- **Older image or compose template:** an image without this change refuses a `kh.yaml` that has `admins` (exit 2, crash loop). tkcs-work-compose re-renders `kh.yaml` from `kh_groups` and `kh_labels` and would drop `admins` and overwrite `tech_repos` while both tools write the file. Run the new panel only after the follow-up plan in tkcs-work-compose lands, or keep the compose registry on `group_vars`. A worker never edits that repository.
- **`reclaim_stuck`** uses `created_at` because a job has no start time; a job that waited long in the queue and is still running can be reclaimed and run twice. `sync_repo` takes an advisory lock and is idempotent, so the cost is repeated work. A worker that finds a job column that records the start time stops with `status=blocked`.
- **Invalid `kh.yaml`:** the panel fails closed (403) and `kh serve` keeps serving the old config; the command line is the fallback and the runbook says so. Health shows `config` failing and the `hub` digest warning.
- **Cookie behaviour on the callback:** the 200 page instead of a 302 is the conservative choice; a worker does not change it to a redirect.
- **Directory sync** restarts on a valid change; if it is not in the same compose project it keeps the old config until restarted by hand.
- **`kh check`** needs the full hub environment (indexer and app passwords, indexer token, Forgejo URL, embedder settings); p15 lists them in the compose example. A worker that sees `kh check` fail for a missing variable reports it in the phase report and does not weaken the check.
- **Stop conditions:** a worker that finds a symbol, line number or file named in its brief missing stops with `status=blocked`; a protected path is never edited (`protected-change:` line).

## Implementation manifest

```yaml
plan_slug: admin-panel
feature_branch: feat/admin-panel
tracking_issue: 63
max_parallel: 2
manual_before: []
manual_after:
  - id: ma1-oidc-client
    title: Register the panel's sign-in client and put yourself in admins
    why: A real identity provider and real secret files cannot be exercised by the tests
  - id: ma2-click-through
    title: Click through every tab against the real stack
    why: Real Forgejo, Postgres, restart behaviour and the sign-in redirect cannot be checked by CI
verify_after_merge:
  - .venv/bin/python -m pytest -q -m unit -k panel
  - .venv/bin/python -m pytest -q -m unit -k config_watch
  - .venv/bin/python -m pytest -q -m unit -k admin
final_checks:
  - "`tld plan deviations check --file docs/plans/63/deviations.md --plan docs/plans/63/plan.md --all` exits 0"
  - "the changelog has entries under ## [Unreleased] and no version heading"
phases:
  - id: p1-settings-admins
    title: The admins key, the config watch setting and PanelSettings
    depends_on: []
    complexity: M
    touches:
      - src/knowledge_hub/config.py
      - src/knowledge_hub/settings.py
      - src/knowledge_hub/panel/__init__.py
      - src/knowledge_hub/panel/settings.py
      - tests/unit/test_config.py
      - tests/unit/test_settings.py
      - tests/unit/test_panel_settings.py
    brief: |
      Design 3.2.
      1. config.py: add the constant `MAX_ADMINS = 50` and the field `admins: list[str] = Field(default_factory=list)` to `KhConfig`. In `KhConfig._validate`, after the `people` loop, add: more than `MAX_ADMINS` entries raises `ValueError(f"admins has more than {MAX_ADMINS} entries")`; each entry must have exactly one `@`, at most `MAX_EMAIL` characters and two non-empty parts, else `ValueError(f"admins entry {email!r} is not a valid email")`; entries are lower-cased; a repeat raises `ValueError(f"admins lists {low} twice")`; finish with `self.admins = <the lower-cased list>`.
      2. settings.py: add `config_watch_seconds: int = Field(10, ge=0, le=3600)` to `Settings`. Move the body of `Settings._check_public_url` into a module function `check_public_url(url: str, env: str, name: str) -> None` that raises `ValueError(f"{name} must be an https URL without a path")` under the same conditions; `Settings._check_public_url` calls `check_public_url(url, self.env, "KH_PUBLIC_URL")`. The text of the existing error does not change.
      3. Create `src/knowledge_hub/panel/__init__.py` (a one-line docstring) and `panel/settings.py` with `class PanelSettings(BaseSettings)` exactly as the table in Design 3.2 says: `model_config = SettingsConfigDict(env_prefix="KH_PANEL_", extra="ignore", populate_by_name=True)`; `env: Literal["dev", "test", "prod"] = Field("prod", validation_alias="KH_ENV")` and `config_file: Path | None = Field(None, validation_alias="KH_CONFIG_FILE")` (the validator raises `KH_CONFIG_FILE is required`); a `model_validator(mode="after")` that applies the rules and error strings of Design 3.2 in the table order, with `dev_admin` skipping the OIDC and session-key requirements and lower-casing the email; properties `allowed_domains_tuple` and `hosted_domains_tuple` that reuse `settings._csv`; a method `read_session_key() -> bytes` that returns `session_key_file.read_bytes().strip()` and raises `ValueError("KH_PANEL_SESSION_KEY_FILE must hold at least 32 bytes")` for a shorter file.
      4. Tests. `tests/unit/test_config.py`: admins are lower-cased, a duplicate, a bad email and 51 entries are refused with the strings above, a config without `admins` still loads. `tests/unit/test_settings.py`: `KH_CONFIG_WATCH_SECONDS=0` and `3600` load, `3601` is refused; `KH_PUBLIC_URL=http://x.test` still raises `KH_PUBLIC_URL must be an https URL without a path`. `tests/unit/test_panel_settings.py`: a panel-shaped environment (no `KH_OIDC_*`, no `KH_PUBLIC_URL`, `KH_PANEL_*` set) loads `PanelSettings` and also `Settings`; each missing variable raises its `is required` text; the Google rule; `KH_PANEL_DEV_ADMIN` with `KH_ENV=prod` is refused; a 31-byte key file is refused.
      Do not edit any other file. If `Settings` already has a field or function with these names, stop with status=blocked.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_config.py tests/unit/test_settings.py tests/unit/test_panel_settings.py -q passes"
      - ".venv/bin/python -m ruff check . passes"
      - "grep -n 'def check_public_url' src/knowledge_hub/settings.py matches"
  - id: p2-config-watch
    title: kh serve exits with code 75 when kh.yaml changes validly
    depends_on: [p1-settings-admins]
    complexity: M
    touches:
      - src/knowledge_hub/config_watch.py
      - src/knowledge_hub/cli.py
      - src/knowledge_hub/app.py
      - tests/unit/test_config_watch.py
      - tests/unit/test_cli_serve.py
      - tests/unit/test_app_healthz.py
    brief: |
      Design 3.2.
      1. Create `src/knowledge_hub/config_watch.py` with `EXIT_RESTART = 75`, `config_digest(config: KhConfig) -> str`, `class ConfigWatcher` (`__init__(self, path: Path, loaded: KhConfig, load: Callable[[Path], KhConfig] = load_config)`, `poll(self) -> bool`) and `async def watch(path, loaded, interval, on_change, *, sleep=asyncio.sleep, load=load_config) -> None`, with the semantics and log texts of Design 3.2. `poll` catches `ConfigError` and `OSError` from `load`. The module imports only `knowledge_hub.config` and the standard library.
      2. cli.py: change `_serve(settings, config, port)` to return `int`. After the server object exists, when `settings.config_file is not None and settings.config_watch_seconds > 0`, start `asyncio.create_task(watch(settings.config_file, config, settings.config_watch_seconds, on_change))` where `on_change()` (no arguments) sets `server.should_exit = True` and appends `True` to a list `changed`; `watch` returns after calling `on_change` once. Import `watch` at the top of `cli.py` so tests can monkeypatch `knowledge_hub.cli.watch`; cancel the task in `finally`; return `EXIT_RESTART` when `changed` is not empty, else 0. In `main`, replace `asyncio.run(_serve(...))` followed by `return 0` with `return asyncio.run(_serve(...))` inside the existing `try` (the `ValueError` and `KeyboardInterrupt` handlers stay and still return 2 and 0).
      3. app.py: add the module constant `DRAIN_SECONDS = 60` and use it in place of the `10` in `asyncio.wait_for(task, 10)`. Replace the `healthz` function by `make_healthz(config)` returning an endpoint that answers `PlainTextResponse("ok", headers={"X-KH-Config-Digest": digest})` where `digest = config_digest(config)` is computed once; the route becomes `Route("/healthz", make_healthz(deps.config))`.
      4. Tests. `tests/unit/test_config_watch.py` (a temp `kh.yaml`, a fake `sleep`): an unchanged file never fires; a valid change fires on the second poll only; a change reverted before the second poll resets; an invalid change never fires and logs the warning once per distinct error (`caplog`); an `admins`-only edit and a comment-only edit never fire; `config_digest` does not depend on key order. `tests/unit/test_cli_serve.py`: with `uvicorn.Server`, `uvicorn.Config`, `knowledge_hub.deps.build_deps`, `close_deps` and `knowledge_hub.app.build_app` monkeypatched (the fake server's `serve` waits until `should_exit`), a patched `knowledge_hub.cli.watch` that calls `on_change()` makes `cli.main(["serve"])` return 75, and `KH_CONFIG_WATCH_SECONDS=0` never starts a watcher and returns 0. `tests/unit/test_app_healthz.py`: `/healthz` answers `ok` with the header equal to `config_digest(config)`.
      Stop with status=blocked if `cli._serve` no longer has the shape described in Current state.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_config_watch.py tests/unit/test_cli_serve.py tests/unit/test_app_healthz.py -q passes"
      - ".venv/bin/python -m pytest -q -m unit passes"
      - "grep -n 'EXIT_RESTART = 75' src/knowledge_hub/config_watch.py matches"
  - id: p3-directory-watch
    title: The directory sync restarts itself on a config change
    depends_on: [p2-config-watch]
    complexity: S
    touches:
      - src/knowledge_hub/directory/sync.py
      - src/knowledge_hub/directory/cli.py
      - tests/unit/test_directory_sync.py
    brief: |
      Design 3.2.
      1. directory/sync.py: give `run_every` the extra keyword `watcher: Any = None` and the return type `int`. After each `await sleep(interval)` add: `if watcher is not None and watcher.poll(): log.info("directory: config changed on disk; exiting with code %d so the container restarts", EXIT_RESTART); return EXIT_RESTART`, with `EXIT_RESTART` imported from `knowledge_hub.config_watch`. The loop has no other exit.
      2. directory/cli.py `_sync`: in the `args.every is not None` branch build `watcher = ConfigWatcher(settings.config_file, config) if settings.config_file is not None else None`, pass it as `watcher=watcher` and `return await run_every(...)` instead of the current `await run_every(...)` followed by `return 0`.
      3. Tests in `tests/unit/test_directory_sync.py`: with a fake `sleep` and a fake watcher whose `poll` returns False, False, True, `run_every` returns 75 after three sleeps; with `watcher=None` the existing tests are unchanged.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_directory_sync.py -q passes"
      - "grep -n 'EXIT_RESTART' src/knowledge_hub/directory/sync.py matches"
  - id: p4-admin-if-sha256
    title: The admin commands refuse to overwrite a kh.yaml that changed
    depends_on: []
    complexity: S
    touches:
      - src/knowledge_hub/admin/cli.py
      - src/knowledge_hub/admin/apply.py
      - tests/unit/test_admin_apply.py
      - tests/unit/test_admin_cli.py
    brief: |
      Design 3.6, "Write-race protection".
      1. admin/apply.py: add `def text_sha256(text: str) -> str` returning `hashlib.sha256(text.encode()).hexdigest()`. Add the keyword `if_sha256: str | None = None` to `execute` and `_run`. In `_run`, directly after `doc = yamledit.load(path)`: when `if_sha256 is not None and if_sha256 != text_sha256(doc.text)`, print `admin: kh.yaml changed since the preview; preview again` and return 2. Directly before `yamledit.commit(path, new_text)`: read `Path(path).read_text()`; when it differs from `doc.text`, print `admin: kh.yaml changed while the command ran; run it again` and return 2.
      2. admin/cli.py: in `add_common` with `mutating=True` add `--if-sha256` (`metavar="HEX"`, default None, a `type` function that raises `argparse.ArgumentTypeError("--if-sha256 must be 64 lower case hex digits")` unless the value matches `[0-9a-f]{64}`). In `run`, pass `if_sha256=args.if_sha256` to `apply.execute` (add it to the keyword arguments of the call).
      3. Change `RESTART_NOTE` in apply.py to `kh serve restarts itself within KH_CONFIG_WATCH_SECONDS; restart it by hand when the watch is off` and update the assertion on it in `tests/unit/test_admin_apply.py`.
      4. Tests. `test_admin_apply.py`: a wrong `if_sha256` returns 2 with the first text and leaves the file untouched, also with `dry_run=True`; the right one proceeds; a file rewritten by the `after` callback of a fake step makes the commit step return 2 with the second text and leaves the rewritten content. `test_admin_cli.py`: the option parses, a 63-character value is an argparse error (exit code 2 from `SystemExit`), and `kh label list` (non-mutating) has no such option.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_admin_apply.py tests/unit/test_admin_cli.py -q passes"
      - "grep -n 'def text_sha256' src/knowledge_hub/admin/apply.py matches"
  - id: p5-job-queue-ops
    title: Retry dead jobs, reclaim stuck jobs and queue a rebuild
    depends_on: [p2-config-watch]
    complexity: M
    touches:
      - src/knowledge_hub/jobs/retry.py
      - src/knowledge_hub/jobs/rebuild_queue.py
      - src/knowledge_hub/cli.py
      - tests/integration/test_jobs_retry.py
      - tests/integration/test_rebuild_queue.py
      - tests/unit/test_cli_rebuild.py
    brief: |
      Design 3.7. Pattern for the SQL style: `src/knowledge_hub/jobs/requeue.py`; for the tests: `tests/integration/test_webhook_worker.py` and the fixtures `indexer_pool`, `fresh_db` of `tests/support/fixtures_db.py`.
      1. Create `jobs/retry.py` with `from knowledge_hub.indexer.worker import MAX_ATTEMPTS`, `async def retry_jobs(pool, ids: list[int] | None = None) -> int` and `async def reclaim_stuck(pool, older_than_minutes: int = 60) -> int`, as written in Design 3.7, both in `async with pool.connection() as conn, conn.transaction()` and returning `cur.rowcount`.
      2. Create `jobs/rebuild_queue.py` with `async def queue_rebuild(pool, repo: str) -> bool`: in one transaction `DELETE FROM kh.repo_state WHERE repo = %s`, then `INSERT INTO kh.index_jobs (kind, repo, payload) SELECT 'git_push', %s, '{}'::jsonb WHERE NOT EXISTS (SELECT 1 FROM kh.index_jobs j WHERE j.kind = 'git_push' AND j.repo = %s AND (j.state = 'queued' OR (j.state = 'running' AND j.created_at > now() - interval '1 hour')))`; return `cur.rowcount == 1`.
      3. cli.py: add `--queue` (store_true) to the `rebuild` parser; change `_rebuild(deps, repo)` to `_rebuild(deps, repo, queue=False)`; the job lambda passes `getattr(args, "queue", False)`. With `queue` true and no repo print `kh rebuild --queue needs --repo` and return 2; otherwise check the repo against `configured_repos(deps.config) + configured_tech_repos(deps.config)` with the same two `ValueError` texts as `rebuild()` (`repo X is archived`, `repo X is in no group`), call `queue_rebuild(deps.indexer_pool, repo)` and print `queued rebuild of <repo>` or `rebuild of <repo> is already queued`; return 0.
      4. Tests. `test_jobs_retry.py`: a failed job with `attempts = 5` is requeued with `attempts = 0` and keeps `last_error`; a failed job with `attempts = 2` is not touched; `ids` limits the update; a `running` job created two hours ago is reclaimed and one created a minute ago is not. `test_rebuild_queue.py`: the `repo_state` row is deleted and one job inserted; a second call returns False and inserts nothing. `test_cli_rebuild.py`: `--queue` without `--repo` exits 2; `--queue --repo kb-all` prints `queued rebuild of kb-all` with `queue_rebuild` monkeypatched and `stubs.deps.config = parse_config({"groups": {"all": {"members": ["a"], "approvers": ["a"]}}, "people": {"a": "a@x.test"}})`, and `--queue --repo kb-x` exits 2 with `repo kb-x is in no group`; the existing tests are unchanged.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_cli_rebuild.py tests/integration/test_jobs_retry.py tests/integration/test_rebuild_queue.py -q passes"
      - "grep -n 'async def queue_rebuild' src/knowledge_hub/jobs/rebuild_queue.py matches"
  - id: p6-panel-session-auth
    title: Panel context, signed cookies and the sign-in routes
    depends_on: [p1-settings-admins]
    complexity: M
    touches:
      - src/knowledge_hub/panel/context.py
      - src/knowledge_hub/panel/session.py
      - src/knowledge_hub/panel/auth.py
      - tests/unit/test_panel_session.py
      - tests/unit/test_panel_auth.py
    brief: |
      Design 3.3. Patterns: cookies and the secure prefix in `src/knowledge_hub/oauth/routes.py:79` and `:121-125`; the stub upstream in `tests/unit/test_oauth_flow.py` and the ASGI testing style of `tests/unit/test_oauth_routes.py`.
      1. `panel/context.py`: `@dataclass class PanelContext` with the fields `settings: PanelSettings`, `signer: Signer | None`, `upstream: Any | None`, `load_config: Callable[[], KhConfig]` (raises `ConfigError`), `pool: Any | None = None`, `hub_settings: Any | None = None`, `now: Callable[[], float] = time.time`, plus the property `secure -> bool` (the public URL starts with `https://`) and the helper `cookie_name(base: str) -> str` returning `"__Host-" + base` when `secure`, else `base`.
      2. `panel/session.py`: `class Signer`, `sign` and `verify` exactly as Design 3.3; a token is two base64url parts separated by one dot; `verify` returns None for any malformed, wrongly signed, wrong-purpose or expired token and never raises.
      3. `panel/auth.py`: `def build_auth_routes(ctx) -> list[Route]` with `GET /auth/login`, `GET /auth/callback` and `POST /api/logout` (clears the session cookie, answers 204), and `def current_admin(request, ctx) -> str | None`, all as Design 3.3 (cookie names `kh_panel_flow` and `kh_panel` through `ctx.cookie_name`, fixed texts `sign-in refused`, `invalid request`, `not an administrator`, `panel unavailable: kh.yaml is invalid`, and the 200 callback page with the meta refresh and the `Continue` link; the page has no inline script and no inline style). Log refusals with `log.warning` and the upstream error code only, never a token.
      4. Tests. `test_panel_session.py`: round trip, tampered payload, tampered signature, wrong purpose, expiry with a fake `now`, garbage input. `test_panel_auth.py` (Starlette `TestClient` or `httpx.ASGITransport`, a stub upstream with async `authorization_url`, `exchange` (returns `{"id_token": "t"}`) and `verify_id_token` (returns a `VerifiedIdentity`), and `new_pkce`): login sets the flow cookie with Lax and Max-Age 600 and redirects to the stub's authorization URL; the callback with a wrong state is 400; an admin email gets the session cookie (Strict, HttpOnly, Secure) and a 200 page; a non-admin is 403 `not an administrator`; an invalid `kh.yaml` is 403; `UpstreamError` is 403 `sign-in refused`; `current_admin` returns None after `admins` loses the email (the loader is called per request) and returns the dev admin when `dev_admin` is set; `POST /api/logout` clears the cookie.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_panel_session.py tests/unit/test_panel_auth.py -q passes"
      - "grep -rn 'compare_digest' src/knowledge_hub/panel/auth.py matches"
  - id: p7-panel-app-shell
    title: The panel app, `kh panel serve`, the static shell and packaging
    depends_on: [p5-job-queue-ops, p6-panel-session-auth]
    complexity: M
    touches:
      - src/knowledge_hub/panel/app.py
      - src/knowledge_hub/panel/cli.py
      - src/knowledge_hub/cli.py
      - src/knowledge_hub/panel/static/index.html
      - src/knowledge_hub/panel/static/panel.css
      - src/knowledge_hub/panel/static/app.js
      - pyproject.toml
      - tests/unit/test_panel_app.py
      - tests/unit/test_panel_static.py
      - tests/unit/test_import_boundary.py
    brief: |
      Design 3.1, 3.3, 3.8.
      1. `panel/app.py`: `ROUTE_BUILDERS: tuple[Callable[[PanelContext], list[Route]], ...] = (build_auth_routes,)` (later phases append their builder to this tuple and to nothing else); `def build_panel_app(ctx: PanelContext) -> Starlette` with the four middlewares of Design 3.3 in that order (write them as `BaseHTTPMiddleware` classes), `GET /healthz` answering `ok` as `text/plain`, `GET /` serving `static/index.html` (302 to `/auth/login` without an admin), `GET /static/{path:path}` serving files from `Path(__file__).parent / "static"` with `mimetypes.guess_type` (reject any path whose `os.path.realpath` is outside that directory with 404), and the routes of every builder in `ROUTE_BUILDERS`. The gate stores the admin email in `request.state.email`.
      2. `panel/cli.py`: `def main(argv: list[str]) -> int` with the subcommand `serve [--port N]` (default 8081): load `PanelSettings()` and `load_settings()` (a `ValueError` or `ValidationError` prints the message to stderr and returns 2), build `load_config=lambda: load_config(panel_settings.config_file)` (`knowledge_hub.config.load_config`, which raises `ConfigError`), the `Signer` from `read_session_key()` unless `dev_admin`, the `UpstreamClient` (Design 3.3; an `httpx.AsyncClient` closed on shutdown) unless `dev_admin`, create `make_indexer_pool(hub_settings)` and `await pool.open()`, read the client secret with `read_text().strip()` for `UpstreamConfig.client_secret`, run `uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info", workers=1))`, close the pool and the client in `finally`.
      3. `src/knowledge_hub/cli.py` `main`: add `if argv and argv[0] == "panel": from knowledge_hub.panel.cli import main as panel_main; return panel_main(argv[1:])` next to the `setup` dispatch.
      4. Static shell: `index.html` (`<title>kh admin</title>`, a header with the signed-in email and a Sign out button, a `nav` with the five tab buttons Groups, Labels, Repos, Pipeline, Health, an empty `main`, `<script type="module" src="/static/app.js">`, `<link rel="stylesheet" href="/static/panel.css">`, no inline script or style); `panel.css` (system font, a light and a dark theme through `prefers-color-scheme`, a table style, `.ok .warn .fail` status colours with a text label so colour is never the only signal, visible focus outlines); `app.js` (an `api(method, path, body)` helper that sends JSON, redirects to `/auth/login` on 401 and throws an `Error` with the server `detail` otherwise; `GET /api/me` to fill the header; tab switching that shows one `section` per tab and calls `window.panelTabs[name]?.()` when a tab opens; Sign out posts `/api/logout` and goes to `/auth/login`). No `innerHTML` anywhere.
      5. pyproject.toml: `[tool.setuptools.package-data]` becomes `knowledge_hub = ["migrations/*.sql", "panel/static/*"]`.
      6. Tests. `test_panel_app.py` (gate tests use a context without `dev_admin` holding `Signer(b"k" * 32)`, a stub upstream and one fake route; one case uses a `dev_admin` context to show every route open for that admin): `/healthz` is open and answers 200 with Host `127.0.0.1`; `/api/me`-like routes answer 401 without a session; `GET /` redirects to `/auth/login`; a wrong `Host` is 400; POST without the right `Origin`, with `Sec-Fetch-Site: cross-site` or with `text/plain` is 403 `csrf`; the headers of Design 3.3 are present, `Cache-Control: no-store` on `/api/*`; an unknown exception becomes 500 `internal` with an empty detail; `/static/../x` and an encoded variant are 404. `test_panel_static.py`: every file under `panel/static` is readable through `importlib.resources.files("knowledge_hub.panel").joinpath("static")`; no script contains `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `eval(`, `new Function`, `document.write`, or an inline `on<event>=` attribute in `index.html`; `index.html` has no inline `<script>` body or `style=` attribute. `test_import_boundary.py`: add a case that `import knowledge_hub.app` loads no `knowledge_hub.panel` module, and a case that `import knowledge_hub.panel.app` loads none of `knowledge_hub.app`, `knowledge_hub.mcp*`, `knowledge_hub.admin*` or `ruamel*`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_panel_app.py tests/unit/test_panel_static.py tests/unit/test_import_boundary.py -q passes"
      - "grep -n 'panel/static' pyproject.toml matches"
      - ".venv/bin/python -m knowledge_hub.cli panel serve --help exits 0"
  - id: p8-panel-read-api
    title: Stats queries and the read API
    depends_on: [p7-panel-app-shell]
    complexity: M
    touches:
      - src/knowledge_hub/panel/stats.py
      - src/knowledge_hub/panel/api_read.py
      - src/knowledge_hub/panel/app.py
      - tests/integration/test_panel_stats.py
      - tests/unit/test_panel_read_api.py
    brief: |
      Design 3.4. Seed helpers for the tests: `tests/support/seed.py` (`add_doc`, `add_record`, `add_chunk`, `add_chunked_doc`, `add_chunked_record`); pool fixtures `indexer_pool` and `fresh_db`.
      1. `panel/stats.py`: async functions taking the indexer pool: `group_stats(pool) -> dict[str, dict]` (keys are audiences, values the `stats` object of Design 3.4, zeros when absent: three `GROUP BY audience` queries over `kh.documents` (by tier, with stale count, `sum(length(body))`, `max(indexed_at)`), `kh.records` (count, `count(*) FILTER (WHERE outdated)`, `max(ingested_at)`) and `kh.chunks` (by tier, `count(embedding)`)), `label_usage(pool) -> dict[str, dict]` (documents and records per label through `unnest(labels)`), `repo_state(pool) -> dict[str, tuple]`, `document_counts_by_repo(pool) -> dict[str, int]`, `pipeline(pool) -> dict` (the `/api/pipeline` object). Every function runs `BEGIN READ ONLY` plus `SET LOCAL statement_timeout = '5s'` through a shared helper `_read(pool)` (an async context manager yielding a connection inside `conn.transaction()` after `await conn.execute("SET TRANSACTION READ ONLY")` and the `SET LOCAL`). Import `MAX_ATTEMPTS` from `knowledge_hub.indexer.worker`.
      2. `panel/api_read.py`: `build_read_routes(ctx) -> list[Route]` for `/api/me`, `/api/labels`, `/api/groups`, `/api/repos`, `/api/pipeline` with the JSON of Design 3.4; the config comes from `ctx.load_config()` per request (`ConfigError` is 503 `config_invalid` with the message), `config_sha256` from `sha256(Path(config_file).read_text().encode())`. `/api/me` reads the email from `request.state.email`.
      3. app.py: append `build_read_routes` to `ROUTE_BUILDERS`.
      4. Tests. `test_panel_stats.py` (database): seed two groups with documents of both tiers, stale documents, records (one outdated), chunks with and without embeddings and index jobs in every state and assert every number of `/api/groups` stats, `/api/labels` and `/api/pipeline`; a group without rows has zeros; the failed split (`attempts` 5 against 2); the read-only transaction rejects a write attempted through the helper. `test_panel_read_api.py` (no database, stats functions monkeypatched): the JSON shapes, an invalid config gives 503, the `directory` references of a group with a `directory.keycloak` block appear read-only.
    acceptance:
      - ".venv/bin/python -m pytest tests/integration/test_panel_stats.py tests/unit/test_panel_read_api.py -q passes"
      - "grep -n 'build_read_routes' src/knowledge_hub/panel/app.py matches"
  - id: p9-panel-ops-args
    title: Operation allowlist, validation and argv
    depends_on: [p1-settings-admins, p4-admin-if-sha256]
    complexity: M
    touches:
      - src/knowledge_hub/panel/ops.py
      - tests/unit/test_panel_ops.py
    brief: |
      Design 3.6. This phase is pure: no I/O, no subprocess.
      1. `panel/ops.py`: `class OpError(ValueError)` carrying a `reason`; `@dataclass(frozen=True) class Op` with `name`, `kind` (`"cli"` or `"inproc"`), `keys` (required argument names), `optional` (optional names) and `build` (a function from the validated args to the argv list); `OPS: dict[str, Op]` with exactly the 24 operations of the table in Design 3.6; `def validate(op: str, args: object, dry_run: object, config_sha256: object) -> ValidatedOp` raising `OpError` for an unknown op, args that are not exactly the allowed keys, a wrong type, a value that fails its rule, `dry_run` that is not a bool or is true for an `inproc` op or for `health.*`, and a `config_sha256` that is not 64 lower case hex digits or is given for an op that is not a `cli` config op (`health.*` and the in-process ops); `def argv(valid: ValidatedOp, config_file: str) -> list[str]` returning the `kh` arguments (without the interpreter), in the order: command words, `--config=<file>`, `--dry-run`, `--if-sha256=<hex>`, the op options, then `--`, then the positionals, exactly as the table shows (`setup plan` and `check` get `--config=` only for `setup plan`, no `--`). Rules and limits are in Design 3.6; use `fullmatch` with `LABEL_RE`, `GROUP_RE`, `LOGIN_RE`, `REPO_RE`, `DIR_RE` from `knowledge_hub.config`; email rule: exactly one `@`, 254 characters at most, both parts non-empty; the group name rule also refuses `owners` and a name ending `-approve`; description: at most 200 characters and no character of Unicode category `Cc`.
      2. Tests in `tests/unit/test_panel_ops.py`: one valid example per op with the exact expected argv; a table of at least 40 refusals (leading `-`, `--config=/x`, spaces, control characters, 51 list items, wrong types, extra keys, missing keys, `dry_run` on `jobs.retry`); and a round-trip test that feeds the argv of every `cli` op to the real parser (build the parser as `admin/cli.py` `main` does, an `argparse.ArgumentParser(prog="kh")` with `sub` and `importlib.import_module(...).register(sub)` for every name of `COMMAND_MODULES`, and call `parse_args(argv)`) and asserts the parsed values equal the validated args (the hostile cases `--config=/x@y` as an email and `-x` as a description are included and arrive as plain values).
      A test named `test_ops_table_has_24_operations` asserts `len(OPS) == 24`. The exact argv of the table is the contract; do not add operations. If the real parser rejects an argv built from this table, stop with status=blocked and report the argv.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_panel_ops.py -q passes"
      - ".venv/bin/python -m pytest tests/unit/test_panel_ops.py::test_ops_table_has_24_operations -q passes"
  - id: p10-panel-health
    title: Health checks and the health route
    depends_on: [p8-panel-read-api, p2-config-watch]
    complexity: M
    touches:
      - src/knowledge_hub/panel/health.py
      - src/knowledge_hub/panel/api_health.py
      - src/knowledge_hub/panel/app.py
      - tests/unit/test_panel_health.py
      - tests/integration/test_panel_health_db.py
    brief: |
      Design 3.5.
      1. `panel/health.py`: `async def run_checks(ctx, *, http=None, pending=None) -> list[dict]` returning the nine checks of the table in Design 3.5 in that order, each `{"id", "status", "detail"}`. `http` is an optional `httpx.AsyncClient` (default a new one with a 3 s timeout, closed after use) and `pending` an optional function replacing `db.migrate.pending` (the default runs `asyncio.to_thread(migrate.pending, ctx.hub_settings.dsn("migrator"))`; `n/a` when `ctx.hub_settings.db_migrator_password_file is None`). The queue, alerts, indexed and embeddings checks use `stats.pipeline(ctx.pool)`, `stats.repo_state` and the freshly loaded config; the directory check calls `directory.store.load_snapshot` on `ctx.pool.connection()`. A check that raises becomes `fail` with `type(exc).__name__` and a 200-character message; one failing check never stops the others.
      2. `panel/api_health.py`: `build_health_routes(ctx) -> list[Route]` with `GET /api/health` returning `{"generated_at": <ISO 8601 UTC>, "checks": [...]}`.
      3. app.py: append `build_health_routes` to `ROUTE_BUILDERS`.
      4. Tests. `test_panel_health.py` (fakes for pool queries, `http` via `httpx.MockTransport`): one test per row of the table for ok, warn and fail where the table defines them, including the hub digest mismatch warning and a connection error, `n/a` for provider `none`, one failing check not stopping the others. `test_panel_health_db.py`: with a database, a dead job and a stuck running job turn `queue` into fail and warn, an alert row turns `alerts` into warn, and a configured repo with no `repo_state` row turns `indexed` into warn.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_panel_health.py tests/integration/test_panel_health_db.py -q passes"
      - "grep -n 'build_health_routes' src/knowledge_hub/panel/app.py matches"
  - id: p11-panel-ops-run
    title: The subprocess runner
    depends_on: [p9-panel-ops-args]
    complexity: M
    worker_level: strong
    worker_level_reason: Process-group handling, output capping and secret redaction are security-sensitive and easy to get subtly wrong
    touches:
      - src/knowledge_hub/panel/runner.py
      - tests/unit/test_panel_runner.py
    brief: |
      Design 3.6.
      1. `panel/runner.py`: `class ApiBusy(Exception)`; `class Runner` with `__init__(self, ctx: PanelContext, *, inproc=None, env=None, spawn=asyncio.create_subprocess_exec, timeout=300, now=time.time)` (the config file is `ctx.settings.config_file`; `inproc` is an async callable `(name, args, ctx) -> str` injected by p11b; `env` defaults to the explicit environment of Design 3.6), `async submit(self, valid: ValidatedOp, email: str) -> str` (raises `ApiBusy` when the lock is held; starts the job as an `asyncio` task and returns its uuid4 hex id at once), `get(self, job_id: str) -> dict | None` (the object of Design 3.6), a job table of at most 50 entries (oldest dropped), an `asyncio.Lock`, `start_new_session=True`, `os.killpg(proc.pid, SIGKILL)` on timeout (exit code -9 and the output ends `timed out after 300 s`), the 8192-character output cap with draining, the `***` redaction of `KH_*_FILE` file contents (files up to 4096 bytes, values of at least 8 characters, stripped), `stdin=DEVNULL`, `stderr=STDOUT`, and the audit line of Design 3.6 printed as JSON. The command is `[sys.executable, "-m", "knowledge_hub.cli", *ops.argv(valid, config_file)]`. For a `kind == "inproc"` op call `await inproc(name, args, ctx)` instead of spawning; its string is the `output`, `exit_code` 0 and `ok` true, and a raised `OpError` gives `ok` false with the reason as output. `restart_expected` is true for a successful, non-dry `cli` op that is not `health.*`.
      2. Tests in `tests/unit/test_panel_runner.py` with a fake `spawn` (a scripted process object with `stdout` as an async stream, `wait`, `pid`): output cap and draining, redaction of a file secret, timeout kills the group (patch `os.killpg`) and gives exit code -9, a second submit while one runs raises `ApiBusy`, the table keeps 50, the environment holds `KH_*` but no `KH_PANEL_*`, `stdin` is DEVNULL and `start_new_session` is true, the argv equals `ops.argv`, an in-process op never spawns, `restart_expected` values.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_panel_runner.py -q passes"
      - "grep -n 'start_new_session=True' src/knowledge_hub/panel/runner.py matches"
  - id: p11b-panel-ops-routes
    title: In-process pipeline ops and the ops routes
    depends_on: [p11-panel-ops-run, p10-panel-health, p5-job-queue-ops]
    complexity: M
    touches:
      - src/knowledge_hub/panel/pipeline_ops.py
      - src/knowledge_hub/panel/api_ops.py
      - src/knowledge_hub/panel/app.py
      - tests/unit/test_panel_api_ops.py
      - tests/integration/test_panel_pipeline_ops.py
    brief: |
      Design 3.6 and 3.7.
      1. `panel/pipeline_ops.py`: `async def run_inproc(name: str, args: dict, ctx: PanelContext) -> str` for `repo.rebuild` (check the repo against the active group repos plus `config.tech_repo_names` of `ctx.load_config()`, else raise `OpError("repo X is not configured")`; call `queue_rebuild(ctx.pool, repo)`; return `queued rebuild of X` or `rebuild of X is already queued`), `jobs.retry` (`retry_jobs(ctx.pool, args.get("ids"))`, return `retried N jobs`), `jobs.reclaim` (`reclaim_stuck(ctx.pool)`, return `reclaimed N jobs`) and `embeddings.requeue` (`requeue_embeddings(ctx.pool)`, return `queued N embed_record jobs`).
      2. `panel/api_ops.py`: `build_ops_routes(ctx, runner=None)` (a default `Runner(ctx, inproc=run_inproc)` is created when `runner` is None) with `POST /api/ops` (body keys exactly `op`, `args`, `dry_run`, `config_sha256`, the last two optional; `ops.validate` raising `OpError` gives 400 `bad_args` with the reason; `ApiBusy` gives 409 `busy`; success 202 `{"id", "state": "running"}`) and `GET /api/ops/{id}` (404 `no_such_op`). The admin email for the audit line comes from `request.state.email`.
      3. app.py: append `build_ops_routes` to `ROUTE_BUILDERS`.
      4. Tests. `test_panel_api_ops.py` (a fake runner): validation errors are 400 with the reason, busy is 409, success is 202 then `GET` returns the job, an unknown id is 404, a body with an extra key is 400. `test_panel_pipeline_ops.py` (database): `jobs.retry`, `jobs.reclaim` and `repo.rebuild` change the rows as p5's tests define, and `repo.rebuild` of an unconfigured repo is refused.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_panel_api_ops.py tests/integration/test_panel_pipeline_ops.py -q passes"
      - "grep -n 'build_ops_routes' src/knowledge_hub/panel/app.py matches"
  - id: p12-ui-core-labels
    title: UI core and the Labels tab
    depends_on: [p11b-panel-ops-routes]
    complexity: M
    touches:
      - src/knowledge_hub/panel/static/index.html
      - src/knowledge_hub/panel/static/app.js
      - src/knowledge_hub/panel/static/dom.js
      - src/knowledge_hub/panel/static/ops.js
      - src/knowledge_hub/panel/static/labels.js
      - src/knowledge_hub/panel/static/panel.css
      - tests/unit/test_panel_static.py
    brief: |
      Design 3.8. Build on the shell of p7.
      1. `dom.js`: export `h(tag, attrs, ...children)` (attributes through `setAttribute`, `class` and `disabled` handled, string children become text nodes, never HTML), `clear(node)` and `fmt(n)` (thousands separators). `ops.js`: export `opForm({op, fields, summary, previewless = false})` returning a form element plus a status area that implements the flow of Design 3.8: Preview posts `{op, args, dry_run: true}` (plus the last `config_sha256`), polls `GET /api/ops/{id}` every 1000 ms until `state` is `done`, prints the output in a `pre`, enables Apply only when the Preview exit code was 0 and the fields are unchanged since; Apply posts `dry_run: false` with the `config_sha256` of the Preview, shows the output and, when `restart_expected`, the restart message of Design 3.8; a 409 `busy` or a result whose output contains `admin: cannot lock` shows `Another change is running; try again in a minute`. With `previewless: true` there is no Preview button, Apply is enabled at once and sends `dry_run: false` (used for ops that have no dry run). Export also `confirmButton(label, onConfirm)` that asks `window.confirm`.
      2. `labels.js`: registers `window.panelTabs.labels` which loads `/api/labels` and renders a table (name, description, state, documents, records, tech repos) with per-row buttons for describe (an inline form), archive or unarchive, and remove (disabled while documents, records or tech repos use the label, with the reason as a `title`), and an Add form (name, description); every action goes through `opForm`, and after a successful Apply the table reloads.
      3. index.html: add the Labels `section` and the script tags for the new modules (`type="module"`); app.js: import `labels.js` for its side effect; panel.css: classes for forms, `pre` output and the table actions.
      4. Extend `tests/unit/test_panel_static.py`: every `"/api/..."` literal in `panel/static/*.js` (first replace every `\$\{[^}]*\}` by `{id}`, then match the regex `["'`](/api/[A-Za-z0-9_/{}.-]*)`, with a `{id}` segment matching one path segment) matches a method and path registered by building the app with every builder in `ROUTE_BUILDERS` and a stub context; `labels.js` uses only `label.*` ops that exist in `ops.OPS`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_panel_static.py -q passes"
      - "grep -c 'innerHTML' src/knowledge_hub/panel/static/*.js reports 0 for every file"
  - id: p13-ui-groups-repos
    title: The Groups tab with per-group figures, and the Repos tab
    depends_on: [p12-ui-core-labels]
    complexity: M
    touches:
      - src/knowledge_hub/panel/static/index.html
      - src/knowledge_hub/panel/static/app.js
      - src/knowledge_hub/panel/static/groups.js
      - src/knowledge_hub/panel/static/repos.js
      - src/knowledge_hub/panel/static/panel.css
      - tests/unit/test_panel_static.py
    brief: |
      Design 3.4 and 3.8. Use `h`, `opForm` and `confirmButton` of p12.
      1. `groups.js`: `window.panelTabs.groups` loads `/api/groups` and renders one card per group: header with name, repo, state (active or archived) and Slack channel; a figures table from `stats` (documents T0, documents T0a, stale documents, records, outdated records, chunks per tier, embedded chunks as `n of m`, document characters, last indexed and last ingested times formatted with `toLocaleString`); members and approvers as removable chips (buttons) that run `member.remove` and `approver.remove` (a member who is an approver: the remove button runs `member.remove` with `with_approver: true` after `confirmButton`); an add-member select filled from `people` (`member.add`) and an add-approver select filled from the members (`approver.add`); the directory references as a read-only list headed `From the directory (read only)`; archive, unarchive and remove buttons (`group.remove` with `confirm: true` only after `confirmButton` whose text names the group). Below the cards: an Add group form (name, members and approvers as comma lists) and a People section (table from `people`, an add form with login and email for `person.add`, a remove button with `person.remove`, `from_groups: true` after `confirmButton`).
      2. `repos.js`: `window.panelTabs.repos` loads `/api/repos` and renders a table of group repos (read only) and tech repos (name, dirs, labels, documents, last indexed sha shortened to 8 characters, indexed time) with edit (dirs and labels, `repo.set`), remove (`repo.remove`, `confirm: true` after `confirmButton`) and an Add form (`repo.add`).
      3. index.html: add the Groups and Repos sections; app.js: import both modules; panel.css: card and chip styles.
      4. The route-literal and op-name checks of `test_panel_static.py` cover the new scripts (every `op:` string literal used in `groups.js` and `repos.js` exists in `ops.OPS`).
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_panel_static.py -q passes"
      - "grep -c 'innerHTML' src/knowledge_hub/panel/static/*.js reports 0 for every file"
  - id: p14-ui-pipeline-health
    title: The Pipeline and Health tabs
    depends_on: [p13-ui-groups-repos]
    complexity: M
    touches:
      - src/knowledge_hub/panel/static/index.html
      - src/knowledge_hub/panel/static/app.js
      - src/knowledge_hub/panel/static/pipeline.js
      - src/knowledge_hub/panel/static/health.js
      - src/knowledge_hub/panel/static/panel.css
      - tests/unit/test_panel_static.py
    brief: |
      Design 3.4, 3.5, 3.7 and 3.8.
      1. `pipeline.js`: `window.panelTabs.pipeline` loads `/api/pipeline` and renders: job counters (queued, running, done, failed retrying, failed dead), the oldest queued age, stuck running jobs and records without chunks; a Failed jobs table (id, kind, repo, attempts, dead flag, error, times) with a Retry dead jobs button (`jobs.retry`, no ids) and a Retry button per dead row (`jobs.retry` with `ids: [id]`); a Reclaim stuck jobs button (`jobs.reclaim`, shown when `stuck_running > 0`); a Queue embeddings button (`embeddings.requeue`); a Repos table (repo, last indexed sha, indexed time, a Rebuild button running `repo.rebuild` after `confirmButton`); the Alerts table. These ops have no dry run: use `opForm` with `previewless: true` (implemented in p12). The page reloads the data after a done op and refreshes every 15 seconds while the tab is open.
      2. `health.js`: `window.panelTabs.health` loads `/api/health`, renders each check as a row with a status label and class (`ok`, `warn`, `fail`, `n/a`), and two buttons, `Run kh setup plan` (`health.setup_plan`) and `Run kh check` (`health.index_check`), whose output shows in a `pre` (use `previewless`); it refreshes every 15 seconds while open and keeps polling for 90 seconds after a config Apply until the `hub` check is ok.
      3. index.html: add the sections; app.js: import both; panel.css: counters and status styles.
      4. Extend the static test so every `op:` literal in `pipeline.js` and `health.js` exists in `ops.OPS`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_panel_static.py -q passes"
      - "grep -c 'innerHTML' src/knowledge_hub/panel/static/*.js reports 0 for every file"
  - id: p15-deploy-example
    title: The panel service in the compose example and the example config
    depends_on: [p7-panel-app-shell, p3-directory-watch]
    complexity: S
    touches:
      - deploy/compose.example.yaml
      - deploy/kh.example.yaml
      - tests/unit/test_deploy_files.py
    brief: |
      Design 3.1, 3.2, 3.6 and the Risks section.
      1. `deploy/kh.example.yaml`: add a top-level `admins:` list with one placeholder lower-case email on `example.com` and a comment line saying that only these emails may sign in to the admin panel and that the panel does not edit this key.
      2. `deploy/compose.example.yaml`: add the service `knowledge-hub-panel` after `knowledge-hub-directory`: the same image and tag, `command: ["panel", "serve", "--port", "8081"]`, `restart: unless-stopped`, networks `[db, kh, egress]` with a comment that egress is only for the sign-in issuer and that the panel must be published only to a private network or behind an access proxy (no `ports:` entry), `read_only: true`, `tmpfs: [/tmp]`, `cap_drop: [ALL]`, `security_opt`, the read-write directory mount `./config:/etc/kh` (a comment says it must be a directory because the commit renames a file and keeps a lock file), the environment: `KH_ENV`, `KH_DB_HOST`, `KH_DB_NAME`, `KH_DB_MIGRATOR_PASSWORD_FILE`, `KH_DB_APP_PASSWORD_FILE`, `KH_DB_INDEXER_PASSWORD_FILE`, `KH_CONFIG_FILE`, `KH_FORGEJO_URL`, `KH_FORGEJO_BOT_TOKEN_FILE`, `KH_FORGEJO_INDEXER_TOKEN_FILE`, `KH_WEBHOOK_HMAC_FILE`, `KH_EMBED_PROVIDER`, `KH_OLLAMA_URL`, `KH_S3_ENDPOINT`, `KH_S3_BUCKET`, `KH_S3_ACCESS_KEY_FILE`, `KH_S3_SECRET_KEY_FILE`, `KH_CURSOR_KEY_FILE`, `KH_SETUP_PG_ADMIN_DSN_FILE`, `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE` (the variables `kh check` and `kh setup plan` need, a comment names them), and `KH_PANEL_PUBLIC_URL`, `KH_PANEL_OIDC_ISSUER`, `KH_PANEL_OIDC_CLIENT_ID`, `KH_PANEL_OIDC_CLIENT_SECRET_FILE: /run/secrets/kh_panel_oidc_client_secret`, `KH_PANEL_OIDC_ALLOWED_DOMAINS`, `KH_PANEL_OIDC_HOSTED_DOMAINS`, `KH_PANEL_SESSION_KEY_FILE: /run/secrets/kh_panel_session_key`, `KH_PANEL_HUB_URL: "http://knowledge-hub:8080"`; `security_opt: ["no-new-privileges:true"]`, the `secrets:` list `pg_kh_migrator`, `pg_kh_app`, `pg_kh_indexer`, `kh_hub_bot_token`, `kh_indexer_token`, `kh_webhook_hmac`, `kh_s3_access_key`, `kh_s3_secret_key`, `kh_cursor_key`, `kh_setup_pg_admin_dsn`, `kh_setup_forgejo_admin_token`, `kh_panel_oidc_client_secret`, `kh_panel_session_key` (each mounted at `/run/secrets/<name>`), and top-level `secrets:` entries for the new names in the style of the existing ones (`{file: /etc/stack-secrets/<name>}`); a healthcheck calling `http://127.0.0.1:8081/healthz`. The panel's environment has no `KH_PUBLIC_URL` and no `KH_OIDC_*`. Add a comment above the file's services: every kh container must run the same image tag, and `kh serve` exits with code 75 when `kh.yaml` changes so `restart: unless-stopped` brings it back.
      3. `tests/unit/test_deploy_files.py`: extend `test_compose_example_has_no_secret_values` to the service `knowledge-hub-panel`; add `test_panel_service_is_wired` asserting the command, no `ports` key, a restart policy, the config mount without `:ro`, no `KH_OIDC_*` or `KH_PUBLIC_URL` key, every `KH_PANEL_*` key of `PanelSettings` that is required in prod is present, and that `PanelSettings` loads from the service's environment with the file paths replaced by temp files; add `test_example_config_has_admins` asserting `load_config("deploy/kh.example.yaml").admins` is a non-empty list.
      Do not edit any file under `.github/`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_deploy_files.py -q passes"
      - "grep -n 'knowledge-hub-panel' deploy/compose.example.yaml matches"
  - id: p16-docs-retirement
    title: Documentation, the changelog and the retirement notes
    depends_on: [p14-ui-pipeline-health, p15-deploy-example, p12-ui-core-labels]
    complexity: M
    touches:
      - README.md
      - docs/configuration.md
      - docs/runbook.md
      - docs/design.md
      - CHANGELOG.md
    brief: |
      Design 3 as built; read the final code of the earlier phases before writing. No ADR directory and no docs index exist in this repository. Write plain prose in the style of the surrounding docs; each file stays under 400 lines.
      1. README.md: add `kh panel serve` to the `kh` commands table, add `kh rebuild --queue` to the `kh rebuild` row, and a link `[Admin panel](docs/runbook.md#admin-panel)` in the Documentation list.
      2. docs/configuration.md: under "Environment variables" add a subsection `### Admin panel (kh panel serve only)` with the table of `PanelSettings` (variable, default, meaning) and `KH_CONFIG_WATCH_SECONDS`; under "kh.yaml" document `admins` (lower-case emails, at most 50, not editable through the panel, ignored by the restart check); under "Commands" document `kh panel serve [--port N]`, `kh rebuild --repo R --queue`, the exit code 75 of `kh serve` and `kh directory sync --every`, and `--if-sha256` of the admin commands.
      3. docs/runbook.md: add `## Admin panel` (anchor `admin-panel`) with: what it is and what it holds; first start in order (OIDC client and redirect URI `https://<panel host>/auth/callback`, the two secret files, `admins`, the compose service, same image tag everywhere); the five tabs and what each does; how a change reaches the hub (Apply then the hub exits with 75 and restarts, the Health `hub` check turns ok); what to do when `kh.yaml` is invalid (the panel answers 403, `kh serve` keeps the old config, fix the file by hand or with `kh label|group|person|repo`); do not hand-edit `kh.yaml` while an op runs (the second check refuses and says so); a failed dead job: Retry; the asynchronous rebuild; the accepted 8 h cookie window. Change the sentence in "Adding a group" step 3 (`Restart kh serve, because the config is read at start.`) to say that `kh serve` restarts itself within `KH_CONFIG_WATCH_SECONDS` plus the debounce when the restart policy allows it, and the same for any other runbook sentence that tells the operator to restart after a config change (use `grep -n -i "restart" docs/runbook.md docs/configuration.md docs/design.md`; the sentences in `docs/configuration.md` and `docs/design.md:111` that say a change to `kh.yaml` needs a restart are updated the same way).
      4. docs/design.md: add `## Admin panel` before `## Rejected alternatives` with the trust and credential split, the sign-in and cookie rules, the request rules, the subprocess-only write path and its argument hygiene, the exit-75 restart and its debounce, the queued rebuild, the accepted residuals (8 h cookie window; `reclaim_stuck` can repeat work; the compose template of tkcs-work-compose must carry `admins` and the panel must own `tech_repos` before both tools run together), and add the rows of Design 3.9 to the Rejected alternatives table.
      5. CHANGELOG.md under `## [Unreleased]`: in the first `### Added` list one bullet each for the admin panel (`kh panel serve`), the `admins` key, `kh rebuild --queue`, `--if-sha256` and the panel compose service; one `### Changed` bullet for `kh serve` and `kh directory sync --every` exiting with code 75 when `kh.yaml` changes validly (set `KH_CONFIG_WATCH_SECONDS=0` to switch it off) and for the `/healthz` header. Do not open a version heading.
      6. Run `.venv/bin/python -m pytest tests/unit/test_docs_links.py -q`.
    acceptance:
      - ".venv/bin/python -m pytest tests/unit/test_docs_links.py -q passes"
      - ".venv/bin/python -m pytest -q passes"
      - "grep -n '## Admin panel' docs/runbook.md docs/design.md matches both files"
      - "grep -n 'kh panel serve' README.md docs/configuration.md CHANGELOG.md matches all three files"
```
