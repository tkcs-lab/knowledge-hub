# Knowledge hub configuration

Every `KH_*` environment variable read by `knowledge_hub/settings.py`, every key of the `kh.yaml` file named by `KH_CONFIG_FILE`, the `kh` commands and `kh setup`.
Secrets are never variables: a `*_FILE` setting names a file that holds the secret.

## Environment variables

Unknown `KH_*` variables are ignored. `kh serve` and every other command exit 2 on a settings or config error.

### Runtime and caller verification

| Variable | Default | Meaning |
|---|---|---|
| `KH_ENV` | `prod` | `dev`, `test` or `prod`. `prod` refuses `KH_DEV_IDENTITY` and the fake embedder. |
| `KH_DEV_IDENTITY` | none | JSON `{"sub", "principal_type", "audiences", "email"?}` used for requests with no claim header. `principal_type` is `claude_client` or `n8n_workflow`. Refused when `KH_ENV=prod`. `audiences` is ignored for a `claude_client`: its groups come from its `email` through `people` in `kh.yaml`, so a dev identity without a listed email reads nothing. For an `n8n_workflow` the audiences that are configured groups are held. |
| `KH_PF_ISSUER` | none | Issuer (`iss`) the verifier accepts: the address PrivacyFence is served at, with no path, for example `https://pf.tkcs.name`. |
| `KH_PF_AUDIENCE` | none | Audience (`aud`) the claim must carry. |
| `KH_PF_JWKS_URL` | none | URL of PrivacyFence's signing keys, `<issuer>/upstream-mcp/jwks`. It may use another host address than the issuer: see below. |
| `KH_ALLOWED_HOSTS` | empty | Comma list of `host:port` entries accepted in the `Host` header (DNS-rebinding protection), for example `knowledge-hub:8080`. A bare host gets HTTP 421. |
| `KH_MAX_BODY_BYTES` | `1048576` | Largest `/mcp` request body; larger gets 413. |

### Claim verification over the private network

`KH_PF_ISSUER` is compared only with the `iss` claim and is never fetched. `KH_PF_JWKS_URL` is only where the signing keys are fetched, and the vendored verifier does not tie its host to `iss`: it accepts any `http://` or `https://` URL (`tests/unit/test_claims.py` pins this). In production the issuer is the public address `https://pf.tkcs.name`, and the JWKS URL is `http://pf.tkcs.name:8765/upstream-mcp/jwks`, reached over the private `pf-kh` Docker network where the PrivacyFence container has the network alias `pf.tkcs.name`. The host is the issuer's host name because PrivacyFence answers any other `Host` header with 400; the scheme is `http` because the hop never leaves the Docker network.

Why that is safe: the keys are public, so nothing secret crosses the hop. What matters is that nobody else can answer at that address, and the network guarantees it: `pf-kh` is `internal` (no internet, no published port) and only the hub and PrivacyFence join it. Never attach another service to `pf-kh`. A claim is still Ed25519 signed, bound to one call, and checked for audience, issuer, expiry and replay.

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
| `KH_WEBHOOK_HMAC_FILE` | none | Secret for `X-Forgejo-Signature`. A missing or empty secret makes `/webhooks/forgejo` answer 401. `kh setup` also writes it into the org webhook. |

### Setup credentials (`kh setup` only)

| Variable | Default | Meaning |
|---|---|---|
| `KH_SETUP_PG_ADMIN_DSN_FILE` | none | File with a Postgres superuser (or role-creating) DSN. Optional: without it the `postgres` step only checks. |
| `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE` | none | Token of a Forgejo site administrator. Required by the `forgejo` step. |

The `kh serve` service never mounts these two files. The hub never holds admin credentials; `kh setup` does.

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
| `KH_TIER_WEIGHTS` | `T0:1.0,T3:0.4` | Score multiplier per tier; it must name exactly `T0` and `T3`. |
| `KH_STALE_WEIGHT` | `0.5` | Multiplier for a stale hit. |
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
| `kh rebuild [--repo R]` | Forget the indexed head and sync again. Idempotent. |
| `kh check` | Compare the index with Forgejo; exit 1 on drift (`head of <repo> is <a>, indexed <b>`, `document <path> in <repo> has no chunks`, `record <id> has no chunks`, `document <path> in <repo> has audience <a>, config says <b>`). |
| `kh daily` | Recompute `stale` flags, purge expired claim ids and delete usage events past `KH_USAGE_RETENTION_DAYS`. |
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
| `people` | `{}` | Map of Forgejo login to the email PrivacyFence verifies for that person. Logins match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}$` and are unique case-folded; an email has exactly one `@`, at most 254 characters, is stored lower-case and is unique (`config: email <e> belongs to two people`). |
| `groups` | `{}` | Map of group name to `{repo, members, approvers}`. |
| `groups.<g>.repo` | `kb-<g>` | Repo name, `^[a-z0-9][a-z0-9._-]{0,99}$`, unique. Immutable once applied. |
| `groups.<g>.members` | `[]` | Logins; each must be in `people` (`config: group <g> member <login> is not in people`). Members read and write. |
| `groups.<g>.approvers` | required | Non-empty subset of `members` (`config: group <g> approver <login> is not a member`). Approvers merge. |
| `groups.<g>.archived` | `false` | An archived group is held by nobody: its documents and records are hidden, writes into it are refused (`audience is not held by this caller or not configured`), and nothing is indexed for it. Nothing is deleted. Every other group check still applies. |
| `labels` | `{}` | Map of label name (`^[a-z0-9][a-z0-9_.:-]{0,62}$`) to a description of at most 200 characters; at most 500 labels. |
| `archived_labels` | `[]` | Label names, each a key of `labels` (`config: archived_labels entry '<x>' is not in labels`, `config: archived_labels lists '<x>' twice`). An archived label is hidden from `list_labels`, from the `labels` of results (`search`, `get_doc`, `list_stale`, `usage`) and from search filters; `ingest_record` and `propose_doc` using it fail with `unknown label`. The label and its rows stay. |

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
```

The hub reads `kh.yaml` when `kh serve` starts, so a change to `people` or a group needs a restart. `deploy/kh.example.yaml` is the maintained example.

## kh setup

`kh setup plan|apply --config kh.yaml [--only postgres|labels|forgejo|ollama|s3] [--prune] [--create-bucket]`. `plan` is strictly read-only. `apply` performs every possible change, prints the same lines and is idempotent. `--create-bucket` matters only for `apply`. `--prune` removes objects the config no longer lists.

Steps run in this order: `postgres`, `labels`, `forgejo`, `ollama`, `s3`. A step that fails prints `setup: <step>: <message>` and the later steps are skipped.

**Lines.** `<prefix> <object kind> <name>[: <detail>]`:

| Prefix | Meaning |
|---|---|
| `+` | create |
| `~` | update |
| `-` | remove (only with `--prune`) |
| `!` | refused: a change the script will not make, for example `! group all: changed repo; remove the group and add a new one` |
| `?` | unmanaged object (`? unmanaged`) |
| `i` | information (`i webhook secret not verifiable`, `i owners: <logins> can bypass protection`) |

Drift is any `+`, `~`, `-` or `!` line. `?` and `i` lines never change an exit code.

**Exit codes.** `plan`: 0 no drift, 1 drift, 2 error. `apply`: 0, or 2 when an error occurred or any `!` line remains.

**Credential files.** Never in the YAML: `KH_SETUP_PG_ADMIN_DSN_FILE` (optional), `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE`, and the existing `KH_DB_*_PASSWORD_FILE`, `KH_FORGEJO_URL`, `KH_FORGEJO_BOT_TOKEN_FILE`, `KH_FORGEJO_INDEXER_TOKEN_FILE`, `KH_WEBHOOK_HMAC_FILE`, `KH_OLLAMA_URL`, `KH_EMBED_MODEL`, `KH_S3_*`. A missing required file prints `setup: <name> is not set` and exits 2. Setup never creates Forgejo users or tokens: a missing `bot_login` or `indexer_login` account is an error.

**Steps.**

1. `postgres`. With an admin DSN: create the roles `kh_migrator`, `kh_app` and `kh_indexer` (passwords from the `KH_DB_*_PASSWORD_FILE` files, re-set on every apply because they cannot be read back), the database owned by `kh_migrator`, `CREATE EXTENSION vector`, then run pending migrations. Without an admin DSN it only checks, and pending migrations count as drift. It then syncs `kh.groups`: groups are added; a removed group goes only with `--prune`, is refused when records exist, and its documents are deleted.
2. `labels`. Inserts new labels and updates descriptions. A removed label is deleted only with `--prune` and only when unused (`! label <x>: in use`). After a label is added, apply queues one `git_push` job per repo so files rejected for an unknown label are retried.
3. `forgejo`. Per group: both teams with exactly the configured members and approvers, the private repo with `main`, the bot (write) and indexer (read) collaborators, and branch protection of `main` (no direct push, merge whitelist the approve team, `required_approvals: 0`, `block_on_rejected_reviews`, `apply_to_admins`), read back after writing. One org webhook (push events) to `<hub_url>/webhooks/forgejo`, with its secret rewritten on every apply; a config without `hub_url` prints `! hub_url: not set`. When token files are set it checks that each token belongs to its configured login and has no excess rights, and lists the Owners-team members. Unmanaged teams and repos print `? unmanaged`; `--prune` removes unmanaged teams only, and repos are never deleted.
4. `ollama`. `plan` checks that the model is listed and that one embedding has length `KH_EMBED_DIM`; `apply` pulls it.
5. `s3`. Checks that the bucket exists and takes a put, get and delete of `kh-setup-probe/<uuid>`. `apply --create-bucket` also creates a missing bucket (Garage often forbids this to ordinary keys).
