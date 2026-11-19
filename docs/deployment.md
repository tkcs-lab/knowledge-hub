# Deployment guide

What the hub needs, which secret goes into which container, where each `kh` command runs, which jobs you schedule, and the order of the first deploy, the checks and the upgrade.
Day-two work is in the [runbook](runbook.md); sign-in, directory and hub keys are in the [identity runbook](runbook-identity.md); every variable is in [configuration](configuration.md).

## What you deploy and what you bring

`deploy/compose.example.yaml` has four services:

- `knowledge-hub`: `kh serve`, the HTTP app (MCP, OAuth, Forgejo webhook, `/healthz`) and the job worker.
- `knowledge-hub-directory`: `kh directory sync --every 300`, the only container that holds a directory credential. Start it only when `directory.provider` is set.
- `knowledge-hub-panel`: `kh panel serve`, the admin panel.
- `ollama`: embeddings with the model `bge-m3`.

You bring: PostgreSQL, Forgejo, an S3-compatible store, a reverse proxy or edge, timers (see "Scheduled jobs"), and a Forgejo account for every directory person.

This guide is a contract with that stack: this repository cannot test how you run Postgres, Forgejo, the store, the proxy or the timers. It states what the hub needs from each and how to see whether it is there.

```
 internet --> [proxy / edge] --> knowledge-hub --+--> postgres      (db, internal)
                                  |  |  |        +--> forgejo       (kh, internal)
 private net / access proxy       |  |  |        +--> s3 store      (kh, internal)
        |                         |  |  +--------+--> ollama        (kh, internal)
        +--> knowledge-hub-panel -+  |
                                     |
 knowledge-hub-directory --> postgres (db) and the directory provider (egress)
 sign-in issuer (OIDC) <-- egress -- knowledge-hub, knowledge-hub-panel
 timers (yours): kh daily, kh setup plan, kh setup apply --only forgejo, backup
```

## Dependencies

| Component | Version | Where pinned | How verified |
|---|---|---|---|
| Python | `>=3.12`; image base `python:3.13-slim`; the release image is `linux/arm64` only | `pyproject.toml` `requires-python`, `Dockerfile`, `.github/workflows/release.yml` | CI runs 3.12 and 3.13 (`.github/workflows/ci.yml`) |
| Python libraries | `mcp==2.3.0`, `ruamel.yaml>=0.18,<0.19`; the others are not pinned and there is no lockfile | `pyproject.toml` | CI installs current versions on each run |
| PostgreSQL | 16 with pgvector (extension `vector`); roles `kh_migrator`, `kh_app`, `kh_indexer`, none superuser, none `BYPASSRLS`; vector search is exact, no index. Other majors: not verified | `scripts/ensure_test_pg.sh`, `.github/workflows/ci.yml` | CI tests against it; `kh setup plan` (postgres step) |
| Forgejo | not pinned, minimum not confirmed | not pinned | `kh setup plan` reads back teams, branch protection, webhook and token identity |
| Ollama | `0.40.16`, model `bge-m3`, 1024 dimensions, `mem_limit: 3g` (`KH_EMBED_PROVIDER=http` is the alternative) | `deploy/compose.example.yaml` | the setup `ollama` step |
| S3-compatible store | path-style requests, region default `garage` (`KH_S3_REGION`); Garage in the reference deployment, version not pinned | not pinned | the setup `s3` step (put, get, delete probe) |
| OIDC provider | Google (`KH_OIDC_HOSTED_DOMAINS` is mandatory; the directory needs the Admin SDK and domain-wide delegation) or Keycloak; ID tokens must be RS256 or ES256 | not pinned | by hand, see the [identity runbook](runbook-identity.md) |
| Reverse proxy or edge | operator-provided, in front of the public paths; the panel sits on a private network or behind an access proxy | not pinned | by hand, not verified |
| n8n (optional client) | sends `Authorization: Bearer khk_<id>.<secret>`; version not pinned; support for the hub's streamable HTTP endpoint is not confirmed | not pinned | by hand |
| Slack incoming webhooks (optional) | not pinned | not pinned | by hand |
| Backup tools | `pg_dump`, `pg_restore`, the `aws` CLI and restic, used by `deploy/backup-kh.sh` | not pinned | not verified: the script is not run in CI |
| GHCR | `ghcr.io/tkcs-lab/knowledge-hub`, tags `X.Y.Z`, no `latest` | `deploy/compose.example.yaml` | `docker buildx imagetools inspect` (see "Upgrade") |

The admin panel adds no component and no Python dependency. Its container needs Postgres, the hub at `KH_PANEL_HUB_URL` (`/healthz`), Forgejo, Ollama and the S3 store (for `setup plan` and `kh check`), and egress to the OIDC issuer.

The image is the supported artefact. A wheel install takes the current versions of every dependency that `pyproject.toml` does not pin.

## Networks, hosts and public paths

The compose example has three networks: `db` and `kh` are internal (no internet), `egress` is not internal.

| Service | Networks |
|---|---|
| `knowledge-hub` | `db`, `kh`, `egress` (only for the sign-in) |
| `knowledge-hub-directory` | `db`, `egress` |
| `knowledge-hub-panel` | `db`, `kh`, `egress` (only for the sign-in issuer) |
| `ollama` | `kh` |

`egress` is not filtered by the example: allow only the hosts below at a proxy or firewall.

- Hub sign-in with Google: `accounts.google.com`, `oauth2.googleapis.com`, `www.googleapis.com`.
- Directory sync with Google: `oauth2.googleapis.com` and `admin.googleapis.com`. With Keycloak: only the internal admin URL.
- The panel needs egress to the issuer of its own sign-in (`KH_PANEL_OIDC_ISSUER`).

The paths that may be reachable from the internet are listed in the [identity runbook](runbook-identity.md), "Public paths". Do not copy that list into a proxy rule set from here. The panel is never published on the internet: put it on a private network or behind an access proxy.

## Secrets

Every secret is a file; the compose example mounts it at `/run/secrets/<name>` and names it in a `*_FILE` variable. What each variable means is in [configuration](configuration.md).

| Secret file | Variable | Mounted by | Needed for |
|---|---|---|---|
| `pg_kh_migrator` | `KH_DB_MIGRATOR_PASSWORD_FILE` | hub, panel | `kh migrate`, `kh setup`, admin commands, `kh key`, `kh directory plan\|people\|show` |
| `pg_kh_app` | `KH_DB_APP_PASSWORD_FILE` | hub, panel | the search and tool queries |
| `pg_kh_indexer` | `KH_DB_INDEXER_PASSWORD_FILE` | hub, directory, panel | worker, rebuild, daily, directory sync |
| `kh_hub_bot_token` | `KH_FORGEJO_BOT_TOKEN_FILE` | hub, panel | opening pull requests as `bot_login` |
| `kh_indexer_token` | `KH_FORGEJO_INDEXER_TOKEN_FILE` | hub, panel | read-only Forgejo access as `indexer_login` |
| `kh_webhook_hmac` | `KH_WEBHOOK_HMAC_FILE` | hub, panel | checking `/webhooks/forgejo` |
| `kh_s3_access_key` | `KH_S3_ACCESS_KEY_FILE` | hub, panel | record bodies in the S3 store |
| `kh_s3_secret_key` | `KH_S3_SECRET_KEY_FILE` | hub, panel | record bodies in the S3 store |
| `kh_cursor_key` | `KH_CURSOR_KEY_FILE` | hub, panel | signing result cursors |
| `kh_metrics_token` | `KH_METRICS_TOKEN_FILE` | hub | `/metrics` (on only when set) |
| `kh_oidc_client_secret` | `KH_OIDC_CLIENT_SECRET_FILE` | hub | the hub's OAuth sign-in |
| `kh_directory_google_key` | `KH_DIRECTORY_GOOGLE_CREDENTIALS_FILE` | directory | the Google directory provider |
| `kh_directory_keycloak_secret` | `KH_DIRECTORY_KEYCLOAK_CLIENT_SECRET_FILE` | directory | the Keycloak directory provider |
| `kh_setup_pg_admin_dsn` | `KH_SETUP_PG_ADMIN_DSN_FILE` | panel | `kh setup`: roles, database, extension (without it the postgres step only checks) |
| `kh_setup_forgejo_admin_token` | `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE` | panel | `kh setup`: the forgejo step (required) |
| `kh_panel_oidc_client_secret` | `KH_PANEL_OIDC_CLIENT_SECRET_FILE` | panel | the panel's sign-in |
| `kh_panel_session_key` | `KH_PANEL_SESSION_KEY_FILE` | panel | signing panel sessions |
| (none in the example) | `KH_SETUP_SLACK_WEBHOOKS_FILE` | no example service | `kh setup`, and the panel when a group has `slack_channel` |

A mounted secret file must exist, or compose fails to start the service. For the directory provider you do not use, an empty file is enough: only `directory.provider` decides which credential is read (`build_provider` in `src/knowledge_hub/directory/provider.py` builds only the provider it is given).

## Where each command runs

| Command | Container | Database role | Extra secrets |
|---|---|---|---|
| `kh serve`, `kh rebuild` (also `--queue`), `kh check`, `kh daily`, `kh requeue-embeddings` | `knowledge-hub` | app and indexer | none; needs `KH_OLLAMA_URL` (with `KH_EMBED_PROVIDER=ollama`) and, with `KH_FORGEJO_URL`, the indexer token; `--queue` needs no answering server |
| `kh migrate`, `kh label\|group\|person\|repo` | one with the migrator password | migrator | none |
| `kh setup` | one-off container or the panel container | migrator | setup Forgejo admin token (required), setup Postgres admin DSN (optional), Slack webhooks file (when a group has `slack_channel`) |
| `kh key create\|list\|revoke`, `kh directory plan\|people\|show` | `knowledge-hub` (it has the migrator password) | migrator | none |
| `kh directory sync` | `knowledge-hub-directory` | indexer | the directory credential; needs migration 0006 and the role `kh_indexer` |
| `kh panel serve` | `knowledge-hub-panel` | indexer (statistics, health reads, pipeline operations) and migrator (the `migrations` health check); its subprocesses use migrator plus the Forgejo admin token (admin commands, `setup plan`) and app plus indexer (`kh check`) | the example mounts both setup secrets, not the Slack file |
| `kh lint` | anywhere | none | none |

`kh setup` needs secrets the `knowledge-hub` service never mounts (the setup Postgres admin DSN and the setup Forgejo admin token), so it runs in a one-off container or in the panel container.

## Scheduled jobs

This repository ships none of these timers; your stack provides them.

- `kh daily`: once a day.
- `kh setup plan`: as a drift alarm, alert on exit 1 or 2.
- `kh setup apply --only forgejo`: every 15 minutes, so directory joiners and leavers reach the Forgejo teams.
- The `knowledge-hub-directory` service (`kh directory sync --every 300`), when a provider is set.
- `deploy/backup-kh.sh`.
- A job of yours that creates the Forgejo account of every new directory person.

**Forgejo accounts of directory people.** The login is the lower-case local part of the email and matches `[a-z0-9][a-z0-9_.-]{0,38}` (`login_from_email`). `kh setup` never creates users. Until the account exists, `kh setup plan` prints:

```
i account <login>: no Forgejo account yet; not added to its teams
```

An account whose email differs from the directory email prints the following, which is a `!` line, so apply exits 2:

```
! account <login>: email differs from directory; not added
```

**Restarts.** `kh serve` exits with code 75 within `KH_CONFIG_WATCH_SECONDS` plus a debounce after a valid change of `kh.yaml` (not of `admins` or comments); `restart: unless-stopped` brings it back. `kh directory sync --every` exits with code 75 as well, but it polls the file once per sleep and debounces over two polls, so it restarts one to two sync intervals after the change (5 to 10 minutes at 300). `KH_CONFIG_WATCH_SECONDS` only switches that watch on (above 0). With `directory.provider: none` the sync exits 0 with a note and `unless-stopped` restarts it in a loop: start the service only when a provider is set.

## First deploy

On an empty database `kh directory sync --once` cannot run first: the role `kh_indexer`, the database and the function `kh.directory_snapshot_put` (migration 0006) do not exist yet. The order is:

1. **Prepare.**
   - Have Postgres, Forgejo, the S3 store and Ollama running.
   - In Forgejo create the organisation named by `org` and two ordinary accounts, `bot_login` and `indexer_login`. Neither is an org owner or a site admin.
   - Write the secret files of "Secrets".
   - Copy `deploy/kh.example.yaml` to `./config/kh.yaml` next to the compose file and edit it.
   - Pull the Ollama model with a one-off container that shares the volume `kh-ollama-models`:

   ```bash
   docker run --rm -v kh-ollama-models:/root/.ollama --entrypoint sh ollama/ollama:0.40.16 \
     -c 'ollama serve >/dev/null 2>&1 & until ollama list >/dev/null 2>&1; do sleep 1; done; ollama pull bge-m3'
   ```

   This runs before the first setup because Ollama sits on the internal `kh` network and cannot download the model. Otherwise the setup `ollama` step fails, the later steps are skipped and `kh setup apply` exits 2. Until the model is available embedding fails; run `kh rebuild` and `kh requeue-embeddings` afterwards. To measure speed: `docker compose exec -T knowledge-hub python - --count 200 < scripts/bench_embed.py` prints chunks per second.
2. **Plan the database.** `kh setup plan --only postgres --config config/kh.yaml`. Expect `+` lines for the roles and the database, exit 1. A full `kh setup plan` on an empty server exits 2 and prints `setup: labels: <error>`, because the labels step cannot connect to the missing database.
3. **Apply.** With a directory provider: `kh setup apply --only postgres`, then `kh directory sync --once`, then `kh setup apply`. Without a provider: `kh setup apply`. A plain `kh setup apply` with a provider set and no snapshot still runs every later step, but ends with `! directory: snapshot missing` and exit 2, and directory-only members stay out of the Forgejo teams until the next apply.
4. **Plan again.** `kh setup plan`: only `i` lines, exit 0.
5. **Start the services.** `kh serve` (the image does it; the worker starts only when `KH_FORGEJO_URL` is set and `/healthz` answers `ok`), the sync service when a provider is set, and the panel when you want it (first start: [runbook](runbook.md), "Admin panel").
6. **Index.** Run `kh rebuild`, then `kh check` and expect exit 0.
7. **Identity.** Set up sign-in, the directory and hub keys as the [identity runbook](runbook-identity.md) says.

## Verify

After the first deploy:

- A second `kh setup plan` shows only `i` lines.
- In Forgejo, Settings, Branches of a repo: direct push is off, the merge whitelist is exactly `<group>-approve`, admins are bound.
- Merge a `propose_doc` pull request as an approver and find the document with `search`.
- A listed person reads only their groups.
- A person whose sign-in email is in neither `people` nor the directory reads nothing.
- Add a third group and run apply: its team, repo and protection appear and the first two groups are untouched.
- Any `+`, `~` or `!` line after apply is a bug.

The search similarity floor is calibrated once after the first deploy: see [runbook](runbook.md), "Calibrating the search similarity floor".

## Upgrade

1. All kh services (`knowledge-hub`, `knowledge-hub-directory`, `knowledge-hub-panel`, one-off commands) run the same image tag. Pin the digest: `docker buildx imagetools inspect ghcr.io/tkcs-lab/knowledge-hub:X.Y.Z`. There is no `latest` tag.
2. `kh serve` does not check the schema. Run `kh setup apply --only postgres` (or `kh migrate`) with the new image before you restart anything.
3. Restart `kh serve`, the sync service and the panel.
4. `kh setup plan` expects exit 0; then run `kh check`.
5. `kh migrate` and the postgres step refuse a database that holds a migration the image does not ship, with `database predates the baseline; a fresh install is required` (`kh serve` itself checks nothing). An older image says the same after migration 0006; the rollback is in the [identity runbook](runbook-identity.md), "Moving from PrivacyFence".
6. The identity release (OIDC sign-in, directory, hub keys): follow the [identity runbook](runbook-identity.md), "Moving from PrivacyFence".
7. Without Docker: `pip install knowledge_hub-X.Y.Z-py3-none-any.whl`, then `kh setup plan --config kh.yaml`. The wheel does not pin its dependencies.

## Backup and restore

The backup is `deploy/backup-kh.sh`. The restore, and what the dump does not contain, are in [runbook](runbook.md), section "Backup and restore".
