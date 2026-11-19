# Knowledge hub configuration

Every `KH_*` environment variable read by `knowledge_hub/settings.py`, every key of the `kh.yaml` file named by `KH_CONFIG_FILE`, the `kh` commands and `kh setup`.
Secrets are never variables: a `*_FILE` setting names a file that holds the secret.

## Environment variables

Unknown `KH_*` variables are ignored. `kh serve` and every other command exit 2 on a settings or config error.

### Runtime and caller verification

| Variable | Default | Meaning |
|---|---|---|
| `KH_ENV` | `prod` | `dev`, `test` or `prod`. `prod` refuses `KH_DEV_IDENTITY` and the fake embedder. |
| `KH_DEV_IDENTITY` | none | JSON `{"sub", "principal_type", "audiences", "email"?}` used for requests with no `Authorization` header (a malformed one is a 401, never the dev identity). `principal_type` is `claude_client` or `n8n_workflow`. Refused when `KH_ENV=prod` and when OAuth is on. `audiences` is ignored for a `claude_client`: its groups come from its `email` through `people` in `kh.yaml`, so a dev identity without a listed email reads nothing. For an `n8n_workflow` the audiences that are configured groups are held. |
| `KH_ALLOWED_HOSTS` | empty | Comma list of `host:port` entries accepted in the `Host` header (DNS-rebinding protection), for example `knowledge-hub:8080`. A bare host gets HTTP 421. |
| `KH_MAX_BODY_BYTES` | `1048576` | Largest `/mcp` request body; larger gets 413. |

### Sign-in and OAuth

Secrets only as `*_FILE`. OAuth is on only when `KH_PUBLIC_URL` and all three `KH_OIDC_*` settings are set; some but not all exits 2 with `KH_OIDC_* and KH_PUBLIC_URL must be set together`. With OAuth off only hub keys work. `kh serve` logs `auth paths: oauth=<on|off> keys=on dev_identity=<on|off>`. The old PrivacyFence variables are gone; a leftover one is ignored like any unknown `KH_*` variable.

| Variable | Default | Meaning |
|---|---|---|
| `KH_PUBLIC_URL` | none | `https://host` with no path, userinfo, query, fragment or trailing slash; `http://` only when `KH_ENV` is `dev` or `test` and the host is `localhost`, `127.0.0.1` or `[::1]`. It is the authorization server's issuer; the resource is `<KH_PUBLIC_URL>/mcp`. |
| `KH_OIDC_ISSUER` | none | Issuer of the upstream OIDC provider that checks the password (Google or Keycloak). |
| `KH_OIDC_CLIENT_ID` | none | The hub's client id at that provider. |
| `KH_OIDC_CLIENT_SECRET_FILE` | none | File with the client secret. |
| `KH_OIDC_ALLOWED_DOMAINS` | empty | Comma list of lower-case email domains; required when OAuth is on (`KH_OIDC_ALLOWED_DOMAINS is required when OAuth is on`). |
| `KH_OIDC_HOSTED_DOMAINS` | none | Comma list; when set the ID token's `hd` must be in it. Required for a Google issuer (`accounts.google.com`): `KH_OIDC_HOSTED_DOMAINS is required for a Google issuer`. |
| `KH_OAUTH_DCR` | `true` | Mount dynamic client registration (`/register`). |
| `KH_OAUTH_REDIRECT_ALLOWLIST` | `https://claude.ai/api/mcp/auth_callback,http://localhost,http://127.0.0.1` | Comma list. An `https` entry matches only the identical URI; an `http://localhost` or `http://127.0.0.1` entry matches any port and path on that exact host, with no userinfo or fragment. Checked at registration, at `/authorize` and in the callback. |
| `KH_OAUTH_ACCESS_TTL_SECONDS` | `3600` | 300 to 86400. |
| `KH_OAUTH_REFRESH_TTL_DAYS` | `30` | 1 to 365; also the length of a refresh family counted from the first sign-in. |
| `KH_OAUTH_MAX_CLIENTS` | `200` | Registered clients that were authorised plus unauthorised ones younger than one hour; more is refused. `kh daily` purges old ones. |
| `KH_OAUTH_REGISTER_PER_MIN` | `30` | Registration requests per minute for the whole hub. |
| `KH_OAUTH_AUTHORIZE_PER_MIN` | `600` | Requests per minute for the whole hub to `/authorize`, `/oauth/callback` and `/oauth/consent` together (429 above it). |

### Directory

Read by `kh serve` (the view) and by `kh directory` and `kh setup`. The sync is a separate process with its own secret files: `kh serve` never needs the Google key or the Keycloak secret. The hub never holds admin credentials; `kh setup` does.

| Variable | Default | Meaning |
|---|---|---|
| `KH_DIRECTORY_VIEW_SECONDS` | `30` | 5 to 600: how often `kh serve` re-reads the snapshot. |
| `KH_DIRECTORY_GOOGLE_CREDENTIALS_FILE` | none | Service account key (JSON) for domain-wide delegation. |
| `KH_DIRECTORY_GOOGLE_SUBJECT` | none | Admin address the key impersonates (a dedicated reader account with a custom role: Groups read and Users read). |
| `KH_DIRECTORY_KEYCLOAK_URL` | none | Base URL, no trailing slash; `https` unless `KH_ENV` is `dev` or `test` or the host is a single-label name such as `keycloak` on the internal admin network. |
| `KH_DIRECTORY_KEYCLOAK_REALM` | none | Realm name. |
| `KH_DIRECTORY_KEYCLOAK_CLIENT_ID` | none | Service-account client with the realm-management roles `view-users`, `query-users` and `query-groups`. |
| `KH_DIRECTORY_KEYCLOAK_CLIENT_SECRET_FILE` | none | File with that client's secret. |
| `KH_DIRECTORY_KEYCLOAK_SUBGROUPS` | `false` | `true` also reads the members of subgroups. Off by default: anyone who manages a subgroup could otherwise grant access to the parent. |

### Database

| Variable | Default | Meaning |
|---|---|---|
| `KH_DB_HOST` | `postgres` | Postgres host; a directory path means a unix socket. |
| `KH_DB_PORT` | `5432` | Postgres port. |
| `KH_DB_NAME` | `kh` | Database name. |
| `KH_DB_MIGRATOR_PASSWORD_FILE` | none | Password file for `kh_migrator` (`kh migrate`, `kh setup`). No file means no password. |
| `KH_DB_APP_PASSWORD_FILE` | none | Password file for `kh_app` (the request path). |
| `KH_DB_INDEXER_PASSWORD_FILE` | none | Password file for `kh_indexer` (worker, rebuild, check, daily, requeue). |

### Config file, Forgejo and webhook

| Variable | Default | Meaning |
|---|---|---|
| `KH_CONFIG_FILE` | none | Path of `kh.yaml`. Without it the built-in defaults apply (no people, no groups). |
| `KH_FORGEJO_URL` | none | Forgejo base URL. The worker exists only when it is set. The Forgejo organisation is `org` in `kh.yaml`. |
| `KH_FORGEJO_BOT_TOKEN_FILE` | none | Token of the `bot_login` account, used only to open `propose_doc` pull requests. |
| `KH_FORGEJO_INDEXER_TOKEN_FILE` | none | Read-only token of the `indexer_login` account. Required when `KH_FORGEJO_URL` is set. |
| `KH_TECH_POLL_MINUTES` | `60` | Minutes between polls that queue a `git_push` job for every repo in `tech_repos`, 0 to 10080; 0 switches the poll off. Needs `KH_FORGEJO_URL`. |
| `KH_WEBHOOK_HMAC_FILE` | none | Secret for `X-Forgejo-Signature`. A missing or empty secret makes `/webhooks/forgejo` answer 401. `kh setup` also writes it into the org webhook. |

### Setup credentials (`kh setup` only)

| Variable | Default | Meaning |
|---|---|---|
| `KH_SETUP_PG_ADMIN_DSN_FILE` | none | File with a Postgres superuser (or role-creating) DSN. Optional: without it the `postgres` step only checks. |
| `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE` | none | Token of a Forgejo site administrator. Required by the `forgejo` step. |
| `KH_SETUP_SLACK_WEBHOOKS_FILE` | none | YAML file mapping group name to its Slack incoming-webhook URL (`https://`). Needed only for groups with `slack_channel`. |

The `kh serve` service never mounts these files. The hub never holds admin credentials; `kh setup` does. The directory sync is a separate process with its own secret files (see "Directory").

### Embeddings

| Variable | Default | Meaning |
|---|---|---|
| `KH_EMBED_PROVIDER` | `ollama` | `ollama`, `http` or `fake`. `fake` is allowed only with `KH_ENV` `dev` or `test`. Chosen once per deployment, for documents and queries alike. |
| `KH_OLLAMA_URL` | none | Ollama base URL (`POST /api/embed`). Required for `ollama`. |
| `KH_EMBED_MODEL` | `bge-m3` | Model name. Vectors are compared only with chunks of the same model. |
| `KH_EMBED_DIM` | `1024` | Vector size; the provider refuses any other. The column is `vector(1024)`. |
| `KH_EMBED_HTTP_URL` | none | Endpoint of the `http` provider; it must serve the same model as `KH_EMBED_MODEL`. |
| `KH_EMBED_HTTP_TOKEN_FILE` | none | Token file for the `http` provider. |
| `KH_EMBED_HTTP_ALLOWED_AUDIENCES` | `all` | Comma list of groups. The `http` provider embeds only chunks whose group is in it; others are found by full text only. |

### Blob storage (S3-compatible)

| Variable | Default | Meaning |
|---|---|---|
| `KH_S3_ENDPOINT` | none | Endpoint URL, path-style addressing. |
| `KH_S3_BUCKET` | none | Bucket for record bodies (`records/<uuid>`). |
| `KH_S3_ACCESS_KEY_FILE` | none | File with the access key. |
| `KH_S3_SECRET_KEY_FILE` | none | File with the secret key. |
| `KH_S3_REGION` | `garage` | Region used when signing requests. |

A partial S3 setting is a startup error. With no S3 setting `ingest_record` and `get_doc` of a record are unavailable.

### Search, limits and tools

| Variable | Default | Meaning |
|---|---|---|
| `KH_CURSOR_KEY_FILE` | none | HMAC key for `search` and list cursors. Without it `search` fails with an internal error. |
| `KH_CHUNKER_VERSION` | `1` | Part of the chunk hash; raising it re-chunks every document on the next sync. |
| `KH_RRF_K` | `60` | Reciprocal-rank-fusion constant. |
| `KH_SEARCH_POOL` | `50` | Candidates per list (vector and full text) before fusion. |
| `KH_SEARCH_MIN_SIMILARITY` | `0` | Smallest cosine similarity (0 to 1) between the query and a chunk for the chunk to enter `search`'s vector list; `0` switches the floor off. A full-text match is kept whatever its similarity. Calibrate it as the runbook says ("Calibrating the search similarity floor"). |
| `KH_TIER_WEIGHTS` | `T0:1.0,T0a:0.7,T3:0.4` | Score multiplier per tier. `T0` and `T3` are required, `T0a` is optional (missing, it is the midpoint of T0 and T3, so `T0:1.0,T3:0.4` still starts); no other name; every weight positive and `T0 >= T0a >= T3`. |
| `KH_STALE_WEIGHT` | `0.5` | Multiplier for a stale hit or an included outdated hit. |
| `KH_SIMILAR_LIMIT` | `3` | Most `similar` items `ingest_record` and `propose_doc` return, 0 to 10; 0 switches the lookup off. |
| `KH_SIMILAR_MIN_SCORE` | `0.5` | Smallest `similarity` (0 to 1) an item needs to be listed as similar. |
| `KH_SEARCH_MAX_OFFSET` | `100` | Largest offset a `search` cursor may carry; above it `CURSOR_FOREIGN`. |
| `KH_MAX_RECORD_BYTES` | `1048576` | Largest `ingest_record` body (was `KH_MAX_RAW_BYTES`). |
| `KH_PROPOSE_MAX_BYTES` | `131072` | Largest `propose_doc` body. |
| `KH_PROPOSE_MAX_OPEN` | `20` | Open bot pull requests per repo; above it `propose_doc` is refused. |
| `KH_RATE_LIMIT_PER_MIN` | `120` | Tool calls per minute per caller `sub`. The bucket is in memory, so the limit is per process. |
| `KH_METRICS_TOKEN_FILE` | none | Bearer token for `/metrics`. Without it the route does not exist. The file is read on every request, so the token can be rotated. |

### Usage log

| Variable | Default | Meaning |
|---|---|---|
| `KH_USAGE_LOG` | `true` | Log people's searches and opens in `kh.usage_events`. `false` switches logging off. Nothing is logged while `KH_CURSOR_KEY_FILE` is unset. |
| `KH_USAGE_RETENTION_DAYS` | `180` | Days to keep usage events, 1 to 3650; `kh daily` deletes older ones, and the `usage` tool never reads past it. |

`KH_CURSOR_KEY_FILE` is also the source of the searcher hash stored with each event, so rotating it resets the distinct-people counts of the usage log.

Removed settings: `KH_FORGEJO_ORG` (now `org:` in `kh.yaml`), `KH_MAX_RAW_BYTES` (renamed), `KH_FORGEJO_NOTES_TOKEN_FILE`, every `KH_AGENT_*`, `KH_CI_STATUS_LOGINS`, `KH_PROMOTION_DAILY_LIMIT`, `KH_SUPERSEDED_WEIGHT`, `KH_DISPUTED_WEIGHT` and `KH_GRIST_*`.

## Commands

| Command | Use |
|---|---|
| `kh serve [--port 8080]` | Run the HTTP app and the job worker. |
| `kh migrate` | Apply the pending migrations as `kh_migrator`. Refuses a database that predates the baseline. |
| `kh setup plan\|apply --config kh.yaml` | Check or build the environment; see below. |
| `kh label add\|rename\|archive\|unarchive\|remove\|list` | Edit the labels of `kh.yaml` and apply the change; see `kh admin commands`. |
| `kh group add\|remove\|archive\|unarchive\|member\|approver\|list` | Edit the groups of `kh.yaml` and apply the change. |
| `kh person add\|remove\|list` | Edit `people` (and with `--from-groups` the group lists) and apply the change. |
| `kh key create --group G [--tools t1,t2] --name N [--expires-days D] [--out FILE]` | Create a hub key (`khk_<id>.<secret>`) for one active group. Default tools `search,ingest_record`; `move_item` cannot be given. The key is shown once (or written to FILE, mode 0600). Errors, exit 1: `key: group <G> is not an active group`, `key: unknown tool <t>`, `key: tool <t> cannot be given to a key`, `key: name <N> is not valid`. |
| `kh key list [--json]` | Tab-separated `id name group tools expires last_used status` (`active`, `expired` or `revoked`); never a secret. |
| `kh key revoke ID` | Revoke a key; `key: <id> not found` exits 1. |
| `kh directory sync [--once \| --every N] [--accept-change]` | Fetch the groups of the provider and store the snapshot. Exit 0, 1 (`directory: <provider>: <reason>`) or 3 (refused change: rerun with `--accept-change`). `--every` loops and logs errors; the previous snapshot stays. Run it as a separate service with its own secret files. |
| `kh directory plan` | Print group, person and skip counts and one `skip <group> <role> <who>: <reason>` line per skip; exit 1 when the snapshot is missing or stale. |
| `kh directory people [--json]` | The effective people with `login`, `email`, `source` (`static` or `directory`), `groups` and `approver_of`. |
| `kh directory show` | The provider and the snapshot's age and size, or `snapshot: none`. |
| `kh rebuild [--repo R]` | Forget the indexed head and sync again. Idempotent. |
| `kh check` | Compare the index with Forgejo; exit 1 on drift (`head of <repo> is <a>, indexed <b>`, `document <path> in <repo> has no chunks`, `record <id> has no chunks`, `document <path> in <repo> has audience <a>, config says <b>`). |
| `kh daily` | Recompute `stale` flags, purge expired OAuth rows and stale flags and delete usage events past `KH_USAGE_RETENTION_DAYS`. |
| `kh requeue-embeddings` | Queue an `embed_record` job for every record without chunks. |
| `kh lint FILE...` | Offline frontmatter check, no settings or config needed; exit 1 on a problem. |

Exit 2 means a settings or config error.

## kh.yaml

One file for the hub and for `kh setup`. Unknown keys are refused and every error text starts with `config:`. `KhConfig()` with no file is valid: no people, no groups, no labels.

| Key | Default | Validation and meaning |
|---|---|---|
| `org` | `kh` | Forgejo organisation that holds the repos. A webhook from another owner is ignored. |
| `bot_login` | `kh-bot` | Account that opens every T0 pull request and is stamped as `written_by` on every T3 record. |
| `indexer_login` | `kh-indexer` | Owner of the read-only token. Must differ from `bot_login`; neither may appear in `people` or a member list. |
| `forgejo_web_url` | null | Null or an `https://` URL; the base of `source_link` (empty links when null). |
| `hub_url` | null | Null or an http(s) URL without a path: how Forgejo reaches the hub. Only `kh setup` needs it (webhook target). |
| `people` | `{}` | Map of Forgejo login to the email the sign-in verifies for that person. Logins match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}$` and are unique case-folded; an email has exactly one `@`, at most 254 characters, is stored lower-case and is unique (`config: email <e> belongs to two people`). |
| `groups` | `{}` | Map of group name to `{repo, members, approvers, archived, slack_channel}`. |
| `groups.<g>.directory` | `{}` | Map of provider name (`^[a-z][a-z0-9_]{0,30}$`, else `config: directory provider name <x> is not valid`) to `{members, approvers}`: group references of that provider (Google group addresses; Keycloak group names or `/paths`). Entries are non-empty strings of at most 254 characters, unique in a list (`config: directory reference <x> is not valid`, `config: directory reference <x> is listed twice`). Both providers' references can sit side by side; `directory.provider` picks the one in use. Only the shape is checked here. |
| `directory` | see below | Directory sync policy. |
| `directory.provider` | `none` | `none`, `google` or `keycloak` (`config: directory.provider <x> is not valid` for a malformed name; an unknown name is refused by `kh directory` and `kh serve` with `directory: unknown provider <x>`, exit 2). |
| `directory.domains` | `[]` | Lower-case email domains a person may have; required when the provider is not `none` (`config: directory.domains is required when directory.provider is set`; `config: directory.domains entry <x> is not valid`; `config: directory.domains lists <x> twice`). |
| `directory.refresh_seconds` | `300` | 60 to 3600: how often `kh directory sync --every` fetches. |
| `directory.max_stale_seconds` | `21600` | At most 604800 and at least twice `refresh_seconds` (`config: directory.max_stale_seconds must be at least twice refresh_seconds`). A snapshot older than this is ignored: directory-derived groups disappear and static entries stay. |
| `directory.shrink_guard_percent` | `50` | 1 to 99: `kh directory sync` refuses a snapshot with fewer than `100 - n` percent of the stored user count, or more than double it (from 10 users), unless `--accept-change`. |
| `groups.<g>.repo` | `kb-<g>` | Repo name, `^[a-z0-9][a-z0-9._-]{0,99}$`, unique. Immutable once applied. |
| `groups.<g>.members` | `[]` | Logins; each must be in `people` (`config: group <g> member <login> is not in people`). Members read and write. |
| `groups.<g>.approvers` | required | Non-empty subset of `members` (`config: group <g> approver <login> is not a member`); may be empty only when some `directory.<p>.approvers` of the group is non-empty (otherwise `config: group <g> needs at least one approver`). With a `directory.<p>.members` list the check is made on the effective membership instead. Approvers merge. |
| `groups.<g>.archived` | `false` | An archived group is held by nobody: its documents and records are hidden, writes into it are refused (`audience is not held by this caller or not configured`), and nothing is indexed for it. Nothing is deleted. Every other group check still applies. |
| `groups.<g>.slack_channel` | null | Null or a Slack channel `#name` (lower case, digits, `-`, `_`, at most 80 characters after `#`). `kh setup` keeps one Slack hook on the group's repo for pull request events; its URL comes from `KH_SETUP_SLACK_WEBHOOKS_FILE`. Ignored while the group is archived. |
| `labels` | `{}` | Map of label name (`^[a-z0-9][a-z0-9_.:-]{0,62}$`) to a description of at most 200 characters; at most 500 labels. |
| `archived_labels` | `[]` | Label names, each a key of `labels` (`config: archived_labels entry '<x>' is not in labels`, `config: archived_labels lists '<x>' twice`). An archived label is hidden from `list_labels`, from the `labels` of results (`search`, `get_doc`, `list_stale`, `usage`) and from search filters; `ingest_record` and `propose_doc` using it fail with `unknown label`. The label and its rows stay. |

| `tech_org` | null | Forgejo organisation that holds the clones or forks of the technical repos; a valid login that differs from `org` (case-insensitive). Required when `tech_repos` is not empty. |
| `tech_repos` | `{}` | Map of bare repository name in `tech_org` to `{labels}`. Their `CLAUDE.md` and `docs/**/*.md` are indexed as tier T0a with audience `all`, no approval. Needs the group `all`, active. |
| `tech_repos.<r>.labels` | `[]` | Labels put on every document of the repo; each must be a key of `labels`, none twice. A label archived later stays on the documents and is hidden. |

Tech repo errors: `config: tech_repos needs tech_org`, `tech_org '<x>' is not a valid login`, `tech_org must differ from org`, `tech repo '<r>' is not a valid repository name`, `tech repo <r> is also the repo of group <g>`, `tech_repos needs the group all`, `tech_repos needs the group all to be active`, `tech repo <r> label '<x>' is not in labels`, `tech repo <r> lists label '<x>' twice`. A change needs a `kh serve` restart.

The hub reads `archived` and `archived_labels` when `kh serve` starts, so changing either needs a restart.

Group names match `^[a-z0-9][a-z0-9_-]{0,62}$`, are not `owners` in any case, and `<name>-approve` may not itself be a group name. Forgejo teams are derived: `<group>` (write on its repo) and `<group>-approve` (read on its repo, in the merge whitelist).

```yaml
org: kh
bot_login: kh-bot
indexer_login: kh-indexer
hub_url: http://knowledge-hub:8080
people:
  alice: alice@example.com
  bob: bob@example.com
groups:
  all:
    members: [alice, bob]
    approvers: [alice]
  mgmt:
    members: [alice]
    approvers: [alice]
labels:
  product:ember: Ember product
tech_org: kh-tech
tech_repos:
  ember-core:
    labels: [product:ember]
```

The hub reads `kh.yaml` when `kh serve` starts, so a change to `people` or a group needs a restart; directory changes do not (the snapshot is re-read every `KH_DIRECTORY_VIEW_SECONDS`). `deploy/kh.example.yaml` is the maintained example.

## kh setup

`kh setup plan|apply --config kh.yaml [--only postgres|labels|tech|forgejo|ollama|s3] [--prune] [--create-bucket]`. `plan` is strictly read-only. `apply` performs every possible change, prints the same lines and is idempotent. `--create-bucket` matters only for `apply`. `--prune` removes objects the config no longer lists.

Steps run in this order: `postgres`, `labels`, `tech`, `forgejo`, `ollama`, `s3`. A step that fails prints `setup: <step>: <message>` and the later steps are skipped.

**Lines.** `<prefix> <object kind> <name>[: <detail>]`:

| Prefix | Meaning |
|---|---|
| `+` | create |
| `~` | update |
| `-` | remove (with `--prune`, or by `kh label remove` and `kh group remove`) |
| `!` | refused: a change the script will not make, for example `! group all: changed repo; remove the group and add a new one` |
| `?` | unmanaged object (`? unmanaged`) |
| `i` | information (`i webhook secret not verifiable`, `i owners: <logins> can bypass protection`, `i slack hook <repo>: url not verifiable`) |

Drift is any `+`, `~`, `-` or `!` line. `?` and `i` lines never change an exit code.

**Exit codes.** `plan`: 0 no drift, 1 drift, 2 error. `apply`: 0, or 2 when an error occurred or any `!` line remains.

**Credential files.** Never in the YAML: `KH_SETUP_PG_ADMIN_DSN_FILE` (optional), `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE`, `KH_SETUP_SLACK_WEBHOOKS_FILE` (optional), and the existing `KH_DB_*_PASSWORD_FILE`, `KH_FORGEJO_URL`, `KH_FORGEJO_BOT_TOKEN_FILE`, `KH_FORGEJO_INDEXER_TOKEN_FILE`, `KH_WEBHOOK_HMAC_FILE`, `KH_OLLAMA_URL`, `KH_EMBED_MODEL`, `KH_S3_*`. A missing required file prints `setup: <name> is not set` and exits 2. Setup never creates Forgejo users or tokens: a missing `bot_login` or `indexer_login` account is an error.

**Steps.**

1. `postgres`. With an admin DSN: create the roles `kh_migrator`, `kh_app` and `kh_indexer` (passwords from the `KH_DB_*_PASSWORD_FILE` files, re-set on every apply because they cannot be read back), the database owned by `kh_migrator`, `CREATE EXTENSION vector`, then run pending migrations. Without an admin DSN it only checks, and pending migrations count as drift. It then syncs `kh.groups`: groups are added; a removed group goes only with `--prune`, is refused when records exist, and its documents are deleted.
2. `labels`. Inserts new labels and updates descriptions. A removed label is deleted only with `--prune` and only when unused (`! label <x>: in use`). After a label is added, apply queues one `git_push` job per repo so files rejected for an unknown label are retried.
3. `tech`. Reports `? tech documents of <repo>: repo is not in tech_repos` for each repo that has T0a documents but is no longer in `tech_repos`; `apply --prune` deletes those documents and the repo's indexed head (`- tech documents of <repo>`). A configured repo is never touched.
4. `forgejo`. Per group: both teams with exactly the configured members and approvers, the private repo with `main`, the bot (write) and indexer (read) collaborators, and branch protection of `main` (no direct push, merge whitelist the approve team, `required_approvals: 0`, `block_on_rejected_reviews`, `apply_to_admins`), read back after writing. One org webhook (push events) to `<hub_url>/webhooks/forgejo`, with its secret rewritten on every apply; a config without `hub_url` prints `! hub_url: not set`. When token files are set it checks that each token belongs to its configured login and has no excess rights, and lists the Owners-team members. Unmanaged teams and repos print `? unmanaged`; `--prune` removes unmanaged teams and unmanaged Slack repo hooks, and repos are never deleted. For each group with `slack_channel`: one Slack repo hook (`pull_request` events) to the group's URL from `KH_SETUP_SLACK_WEBHOOKS_FILE`, with its URL rewritten on every apply; `apply` for a group without a URL prints `! slack hook <repo>: no URL for group <g> in KH_SETUP_SLACK_WEBHOOKS_FILE` (`plan` does not need the file). A Slack hook on the repo of a group without `slack_channel` prints `? unmanaged slack hook <repo>` and is removed with `--prune`. An archived group keeps its teams; members the config no longer lists are removed from them, none are added.
5. `ollama`. `plan` checks that the model is listed and that one embedding has length `KH_EMBED_DIM`; `apply` pulls it.
6. `s3`. Checks that the bucket exists and takes a put, get and delete of `kh-setup-probe/<uuid>`. `apply --create-bucket` also creates a missing bucket (Garage often forbids this to ordinary keys).

**The tech org.** With `tech_repos` set, `forgejo` also reads `tech_org` and never creates anything there; clones and forks are made by hand. Per tech repo it prints `! tech repo <r>: not in org <tech_org>; create the clone or fork there` when it is missing, gives the indexer read access, and prints `i tech repo <r>: collaborators <logins>` (push access is the trust boundary of T0a). Repos of the org that are not configured print `? unmanaged tech repo <name>`. It adds the org webhook of the tech org (`tech webhook <url>`, `tech hub_url: not set`). It sets no teams, branch protection or repo settings, and `--prune` never touches the tech org. After a new label, `labels` also queues a `git_push` job for each tech repo.

## kh admin commands

`kh label`, `kh group` and `kh person` edit `kh.yaml` (comments are kept) and then apply the change to the database and Forgejo. A group cannot be renamed: its name is the audience of every document and record.

| Command | Edit of `kh.yaml` | Effect applied |
|---|---|---|
| `kh label add NAME [-d TEXT]` | `labels[NAME] = TEXT` (default empty); error if it exists | runs the labels step |
| `kh label rename NAME TEXT` | new description; the key never changes | runs the labels step |
| `kh label archive NAME` / `unarchive NAME` | add to / remove from `archived_labels` | none (restart `kh serve`) |
| `kh label remove NAME` | delete from `labels` and `archived_labels`; refused while a tech repo lists the label | runs the labels step, then removes the label |
| `kh label list [--json]` | none | none |
| `kh group add NAME --members A,B --approvers A [--repo R] [--slack-channel #C]` | new group | runs the postgres and forgejo steps for that group |
| `kh group remove NAME --yes` | delete the group | runs the postgres and forgejo steps for that group (refused while records exist; the Forgejo repo is kept). Without `--yes` the command prints the document count and the kept repo, then exits 2; `--yes` is required also with `--no-apply` |
| `kh group archive NAME` / `unarchive NAME` | set / delete `archived` | runs the forgejo step for that group |
| `kh group member add\|remove GROUP LOGIN [--with-approver]` | edit `members` | runs the forgejo step for that group |
| `kh group approver add\|remove GROUP LOGIN` | edit `approvers` | runs the forgejo step for that group |
| `kh group list [--json]` | none | none |
| `kh person add LOGIN EMAIL` | `people[LOGIN] = EMAIL` (lower case) | none |
| `kh person remove LOGIN [--from-groups]` | delete from `people`; with the flag also from every group's `members` and `approvers` | runs the forgejo step for each group the login was in |
| `kh person list [--json]` | none | none |

The commands do not edit `tech_org`, `tech_repos` or a group's `slack_channel` after creation: edit the file by hand, then run `kh setup apply`. There is no `group rename` and no email change for a person.

**Common flags.** `--config FILE` (default: the environment variable `KH_CONFIG_FILE`; with neither the command exits 2), `--dry-run` (print the diff and the planned lines, write nothing) and `--no-apply` (edit the file only; then run `kh setup apply`) on every mutating command. `list` commands take `--config` and `--json` and read only the file. A command whose edit changes nothing prints `nothing to change` and exits 0.

**Exit codes.** 0 done, 2 error, refusal or a remaining `!` line.

**Stage, apply, commit.** A command stages the new text, validates it as `kh.yaml` and prints the diff. It then applies the change to the database and Forgejo while the old file is still in place, so a refusal at apply time (for example a group that still has records) leaves `kh.yaml` unchanged. Only then is the file replaced; if a later step fails, the file is already changed and `kh setup apply` repairs the rest.

The commands run where `kh setup` runs, with the `KH_SETUP_*` credential files set, and `kh.yaml` must sit in a writable directory (the file is replaced atomically, so the directory must be writable, not only the file). Restart `kh serve` afterwards: the hub reads `kh.yaml` at start.
