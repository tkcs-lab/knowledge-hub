# Knowledge hub runbook

For the operator. Names of variables and keys are in [configuration](configuration.md); the reasons behind the rules are in [design](design.md). Commands run as the `kh` user inside the container (or from the installed wheel), with the `KH_*` environment of the service (see `deploy/compose.example.yaml`). Secrets are files, never variables, and nothing in this document is a real secret or host.

This is a breaking release for a fresh install only: a database that holds an older migration is refused (`database predates the baseline; a fresh install is required`).

Not run in the cloud, so yours to do and check by hand: the backup script's `pg_dump` (`deploy/backup-kh.sh`) and the first `kh setup` against a real Forgejo. Check both on the first deploy.

## Install order

1. **Accounts.** In Forgejo create the organisation named by `org` in `kh.yaml` and two ordinary accounts, `bot_login` (default `kh-bot`) and `indexer_login` (default `kh-indexer`), neither an org owner nor a site admin. `kh setup` never creates users or tokens.
2. **Secrets.** One file per secret under the secrets directory, named by the matching `*_FILE` variable: the three database passwords, the bot token, the indexer token (read only), the webhook secret `KH_WEBHOOK_HMAC_FILE`, the S3 access and secret key, `kh_cursor_key` and, for metrics, `kh_metrics_token`. For `kh setup` only: `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE` (a site administrator's token) when setup should create the roles, `KH_SETUP_PG_ADMIN_DSN_FILE`, and, only when groups set `slack_channel`, `KH_SETUP_SLACK_WEBHOOKS_FILE`. The `kh serve` service never mounts those.
3. **Write `kh.yaml`.** Start from `deploy/kh.example.yaml`: `people`, `groups`, `labels`, `hub_url`.
4. **Plan.** `kh setup plan --config kh.yaml`. Expect `+` lines and exit 1. Read every `!` line: it is a change the script will not make.
5. **Apply.** `kh setup apply --config kh.yaml`. It creates the Postgres roles, database, extension and tables, groups and labels, the Forgejo teams, repos, branch protection and org webhook, and pulls the Ollama model. Run `kh setup plan` again and expect only `i` lines and exit 0. Without an admin DSN the `postgres` step only checks: create the roles and database yourself, then run `kh migrate`. The S3 bucket is only checked unless you pass `--create-bucket`.
6. **Serve.** `kh serve` (the image does this). The worker starts only when `KH_FORGEJO_URL` is set. `/healthz` answers `ok`.
7. **Ollama.** If `kh setup` cannot reach the model server (the `kh` network has no internet), pull the model once with a short-lived container on the default bridge network, writing to the same volume `kh-ollama-models`:

   ```bash
   docker run --rm -v kh-ollama-models:/root/.ollama --entrypoint sh ollama/ollama:0.40.16 \
     -c 'ollama serve >/dev/null 2>&1 & until ollama list >/dev/null 2>&1; do sleep 1; done; ollama pull bge-m3'
   ```

   Until the model is available embedding fails; run `kh rebuild` and `kh requeue-embeddings` afterwards. To measure speed: `docker compose exec -T knowledge-hub python - --count 200 < scripts/bench_embed.py` prints chunks per second.
8. **Index.** Put the first documents in each repo through pull requests (see "Changing T0"), or run `kh rebuild` once, then `kh check` and expect exit 0.
9. **PrivacyFence.** Declare the hub as an upstream, give the audiences of n8n workflows a group map and the administrators who create tokens a membership source, and create the n8n tokens (sections below).

## PrivacyFence setup

Copy `deploy/privacyfence-upstream.example.yaml` to the PrivacyFence host, set `PRIVACYFENCE_UPSTREAM_MCP_FILE` to it and restart PrivacyFence; the file and the hub's tool list are read at startup only. Its `audience` equals `KH_PF_AUDIENCE`.

- People need no entry in PrivacyFence's group map: their groups come from `people` in `kh.yaml`.
- `email_for_visibility: true` stays in the declaration because a person's groups come from their verified email; without it no person reads anything.
- n8n tokens need the map and a membership source (see "Groups for n8n tokens").

If `admin_config.json` already holds a group-to-audience map for this upstream, keep the entries for the audiences n8n workflows should hold; the entries that existed only for people can go.

### Groups for n8n tokens

People need no entry in PrivacyFence's group map. An n8n token is different: PrivacyFence gives a scoped token its creator's audiences at each call, intersected with the audiences ticked when the token was made (PrivacyFence ADR 0298), and the hub keeps the token's audiences that are groups of `kh.yaml`. So `admin_config.json` keeps a map for this upstream with an entry only for each audience an n8n workflow should hold, and every administrator who creates an n8n token is a member of those groups:

```json
"upstream_mcp": {
  "kh": {"groups": {
    "kh-n8n-all@company.example": ["all"],
    "kh-n8n-mgmt@company.example": ["mgmt"]
  }}
}
```

Each audience is a group name of `kh.yaml`. PrivacyFence re-reads `admin_config.json` when it changes; edit it while nobody is saving on the Admin page. PrivacyFence's one unrestricted audience is also named `all`, like the example group `all` of `kh.yaml`; that works, and a member of a group mapped to `all` holds it at once, while a restricted audience such as `mgmt` is approved per person.

Membership comes from one of two sources, set by the top-level `membership` key of the declaration file:

- `static` (recommended when only a few administrators create tokens): uncomment the `membership` example in `deploy/privacyfence-upstream.example.yaml`, restart PrivacyFence, and write the JSON file it names, for example `{"version": 1, "groups": {"kh-n8n-all@company.example": ["admin@company.example"], "kh-n8n-mgmt@company.example": ["admin@company.example"]}}`. No approvals and no sync job; PrivacyFence re-reads the file when it changes.
- `snapshot` (the default, used when the declaration sets no `membership` key, as the example does): a job of yours calls `privacyfence_update_group_membership` with the members of each mapped group. Nothing in the hub runs that job. An addition to `all` applies at once; an addition to a restricted audience such as `mgmt` waits until an administrator approves it in PrivacyFence, Admin, Group membership.

Before the first token for a group an administrator:

1. adds `"<Workspace group>": ["<kh.yaml group>"]` under `upstream_mcp.kh.groups` in `admin_config.json`;
2. makes each administrator who creates tokens a member of that Workspace group: in the static file, or through the sync job and, for a restricted audience, an approval in Admin, Group membership.

Then the token is created as in "Creating an n8n token": the group name as its audience, the gated tools ticked.

When an n8n call answers `audience is not held by this caller or not configured`, check the token creator's PrivacyFence membership first: the creator must be a member of a Workspace group that the map sends to that audience (and be approved for it, if it is restricted), and the audience must be a group of `kh.yaml`. A creator who loses the membership takes the audience from the token at the next call.

### Creating an n8n token

Scoped tokens call gated upstream tools only through an administrator's tick. In PrivacyFence open Settings, Scoped tokens, New token and:

1. name the token for the workflow and set an expiry;
2. under "Upstream tools that change data or release results unseen" tick `kh_ingest_record`, and `kh_propose_doc` only when the workflow proposes document changes; tick the reads the workflow needs (`kh_search`, `kh_usage`, ...) as usual;
3. under "Audiences for Knowledge hub" tick exactly the group names the workflow may write into and read; a token never holds more than its creator. A name that is not a configured group fails closed (`audience is not held by this caller or not configured`), and so does a group the creator does not hold: the box appears only for an audience the creator holds through the map and a membership source (see "Groups for n8n tokens");
4. store the secret in n8n's credential store, never in chat.

The tick is pinned to the declaration: changing a tool's gate, verb or effect, or the url, audience or claim mode, voids it and the token is created again. All n8n tokens from before this release are void and must be recreated.

## Routine tasks

| Task | Command and notes |
|---|---|
| Check the whole environment | `kh setup plan --config kh.yaml`. Exit 0 means no drift. Run it from a scheduler (for example daily) and alert on exit 1 or 2: it catches a branch-protection or team change made in the Forgejo UI. |
| Re-index after review | `kh rebuild` or `kh rebuild --repo kb-all`. Idempotent. |
| Look for drift | `kh check`. Exit 1 means drift; read the printed lines. |
| Daily job | `kh daily`, once a day from a scheduler: recomputes `stale`, purges expired claim ids and deletes usage events past `KH_USAGE_RETENTION_DAYS`. |
| Queue missing embeddings | `kh requeue-embeddings` prints the number queued. It also re-queues a record whose job was left `running` by a worker that stopped mid-job. |
| Change the embedding model or chunker | Change `KH_EMBED_MODEL` or `KH_CHUNKER_VERSION`, restart, run `kh setup apply --only ollama`, `kh rebuild`, then `kh requeue-embeddings`. |
| Check a document before a pull request | `kh lint path/to/file.md`. |

### Adding a group

1. Add the group (and any new people) to `kh.yaml`. Every member needs a `people` entry; approvers are a non-empty subset of members.
2. `kh setup plan`, then `kh setup apply`. It creates the group row, the two Forgejo teams, the repo, its protection and the webhook coverage; the other groups are untouched.
3. Restart `kh serve`, because the config is read at start.
4. Put the first files into the new repo through a pull request.

A group's repo cannot change: a changed `repo` is a `!` line. Remove the group (with `--prune`) and add a new one instead. Removing a group is refused while records exist.

### Archiving and unarchiving

1. Set `archived: true` on the group, or add the label to `archived_labels`, in `kh.yaml`.
2. `kh setup plan`, then `kh setup apply`. For a group, `kh setup` also marks its Forgejo repo archived and leaves the teams and members as they are.
3. Restart `kh serve`, because the config is read at start.

Nothing is deleted. To unarchive, remove the flag or the `archived_labels` entry, run `kh setup apply`, restart `kh serve`, and for a group run `kh rebuild --repo <repo>` once, because pushes while archived were ignored. `kh setup apply --prune` never deletes an archived group or label. To delete one for good, remove it from `kh.yaml` and use `--prune` as before.

### Joiners and leavers

One file decides both what a person can read and which Forgejo teams they are in. Edit `people` and the group `members` and `approvers` lists, run `kh setup apply`, restart `kh serve`. The restart is what changes what the hub lets them read. For a leaver, remove them from `people` and the lists in the same edit; a reassigned Workspace email gives its new holder the old person's groups until `people` is edited. Also disable the Forgejo account if they leave the company.

### When a person reads nothing

First check that their PrivacyFence email equals their `people` email in `kh.yaml` (compared in lower case). A different address, a missing `people` entry or a missing group membership all give no groups. Then check that `kh serve` was restarted after the last edit, and that the declaration still carries `email_for_visibility: true`.

### Slack notifications for approvers

1. In Slack, create an incoming webhook for each approver channel (Slack: *Apps → Incoming Webhooks*, one per channel).
2. Write the URLs into the `KH_SETUP_SLACK_WEBHOOKS_FILE` file as `<group>: <url>`, one line each, file mode 600.
3. Set `slack_channel: "#<channel>"` on those groups in `kh.yaml`.
4. `kh setup plan`, expect `+ slack hook <repo>: #<channel>`, then `kh setup apply`.
5. Open a test pull request and check that the channel gets the message.

The message names the pull request's author, which for `propose_doc` is the bot account. To stop the notifications remove `slack_channel` and run `kh setup apply --prune`. To rotate a URL, change the file and run `kh setup apply --only forgejo`.

### Changing T0

People do not push to `main`: direct push is off. They open a pull request (in Forgejo, or through `propose_doc`) and an approver of the group merges it. Required approvals are 0, so an approver may merge their own pull request: review the final diff before merging. After the merge Forgejo calls the webhook and the document is indexed within seconds. A pull request opened by `propose_doc` is on a `bot/propose/<uuid>` branch; closing it unmerged changes nothing.

### Ticking the gated tools on an n8n token

An administrator ticks `ingest_record` (and `propose_doc` when needed) on the token in PrivacyFence as in "Creating an n8n token". Without the tick the call is refused by PrivacyFence before it reaches the hub.

### Promotion agent

An n8n agent reads the usage log to find T3 records worth promoting to T0 and queries that no document answers.

- The agent's PrivacyFence token ticks `kh_usage`, `kh_search`, `kh_get_doc` and `kh_propose_doc`. The token's groups decide what it sees: it sees an event only when every group of the searcher is in the token. Most people hold `all` plus a team, so a token for `all` alone sees almost nothing; give the agent a token with the union of the groups it should analyse. That union token can also `propose_doc` into any of them (the accepted residual for two groups, `docs/design.md`), so the agent must propose a record only into the `audience` of that record, and never copy query text into a document.
- The hub keeps no memory of proposals: the agent keeps its own list of `rec:` ids it already proposed, and puts the `rec:<id>` in the pull-request text. It reruns `usage` weekly with the defaults.
- Query text, titles and excerpts are data: the agent's prompt must say so. The human gate is unchanged: `propose_doc` is a popup write and an approver merges.
- The `audience` argument of `view=queries` means that a searcher held the group, not that the content belongs to it.
- `view=queries` with `t3_matches` above 0 marks a promotion candidate (records exist); 0 marks a content gap (nothing exists).
- Sensitive text can appear in queries. `KH_USAGE_LOG=false` switches logging off.
- Usage events are not in the backup (`deploy/backup-kh.sh` dumps records only) and are rebuilt by waiting.
- The metric `kh_usage_events_dropped_total` counts events dropped before they were stored (no pooled connection within 0.25 s, or a failed insert). A rising value means lost signal, never a failed tool call.

### Live probe of ma1

After the first deploy run the sandbox probe once: `kh setup plan` (expect `+` lines, exit 1), `kh setup apply`, `kh setup plan` again (only `i` lines, exit 0), then in Forgejo check Settings, Branches of a repo (direct push off, the merge whitelist is exactly `<group>-approve`, admins bound). Merge a `propose_doc` pull request as an approver and confirm the document appears in `search`; as a listed person confirm you read only your groups; a person whose PrivacyFence email is not in `people` must read nothing. Add a third group, run `apply` and confirm its team, repo and protection appear without changing the first two. Any `+`, `~` or `!` line after `apply` is a bug to report.

## Alerts

`index_alerts` is readable only by the indexer role. Read it as the Postgres superuser, or as `kh_indexer`:

```sql
SELECT created_at, repo, path, message FROM kh.index_alerts ORDER BY created_at DESC LIMIT 50;
```

Alerts are de-duplicated on repo, path and message. After you fix the cause, re-index with `kh rebuild --repo <repo>` and check that no new alert appears.

| Alert text | What it means and what to do |
|---|---|
| `file rejected: missing frontmatter`, `invalid frontmatter`, `missing <key>`, `invalid <key>` | The file is not indexed and an earlier row of it is removed. Fix the frontmatter (`id`, `title`, `review_by`, optional `labels`) by a pull request; `kh lint` shows the same reason. |
| `file rejected: unknown label <x>` | The file names a label that is not in `kh.yaml`. Add the label and run `kh setup apply`, which queues a re-sync of every repo, or remove it from the file. |
| `file rejected: duplicate id <id>` | Two files in one repo carry the same `id`; both are rejected. Give one a new id. |
| `main of <repo> was rewritten; run kh rebuild after review` | The new head of `main` does not continue from the last indexed sha. The index is unchanged. Inspect the force push in Forgejo. If it was intended, run `kh rebuild --repo <repo>`. If not, restore the branch from a clone and look at who could push (`kh setup plan` shows protection drift and Owners-team bypass). |
| `supersedes unknown id <key>` | The file lists an id that no document of the repo has. The line is ignored. Fix the id or remove it by a pull request. |
| `superseded by <n> documents; using <doc_key>` | Several documents list the same id; the one with the smallest id wins. Remove the line from the others. |
| `supersedes cycle: <keys>` | Documents replace each other; none of them is hidden. Remove one link. |

A hidden (superseded) document is still readable with `get_doc` or `search` with `include_outdated=true`.

### Rollout of supersede and outdated

1. Deploy the new image and run `kh migrate` (prints `applied 0003_supersede.sql`; it also applies `0002_usage_events.sql` when that is pending). It blanks every `blob_sha`.
2. Run `kh rebuild` so every file is read again (chunks are matched by content, so nothing is embedded again), then `kh check`.
3. Restart PrivacyFence, which reads the upstream declaration only at startup, and create the n8n tokens again for workflows that should call `mark_record_outdated`.

Between steps 1 and 2 `propose_doc` with a `base_blob` waits for the rebuild. Afterwards every document has a new `commit_sha`, `indexed_at` and `source_link`, once.

## Backup and restore

Documents and chunks are rebuilt from Forgejo, groups and labels from `kh.yaml`, so only `kh.records` and the blob bucket need a backup. `deploy/backup-kh.sh` writes `kh-t2t3-<date>.dump` (custom format, INSERT statements) and mirrors the blob bucket into `<BACKUP_DIR>/blobs/`. Back both up off the box (restic).

1. Start with an empty database: run `kh setup apply` (or create the roles and run `kh migrate`), so the schema, groups and labels exist.
2. As the Postgres superuser restore the records: `pg_restore --no-owner -d kh kh-t2t3-<date>.dump` (INSERT format, so RLS does not block it).
3. Copy the blobs back: `aws s3 sync "<BACKUP_DIR>/blobs/" s3://<bucket> --endpoint-url <endpoint>`.
4. Run `kh setup apply`, then `kh rebuild` for the git-derived tables.
5. Run `kh requeue-embeddings`: chunks are not in the dump, so this is required after every restore.
6. Run `kh check` and expect exit 0, then `search` for a restored record.

Garage keeps one version of each object, so the mirror is only as old as the last run; history comes from restic.

## Rotating secrets

**Webhook secret.** Write a new value to the `KH_WEBHOOK_HMAC_FILE` file and run `kh setup apply --only forgejo`, which rewrites the org webhook secret. The file is read on every request. Between the two steps pushes answer 401 and are not indexed, so finish with `kh rebuild`.

**Slack webhook URLs.** Create the new incoming webhook in Slack, put it in the `KH_SETUP_SLACK_WEBHOOKS_FILE` file and run `kh setup apply --only forgejo`, which rewrites the URL in the Forgejo hook. Then revoke the old webhook in Slack.

**Forgejo tokens.** Create the new token under the same user (the bot or the indexer) with the same scopes, write it to the token file, restart, then revoke the old token. The indexer token is read only. The bot may only write `bot/` branches and open pull requests to `main`. Run `kh setup plan`: its identity checks confirm the token belongs to the configured login.

**Cursor key.** Rotating `KH_CURSOR_KEY_FILE` also resets the distinct-people counts of the usage log, because the searcher hash is derived from it.

**Metrics token.** Replace the content of the `KH_METRICS_TOKEN_FILE` file; it is re-read per request, no restart.

**Database passwords.** Change the file, run `kh setup apply --only postgres` (passwords are always re-set), then restart.

**PrivacyFence keys.** Follow PrivacyFence's own runbook; the hub fetches the signing keys from `KH_PF_JWKS_URL`.

## Releasing

Releases follow the `/tld:release` skill. The version lives only in `src/knowledge_hub/__init__.py`.

1. Run `/tld:release`. It moves the `## [Unreleased]` changelog entries under a `## [X.Y.Z] - <date>` heading, bumps `__version__` and opens a `chore/release-<X-Y-Z>` pull request. Review and merge it.
2. The skill dispatches `.github/workflows/release.yml` on `main` with `dry_run` true first, once the `ci` workflow has finished on the merge commit. A dry run checks the changelog heading, that `lint`, `test (3.12)`, `test (3.13)` and `image` are green, builds the arm64 image and the wheel and runs a smoke test. It pushes nothing; look under Artifacts for the item `wheel`.
3. When the dry run is green, the skill dispatches the workflow again with `dry_run` false. The `build` job pushes the image to GHCR by digest only.
4. The `publish` job waits for your approval of the `release` environment in GitHub (Actions, the run, "Review deployments"). After you approve, it tags the digest `X.Y.Z`, creates the annotated tag `vX.Y.Z` and a GitHub release whose notes are the changelog section. The release carries the wheel `knowledge_hub-X.Y.Z-py3-none-any.whl`, `SHA256SUMS` and `deploy/kh.example.yaml`.
5. The image is `ghcr.io/tkcs-lab/knowledge-hub:X.Y.Z`. There is no `latest` tag. Check the digest with `docker buildx imagetools inspect ghcr.io/tkcs-lab/knowledge-hub:X.Y.Z`.
6. To roll out, update the image in the compose repo, pin the digest and deploy as usual. Without Docker: `pip install knowledge_hub-X.Y.Z-py3-none-any.whl`, then `kh setup plan --config kh.yaml`.

The change to `.github/workflows/release.yml` that builds and attaches the wheel is a protected path: it reaches `main` as a protected-change pull request, reviewed and merged by a person.

## Accepted risks

- **Two groups, the wider one.** A caller holding two groups can write into the wider one; the PrivacyFence popup shows `audience` and `group` and is the only gate.
- **Leaked bot token.** It reads every T0 repo and can open pull requests and push to open pull-request branches, but cannot merge.
- **Zero required approvals.** The approver must review the final diff.
- **Scan timing.** Exact vector scans take time in proportion to the number of hidden rows.
- **Per-process limits.** The rate limiter is not shared between processes.
