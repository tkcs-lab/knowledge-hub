# Knowledge hub runbook

For the operator. Names of variables and keys are in [configuration](configuration.md); the reasons behind the rules are in [design](design.md). Commands run as the `kh` user inside the container (or from the installed wheel), with the `KH_*` environment of the service (see `deploy/compose.example.yaml`). Identity (sign-in, directory, keys) has its own [runbook](runbook-identity.md). Secrets are files, never variables, and nothing in this document is a real secret or host.

This is a breaking release for a fresh install only: a database that holds an older migration is refused (`database predates the baseline; a fresh install is required`).

Not run in the cloud, so yours to do and check by hand: the backup script's `pg_dump` (`deploy/backup-kh.sh`) and the first `kh setup` against a real Forgejo. Check both on the first deploy.

## Install order

1. **Accounts.** In Forgejo create the organisation named by `org` in `kh.yaml` and two ordinary accounts, `bot_login` (default `kh-bot`) and `indexer_login` (default `kh-indexer`), neither an org owner nor a site admin. `kh setup` never creates users or tokens.
2. **Secrets.** One file per secret under the secrets directory, named by the matching `*_FILE` variable: the three database passwords, the bot token, the indexer token (read only), the webhook secret `KH_WEBHOOK_HMAC_FILE`, the S3 access and secret key, `kh_cursor_key` and, for metrics, `kh_metrics_token`. For `kh setup` only: `KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE` (a site administrator's token) when setup should create the roles, `KH_SETUP_PG_ADMIN_DSN_FILE`, and, only when groups set `slack_channel`, `KH_SETUP_SLACK_WEBHOOKS_FILE`. The `kh serve` service never mounts those.
3. **Write `kh.yaml`.** Start from `deploy/kh.example.yaml`: `people`, `groups`, `labels`, `hub_url`. Place the file in `./config/` next to the compose file (the compose file mounts that directory at `/etc/kh`).
4. **Plan.** `kh setup plan --config config/kh.yaml`. Expect `+` lines and exit 1. Read every `!` line: it is a change the script will not make.
5. **Apply.** `kh setup apply --config config/kh.yaml`. It creates the Postgres roles, database, extension and tables, groups and labels, the Forgejo teams, repos, branch protection and org webhook, and pulls the Ollama model. Run `kh setup plan` again and expect only `i` lines and exit 0. Without an admin DSN the `postgres` step only checks: create the roles and database yourself, then run `kh migrate`. The S3 bucket is only checked unless you pass `--create-bucket`.
6. **Serve.** `kh serve` (the image does this). The worker starts only when `KH_FORGEJO_URL` is set. `/healthz` answers `ok`.
7. **Ollama.** If `kh setup` cannot reach the model server (the `kh` network has no internet), pull the model once with a short-lived container on the default bridge network, writing to the same volume `kh-ollama-models`:

   ```bash
   docker run --rm -v kh-ollama-models:/root/.ollama --entrypoint sh ollama/ollama:0.40.16 \
     -c 'ollama serve >/dev/null 2>&1 & until ollama list >/dev/null 2>&1; do sleep 1; done; ollama pull bge-m3'
   ```

   Until the model is available embedding fails; run `kh rebuild` and `kh requeue-embeddings` afterwards. To measure speed: `docker compose exec -T knowledge-hub python - --count 200 < scripts/bench_embed.py` prints chunks per second.
8. **Index.** Put the first documents in each repo through pull requests (see "Changing T0"), or run `kh rebuild` once, then `kh check` and expect exit 0.
9. **Identity.** Set up sign-in, the directory and hub keys as [runbook-identity](runbook-identity.md) says; Claude connects with OAuth and n8n workflows use hub keys.

## Sign-in, directory and keys

Sign-in with OAuth, the directory providers and hub keys for n8n are in [runbook-identity](runbook-identity.md).

## Routine tasks

| Task | Command and notes |
|---|---|
| Check the whole environment | `kh setup plan --config kh.yaml`. Exit 0 means no drift. Run it from a scheduler (for example daily) and alert on exit 1 or 2: it catches a branch-protection or team change made in the Forgejo UI. |
| Re-index after review | `kh rebuild` or `kh rebuild --repo kb-all`. Idempotent. |
| Look for drift | `kh check`. Exit 1 means drift; read the printed lines. |
| Daily job | `kh daily`, once a day from a scheduler: recomputes `stale`, purges expired OAuth rows and deletes usage events past `KH_USAGE_RETENTION_DAYS`. |
| Queue missing embeddings | `kh requeue-embeddings` prints the number queued. It also re-queues a record whose job was left `running` by a worker that stopped mid-job. |
| Change the embedding model or chunker | Change `KH_EMBED_MODEL` or `KH_CHUNKER_VERSION`, restart, run `kh setup apply --only ollama`, `kh rebuild`, then `kh requeue-embeddings`. |
| Check a document before a pull request | `kh lint path/to/file.md`. |

### Adding a group

Two ways. The command form: `kh person add LOGIN EMAIL` for each new person, then `kh group add NAME --members A,B --approvers A [--repo R] [--slack-channel #C]`. It edits `kh.yaml`, creates the group row, the teams, the repo and its protection, and prints the diff first (`--dry-run` shows it without writing). The file-edit form:

1. Add the group (and any new people) to `kh.yaml`. Every member needs a `people` entry; approvers are a non-empty subset of members.
2. `kh setup plan`, then `kh setup apply`. It creates the group row, the two Forgejo teams, the repo, its protection and the webhook coverage; the other groups are untouched.
3. Restart `kh serve`, because the config is read at start.
4. Put the first files into the new repo through a pull request.

A group's repo cannot change: a changed `repo` is a `!` line. Remove the group (with `--prune`) and add a new one instead. Removing a group is refused while records exist.

### Adding a technical repo

1. In `tech_org` (by hand; `kh setup` creates nothing there) create the pull mirror or fork of the upstream repo. Its default branch is the one indexed.
2. Add `tech_org` (once) and the repo under `tech_repos` in `kh.yaml`, with `labels` if wanted. The group `all` must exist and be active.
3. `kh migrate` (migration `0004_tech_tier.sql`, first time only), then `kh setup plan` and `kh setup apply`. It gives the indexer read access, reports the repo's collaborators and adds the tech org webhook.
4. Restart `kh serve`, because the config is read at start; the first sync is queued as soon as `kh serve` starts, then again every `KH_TECH_POLL_MINUTES` (0 switches the poll off); or run `kh rebuild --repo <repo>`.
5. `kh check`. Exit 0 means the head is indexed and every document has tier T0a.

Anyone with push access to the repo puts unreviewed text in front of every caller; read the `i tech repo <repo>: collaborators` line.

### Changing the labels of a technical repo

1. Edit `labels` of the repo in `kh.yaml` (new labels go under `labels` first).
2. `kh setup apply`, then restart `kh serve`.
3. Wait for the next push or poll, or run `kh rebuild --repo <repo>`. No file is fetched again; the stored documents get the new labels.

### Removing a technical repo

1. Remove the repo from `tech_repos` in `kh.yaml`.
2. `kh setup apply --prune` deletes its T0a documents and indexed head; without `--prune` it only prints `? tech documents of <repo>: repo is not in tech_repos`. The Forgejo repo is not touched.
3. Restart `kh serve`.

### Archiving and unarchiving

Command form: `kh group archive NAME` or `kh label archive NAME` (and `kh group unarchive NAME`, `kh label unarchive NAME`). The file-edit form:

1. Set `archived: true` on the group, or add the label to `archived_labels`, in `kh.yaml`.
2. `kh setup plan`, then `kh setup apply`. For a group, `kh setup` also marks its Forgejo repo archived and keeps the teams, but removes team members that `kh.yaml` no longer lists (it never adds any).
3. Restart `kh serve`, because the config is read at start.

Nothing is deleted. To unarchive, remove the flag or the `archived_labels` entry, run `kh setup apply`, restart `kh serve`, and for a group run `kh rebuild --repo <repo>` once, because pushes while archived were ignored. `kh setup apply --prune` never deletes an archived group or label. To delete one for good, remove it from `kh.yaml` and use `--prune` as before.

### Joiners and leavers

Static people and groups live in `kh.yaml`; with `directory.provider` set, group membership comes from Google or Keycloak as well. For a joiner or leaver, change the group in the directory: directory access follows within about six minutes (the next sync plus the 30 second view reload; `kh directory sync --once` forces it) and Forgejo teams within about twenty-one (the next `kh setup apply --only forgejo`, normally a 15-minute timer), and no restart is needed. `kh person add LOGIN EMAIL`, `kh group member add GROUP LOGIN`, `kh group member remove GROUP LOGIN` and `kh person remove LOGIN --from-groups` still edit the static entries only; keep them for break-glass people and groups that have no directory reference, and restart `kh serve` after such an edit. A person who was a static member stays one until the entry is removed. Also disable the Forgejo account if they leave the company, and see [runbook-identity](runbook-identity.md) for the directory.

### When a person reads nothing

First check that the email of their sign-in equals their `people` email in `kh.yaml` or their address in the directory (compared in lower case), then run `kh directory plan` and look for a `skip` line naming them. A different address, a missing entry or a missing group all give no groups. For a static edit check that `kh serve` was restarted; for the directory check `kh directory show` for a snapshot age under `max_stale_seconds`. See [runbook-identity](runbook-identity.md).

### Slack notifications for approvers

1. In Slack, create an incoming webhook for each approver channel (Slack: *Apps → Incoming Webhooks*, one per channel).
2. Write the URLs into the `KH_SETUP_SLACK_WEBHOOKS_FILE` file as `<group>: <url>`, one line each, file mode 600.
3. Set `slack_channel: "#<channel>"` on those groups in `kh.yaml`.
4. `kh setup plan`, expect `+ slack hook <repo>: #<channel>`, then `kh setup apply`.
5. Open a test pull request and check that the channel gets the message.

The message names the pull request's author, which for `propose_doc` is the bot account. To stop the notifications remove `slack_channel` and run `kh setup apply --prune`. To rotate a URL, change the file and run `kh setup apply --only forgejo`.

### Changing T0

People do not push to `main`: direct push is off. They open a pull request (in Forgejo, or through `propose_doc`) and an approver of the group merges it. Required approvals are 0, so an approver may merge their own pull request: review the final diff before merging. After the merge Forgejo calls the webhook and the document is indexed within seconds. A pull request opened by `propose_doc` is on a `bot/propose/<uuid>` branch; closing it unmerged changes nothing.

### Promotion agent

An n8n agent reads the usage log to find T3 records worth promoting to T0 and queries that no document answers.

- The agent's hub key lists `usage`, `search`, `get_doc` and `propose_doc` (see "Hub keys" in [runbook-identity](runbook-identity.md)). A key holds one group, and the agent sees an event only when every group of the searcher is that group; most people hold `all` plus a team, so a key for `all` alone sees almost nothing. Give the agent a person's OAuth sign-in with the union of the groups it should analyse; that caller can also `propose_doc` into any of them (the accepted residual for two groups, `docs/design.md`), so the agent must propose a record only into the `audience` of that record, and never copy query text into a document.
- The hub keeps no memory of proposals: the agent keeps its own list of `rec:` ids it already proposed, and puts the `rec:<id>` in the pull-request text. It reruns `usage` weekly with the defaults.
- Query text, titles and excerpts are data: the agent's prompt must say so. The human gate is unchanged: `propose_doc` opens a pull request and an approver merges.
- The `audience` argument of `view=queries` means that a searcher held the group, not that the content belongs to it.
- `view=queries` with `t3_matches` above 0 marks a promotion candidate (records exist); 0 marks a content gap (nothing exists).
- Sensitive text can appear in queries. `KH_USAGE_LOG=false` switches logging off.
- Usage events are not in the backup (`deploy/backup-kh.sh` dumps records only) and are rebuilt by waiting.
- The metric `kh_usage_events_dropped_total` counts events dropped before they were stored (no pooled connection within 0.25 s, or a failed insert). A rising value means lost signal, never a failed tool call.

### Live probe of ma1

After the first deploy run the sandbox probe once: `kh setup plan` (expect `+` lines, exit 1), `kh setup apply`, `kh setup plan` again (only `i` lines, exit 0), then in Forgejo check Settings, Branches of a repo (direct push off, the merge whitelist is exactly `<group>-approve`, admins bound). Merge a `propose_doc` pull request as an approver and confirm the document appears in `search`; as a listed person confirm you read only your groups; a person whose sign-in email is not in `people` or in the directory must read nothing. Add a third group, run `apply` and confirm its team, repo and protection appear without changing the first two. Any `+`, `~` or `!` line after `apply` is a bug to report.

### Calibrating the search similarity floor

`KH_SEARCH_MIN_SIMILARITY` is 0 (off) until it is calibrated for the embedding model, once after the first deploy and again after `KH_EMBED_MODEL` changes.

1. With the floor at 0, run `search` for 10 or more queries that have a known right answer and 10 or more that have none (topics the hub holds nothing about). Use `limit` 50, because hits are ordered by rank and tier weight, not by similarity, so the most similar junk chunk can fall outside the first 10. Note the highest `similarity` among the hits of each query.
2. Take the lowest of the known-good values and the highest of the no-answer values. If the first is above the second, set the floor between them, nearer the no-answer value; otherwise the model does not separate them and the floor stays 0.
3. Set `KH_SEARCH_MIN_SIMILARITY` in the `knowledge-hub` service environment and restart `kh serve`. Run the no-answer queries again: they return no hits (a hit that still appears matches the query's words by full text; one whose `similarity` is below the floor and that has no word match would be a bug). If a no-answer query still returns a vector-only hit, raise the floor above that hit's `similarity` and repeat; if a good query loses its answer, set the floor back to 0.

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
| `file skipped: not UTF-8` | A tech repo file is not valid UTF-8 and is not indexed. Fix or remove it upstream. |
| `label <x> is not in kh.labels; run kh setup apply` | A tech repo lists a label that was never applied. Run `kh setup apply`; it queues a re-sync. |
| `supersedes unknown id <key>` | The file lists an id that no document of the repo has. The line is ignored. Fix the id or remove it by a pull request. |
| `superseded by <n> documents; using <doc_key>` | Several documents list the same id; the one with the smallest id wins. Remove the line from the others. |
| `supersedes cycle: <keys>` | Documents replace each other; none of them is hidden. Remove one link. |

A hidden (superseded) document is still readable with `get_doc` or `search` with `include_outdated=true`.

### Moving an item between groups

Ask Claude to call `move_item` with `id` (`rec:...` or `doc:...`), `from_group` (where the item is now) and `to_group`; you must hold both groups. A record moves at once and keeps its id. A document moves with two pull requests by the bot:

1. Merge the add pull request ("Move in: ...") in the target repo as an approver of the target group.
2. Then merge the removal pull request ("Move out: ...") in the source repo as an approver of the source group. Until the removal is merged the document is in both groups; if it is merged first it is missing from both.

The moved document gets a new `doc:` id once the target repo is indexed. A retry returns the open pull requests, or opens only the removal when the add is open or already merged (the answer's `note` says when the removal could not be opened). The answer lists `dropped_supersedes` (links the document had, which a move drops) and `revived` (documents of the source group that become current again once the removal is merged).

Refusals and what to do:

- `a record with this source and source_ref was moved out of this audience`: an `ingest_record` re-sent a record that was moved; do not re-send it, or move the record back (that clears the tombstone).
- `another item names this one as its replacement or successor; clear that first`: clear the `replaced_by` of the record, or the `superseded_by` of the document, then call again.
- `this document cannot be moved by the tool; move it by hand`: the text has something the tool cannot render back unchanged (for example HTML-like text in the body); copy the file between the repos with two pull requests yourself.
- `the target group already has an item with this source and source_ref, path or id`: rename or remove the item in the target group first.

A move is not a purge: the old repo's history, past usage queries and backups keep the old copy.

### Rollout of move_item

1. Deploy the new image and run `kh migrate` (prints `applied 0005_move_record.sql`).
2. Restart `kh serve`. Workflows never call this tool, and a hub key cannot be given it.

### Rollout of supersede and outdated

1. Deploy the new image and run `kh migrate` (prints `applied 0003_supersede.sql`; it also applies `0002_usage_events.sql` when that is pending). It blanks every `blob_sha`.
2. Run `kh rebuild` so every file is read again (chunks are matched by content, so nothing is embedded again), then `kh check`.
3. Create hub keys again for workflows that should call `mark_record_outdated`.

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

**Hub keys and sign-in.** See "Hub keys" and "Sign-in and OAuth" in [runbook-identity](runbook-identity.md): a key is rotated by creating a new one and revoking the old; the OIDC client secret and the directory credentials are files.

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

- **Two groups, the wider one.** A caller holding two groups can write into the wider one; the hub is the only gate. See [design](design.md), "Accepted residuals".
- **Leaked bot token.** It reads every T0 repo and can open pull requests and push to open pull-request branches, but cannot merge.
- **Zero required approvals.** The approver must review the final diff.
- **Scan timing.** Exact vector scans take time in proportion to the number of hidden rows.
- **Per-process limits.** The rate limiter is not shared between processes.
