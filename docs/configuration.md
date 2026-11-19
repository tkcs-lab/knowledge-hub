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
| `KH_TECH_POLL_MINUTES` | `60` | Minutes between polls that queue a `git_push` job for every repo in `tech_repos`, 0 to 10080; 0 switches the poll off. Needs `KH_FORGEJO_URL`. |
| `KH_WEBHOOK_HMAC_FILE` | none | Secret for `X-Forgejo-Signature`. A missing or empty secret makes `/webhooks/forgejo` answer 401. `kh setup` also writes it into the org webhook. |

### Setup credentials (`kh setup` only)

| Variable | Default | Meaning |
|---|---|---|
| `KH_SETUP_PG_ADMIN_DSN_FILE` | none | File with a Postgres superuser (or role-creating) DSN. Optional: without it the `postgres` step only checks. |
| `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE` | none | Token of a Forgejo site administrator. Required by the `forgejo` step. |
| `KH_SETUP_SLACK_WEBHOOKS_FILE` | none | YAML file mapping group name to its Slack incoming-webhook URL (`https://`). Needed only for groups with `slack_channel`. |

The `kh serve` service never mounts these files. The hub never holds admin credentials; `kh setup` does.

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
| `kh repo list\|add\|set\|remove` | Edit `tech_repos` of `kh.yaml` (the technical repos, T0a) and apply the change; see `kh admin commands`. |
| `kh person add\|remove\|list` | Edit `people` (and with `--from-groups` the group lists) and apply the change. |
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
| `groups` | `{}` | Map of group name to `{repo, members, approvers, archived, slack_channel}`. |
| `groups` (limits) | none | PrivacyFence reads the active groups as its audiences and refuses an answer over its limits, so `kh.yaml` is refused with more than 200 active groups (`config: active groups are more than 200; PrivacyFence accepts at most 200 audiences`), more than 20000 members in all (`config: active groups list more than 20000 members in all; PrivacyFence accepts at most 20000 addresses`) or an audience table over 1000000 characters, compact or indented (`config: the audience table is larger than 1000000 characters; PrivacyFence refuses it`). |
| `groups.<g>.repo` | `kb-<g>` | Repo name, `^[a-z0-9][a-z0-9._-]{0,99}$`, unique. Immutable once applied. |
| `groups.<g>.members` | `[]` | Logins; each must be in `people` (`config: group <g> member <login> is not in people`). Members read and write. |
| `groups.<g>.approvers` | required | Non-empty subset of `members` (`config: group <g> approver <login> is not a member`). Approvers merge. |
| `groups.<g>.archived` | `false` | An archived group is held by nobody: its documents and records are hidden, writes into it are refused (`audience is not held by this caller or not configured`), and nothing is indexed for it. Nothing is deleted. Every other group check still applies. |
| `groups.<g>.slack_channel` | null | Null or a Slack channel `#name` (lower case, digits, `-`, `_`, at most 80 characters after `#`). `kh setup` keeps one Slack hook on the group's repo for pull request events; its URL comes from `KH_SETUP_SLACK_WEBHOOKS_FILE`. Ignored while the group is archived. |
| `labels` | `{}` | Map of label name (`^[a-z0-9][a-z0-9_.:-]{0,62}$`) to a description of at most 200 characters; at most 500 labels. |
| `archived_labels` | `[]` | Label names, each a key of `labels` (`config: archived_labels entry '<x>' is not in labels`, `config: archived_labels lists '<x>' twice`). An archived label is hidden from `list_labels`, from the `labels` of results (`search`, `get_doc`, `list_stale`, `usage`) and from search filters; `ingest_record` and `propose_doc` using it fail with `unknown label`. The label and its rows stay. |

| `tech_org` | null | Forgejo organisation that holds the clones or forks of the technical repos; a valid login that differs from `org` (case-insensitive). Required when `tech_repos` is not empty. |
| `tech_repos` | `{}` | Map of bare repository name in `tech_org` to `{labels, dirs}`. Their root `CLAUDE.md` and the `.md` files under their `dirs` are indexed as tier T0a with audience `all`, no approval. Needs the group `all`, active. |
| `tech_repos.<r>.labels` | `[]` | Labels put on every document of the repo; each must be a key of `labels`, none twice. A label archived later stays on the documents and is hidden. |
| `tech_repos.<r>.dirs` | `[docs]` | Directories of the repo whose `.md` files are indexed, besides the root `CLAUDE.md` (always indexed). Each is a path relative to the repo root (`docs`, `docs/adr`): no leading or trailing `/`, no empty, `.` or dot-prefixed segment, letters, digits, `_`, `.`, `-`; 1 to 20 entries, none twice. A directory matches whole segments (`adr` covers `adr/a.md`, not `adr.md` or `notes/adr/a.md`). A directory removed from `dirs` loses its documents at the next sync. |

Tech repo errors: `config: tech_repos needs tech_org`, `tech_org '<x>' is not a valid login`, `tech_org must differ from org`, `tech repo '<r>' is not a valid repository name`, `tech repo <r> is also the repo of group <g>`, `tech_repos needs the group all`, `tech_repos needs the group all to be active`, `tech repo <r> label '<x>' is not in labels`, `tech repo <r> lists label '<x>' twice`, `tech repo <r> needs at least one dir`, `tech repo <r> has more than 20 dirs`, `tech repo <r> dir '<x>' is not a valid directory`, `tech repo <r> lists dir '<x>' twice`. A change needs a `kh serve` restart.

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
    dirs: [docs, adr]
```

The hub reads `kh.yaml` when `kh serve` starts, so a change to `people` or a group needs a restart. `deploy/kh.example.yaml` is the maintained example.

## kh setup

`kh setup plan|apply --config kh.yaml [--only postgres|labels|tech|forgejo|ollama|s3] [--prune] [--create-bucket]`. `plan` is strictly read-only. `apply` performs every possible change, prints the same lines and is idempotent. `--create-bucket` matters only for `apply`. `--prune` removes objects the config no longer lists.

Steps run in this order: `postgres`, `labels`, `tech`, `forgejo`, `ollama`, `s3`. A step that fails prints `setup: <step>: <message>` and the later steps are skipped.

**Lines.** `<prefix> <object kind> <name>[: <detail>]`:

| Prefix | Meaning |
|---|---|
| `+` | create |
| `~` | update |
| `-` | remove (with `--prune`, or by `kh label remove`, `kh group remove` and `kh repo remove`) |
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

`kh label`, `kh group`, `kh person` and `kh repo` edit `kh.yaml` (comments are kept) and then apply the change to the database and Forgejo. A group cannot be renamed: its name is the audience of every document and record.

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
| `kh repo list [--json]` | none | none; prints `NAME  DIRS  LABELS` |
| `kh repo add NAME [--dirs D1,D2] [--labels A,B]` | new entry under `tech_repos`; an omitted flag writes no key (default `dirs: [docs]`); needs `tech_org`; refused when the repo is not in the tech org (`! tech repo NAME: not in org <tech_org>; create the clone or fork there`, file unchanged) | runs the forgejo step for that repo (indexer read access, collaborators, tech webhook); needs `KH_FORGEJO_URL` and `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE`, or `--no-apply` followed by `kh setup apply` |
| `kh repo set NAME [--dirs D1,D2] [--labels A,B]` | change `dirs` and/or `labels`; `--labels ""` clears the labels; at least one flag | none: restart `kh serve` first (its worker keeps the old config), then its first poll applies it, or run `kh rebuild --repo NAME` |
| `kh repo remove NAME [--yes]` | delete the entry | deletes its T0a documents and indexed head (`- tech documents of NAME`); with documents it needs `--yes` and otherwise exits 2 with the count; `--no-apply` needs no `--yes` (`kh setup apply --prune` deletes later). The Forgejo repo is kept; restart `kh serve` at once |

The commands do not edit `tech_org` or a group's `slack_channel` after creation: edit the file by hand, then run `kh setup apply`. There is no `group rename` and no email change for a person.

**Common flags.** `--config FILE` (default: the environment variable `KH_CONFIG_FILE`; with neither the command exits 2), `--dry-run` (print the diff and the planned lines, write nothing) and `--no-apply` (edit the file only; then run `kh setup apply`) on every mutating command. `list` commands take `--config` and `--json` and read only the file. A command whose edit changes nothing prints `nothing to change` and exits 0.

**Exit codes.** 0 done, 2 error, refusal or a remaining `!` line.

**Stage, apply, commit.** A command stages the new text, validates it as `kh.yaml` and prints the diff. It then applies the change to the database and Forgejo while the old file is still in place, so a refusal at apply time (for example a group that still has records) leaves `kh.yaml` unchanged. Only then is the file replaced; if a later step fails, the file is already changed and `kh setup apply` repairs the rest.

The commands run where `kh setup` runs, with the `KH_SETUP_*` credential files set, and `kh.yaml` must sit in a writable directory (the file is replaced atomically, so the directory must be writable, not only the file). Restart `kh serve` afterwards: the hub reads `kh.yaml` at start.
