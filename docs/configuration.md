# Knowledge hub configuration

Every `KH_*` environment variable read by `knowledge_hub/settings.py`, and every key of the `kh.yaml` file named by `KH_CONFIG_FILE`.
Secrets are never variables: a `*_FILE` setting names a file that holds the secret.

## Environment variables

Unknown `KH_*` variables are ignored. `kh serve` and every other command exit 2 on a settings or config error.

### Runtime and caller verification

| Variable | Default | Meaning |
|---|---|---|
| `KH_ENV` | `prod` | `dev`, `test` or `prod`. `prod` refuses `KH_DEV_IDENTITY` and the fake embedder. |
| `KH_DEV_IDENTITY` | none | JSON `{"sub", "principal_type", "audiences", "email"?}` used for requests with no claim header. `principal_type` is `claude_client` or `n8n_workflow`. Refused when `KH_ENV=prod`. |
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
| `KH_DB_MIGRATOR_PASSWORD_FILE` | none | Password file for `kh_migrator` (used by `kh migrate`). No file means no password. |
| `KH_DB_APP_PASSWORD_FILE` | none | Password file for `kh_app` (the request path). |
| `KH_DB_INDEXER_PASSWORD_FILE` | none | Password file for `kh_indexer` (worker, rebuild, check, daily, recompute, requeue). |

### Config file, Forgejo and webhook

| Variable | Default | Meaning |
|---|---|---|
| `KH_CONFIG_FILE` | none | Path of `kh.yaml`. Without it the built-in defaults apply. |
| `KH_FORGEJO_URL` | none | Forgejo base URL. The worker and promotion exist only when it is set. |
| `KH_FORGEJO_ORG` | `kh` | Forgejo organisation that holds the repos. Webhook events from other owners are ignored. `kh validate` and `kh domain-owners` use it too. |
| `KH_FORGEJO_BOT_TOKEN_FILE` | none | Token of `kh-hub-bot`, used only for promotion branches and pull requests. |
| `KH_FORGEJO_INDEXER_TOKEN_FILE` | none | Read-only token the indexer uses to fetch commits, reviews and files. Required when `KH_FORGEJO_URL` is set. |
| `KH_WEBHOOK_HMAC_FILE` | none | Secret for `X-Forgejo-Signature`. A missing or empty secret makes `/webhooks/forgejo` answer 401. |

### Embeddings

| Variable | Default | Meaning |
|---|---|---|
| `KH_EMBED_PROVIDER` | `ollama` | `ollama`, `http` or `fake`. `fake` is allowed only with `KH_ENV` `dev` or `test`. Chosen once per deployment, for documents and queries alike. |
| `KH_OLLAMA_URL` | none | Ollama base URL (`POST /api/embed`). Required for `ollama`. |
| `KH_EMBED_MODEL` | `bge-m3` | Model name. Vectors are compared only with chunks of the same model. |
| `KH_EMBED_DIM` | `1024` | Vector size; the provider refuses any other. The column is `vector(1024)`. |
| `KH_EMBED_HTTP_URL` | none | Endpoint of the `http` provider; it must serve the same model as `KH_EMBED_MODEL`. |
| `KH_EMBED_HTTP_TOKEN_FILE` | none | Token file for the `http` provider. |
| `KH_EMBED_HTTP_ALLOWED_AUDIENCES` | `all` | Comma list. The `http` provider embeds only chunks whose audiences are a non-empty subset of it and that have no `allowed_emails`; others are found by full text only. |

### Blob storage (S3-compatible)

| Variable | Default | Meaning |
|---|---|---|
| `KH_S3_ENDPOINT` | none | Endpoint URL, path-style addressing. |
| `KH_S3_BUCKET` | none | Bucket for raw bodies (`raw/<uuid>`). |
| `KH_S3_ACCESS_KEY_FILE` | none | File with the access key. |
| `KH_S3_SECRET_KEY_FILE` | none | File with the secret key. |
| `KH_S3_REGION` | `garage` | Region used when signing requests. |

A partial S3 setting is a startup error. With no S3 setting the raw-record tools are unavailable.

### Search, limits and tools

| Variable | Default | Meaning |
|---|---|---|
| `KH_CURSOR_KEY_FILE` | none | HMAC key for `search` and list cursors. Without it `search` fails with an internal error. |
| `KH_CHUNKER_VERSION` | `1` | Part of the chunk hash; raising it re-chunks every document on the next sync. |
| `KH_PROMOTION_DAILY_LIMIT` | `5` | Proposals per `(domain, target repo)` since 00:00 UTC. |
| `KH_RRF_K` | `60` | Reciprocal-rank-fusion constant. |
| `KH_SEARCH_POOL` | `50` | Candidates per list (vector and full text) before fusion. |
| `KH_TIER_WEIGHTS` | `T0:1.0,T1:0.85,T2:0.6,T3:0.4` | Score multiplier per tier. |
| `KH_STALE_WEIGHT` | `0.5` | Multiplier for a stale hit. |
| `KH_SUPERSEDED_WEIGHT` | `0.3` | Multiplier for a superseded hit. |
| `KH_DISPUTED_WEIGHT` | `0.5` | Multiplier for a disputed hit. |
| `KH_SEARCH_MAX_OFFSET` | `100` | Largest offset a `search` cursor may carry; above it `CURSOR_FOREIGN`. |
| `KH_MAX_RAW_BYTES` | `1048576` | Largest `ingest_raw` body. |
| `KH_RATE_LIMIT_PER_MIN` | `120` | Tool calls per minute per caller `sub`. The bucket is in memory, so the limit is per process. |
| `KH_METRICS_TOKEN_FILE` | none | Bearer token for `/metrics`. Without it the route does not exist. The file is read on every request, so the token can be rotated. |

### Grist (daily job)

| Variable | Default | Meaning |
|---|---|---|
| `KH_GRIST_URL` | none | Grist base URL. |
| `KH_GRIST_DOC_ID` | none | Document that holds the Grist table of every kind in `entities` (by default `Products`, `Components`, `Versions`, `Customers`). |
| `KH_GRIST_KEY_FILE` | none | API key file. `kh daily` exits 2 unless all three Grist settings are set. |

## Commands

| Command | Use |
|---|---|
| `kh migrate` | Apply migrations as `kh_migrator`. |
| `kh serve [--port 8080]` | Run the HTTP app and the webhook worker. |
| `kh rebuild [--repo R]` | Re-walk trust from the anchor and re-index. Idempotent. |
| `kh check` | Compare the index with Forgejo and the config; exit 1 on drift. |
| `kh recompute-visibility` | Rewrite visibility from the config. Run once after install or restore, and after any change to audiences, repos or `raw_audience_map`. |
| `kh requeue-embeddings` | Queue embedding jobs for raw and derived records without chunks (after a restore). |
| `kh daily` | Mark stale documents, sync entities from Grist, purge expired claim ids. |
| `kh validate --repo R --base SHA --head SHA` | CI gate for a canonical repo pull request: frontmatter, layout, ids, entities. Exit 0, 1 or 2. See [governance](governance.md). |
| `kh domain-owners --repo R --pr N` | CI gate: owner-team approvals of a pull request. Exit 0, 1 or 2. See [governance](governance.md). |
| `kh governance render --repo R --audience A --out DIR` | Write the schema, `owners.yml`, CODEOWNERS, PR template and workflow for a new canonical repo. |

The CI runner needs `KH_CONFIG_FILE` in its environment when `kh.yaml` is not the default config.

`kh check` reports config drift until `kh recompute-visibility` has recorded the first config hash.

## kh.yaml

Unknown keys are refused. Every error text starts with `config:`.

| Key | Default | Meaning |
|---|---|---|
| `audiences` | `[all, mgmt, hr, contracts]` | Audience names allowed in `required_audiences`. |
| `participant_label` | `attendees` | Name shown for email-scoped raw records. It is not an audience. |
| `domains` | `[portfolio, product, technical]` | Valid values of `domain` for documents, raw, derived and proposals. |
| `repos` | see example | Map of Forgejo repo name to `{tier: T0 or T1, audience, trust_anchor}`. `audience` must be in `audiences`; at most one T0 repo per audience. `trust_anchor` is a commit sha on `main`, or null for none (set one for T0 once its first reviewed pull request is merged; see the runbook, "Bootstrapping the trust anchor"). |
| `raw_audience_map` | `{}` | `series:<id>` or `folder:<id>` mapped to the audiences that may read matching raw records. Unknown audiences are refused. |
| `ingest_principal_types` | `[n8n_workflow]` | Who may call `ingest_raw`. Only `n8n_workflow` is allowed. |
| `restricted_audiences_imply_all` | `false` | When true, a promotion whose sources span `all` and another audience routes to the other audience's repo instead of failing. |
| `entities` | `prd, cmp, ver, cus` (see example) | Map of entity kind (the id prefix) to `{table, audiences, label, reference_audiences}`: the Grist table to read, the audiences of its cache rows, an optional display name. Setting it replaces the defaults. The list form `prd: [all]` is accepted for the four default kinds. Applied on every `kh daily` run. |
| `entities.<kind>.reference_audiences` | `[contracts, mgmt]` for `cus`, unset for the other default kinds | Repository audiences whose documents may cite the kind. Unset means any repository may cite it. Members must be in `audiences`, otherwise `config: entities <kind> has unknown reference audience ...`. Enforced by `kh validate`. See [entities](#entities). |
| `governance.owner_teams` | `{}` | Map of audience to the Forgejo teams whose approval allows widening a PR into that audience (`kh domain-owners`). Keys must be in `audiences`; names match `^[a-z0-9][a-z0-9_-]{0,62}$`. Default: `owners-<audience>`, and the three domain teams for `all`. See [governance](governance.md). |
| `forgejo_web_url` | null | Base URL for `source_link`; empty links when null. |

Example (see `deploy/kh.example.yaml` for the defaults):

```yaml
audiences: [all, mgmt, hr, contracts]
domains: [portfolio, product, technical]
repos:
  canonical-all: {tier: T0, audience: all, trust_anchor: 0123456789abcdef0123456789abcdef01234567}
  canonical-mgmt: {tier: T0, audience: mgmt, trust_anchor: 89abcdef0123456789abcdef0123456789abcdef}
  canonical-hr: {tier: T0, audience: hr, trust_anchor: null}
  canonical-contracts: {tier: T0, audience: contracts, trust_anchor: null}
  notes-all: {tier: T1, audience: all, trust_anchor: null}
raw_audience_map:
  "series:weekly-sync": [mgmt]
  "folder:hr-interviews": [hr]
ingest_principal_types: [n8n_workflow]
restricted_audiences_imply_all: false
entities:
  prd: {table: Products, audiences: [all], label: product}
  cmp: {table: Components, audiences: [all], label: component}
  ver: {table: Versions, audiences: [all], label: version}
  cus: {table: Customers, audiences: [contracts], label: customer, reference_audiences: [contracts, mgmt]}
  # a home install might use only kinds such as this one
  # fam: {table: Family, audiences: [all], label: family member}
forgejo_web_url: https://forgejo.example.invalid
```

### restricted_audiences_imply_all

Documents of a restricted audience such as `hr` usually also cite `all` sources, so the union of source audiences is `{all, hr}`. With `restricted_audiences_imply_all: false` the router refuses that proposal (`SPAN`). With `true`, `all` is removed from the union when other audiences are present, and the proposal goes to the `hr` repo. The stored visibility of the candidate keeps the full union; only the routing changes. Leave it `false` unless people in a restricted audience always also hold `all`.

A change to `audiences`, `repos`, `raw_audience_map` or the visibility of `entities` changes the config hash: run `kh recompute-visibility` after deploying it. Document visibility follows `repos`, raw and derived rows follow the map, entity rows follow `entities` at the next `kh daily`.

### entities

A kind is the id prefix (`fam` in `fam:kid1`): 1-16 lowercase letters, digits, `_` or `-`, starting with a letter; `doc`, `raw` and `derived` are reserved. Each kind reads one Grist table (letters, digits or `_`, starting with a letter), and two kinds cannot share a table. `entities.<kind>.reference_audiences` (optional) lists the repository audiences whose documents may cite the kind; `kh validate` enforces it (default for `cus`: `[contracts, mgmt]`; when unset, any repository may cite the kind). The optional `label` is 1-40 characters without control characters and appears in the tool descriptions. At most 32 kinds are allowed.

The rows of a kind removed from the config are deleted at the next `kh daily`. Adding or removing a kind, or changing its audiences, changes the config hash (run `kh recompute-visibility`); a `table` or `label` change does not.
